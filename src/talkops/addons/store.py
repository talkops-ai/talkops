"""State storage for TalkOps addon marketplaces, installations, and scoped enablement."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from talkops.addons.marketplace_models import (
    AddonInstallScope,
    AddonMarketplaceRecord,
    InstalledAddonEntry,
    split_addon_id,
)
from talkops.config.paths import (
    ADDON_CACHE_DIR,
    ADDON_INSTALLED_PATH,
    ADDON_MARKETPLACES_DIR,
    ADDON_MARKETPLACES_PATH,
    ADDON_STATE_PATH,
    ADDONS_DIR,
    USER_SETTINGS_PATH,
    project_local_settings_path,
    project_settings_path,
    project_talkops_dir,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class AddonNotFoundError(ValueError):
    """Raised when an operation targets an addon that is not installed or found."""


def addon_storage_root() -> Path:
    """Return the addon storage root directory (`~/.talkops/addons` or `TALKOPS_ADDON_DIR`)."""
    raw = os.environ.get("TALKOPS_ADDON_DIR") or os.environ.get("ADDON_CACHE_DIR")
    if raw:
        return Path(raw).expanduser()
    return ADDONS_DIR


def addon_cache_root() -> Path:
    """Return `~/.talkops/addons/cache`."""
    raw = os.environ.get("TALKOPS_ADDON_DIR")
    if raw:
        return Path(raw).expanduser() / "cache"
    return ADDON_CACHE_DIR


def addon_marketplaces_dir() -> Path:
    """Return `~/.talkops/addons/marketplaces`."""
    raw = os.environ.get("TALKOPS_ADDON_DIR")
    if raw:
        return Path(raw).expanduser() / "marketplaces"
    return ADDON_MARKETPLACES_DIR


def _marketplaces_state_path() -> Path:
    raw = os.environ.get("TALKOPS_ADDON_DIR")
    if raw:
        return Path(raw).expanduser() / ".state" / "addon_marketplaces.json"
    return ADDON_MARKETPLACES_PATH


def _installed_addons_path() -> Path:
    raw = os.environ.get("TALKOPS_ADDON_DIR")
    if raw:
        return Path(raw).expanduser() / ".state" / "installed_addons.json"
    return ADDON_INSTALLED_PATH


def _addon_state_path() -> Path:
    raw = os.environ.get("TALKOPS_ADDON_DIR")
    if raw:
        return Path(raw).expanduser() / ".state" / "addon_state.json"
    return ADDON_STATE_PATH


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Could not read JSON state file %s: %s", path, exc)
        return {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── Marketplace Registry ─────────────────────────────────────────────


def load_addon_marketplace_records() -> dict[str, AddonMarketplaceRecord]:
    """Load registered addon marketplaces from state."""
    data = _read_json_dict(_marketplaces_state_path())
    raw_records = data.get("marketplaces")
    if not isinstance(raw_records, dict):
        return {}
    records: dict[str, AddonMarketplaceRecord] = {}
    for name, item in raw_records.items():
        if not isinstance(item, dict):
            continue
        src_type = str(item.get("sourceType") or item.get("source_type") or "directory")
        records[name] = AddonMarketplaceRecord(
            name=name,
            source_type=src_type,  # type: ignore[arg-type]
            source=str(item.get("source") or ""),
            install_location=str(item.get("installLocation") or item.get("install_location") or ""),
            ref=str(item["ref"]) if isinstance(item.get("ref"), str) else None,
            addon_count=int(item.get("addonCount") or item.get("addon_count") or 0),
            is_default=bool(item.get("isDefault") or item.get("is_default")),
        )
    return records


def save_addon_marketplace_record(record: AddonMarketplaceRecord) -> None:
    """Persist an `AddonMarketplaceRecord` in `~/.talkops/.state/addon_marketplaces.json`."""
    current = load_addon_marketplace_records()
    current[record.name] = record
    payload = {
        "version": 1,
        "marketplaces": {
            name: {
                "name": rec.name,
                "sourceType": rec.source_type,
                "source": rec.source,
                "installLocation": rec.install_location,
                "ref": rec.ref,
                "addonCount": rec.addon_count,
                "isDefault": rec.is_default,
            }
            for name, rec in sorted(current.items())
        },
    }
    _atomic_write_json(_marketplaces_state_path(), payload)


def remove_addon_marketplace_record(name: str) -> bool:
    """Remove a registered addon marketplace by name."""
    current = load_addon_marketplace_records()
    if name not in current:
        return False
    record = current.pop(name)
    loc = Path(record.install_location)
    if loc.exists() and addon_marketplaces_dir() in loc.parents:
        shutil.rmtree(loc, ignore_errors=True)
    payload = {
        "version": 1,
        "marketplaces": {
            k: {
                "name": rec.name,
                "sourceType": rec.source_type,
                "source": rec.source,
                "installLocation": rec.install_location,
                "ref": rec.ref,
                "addonCount": rec.addon_count,
                "isDefault": rec.is_default,
            }
            for k, rec in sorted(current.items())
        },
    }
    _atomic_write_json(_marketplaces_state_path(), payload)
    return True


# ── Installed Addons Registry ────────────────────────────────────────


def load_installed_addons() -> dict[str, InstalledAddonEntry]:
    """Load installed addons keyed by `addon@marketplace`."""
    data = _read_json_dict(_installed_addons_path())
    raw_addons = data.get("addons")
    if not isinstance(raw_addons, dict):
        return {}
    result: dict[str, InstalledAddonEntry] = {}
    for addon_id, raw_entry in raw_addons.items():
        item = raw_entry[0] if isinstance(raw_entry, list) and raw_entry else raw_entry
        if not isinstance(item, dict):
            continue
        raw_deps = item.get("pythonDependencies", [])
        deps = tuple(str(d) for d in raw_deps if isinstance(d, str)) if isinstance(raw_deps, list) else ()
        result[addon_id] = InstalledAddonEntry(
            install_path=str(item.get("installPath") or item.get("install_path") or ""),
            version=str(item["version"]) if isinstance(item.get("version"), str) else None,
            scope=str(item.get("scope") or "user"),  # type: ignore[arg-type]
            project_path=str(item["projectPath"]) if isinstance(item.get("projectPath"), str) else None,
            installed_at=str(item["installedAt"]) if isinstance(item.get("installedAt"), str) else None,
            last_updated=str(item["lastUpdated"]) if isinstance(item.get("lastUpdated"), str) else None,
            git_commit_sha=str(item["gitCommitSha"]) if isinstance(item.get("gitCommitSha"), str) else None,
            entry_point=str(item["entryPoint"]) if isinstance(item.get("entryPoint"), str) else None,
            python_dependencies=deps,
        )
    return result


def save_installed_addon(
    addon_id: str,
    entry: InstalledAddonEntry,
) -> InstalledAddonEntry:
    """Save an installed addon entry into `~/.talkops/.state/installed_addons.json`."""
    current = load_installed_addons()
    now = _now_iso()
    updated = InstalledAddonEntry(
        install_path=entry.install_path,
        version=entry.version,
        scope=entry.scope,
        project_path=entry.project_path,
        installed_at=entry.installed_at or now,
        last_updated=now,
        git_commit_sha=entry.git_commit_sha,
        entry_point=entry.entry_point,
        python_dependencies=entry.python_dependencies,
    )
    current[addon_id] = updated
    payload = {
        "version": 1,
        "addons": {
            aid: {
                "installPath": item.install_path,
                "version": item.version,
                "scope": item.scope,
                "projectPath": item.project_path,
                "installedAt": item.installed_at,
                "lastUpdated": item.last_updated,
                "gitCommitSha": item.git_commit_sha,
                "entryPoint": item.entry_point,
                "pythonDependencies": list(item.python_dependencies),
            }
            for aid, item in sorted(current.items())
        },
    }
    _atomic_write_json(_installed_addons_path(), payload)
    return updated


def remove_installed_addon(addon_id: str) -> InstalledAddonEntry | None:
    """Remove an installed addon from state and delete its cached files."""
    current = load_installed_addons()
    entry = current.pop(addon_id, None)
    if entry is None:
        return None
    cache_path = Path(entry.install_path)
    if cache_path.exists() and addon_cache_root() in cache_path.parents:
        shutil.rmtree(cache_path, ignore_errors=True)
    payload = {
        "version": 1,
        "addons": {
            aid: {
                "installPath": item.install_path,
                "version": item.version,
                "scope": item.scope,
                "projectPath": item.project_path,
                "installedAt": item.installed_at,
                "lastUpdated": item.last_updated,
                "gitCommitSha": item.git_commit_sha,
                "entryPoint": item.entry_point,
                "pythonDependencies": list(item.python_dependencies),
            }
            for aid, item in sorted(current.items())
        },
    }
    _atomic_write_json(_installed_addons_path(), payload)
    return entry


# ── Scoped Enablement (`user`, `project`, `local`) ───────────────────


def _scope_settings_path(scope: AddonInstallScope, project_root: Path | None) -> Path:
    raw_dir = os.environ.get("TALKOPS_ADDON_DIR")
    if scope in {"user", "global"}:
        if raw_dir:
            return Path(raw_dir).expanduser() / "settings.json"
        return _addon_state_path()
    root = project_root or Path.cwd()
    if scope == "local":
        return project_local_settings_path(root)
    return project_settings_path(root)


def set_addon_enabled_for_scope(
    addon_id: str,
    enabled: bool,
    *,
    scope: AddonInstallScope = "user",
    project_root: Path | None = None,
) -> None:
    """Record enabled/disabled state for `addon_id` (`name@marketplace`) at the requested scope."""
    target_path = _scope_settings_path(scope, project_root)
    data = _read_json_dict(target_path)
    enabled_map = data.get("enabledAddons")
    if not isinstance(enabled_map, dict):
        enabled_map = {}
    enabled_map[addon_id] = bool(enabled)
    data["enabledAddons"] = enabled_map
    _atomic_write_json(target_path, data)

    # Also update `.talkops/addons.json` if present in project scope so `talkops serve` sees it immediately
    if scope in {"project", "team"} and project_root is not None:
        addons_json_path = project_talkops_dir(project_root) / "addons.json"
        if addons_json_path.is_file():
            addons_doc = _read_json_dict(addons_json_path)
            addons_sec = addons_doc.get("addons")
            if not isinstance(addons_sec, dict):
                addons_sec = {}
            short_name, _ = split_addon_id(addon_id)
            entry_cfg = addons_sec.get(short_name)
            if not isinstance(entry_cfg, dict):
                entry_cfg = {}
            entry_cfg["enabled"] = bool(enabled)
            addons_sec[short_name] = entry_cfg
            addons_doc["addons"] = addons_sec
            _atomic_write_json(addons_json_path, addons_doc)


def load_all_enabled_addon_ids(project_root: Path | None = None) -> frozenset[str]:
    """Return the effective set of enabled `addon@marketplace` IDs merged across scopes."""
    merged: dict[str, bool] = {}

    # 1. User state (`~/.talkops/.state/addon_state.json` & `~/.talkops/settings.json`)
    for path in (_addon_state_path(), USER_SETTINGS_PATH, _scope_settings_path("user", None)):
        doc = _read_json_dict(path)
        enabled_map = doc.get("enabledAddons")
        if isinstance(enabled_map, dict):
            for k, v in enabled_map.items():
                if isinstance(v, bool):
                    merged[str(k)] = v

    # 2. Project & local settings (`.talkops/settings.json`, `.talkops/settings.local.json`)
    if project_root is not None:
        for path in (project_settings_path(project_root), project_local_settings_path(project_root)):
            doc = _read_json_dict(path)
            enabled_map = doc.get("enabledAddons")
            if isinstance(enabled_map, dict):
                for k, v in enabled_map.items():
                    if isinstance(v, bool):
                        merged[str(k)] = v

    return frozenset(aid for aid, is_on in merged.items() if is_on)
