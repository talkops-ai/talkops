# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Addon marketplace parsing (`.talkops/marketplace.json`) and materialization for TalkOps."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from talkops.config.addons import DefaultAddonMarketplaceConfig

from talkops.addons.manifest import validate_addon_name
from talkops.addons.marketplace_models import (
    AddonMarketplace,
    AddonMarketplaceSource,
    AddonSource,
    GithubAddonSource,
    GitSubdirectoryAddonSource,
    LocalAddonMarketplaceSource,
    LocalAddonSource,
    MarketplaceAddonEntry,
    RepositoryAddonMarketplaceSource,
    UrlAddonMarketplaceSource,
    UrlAddonSource,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Unified `.talkops/` catalog lookup paths (no `.talkops-addon/` directory)
_ADDON_MARKETPLACE_RELATIVE_PATHS: tuple[Path, ...] = (
    Path(".talkops") / "marketplace.json",
    Path(".talkops") / "addons" / "marketplace.json",
    Path("marketplace.json"),
)

_SSH_GIT_RE = re.compile(r"^([A-Za-z0-9._-]+@[^:]+:.+?(?:\.git)?)(?:#(.+))?$")
_GITHUB_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_GIT_TIMEOUT_SECONDS = 120


class AddonMarketplaceError(ValueError):
    """Raised when an addon marketplace catalog or source cannot be loaded or materialized."""


def find_addon_marketplace_manifest(root: Path) -> Path | None:
    """Return the first `.talkops/marketplace.json` path under `root`, if present."""
    for rel in _ADDON_MARKETPLACE_RELATIVE_PATHS:
        candidate = root / rel
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            logger.debug("Could not inspect addon marketplace path %s", candidate)
    return None


def parse_addon_marketplace_source(spec: str) -> AddonMarketplaceSource:
    """Parse a user-supplied marketplace specifier (path, GitHub slug, Git URL, or HTTP URL)."""
    raw = spec.strip()
    if not raw:
        msg = "Marketplace source cannot be empty"
        raise AddonMarketplaceError(msg)

    candidate_path = Path(raw).expanduser()
    if candidate_path.exists():
        if candidate_path.is_file():
            return LocalAddonMarketplaceSource(
                source_type="file",
                value=str(candidate_path.resolve()),
            )
        return LocalAddonMarketplaceSource(
            source_type="directory",
            value=str(candidate_path.resolve()),
        )

    ssh_match = _SSH_GIT_RE.match(raw)
    if ssh_match:
        return RepositoryAddonMarketplaceSource(
            source_type="git",
            value=ssh_match.group(1),
            ref=ssh_match.group(2) or None,
        )

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        ref = parsed.fragment or None
        clean_url = raw.split("#", 1)[0]
        if clean_url.endswith(".json"):
            return UrlAddonMarketplaceSource(source_type="url", value=clean_url)
        if parsed.netloc.lower() == "github.com":
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2:
                repo_name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
                return RepositoryAddonMarketplaceSource(
                    source_type="github",
                    value=f"{parts[0]}/{repo_name}",
                    ref=ref,
                )
        return RepositoryAddonMarketplaceSource(
            source_type="git",
            value=clean_url,
            ref=ref,
        )

    # GitHub shorthand: `owner/repo` or `owner/repo#ref`
    repo_part, _, ref_part = raw.partition("#")
    if _GITHUB_REPO_RE.match(repo_part.strip()):
        return RepositoryAddonMarketplaceSource(
            source_type="github",
            value=repo_part.strip(),
            ref=ref_part.strip() or None,
        )

    # Non-existent local path fallback if it looks like a filesystem path
    if raw.startswith((".", "/", "~")) or "\\" in raw:
        msg = f"Local addon marketplace path does not exist: {raw}"
        raise AddonMarketplaceError(msg)

    msg = f"Unsupported addon marketplace source: {spec!r}"
    raise AddonMarketplaceError(msg)


def _parse_addon_source(
    raw_source: object,
    *,
    addon_name: object,
    warnings: list[str],
) -> AddonSource | None:
    """Parse an addon's `source` field inside `.talkops/marketplace.json`."""
    if isinstance(raw_source, str):
        cleaned = raw_source.strip()
        if not cleaned:
            warnings.append(f"Skipping addon {addon_name!r}: empty source path")
            return None
        if cleaned.startswith(("http://", "https://")):
            return UrlAddonSource(source_type="url", url=cleaned)
        return LocalAddonSource(source_type="local", path=cleaned)

    if not isinstance(raw_source, dict):
        warnings.append(f"Skipping addon {addon_name!r}: invalid source {raw_source!r}")
        return None

    src_type = str(raw_source.get("source") or raw_source.get("type") or "").strip().lower()
    ref = str(raw_source["ref"]).strip() if isinstance(raw_source.get("ref"), str) else None
    subpath = str(raw_source["path"]).strip() if isinstance(raw_source.get("path"), str) else None

    if src_type == "github":
        repo = str(raw_source.get("repo") or "").strip()
        if not repo:
            warnings.append(f"Skipping addon {addon_name!r}: github source missing 'repo'")
            return None
        return GithubAddonSource(
            source_type="github",
            repo=repo,
            ref=ref,
            path=subpath,
        )

    if src_type in {"git", "git-subdir"}:
        url = str(raw_source.get("url") or "").strip()
        if not url:
            warnings.append(f"Skipping addon {addon_name!r}: git source missing 'url'")
            return None
        return GitSubdirectoryAddonSource(
            source_type="git-subdir",
            url=url,
            ref=ref,
            path=subpath,
        )

    if src_type == "url":
        url = str(raw_source.get("url") or "").strip()
        if not url:
            warnings.append(f"Skipping addon {addon_name!r}: url source missing 'url'")
            return None
        return UrlAddonSource(
            source_type="url",
            url=url,
            ref=ref,
            path=subpath,
        )

    if "path" in raw_source and isinstance(raw_source["path"], str):
        return LocalAddonSource(source_type="local", path=raw_source["path"].strip())

    warnings.append(f"Skipping addon {addon_name!r}: unsupported source object {raw_source!r}")
    return None


def _parse_marketplace_addon_entry(
    entry: object,
    *,
    warnings: list[str],
) -> MarketplaceAddonEntry | None:
    if not isinstance(entry, dict):
        warnings.append(f"Skipping addon entry: expected object, got {type(entry).__name__}")
        return None

    try:
        name = validate_addon_name(entry.get("name"))
    except ValueError as exc:
        warnings.append(f"Skipping addon entry with invalid name: {exc}")
        return None

    source = _parse_addon_source(entry.get("source", f"./addons/{name}"), addon_name=name, warnings=warnings)
    if source is None:
        return None

    display_name_val = entry.get("displayName") or entry.get("display_name")
    display_name = str(display_name_val).strip() if isinstance(display_name_val, str) else None
    desc_val = entry.get("description")
    description = str(desc_val).strip() if isinstance(desc_val, str) else None
    version_val = entry.get("version")
    version = str(version_val).strip() if isinstance(version_val, str) and version_val.strip() else "0.1.0"

    raw_tier = str(entry.get("tier") or "oss").strip().lower()
    tier: Any = raw_tier if raw_tier in {"oss", "enterprise", "custom"} else "oss"
    cat_val = entry.get("category")
    category = str(cat_val).strip() if isinstance(cat_val, str) else None

    ep_val = entry.get("entryPoint") or entry.get("entry_point") or entry.get("module")
    entry_point = str(ep_val).strip() if isinstance(ep_val, str) and ep_val.strip() else None

    raw_py_path = entry.get("pythonPath", entry.get("python_path", ["."]))
    if isinstance(raw_py_path, list):
        python_path = tuple(str(p) for p in raw_py_path if isinstance(p, str) and p.strip()) or (".",)
    else:
        python_path = (".",)

    raw_py_deps = entry.get("pythonDependencies", entry.get("python_dependencies", []))
    if isinstance(raw_py_deps, list):
        python_dependencies = tuple(
            str(d).strip() for d in raw_py_deps if isinstance(d, str) and d.strip()
        )
    else:
        python_dependencies = ()

    author_val = entry.get("author")
    author = author_val if isinstance(author_val, (str, dict)) else None

    kw_val = entry.get("keywords")
    keywords = (
        tuple(str(k) for k in kw_val if isinstance(k, (str, int)))
        if isinstance(kw_val, list)
        else ()
    )

    return MarketplaceAddonEntry(
        name=name,
        source=source,
        display_name=display_name,
        description=description,
        version=version,
        tier=tier,
        category=category,
        entry_point=entry_point,
        python_path=python_path,
        python_dependencies=python_dependencies,
        author=author,
        keywords=keywords,
    )


def parse_addon_marketplace_json(
    data: dict[str, Any],
    *,
    root_dir: Path | None = None,
    manifest_path: Path | None = None,
    fallback_name: str | None = None,
) -> AddonMarketplace:
    """Parse a raw `.talkops/marketplace.json` dictionary into an `AddonMarketplace`."""
    warnings: list[str] = []
    name = validate_addon_name(data.get("name"), fallback=fallback_name or "talkops-addons")
    display_name_val = data.get("displayName") or data.get("display_name")
    display_name = str(display_name_val).strip() if isinstance(display_name_val, str) else None
    desc_val = data.get("description")
    description = str(desc_val).strip() if isinstance(desc_val, str) else None
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else None

    # Support `"addons"` (canonical) or `"plugins"` (for hybrid repos)
    raw_addons = data.get("addons")
    if isinstance(raw_addons, list):
        raw_entries: list[Any] = raw_addons
    else:
        raw_plugins = data.get("plugins")
        raw_entries = raw_plugins if isinstance(raw_plugins, list) else []

    entries: list[MarketplaceAddonEntry] = []
    seen_names: set[str] = set()
    for raw_item in raw_entries:
        parsed = _parse_marketplace_addon_entry(raw_item, warnings=warnings)
        if parsed is None:
            continue
        if parsed.name in seen_names:
            warnings.append(f"Duplicate addon {parsed.name!r} in marketplace {name!r}; keeping first")
            continue
        seen_names.add(parsed.name)
        entries.append(parsed)

    return AddonMarketplace(
        name=name,
        display_name=display_name,
        description=description,
        owner=owner,
        addons=tuple(entries),
        root_dir=root_dir,
        manifest_path=manifest_path,
        warnings=tuple(warnings),
    )


def load_addon_marketplace_location(location: Path) -> AddonMarketplace:
    """Load an `AddonMarketplace` from a directory or `.talkops/marketplace.json` file."""
    if location.is_file():
        manifest_path = location
        # If the file is `<root>/.talkops/marketplace.json`, the repo root is `location.parent.parent`
        if location.parent.name == ".talkops":
            root_dir = location.parent.parent
        elif location.parent.name == "addons" and location.parent.parent.name == ".talkops":
            root_dir = location.parent.parent.parent
        else:
            root_dir = location.parent
    elif location.is_dir():
        manifest_path = find_addon_marketplace_manifest(location)
        if manifest_path is None:
            msg = f"No .talkops/marketplace.json found in {location}"
            raise AddonMarketplaceError(msg)
        root_dir = location
    else:
        msg = f"Addon marketplace location does not exist: {location}"
        raise AddonMarketplaceError(msg)

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to read addon marketplace manifest {manifest_path}: {exc}"
        raise AddonMarketplaceError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Addon marketplace manifest {manifest_path} must be a JSON object"
        raise AddonMarketplaceError(msg)

    return parse_addon_marketplace_json(
        data,
        root_dir=root_dir,
        manifest_path=manifest_path,
        fallback_name=root_dir.name,
    )


def _clone_git_repository(url: str, target_dir: Path, *, ref: str | None = None) -> str | None:
    """Shallow-clone a git repository into `target_dir` and return its HEAD commit SHA."""
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([url, str(target_dir)])
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        msg = f"Git clone failed for {url}: {exc}"
        raise AddonMarketplaceError(msg) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        msg = f"Failed to clone addon repository {url}: {stderr}"
        raise AddonMarketplaceError(msg)

    try:
        rev_proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if rev_proc.returncode == 0:
            return rev_proc.stdout.strip() or None
    except Exception:
        pass
    return None


def materialize_addon_marketplace_source(
    source: AddonMarketplaceSource,
    *,
    marketplaces_dir: Path,
) -> tuple[AddonMarketplace, Path]:
    """Materialize a local, GitHub, Git, or URL addon marketplace into `marketplaces_dir`."""
    marketplaces_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(source, LocalAddonMarketplaceSource):
        path = Path(source.value).expanduser().resolve()
        marketplace = load_addon_marketplace_location(path)
        return marketplace, path

    if isinstance(source, UrlAddonMarketplaceSource):
        try:
            req = urllib.request.Request(
                source.value,
                headers={"User-Agent": "TalkOps-Addon-Client/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
                raw_data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            msg = f"Failed to download addon marketplace manifest from {source.value}: {exc}"
            raise AddonMarketplaceError(msg) from exc

        if not isinstance(raw_data, dict):
            msg = f"Invalid addon marketplace manifest from {source.value}"
            raise AddonMarketplaceError(msg)
        marketplace = parse_addon_marketplace_json(raw_data)
        dest_dir = marketplaces_dir / marketplace.name
        dest_manifest = dest_dir / ".talkops" / "marketplace.json"
        dest_manifest.parent.mkdir(parents=True, exist_ok=True)
        dest_manifest.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")
        return load_addon_marketplace_location(dest_dir), dest_dir

    # RepositoryAddonMarketplaceSource (`github` or `git`)
    git_url = (
        f"https://github.com/{source.value}.git"
        if source.source_type == "github"
        else source.value
    )
    with tempfile.TemporaryDirectory(dir=marketplaces_dir) as tmp_str:
        tmp_clone = Path(tmp_str) / "repo"
        _clone_git_repository(git_url, tmp_clone, ref=source.ref)
        marketplace = load_addon_marketplace_location(tmp_clone)
        dest_dir = marketplaces_dir / marketplace.name
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(tmp_clone), str(dest_dir))
        return load_addon_marketplace_location(dest_dir), dest_dir


def materialize_default_addon_marketplace(
    config: DefaultAddonMarketplaceConfig,
    *,
    marketplaces_dir: Path,
) -> tuple[AddonMarketplace, Path] | None:
    """Materialize a default addon marketplace via git clone or remote `.talkops/marketplace.json`."""
    from talkops.config.addons import fetch_default_addon_marketplace_manifest

    dest_dir = marketplaces_dir / config.name
    if dest_dir.exists():
        try:
            return load_addon_marketplace_location(dest_dir), dest_dir
        except AddonMarketplaceError:
            pass

    raw = fetch_default_addon_marketplace_manifest(config)
    if raw is not None:
        dest_manifest = dest_dir / ".talkops" / "marketplace.json"
        dest_manifest.parent.mkdir(parents=True, exist_ok=True)
        dest_manifest.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return load_addon_marketplace_location(dest_dir), dest_dir

    return None


def materialize_addon_entry(
    entry: MarketplaceAddonEntry,
    *,
    marketplace: AddonMarketplace,
    destination_dir: Path,
    default_repo_url: str | None = None,
    default_ref: str | None = None,
) -> tuple[Path, str | None]:
    """Materialize an individual addon entry into `destination_dir` and return `(path, commit_sha)`."""
    source = entry.source
    if isinstance(source, LocalAddonSource):
        if marketplace.root_dir is not None:
            rel = source.path.lstrip("./") if source.path.startswith("./") else source.path
            local_candidate = (marketplace.root_dir / rel).resolve()
            # Prevent path traversal outside marketplace root
            try:
                local_candidate.relative_to(marketplace.root_dir.resolve())
            except ValueError as exc:
                msg = f"Addon {entry.name!r} source path {source.path!r} escapes marketplace root"
                raise AddonMarketplaceError(msg) from exc

            if local_candidate.is_dir():
                if destination_dir.exists():
                    shutil.rmtree(destination_dir)
                destination_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(local_candidate, destination_dir)
                return destination_dir, None

        # If marketplace was fetched via manifest_url only, clone the parent repository
        if default_repo_url:
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp_clone = Path(tmp_str) / "repo"
                sha = _clone_git_repository(default_repo_url, tmp_clone, ref=default_ref)
                rel = source.path.lstrip("./") if source.path.startswith("./") else source.path
                sub_dir = (tmp_clone / rel).resolve()
                if not sub_dir.is_dir():
                    msg = f"Addon path {source.path!r} not found in repository {default_repo_url}"
                    raise AddonMarketplaceError(msg)
                if destination_dir.exists():
                    shutil.rmtree(destination_dir)
                destination_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(sub_dir, destination_dir)
                return destination_dir, sha

        msg = f"Local addon directory {source.path!r} not found for addon {entry.name!r}"
        raise AddonMarketplaceError(msg)

    if isinstance(source, (GithubAddonSource, GitSubdirectoryAddonSource)):
        git_url = (
            f"https://github.com/{source.repo}.git"
            if isinstance(source, GithubAddonSource)
            else source.url
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_clone = Path(tmp_str) / "repo"
            sha = _clone_git_repository(git_url, tmp_clone, ref=source.ref)
            target_src = (
                (tmp_clone / source.path.lstrip("/")).resolve()
                if source.path
                else tmp_clone
            )
            if not target_src.is_dir():
                msg = f"Addon subdirectory {source.path!r} not found in {git_url}"
                raise AddonMarketplaceError(msg)
            if destination_dir.exists():
                shutil.rmtree(destination_dir)
            destination_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target_src, destination_dir)
            return destination_dir, sha

    msg = f"Unsupported addon source type for {entry.name!r}: {source}"
    raise AddonMarketplaceError(msg)
