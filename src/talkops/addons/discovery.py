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

"""Addon marketplace discovery, installation, dependency resolution, and enablement for TalkOps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from talkops.addons.manifest import (
    AddonManifestError,
    ensure_addon_python_dependencies,
    find_addon_manifest_path,
    load_addon_manifest,
)
from talkops.addons.marketplace import (
    AddonMarketplaceError,
    load_addon_marketplace_location,
    materialize_addon_entry,
    materialize_addon_marketplace_source,
    materialize_default_addon_marketplace,
    parse_addon_marketplace_source,
)
from talkops.addons.marketplace_models import (
    AddonDiscoveryResult,
    AddonInstallScope,
    AddonInstance,
    AddonManifest,
    AddonMarketplace,
    AddonMarketplaceRecord,
    InstalledAddonEntry,
    MarketplaceAddonEntry,
    RepositoryAddonMarketplaceSource,
    split_addon_id,
)
from talkops.addons.store import (
    AddonNotFoundError,
    addon_cache_root,
    addon_marketplaces_dir,
    load_addon_marketplace_records,
    load_all_enabled_addon_ids,
    load_installed_addons,
    remove_addon_marketplace_record,
    remove_installed_addon,
    save_addon_marketplace_record,
    save_installed_addon,
    set_addon_enabled_for_scope,
)
from talkops.config.addons import (
    get_default_addon_marketplace_config,
    get_default_addon_marketplace_names,
    get_primary_default_addon_marketplace_name,
    is_default_addon_marketplace,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def add_addon_marketplace_source(
    source_spec: str,
    *,
    scope: AddonInstallScope = "user",
    project_root: Path | None = None,
) -> AddonMarketplace:
    """Register and materialize a local, GitHub, Git, or URL addon marketplace."""
    _ = scope, project_root
    source = parse_addon_marketplace_source(source_spec)
    marketplace, location = materialize_addon_marketplace_source(
        source,
        marketplaces_dir=addon_marketplaces_dir(),
    )
    record = AddonMarketplaceRecord(
        name=marketplace.name,
        source_type=source.source_type,
        source=source.value,
        install_location=str(location),
        ref=source.ref if isinstance(source, RepositoryAddonMarketplaceSource) else None,
        addon_count=len(marketplace.addons),
        is_default=is_default_addon_marketplace(marketplace.name),
    )
    save_addon_marketplace_record(record)
    return marketplace


def remove_addon_marketplace(name: str) -> None:
    """Remove a registered addon marketplace by name."""
    if not remove_addon_marketplace_record(name):
        msg = f"Addon marketplace {name!r} is not registered"
        raise AddonMarketplaceError(msg)


def _resolve_or_materialize_marketplace(marketplace_name: str) -> tuple[AddonMarketplace, AddonMarketplaceRecord | None]:
    """Load a registered addon marketplace or materialize a default marketplace on demand."""
    records = load_addon_marketplace_records()
    record = records.get(marketplace_name)
    if record is not None:
        loc = Path(record.install_location)
        if loc.exists():
            return load_addon_marketplace_location(loc), record

    default_cfg = get_default_addon_marketplace_config(marketplace_name)
    if default_cfg is not None:
        mat = materialize_default_addon_marketplace(
            default_cfg,
            marketplaces_dir=addon_marketplaces_dir(),
        )
        if mat is not None:
            marketplace, location = mat
            new_rec = AddonMarketplaceRecord(
                name=marketplace.name,
                source_type="github",
                source=default_cfg.repository_url,
                install_location=str(location),
                ref=default_cfg.ref,
                addon_count=len(marketplace.addons),
                is_default=True,
            )
            save_addon_marketplace_record(new_rec)
            return marketplace, new_rec

    msg = f"Unknown addon marketplace: {marketplace_name!r}"
    raise AddonMarketplaceError(msg)


def refresh_addon_marketplace(name: str) -> AddonMarketplace:
    """Re-fetch and refresh a registered or default addon marketplace catalog."""
    records = load_addon_marketplace_records()
    record = records.get(name)
    if record is not None:
        source_spec = f"{record.source}#{record.ref}" if record.ref else record.source
        return add_addon_marketplace_source(source_spec)

    default_cfg = get_default_addon_marketplace_config(name)
    if default_cfg is not None:
        mat = materialize_default_addon_marketplace(
            default_cfg,
            marketplaces_dir=addon_marketplaces_dir(),
        )
        if mat is not None:
            marketplace, location = mat
            save_addon_marketplace_record(
                AddonMarketplaceRecord(
                    name=marketplace.name,
                    source_type="github",
                    source=default_cfg.repository_url,
                    install_location=str(location),
                    ref=default_cfg.ref,
                    addon_count=len(marketplace.addons),
                    is_default=True,
                )
            )
            return marketplace

    msg = f"Addon marketplace {name!r} not found"
    raise AddonMarketplaceError(msg)


def refresh_all_addon_marketplaces() -> dict[str, AddonMarketplace]:
    """Refresh all registered addon marketplaces."""
    records = load_addon_marketplace_records()
    names = set(records.keys()) | set(get_default_addon_marketplace_names())
    refreshed: dict[str, AddonMarketplace] = {}
    for name in sorted(names):
        try:
            refreshed[name] = refresh_addon_marketplace(name)
        except Exception as exc:
            logger.debug("Could not refresh addon marketplace %s: %s", name, exc)
    return refreshed


def _synthesize_manifest_from_marketplace_entry(
    entry: MarketplaceAddonEntry,
    addon_root: Path,
) -> AddonManifest:
    """Synthesize an `AddonManifest` if the addon directory does not yet have `.talkops/addon.json`."""
    manifest_file = find_addon_manifest_path(addon_root)
    if manifest_file is not None:
        return load_addon_manifest(manifest_file, fallback_name=entry.name)

    ep = entry.entry_point or f"talkops_{entry.name.replace('-', '_')}.addon:Addon"
    return AddonManifest(
        name=entry.name,
        display_name=entry.display_name,
        version=entry.version,
        description=entry.description,
        author=entry.author,
        tier=entry.tier,
        entry_point=ep,
        python_path=entry.python_path,
        python_dependencies=entry.python_dependencies,
    )


def install_addon(
    addon_spec: str,
    *,
    scope: AddonInstallScope = "user",
    project_root: Path | None = None,
    auto_install_deps: bool = True,
    dependency_runner: Any | None = None,
) -> AddonInstance:
    """Install an addon from a registered or default marketplace into `~/.talkops/addons/cache/`.

    Automatically verifies and installs any missing `pythonDependencies` declared in
    `.talkops/addon.json` into the active Python environment (`sys.executable`).
    """
    records = load_addon_marketplace_records()
    if "@" in addon_spec:
        addon_name, marketplace_name = split_addon_id(addon_spec)
    else:
        addon_name = addon_spec.strip()
        # Search registered marketplaces first, then default marketplace
        marketplace_name = ""
        for cand_name, rec in records.items():
            loc = Path(rec.install_location)
            if loc.exists():
                try:
                    mp = load_addon_marketplace_location(loc)
                    if any(a.name == addon_name for a in mp.addons):
                        marketplace_name = cand_name
                        break
                except Exception:
                    continue
        if not marketplace_name:
            marketplace_name = get_primary_default_addon_marketplace_name()

    marketplace, record = _resolve_or_materialize_marketplace(marketplace_name)
    entry = next((a for a in marketplace.addons if a.name == addon_name), None)
    if entry is None:
        msg = f"Addon {addon_name!r} not found in marketplace {marketplace.name!r}"
        raise AddonNotFoundError(msg)

    default_cfg = get_default_addon_marketplace_config(marketplace.name)
    default_repo_url = (
        default_cfg.repository_url
        if default_cfg is not None
        else (record.source if record and record.source_type in {"github", "git"} else None)
    )
    if default_repo_url and not default_repo_url.startswith(("http://", "https://", "git@")):
        default_repo_url = f"https://github.com/{default_repo_url}.git"
    default_ref = default_cfg.ref if default_cfg is not None else (record.ref if record else None)

    dest_dir = addon_cache_root() / marketplace.name / entry.name / (entry.version or "0.1.0")
    materialized_dir, commit_sha = materialize_addon_entry(
        entry,
        marketplace=marketplace,
        destination_dir=dest_dir,
        default_repo_url=default_repo_url,
        default_ref=default_ref,
    )

    manifest = _synthesize_manifest_from_marketplace_entry(entry, materialized_dir)
    all_py_deps = tuple(dict.fromkeys([*entry.python_dependencies, *manifest.python_dependencies]))

    # Automatically install missing Python dependencies into the active environment
    if all_py_deps:
        ensure_addon_python_dependencies(
            all_py_deps,
            addon_name=manifest.name,
            auto_install=auto_install_deps,
            runner=dependency_runner,
        )

    addon_id = f"{manifest.name}@{marketplace.name}"
    save_installed_addon(
        addon_id,
        InstalledAddonEntry(
            install_path=str(materialized_dir),
            version=manifest.version,
            scope=scope,
            project_path=str(project_root) if project_root is not None else None,
            git_commit_sha=commit_sha,
            entry_point=manifest.entry_point,
            python_dependencies=all_py_deps,
        ),
    )
    set_addon_enabled_for_scope(addon_id, True, scope=scope, project_root=project_root)

    return AddonInstance(
        addon_id=addon_id,
        name=manifest.name,
        marketplace=marketplace.name,
        root_dir=materialized_dir,
        manifest=manifest,
        version=manifest.version,
        description=manifest.description or entry.description,
        enabled=True,
    )


def uninstall_addon(
    addon_spec: str,
    *,
    scope: AddonInstallScope = "user",
    project_root: Path | None = None,
) -> str:
    """Uninstall an addon from local cache and state."""
    installed = load_installed_addons()
    target_id = addon_spec.strip()
    if "@" not in target_id:
        matches = [aid for aid in installed if aid.split("@", 1)[0] == target_id]
        if len(matches) == 1:
            target_id = matches[0]
        elif not matches:
            target_id = f"{target_id}@{get_primary_default_addon_marketplace_name()}"
        else:
            msg = f"Addon {target_id!r} is installed from multiple marketplaces ({matches}); specify name@marketplace"
            raise AddonMarketplaceError(msg)

    removed = remove_installed_addon(target_id)
    if removed is None:
        msg = f"Addon {target_id!r} is not installed"
        raise AddonNotFoundError(msg)

    set_addon_enabled_for_scope(target_id, False, scope=scope, project_root=project_root)
    return target_id


def set_installed_addon_enabled(
    addon_spec: str,
    enabled: bool,
    *,
    scope: AddonInstallScope = "user",
    project_root: Path | None = None,
) -> str:
    """Enable or disable an installed addon at the specified scope."""
    installed = load_installed_addons()
    target_id = addon_spec.strip()
    if "@" not in target_id:
        matches = [aid for aid in installed if aid.split("@", 1)[0] == target_id]
        if len(matches) == 1:
            target_id = matches[0]
        elif not matches:
            target_id = f"{target_id}@{get_primary_default_addon_marketplace_name()}"
        else:
            msg = f"Addon {target_id!r} is installed from multiple marketplaces ({matches}); specify name@marketplace"
            raise AddonMarketplaceError(msg)

    if target_id not in installed:
        msg = f"Addon {target_id!r} is not installed"
        raise AddonNotFoundError(msg)

    set_addon_enabled_for_scope(target_id, enabled, scope=scope, project_root=project_root)
    return target_id


def list_available_addons(
    project_root: Path | None = None,
) -> tuple[tuple[str, str, str, bool, bool], ...]:
    """List addons across all configured marketplaces.

    Returns tuples of `(addon_id, version, description, is_installed, is_enabled)`.
    """
    records = load_addon_marketplace_records()
    installed = load_installed_addons()
    enabled = load_all_enabled_addon_ids(project_root=project_root)
    rows: list[tuple[str, str, str, bool, bool]] = []
    seen_ids: set[str] = set()

    for name, record in sorted(records.items()):
        try:
            marketplace = load_addon_marketplace_location(Path(record.install_location))
        except AddonMarketplaceError as exc:
            rows.append((f"<marketplace:{name}>", "0.0.0", str(exc), False, False))
            continue
        for entry in marketplace.addons:
            aid = f"{entry.name}@{marketplace.name}"
            seen_ids.add(aid)
            rows.append(
                (
                    aid,
                    entry.version or "0.1.0",
                    entry.description or "",
                    aid in installed,
                    aid in enabled,
                )
            )

    for aid, inst in sorted(installed.items()):
        if aid not in seen_ids:
            rows.append(
                (
                    aid,
                    inst.version or "0.1.0",
                    f"Installed at {inst.install_path}",
                    True,
                    aid in enabled,
                )
            )
    return tuple(rows)


def discover_marketplace_addons(
    *,
    project_root: Path | None = None,
    requested_addon_names: set[str] | None = None,
    auto_install_deps: bool = True,
) -> AddonDiscoveryResult:
    """Discover installed (and on-demand enabled) marketplace addons.

    Ensures Python dependencies declared in `.talkops/addon.json` are satisfied in
    the active Python environment before returning `AddonInstance` objects.
    """
    warnings: list[str] = []
    installed = load_installed_addons()
    enabled_ids = set(load_all_enabled_addon_ids(project_root=project_root))

    # Also include any installed addon whose short name is explicitly requested (e.g. `--addons channels`)
    if requested_addon_names:
        for req_name in requested_addon_names:
            if "@" in req_name:
                enabled_ids.add(req_name)
            else:
                matched = [aid for aid in installed if aid.split("@", 1)[0] == req_name]
                if matched:
                    enabled_ids.update(matched)
                else:
                    # Check if a registered marketplace offers `req_name` for on-demand auto-install
                    for mp_name, rec in load_addon_marketplace_records().items():
                        loc = Path(rec.install_location)
                        if loc.exists():
                            try:
                                mp = load_addon_marketplace_location(loc)
                                if any(a.name == req_name for a in mp.addons):
                                    enabled_ids.add(f"{req_name}@{mp_name}")
                                    break
                            except Exception:
                                continue

    instances: list[AddonInstance] = []
    for addon_id in sorted(enabled_ids):
        try:
            addon_name, marketplace_name = split_addon_id(addon_id)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        entry = installed.get(addon_id)
        if entry is None or not Path(entry.install_path).exists():
            try:
                inst = install_addon(
                    addon_id,
                    project_root=project_root,
                    auto_install_deps=auto_install_deps,
                )
                instances.append(inst)
                continue
            except Exception as exc:
                warnings.append(f"Addon {addon_id} is enabled but auto-install failed: {exc}")
                continue

        root_dir = Path(entry.install_path)
        try:
            manifest = load_addon_manifest(root_dir, fallback_name=addon_name)
        except AddonManifestError as exc:
            warnings.append(f"Skipping addon {addon_id}: {exc}")
            continue

        all_py_deps = tuple(
            dict.fromkeys([*entry.python_dependencies, *manifest.python_dependencies])
        )
        if all_py_deps:
            _, still_missing = ensure_addon_python_dependencies(
                all_py_deps,
                addon_name=manifest.name,
                auto_install=auto_install_deps,
            )
            if still_missing:
                warnings.append(
                    f"Addon {addon_id} has unsatisfied Python dependencies: {still_missing}"
                )

        instances.append(
            AddonInstance(
                addon_id=addon_id,
                name=manifest.name,
                marketplace=marketplace_name,
                root_dir=root_dir,
                manifest=manifest,
                version=manifest.version,
                description=manifest.description,
                enabled=True,
            )
        )

    return AddonDiscoveryResult(addons=tuple(instances), warnings=tuple(warnings))
