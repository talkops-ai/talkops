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

"""Plugin discovery, install, and enablement helpers for TalkOps."""

from __future__ import annotations

import shutil
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any

from talkops.config.plugins import (
    get_default_marketplace_names,
    get_primary_default_marketplace_name,
)
from talkops.plugins.manifest import (
    PluginManifestError,
    build_inventory,
    find_manifest_path,
    load_manifest,
)
from talkops.plugins.marketplace import (
    MarketplaceError,
    load_marketplace,
    load_marketplace_location,
    materialize_marketplace_source,
    materialize_plugin_source,
    parse_marketplace_source,
    redact_urls_in_text,
    refresh_marketplace_catalog,
)
from talkops.plugins.models import (
    InstallScope,
    MarketplacePluginEntry,
    MarketplaceRecord,
    PluginDiscoveryResult,
    PluginInstance,
    PluginMarketplace,
    RepositoryMarketplaceSource,
    split_plugin_id,
)
from talkops.plugins.store import (
    PluginNotFoundError,
    cache_and_register_plugin,
    ensure_marketplace_cache_dir,
    ensure_plugin_data_dir,
    get_primary_install_entry,
    load_all_disabled_plugin_ids,
    load_all_enabled_plugin_ids,
    load_enabled_plugin_ids,
    load_installed_plugin_entries,
    load_installed_plugins,
    load_local_enabled_plugin_ids,
    load_marketplace_records,
    load_project_enabled_plugin_ids,
    plugin_data_dir,
    plugin_mutation_lock,
    remove_marketplace_record,
    save_marketplace_record,
    set_plugin_enabled_for_scope,
)
from talkops.plugins.store import (
    uninstall_plugin as uninstall_plugin_record,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@plugin_mutation_lock()
def add_local_marketplace(path: str | Path) -> PluginMarketplace:
    """Add a local marketplace to TalkOps state."""
    marketplace = load_marketplace(Path(path))
    save_marketplace_record(
        MarketplaceRecord(
            name=marketplace.name,
            source_type="directory",
            source=str(marketplace.root),
            install_location=str(marketplace.root),
        )
    )
    return marketplace


@plugin_mutation_lock()
def add_marketplace_source(raw: str) -> PluginMarketplace:
    """Add a marketplace from a pasted source string."""
    source = parse_marketplace_source(raw)
    marketplace, location = materialize_marketplace_source(source)
    save_marketplace_record(
        MarketplaceRecord(
            name=marketplace.name,
            source_type=source.source_type,
            source=source.value,
            install_location=str(location),
            ref=source.ref if isinstance(source, RepositoryMarketplaceSource) else None,
        )
    )
    return marketplace


@plugin_mutation_lock()
def remove_marketplace(name: str) -> bool:
    """Remove a marketplace and every plugin installed from it."""
    record = load_marketplace_records().get(name)
    if record is None:
        return False

    installed = load_installed_plugins(strict=True)
    enabled = load_enabled_plugin_ids(strict=True)
    plugin_ids = set(installed) | set(enabled)
    for plugin_id in plugin_ids:
        try:
            _plugin_name, marketplace_name = split_plugin_id(plugin_id)
        except ValueError:
            continue
        if marketplace_name == name:
            with suppress(PluginNotFoundError):
                uninstall_plugin(plugin_id)

    removed = remove_marketplace_record(name)
    location = Path(record.install_location)
    try:
        resolved = location.resolve()
        cache_root = ensure_marketplace_cache_dir().resolve()
    except OSError:
        return removed
    if record.source_type in {"github", "git", "url"} and resolved.is_relative_to(
        cache_root
    ):
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved.is_file():
            resolved.unlink(missing_ok=True)
    return removed


@plugin_mutation_lock()
def refresh_marketplace(
    name: str, *, project_root: Path | None = None
) -> PluginMarketplace:
    """Refresh a marketplace from its remote source and update local cache and records."""
    records = load_marketplace_records(project_root=project_root)
    record = records.get(name)
    if record is None:
        msg = f"Marketplace {name!r} is not configured"
        raise MarketplaceError(msg)

    marketplace = refresh_marketplace_catalog(record)
    save_marketplace_record(
        MarketplaceRecord(
            name=marketplace.name,
            source_type=record.source_type,
            source=record.source,
            install_location=record.install_location,
            ref=record.ref,
            plugin_count=len(marketplace.plugins),
            is_default=record.is_default,
        )
    )
    return marketplace


@plugin_mutation_lock()
def refresh_all_marketplaces(
    *, project_root: Path | None = None
) -> dict[str, PluginMarketplace]:
    """Refresh all configured marketplaces."""
    records = load_marketplace_records(project_root=project_root)
    results: dict[str, PluginMarketplace] = {}
    for name in records:
        try:
            results[name] = refresh_marketplace(name, project_root=project_root)
        except Exception as exc:
            logger.warning("Failed to refresh marketplace %s: %s", name, exc)
    return results


def _normalize_plugin_id(plugin_id: str) -> str:
    """Normalize plugin id, resolving across available marketplaces or defaulting to the primary default marketplace."""
    if "@" in plugin_id:
        return plugin_id

    records = load_marketplace_records()
    for name, rec in records.items():
        try:
            mp = load_marketplace_location(Path(rec.install_location))
            if any(p.name == plugin_id for p in mp.plugins):
                return f"{plugin_id}@{name}"
        except Exception:
            continue

    return f"{plugin_id}@{get_primary_default_marketplace_name()}"


def _resolve_installed_plugin_id(plugin_id: str) -> str:
    """Resolve plugin id against currently installed plugins."""
    if "@" in plugin_id:
        return plugin_id

    installed = load_installed_plugins()
    prefix = f"{plugin_id}@"
    matches = [pid for pid in installed if pid.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        default_names = get_default_marketplace_names()
        for m in matches:
            mp_part = m.split("@", 1)[1]
            if mp_part in default_names:
                return m
        return matches[0]

    return f"{plugin_id}@{get_primary_default_marketplace_name()}"


def _require_installed_plugin(
    plugin_id: str, *, project_root: Path | None = None
) -> None:
    canonical_id = _resolve_installed_plugin_id(plugin_id)
    if canonical_id in load_installed_plugins(strict=True, project_root=project_root):
        return
    if isinstance(project_root, Path):
        bare_name = canonical_id.partition("@")[0]
        for cand in (
            project_root / ".talkops" / "plugins" / bare_name,
            project_root / ".agents" / "plugins" / bare_name,
            project_root / "plugins" / bare_name,
        ):
            if cand.is_dir():
                return
    records = load_marketplace_records(project_root=project_root)
    for mp_name, rec in records.items():
        if getattr(rec, "is_project", False) and canonical_id.endswith(f"@{mp_name}"):
            try:
                mp = load_marketplace_location(Path(rec.install_location))
                plugin_name = canonical_id.partition("@")[0]
                if any(p.name == plugin_name for p in mp.plugins):
                    return
            except Exception:
                pass
    msg = f"Plugin {plugin_id!r} is not installed"
    raise MarketplaceError(msg)


@plugin_mutation_lock()
def set_installed_plugin_enabled(
    plugin_id: str,
    enabled: bool = True,
    *,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> None:
    """Set the enabled state of an installed plugin at the given scope."""
    from talkops.config.settings import settings

    canonical_id = _resolve_installed_plugin_id(plugin_id)
    effective_root = (
        project_root if isinstance(project_root, Path) else settings.effective_project_root
    )
    target_scope: InstallScope = scope
    target_root: Path | None = (
        project_root if isinstance(project_root, Path) else effective_root
    )

    if scope in {"user", "global"} and isinstance(effective_root, Path):
        entries = load_installed_plugin_entries().get(canonical_id, [])
        has_user_install = any(e.scope in {"user", "global"} for e in entries)
        if not has_user_install:
            if canonical_id in load_local_enabled_plugin_ids(effective_root) or any(
                e.scope == "local" and e.project_path == str(effective_root)
                for e in entries
            ):
                target_scope = "local"
                target_root = effective_root
            elif canonical_id in load_project_enabled_plugin_ids(effective_root) or any(
                e.scope == "project" and e.project_path == str(effective_root)
                for e in entries
            ):
                target_scope = "project"
                target_root = effective_root

    if target_scope != "project":
        _require_installed_plugin(canonical_id, project_root=target_root)
    else:
        try:
            _require_installed_plugin(canonical_id, project_root=target_root)
        except MarketplaceError:
            _resolve_marketplace_and_entry(canonical_id)
    set_plugin_enabled_for_scope(
        canonical_id, enabled, scope=target_scope, project_root=target_root
    )
    if enabled:
        ensure_plugin_data_dir(canonical_id)


@plugin_mutation_lock()
def uninstall_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
    project_path: str | None = None,
) -> bool:
    """Uninstall a plugin (disable, clear records, delete orphaned cache)."""
    canonical_id = _resolve_installed_plugin_id(plugin_id)
    return uninstall_plugin_record(
        canonical_id,
        scope=scope,
        project_root=project_root,
        project_path=project_path,
    )


def _resolve_marketplace_and_entry(
    plugin_id: str,
) -> tuple[PluginMarketplace, MarketplacePluginEntry]:
    canonical_id = _normalize_plugin_id(plugin_id)
    try:
        plugin_name, marketplace_name = split_plugin_id(canonical_id)
    except ValueError as exc:
        raise MarketplaceError(str(exc)) from exc
    records = load_marketplace_records()
    record = records.get(marketplace_name)
    if record is None:
        msg = f"Marketplace {marketplace_name!r} is not configured"
        raise MarketplaceError(msg)
    marketplace = load_marketplace_location(Path(record.install_location))
    entry = next(
        (plugin for plugin in marketplace.plugins if plugin.name == plugin_name),
        None,
    )
    if entry is None:
        msg = f"Plugin {plugin_id!r} not found in marketplace {marketplace_name}"
        raise MarketplaceError(msg)
    return marketplace, entry


@plugin_mutation_lock()
def install_plugin(
    plugin_id: str,
    *,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> PluginInstance:
    """Install a marketplace plugin into the versioned cache and enable it.

    Args:
        plugin_id: Plugin id in ``name@marketplace`` form or shorthand ``name``.
        scope: Installation scope — ``"user"``, ``"project"``, or ``"local"``.
        project_root: Required when ``scope`` is ``"project"`` or ``"local"``.
    """
    load_installed_plugins(strict=True)
    canonical_id = _normalize_plugin_id(plugin_id)
    marketplace, entry = _resolve_marketplace_and_entry(canonical_id)
    source_root = materialize_plugin_source(marketplace, entry)
    if source_root is None:
        msg = (
            f"Plugin {canonical_id} has unsupported source "
            f"{redact_urls_in_text(repr(entry.source))}; "
            "use a local path, GitHub repository, or Git repository source"
        )
        raise MarketplaceError(msg)

    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            source_root, fallback_name=entry.name
        )
    except PluginManifestError as exc:
        msg = f"Cannot install {canonical_id}: {exc}"
        raise MarketplaceError(msg) from exc

    for warning in manifest_warnings:
        logger.debug("Plugin install warning for %s: %s", canonical_id, warning)

    version = manifest.version if manifest is not None else None
    cache_path = cache_and_register_plugin(
        canonical_id,
        source_root,
        version=version,
        scope=scope,
        project_root=project_root,
        validate=partial(
            _validate_plugin_copy,
            plugin_id=canonical_id,
            fallback_name=entry.name,
        ),
    )

    set_plugin_enabled_for_scope(
        canonical_id, True, scope=scope, project_root=project_root
    )
    ensure_plugin_data_dir(canonical_id)

    instance, warnings = _plugin_from_install_path(
        plugin_id=canonical_id,
        root=cache_path,
        marketplace_name=marketplace.name,
        fallback_name=entry.name,
    )
    if instance is None:
        detail = "; ".join(warnings)
        uninstall_plugin_record(
            canonical_id, scope=scope, project_root=project_root
        )
        msg = f"Installed {canonical_id} but failed to load from cache: {detail}"
        raise MarketplaceError(msg)
    return instance


def _validate_plugin_copy(
    root: Path,
    *,
    plugin_id: str,
    fallback_name: str,
) -> None:
    try:
        manifest, _manifest_path, warnings = load_manifest(
            root, fallback_name=fallback_name
        )
    except PluginManifestError as exc:
        msg = f"Cannot install {plugin_id}: {exc}"
        raise MarketplaceError(msg) from exc
    build_inventory(root, manifest, warnings)


def _plugin_from_install_path(
    *,
    plugin_id: str,
    root: Path,
    marketplace_name: str,
    fallback_name: str,
) -> tuple[PluginInstance | None, tuple[str, ...]]:
    warnings: list[str] = []
    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            root, fallback_name=fallback_name
        )
    except PluginManifestError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    warnings.extend(manifest_warnings)
    name = manifest.name if manifest and manifest.name else fallback_name
    inventory = build_inventory(root, manifest, tuple(warnings))
    try:
        instance = PluginInstance(
            plugin_id=plugin_id,
            name=name,
            marketplace=marketplace_name,
            version=manifest.version if manifest is not None else None,
            root=root,
            data_dir=plugin_data_dir(plugin_id),
            manifest=manifest,
            inventory=inventory,
        )
    except ValueError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    return instance, inventory.warnings


def _is_likely_plugin_dir(path: Path) -> bool:
    """Return True if ``path`` contains a plugin manifest or standard plugin components."""
    if find_manifest_path(path) is not None:
        return True
    for marker in ("skills", "agents", "commands", ".mcp.json", "SKILL.md"):
        try:
            if (path / marker).exists():
                return True
        except OSError:
            continue
    return False


def _iter_workspace_plugin_dirs(root: Path) -> list[Path]:
    """Return ordered workspace-local plugin directories for ``root``."""
    import json

    discovered: list[Path] = []
    seen_paths: set[Path] = set()

    for cand_parent in (
        root / ".talkops" / "plugins",
        root / ".agents" / "plugins",
        root / "plugins",
    ):
        if not cand_parent.is_dir():
            continue
        try:
            children = sorted(cand_parent.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not _is_likely_plugin_dir(child):
                continue
            try:
                resolved = child.resolve()
            except OSError:
                resolved = child
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                discovered.append(child)

    for dot_dir in (".talkops", ".agents"):
        manifest_file = root / dot_dir / "plugins.json"
        if not manifest_file.is_file():
            continue
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        entries = data.get("entries")
        if not isinstance(entries, list):
            continue
        for raw_entry in entries:
            if not isinstance(raw_entry, str) or not raw_entry.strip():
                continue
            rel = raw_entry.strip()
            for base in (manifest_file.parent, root):
                cand = base / rel
                if cand.is_dir() and _is_likely_plugin_dir(cand):
                    try:
                        resolved = cand.resolve()
                        if not resolved.is_relative_to(root.resolve()):
                            continue
                    except OSError:
                        continue
                    if resolved not in seen_paths:
                        seen_paths.add(resolved)
                        discovered.append(cand)
                    break

    return discovered


def discover_plugins(
    project_root: Path | None = None,
    store: Any = None,
) -> PluginDiscoveryResult:
    """Discover enabled marketplace and workspace-local plugins.

    Uses merged enablement across user + project + local scopes.
    """
    enabled = load_all_enabled_plugin_ids(project_root=project_root)
    disabled = load_all_disabled_plugin_ids(project_root=project_root)
    plugins: list[PluginInstance] = []
    warnings: list[str] = []
    seen_plugin_ids: set[str] = set()

    # 1. Local workspace discovery (.talkops/plugins, .agents/plugins, plugins, plugins.json)
    if project_root:
        root_path = Path(project_root)
        for p in _iter_workspace_plugin_dirs(root_path):
            p_id = f"{p.name}@project"
            if p_id in seen_plugin_ids or p_id in disabled or p.name in disabled:
                continue
            inst, p_warns = _plugin_from_install_path(
                plugin_id=p_id,
                root=p,
                marketplace_name="project",
                fallback_name=p.name,
            )
            warnings.extend(p_warns)
            if inst:
                if f"{inst.name}@project" in disabled or inst.name in disabled:
                    continue
                plugins.append(inst)
                seen_plugin_ids.add(p_id)

    enabled = load_all_enabled_plugin_ids(project_root=project_root)

    for plugin_id in sorted(enabled):
        if plugin_id in seen_plugin_ids:
            continue
        try:
            plugin_name, marketplace_name = split_plugin_id(plugin_id)
        except ValueError:
            warnings.append(f"Ignoring invalid plugin id {plugin_id!r}")
            continue
        entry = get_primary_install_entry(plugin_id)
        needs_install = entry is None
        if entry is not None:
            root = Path(entry.install_path)
            try:
                if not root.is_dir():
                    needs_install = True
            except (OSError, RuntimeError):
                needs_install = True

        if needs_install:
            try:
                scope: InstallScope = "user"
                if project_root:
                    if plugin_id in load_local_enabled_plugin_ids(project_root):
                        scope = "local"
                    elif plugin_id in load_project_enabled_plugin_ids(project_root):
                        scope = "project"
                instance = install_plugin(
                    plugin_id, scope=scope, project_root=project_root
                )
                if instance is not None:
                    plugins.append(instance)
                continue
            except Exception as exc:
                warnings.append(
                    f"Plugin {plugin_id} is enabled but auto-install failed: {exc}"
                )
                continue

        assert entry is not None
        root = Path(entry.install_path)
        try:
            plugin, plugin_warnings = _plugin_from_install_path(
                plugin_id=plugin_id,
                root=root,
                marketplace_name=marketplace_name,
                fallback_name=plugin_name,
            )
        except (OSError, RuntimeError) as exc:
            warnings.append(f"Skipping plugin {plugin_id}: {exc}")
            continue
        warnings.extend(plugin_warnings)
        if plugin is not None:
            plugins.append(plugin)

    return PluginDiscoveryResult(plugins=tuple(plugins), warnings=tuple(warnings))


def list_available_plugins(
    project_root: Path | None = None,
) -> tuple[tuple[str, str, bool], ...]:
    """List plugins from configured marketplaces."""
    records = load_marketplace_records(project_root=project_root)
    enabled = load_all_enabled_plugin_ids(project_root=project_root)
    rows: list[tuple[str, str, bool]] = []
    for name, record in sorted(records.items()):
        try:
            marketplace = load_marketplace_location(Path(record.install_location))
        except MarketplaceError as exc:
            rows.append((f"<marketplace:{name}>", str(exc), False))
            continue
        for plugin in marketplace.plugins:
            plugin_id = f"{plugin.name}@{marketplace.name}"
            rows.append((plugin_id, plugin.description or "", plugin_id in enabled))
    return tuple(rows)


def list_installed_plugin_ids() -> frozenset[str]:
    """Return plugin ids that have install records."""
    return frozenset(load_installed_plugins())
