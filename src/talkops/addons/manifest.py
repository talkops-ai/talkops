"""Addon manifest parsing (`.talkops/addon.json`) and automatic Python dependency installation."""

from __future__ import annotations

from collections.abc import Sequence
import importlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from talkops.addons.marketplace_models import AddonManifest
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Unified `.talkops/` manifest lookup order (no separate `.talkops-addon/` dir)
_ADDON_MANIFEST_RELATIVE_PATHS: tuple[Path, ...] = (
    Path(".talkops") / "addon.json",
    Path("addon.json"),
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Cache verified requirement strings in-process so repeated checks take 0ms
_VERIFIED_REQUIREMENTS_CACHE: set[str] = set()


class AddonManifestError(ValueError):
    """Raised when an addon manifest (`.talkops/addon.json`) is missing or invalid."""


def find_addon_manifest_path(root: Path) -> Path | None:
    """Return the first `.talkops/addon.json` or `addon.json` path under `root`, if present."""
    for rel in _ADDON_MANIFEST_RELATIVE_PATHS:
        candidate = root / rel
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            logger.debug("Could not inspect addon manifest path %s", candidate)
    return None


def validate_addon_name(name: object, *, fallback: str | None = None) -> str:
    """Validate an addon identifier."""
    if isinstance(name, str) and name.strip() and _NAME_RE.fullmatch(name.strip()):
        return name.strip()
    if fallback and _NAME_RE.fullmatch(fallback.strip()):
        return fallback.strip()
    msg = f"Invalid addon name: {name!r}"
    raise AddonManifestError(msg)


def load_addon_manifest(
    root_or_file: Path,
    *,
    fallback_name: str | None = None,
) -> AddonManifest:
    """Load and validate `.talkops/addon.json` (or `addon.json`) from `root_or_file`."""
    if root_or_file.is_dir():
        manifest_path = find_addon_manifest_path(root_or_file)
        if manifest_path is None:
            msg = f"No .talkops/addon.json found under {root_or_file}"
            raise AddonManifestError(msg)
    else:
        manifest_path = root_or_file

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to read addon manifest {manifest_path}: {exc}"
        raise AddonManifestError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Addon manifest {manifest_path} must be a JSON object"
        raise AddonManifestError(msg)

    name = validate_addon_name(data.get("name"), fallback=fallback_name)
    entry_point = str(
        data.get("entryPoint")
        or data.get("entry_point")
        or data.get("module")
        or data.get("package")
        or ""
    ).strip()
    if not entry_point:
        msg = f"Addon manifest {manifest_path} is missing required 'entryPoint' field"
        raise AddonManifestError(msg)

    version = str(data.get("version") or "0.1.0").strip() or "0.1.0"
    display_name_val = data.get("displayName") or data.get("display_name")
    display_name = str(display_name_val).strip() if isinstance(display_name_val, str) else None
    desc_val = data.get("description")
    description = str(desc_val).strip() if isinstance(desc_val, str) else None

    raw_tier = str(data.get("tier") or "oss").strip().lower()
    tier: Any = raw_tier if raw_tier in {"oss", "enterprise", "custom"} else "oss"

    api_version_raw = data.get("apiVersion", data.get("api_version", 1))
    try:
        api_version = int(api_version_raw)
    except (TypeError, ValueError):
        api_version = 1

    raw_py_path = data.get("pythonPath", data.get("python_path", ["."]))
    if isinstance(raw_py_path, str):
        python_path = (raw_py_path,)
    elif isinstance(raw_py_path, list):
        python_path = tuple(str(p) for p in raw_py_path if isinstance(p, str) and p.strip()) or (".",)
    else:
        python_path = (".",)

    raw_py_deps = data.get("pythonDependencies", data.get("python_dependencies", []))
    if isinstance(raw_py_deps, list):
        python_dependencies = tuple(
            str(d).strip() for d in raw_py_deps if isinstance(d, str) and d.strip()
        )
    else:
        python_dependencies = ()

    raw_deps = data.get("dependencies", [])
    if isinstance(raw_deps, list):
        dependencies = tuple(str(d).strip() for d in raw_deps if isinstance(d, str) and d.strip())
    else:
        dependencies = ()

    enabled_by_default = bool(data.get("enabledByDefault", data.get("enabled_by_default", False)))
    raw_caps = data.get("capabilities")
    capabilities: dict[str, Any] = dict(raw_caps) if isinstance(raw_caps, dict) else {}
    raw_ws = data.get("webhookSink")
    if not isinstance(raw_ws, dict):
        raw_ws = data.get("webhook_sink")
    webhook_sink: dict[str, Any] = dict(raw_ws) if isinstance(raw_ws, dict) else {}
    routes_raw = data.get("routes")
    routes = (
        tuple(r for r in routes_raw if isinstance(r, dict))
        if isinstance(routes_raw, list)
        else ()
    )
    tools_raw = data.get("agentTools", data.get("agent_tools"))
    agent_tools = (
        tuple(t for t in tools_raw if isinstance(t, dict))
        if isinstance(tools_raw, list)
        else ()
    )
    env_raw = data.get("envVars", data.get("env_vars"))
    env_vars = (
        tuple(str(e).strip() for e in env_raw if isinstance(e, str) and e.strip())
        if isinstance(env_raw, list)
        else ()
    )
    raw_schema = data.get("settingsSchema")
    if not isinstance(raw_schema, dict):
        raw_schema = data.get("settings_schema")
    settings_schema: dict[str, Any] = dict(raw_schema) if isinstance(raw_schema, dict) else {}
    raw_author = data.get("author")
    author = raw_author if isinstance(raw_author, (str, dict)) else None

    return AddonManifest(
        name=name,
        display_name=display_name,
        version=version,
        description=description,
        author=author,
        tier=tier,
        api_version=api_version,
        entry_point=entry_point,
        python_path=python_path,
        python_dependencies=python_dependencies,
        dependencies=dependencies,
        enabled_by_default=enabled_by_default,
        capabilities=capabilities,
        webhook_sink=webhook_sink,
        routes=routes,
        agent_tools=agent_tools,
        env_vars=env_vars,
        settings_schema=settings_schema,
        raw=dict(data),
    )


def is_requirement_satisfied(requirement_str: str) -> bool:
    """Check whether a PEP 508 requirement string is satisfied in the current Python environment."""
    cleaned = requirement_str.strip()
    if not cleaned:
        return True
    if cleaned in _VERIFIED_REQUIREMENTS_CACHE:
        return True

    try:
        from packaging.requirements import Requirement

        req = Requirement(cleaned)
        if req.marker and not req.marker.evaluate():
            _VERIFIED_REQUIREMENTS_CACHE.add(cleaned)
            return True
        installed_version = importlib.metadata.version(req.name)
        if req.specifier and not req.specifier.contains(installed_version, prereleases=True):
            return False
        _VERIFIED_REQUIREMENTS_CACHE.add(cleaned)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        # Fallback if `packaging` is unavailable or requirement string is non-standard
        match = _REQ_NAME_RE.match(cleaned)
        if not match:
            return False
        pkg_name = match.group(1)
        try:
            importlib.metadata.version(pkg_name)
            _VERIFIED_REQUIREMENTS_CACHE.add(cleaned)
            return True
        except importlib.metadata.PackageNotFoundError:
            return False


def find_missing_python_dependencies(dependencies: Sequence[str]) -> list[str]:
    """Return the subset of `dependencies` not currently satisfied in the active Python environment."""
    missing: list[str] = []
    for dep in dependencies:
        cleaned = str(dep).strip()
        if cleaned and not is_requirement_satisfied(cleaned):
            missing.append(cleaned)
    return missing


def _build_dependency_install_commands(
    missing_specs: Sequence[str],
    *,
    python_executable: str | None = None,
) -> list[list[str]]:
    """Build candidate installation commands targeting the active Python environment (`sys.executable`).

    Tries in order:
    1. `<sys.executable> -m pip install --quiet <specs>`
    2. `uv pip install --python <sys.executable> --quiet <specs>` (for uv-managed venvs without pip)
    3. `pip install --quiet <specs>`
    """
    py_exe = python_executable or sys.executable
    commands: list[list[str]] = [
        [py_exe, "-m", "pip", "install", "--quiet", *missing_specs],
    ]
    uv_bin = shutil.which("uv")
    if uv_bin:
        commands.append([uv_bin, "pip", "install", "--python", py_exe, "--quiet", *missing_specs])
    pip_bin = shutil.which("pip") or shutil.which("pip3")
    if pip_bin:
        commands.append([pip_bin, "install", "--quiet", *missing_specs])
    return commands


def ensure_addon_python_dependencies(
    dependencies: Sequence[str],
    *,
    addon_name: str = "addon",
    python_executable: str | None = None,
    auto_install: bool = True,
    runner: Any | None = None,
) -> tuple[list[str], list[str]]:
    """Verify and automatically install missing Python dependencies declared in `.talkops/addon.json`.

    Args:
        dependencies: PEP 508 requirement strings from `pythonDependencies`.
        addon_name: Human-readable addon identifier for logging.
        python_executable: Target Python interpreter (defaults to `sys.executable`).
        auto_install: Whether to run the internal package installer when packages are missing.
        runner: Optional callable `(cmd: list[str]) -> subprocess.CompletedProcess` for testing.

    Returns:
        Tuple of `(installed_specs, still_missing_specs)`.
    """
    missing = find_missing_python_dependencies(dependencies)
    if not missing:
        return [], []

    if not auto_install:
        return [], missing

    logger.info(
        "Addon %r requires missing Python dependencies %s; installing into active environment (%s)...",
        addon_name,
        missing,
        python_executable or sys.executable,
    )

    candidate_cmds = _build_dependency_install_commands(
        missing,
        python_executable=python_executable,
    )
    last_error = ""
    installed_ok = False

    for cmd in candidate_cmds:
        try:
            if runner is not None:
                proc = runner(cmd)
            else:
                proc = subprocess.run(  # noqa: S603
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            if proc.returncode == 0:
                installed_ok = True
                break
            stderr_text = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
            last_error = stderr_text or f"exit code {proc.returncode}"
        except Exception as exc:
            last_error = str(exc)

    importlib.invalidate_caches()
    _VERIFIED_REQUIREMENTS_CACHE.difference_update(missing)

    if installed_ok:
        for spec in missing:
            _VERIFIED_REQUIREMENTS_CACHE.add(spec)
        logger.info("Successfully installed Python dependencies for addon %r: %s", addon_name, missing)
        return list(missing), []

    still_missing = find_missing_python_dependencies(missing)
    if still_missing:
        logger.warning(
            "Failed to auto-install Python dependencies %s for addon %r: %s",
            still_missing,
            addon_name,
            last_error,
        )
    installed = [m for m in missing if m not in still_missing]
    return installed, still_missing
