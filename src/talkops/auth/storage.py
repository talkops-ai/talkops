"""Resilient composite credential storage (`talkops.auth.storage`).

Implements the storage architecture specified in `talkops_enterprise_auth_plan.md`:
1. `should_bypass_keyring()` with 5 headless/remote environment detectors:
   - `ssh_detector`: `SSH_CLIENT`, `SSH_TTY`, or `SSH_CONNECTION`
   - `container_detector`: `/.dockerenv`, `/run/.containerenv`, or `containerd`
   - `wsl_detector`: `WSL_INTEROP`, `WSL_DISTRO_NAME`, or `/proc/sys/kernel/osrelease`
   - `dbus_detector`: Linux without `DBUS_SESSION_BUS_ADDRESS`
   - `android_detector`: `ANDROID_ROOT` or `TERMUX_VERSION`
2. Bounded 1.5s timer around OS Keyring access (`keyring` package or macOS `security` CLI)
   with timeout memoization in `~/.talkops/.state/-keyring-unavailable`.
3. Atomic `0600` file backend at `~/.talkops/.state/auth.json` (`AUTH_PATH`) +
   backward-compatible bridge to `~/.talkops/.env` (`GLOBAL_ENV_PATH`).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

from talkops.auth.models import StoredSession, hash_api_key, mask_api_key
from talkops.config import paths as paths_mod
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

KEYRING_SERVICE_NAME = "talkops"
KEYRING_TIMEOUT_SECONDS = 1.5
KEYRING_UNAVAILABLE_FILENAME = "-keyring-unavailable"


def get_keyring_unavailable_marker() -> Path:
    """Return path to `~/.talkops/.state/-keyring-unavailable`."""
    return paths_mod.AUTH_PATH.parent / KEYRING_UNAVAILABLE_FILENAME


# ── 1. Headless / Environment Keyring Bypass Detectors ────────────────────


def ssh_detector() -> bool:
    """Return True if running inside an SSH session."""
    return any(
        bool(os.environ.get(k))
        for k in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION")
    )


def container_detector() -> bool:
    """Return True if running inside Docker, Podman, or Kubernetes container."""
    if os.environ.get("KUBERNETES_SERVICE_HOST") or os.environ.get("CONTAINER"):
        return True
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            content = cgroup.read_text(encoding="utf-8", errors="ignore")
            if any(tok in content for tok in ("docker", "containerd", "kubepods", "lxc")):
                return True
    except OSError:
        pass
    return False


def wsl_detector() -> bool:
    """Return True if running inside Windows Subsystem for Linux (WSL)."""
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        osrelease = Path("/proc/sys/kernel/osrelease")
        if osrelease.exists():
            text = osrelease.read_text(encoding="utf-8", errors="ignore").lower()
            if "microsoft" in text or "wsl" in text:
                return True
    except OSError:
        pass
    return False


def dbus_detector() -> bool:
    """Return True on Linux when D-Bus session bus is unavailable."""
    if not sys.platform.startswith("linux"):
        return False
    return not bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def android_detector() -> bool:
    """Return True if running on Android / Termux."""
    return bool(os.environ.get("ANDROID_ROOT") or os.environ.get("TERMUX_VERSION"))


def should_bypass_keyring() -> bool:
    """Return True if OS Keyring should be bypassed in favor of `0600` file storage."""
    explicit = os.environ.get("TALKOPS_BYPASS_KEYRING", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False

    # Bypass automatically during pytest runs unless explicitly enabled above
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True

    marker = get_keyring_unavailable_marker()
    if marker.exists():
        return True

    return (
        ssh_detector()
        or container_detector()
        or wsl_detector()
        or dbus_detector()
        or android_detector()
    )


def memoize_keyring_timeout(reason: str = "timeout") -> Path:
    """Write RFC 3339 timestamp to `~/.talkops/.state/-keyring-unavailable`."""
    marker = get_keyring_unavailable_marker()
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{datetime.now(UTC).isoformat()} ({reason})\n"
        marker.write_text(payload, encoding="utf-8")
    return marker


def _run_with_keyring_timeout(fn: Any, *args: Any, timeout: float = KEYRING_TIMEOUT_SECONDS) -> Any:
    """Execute a synchronous keyring call with a strict bounded timer."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.debug("OS keyring timed out after %.2fs; memoizing unavailability", timeout)
        memoize_keyring_timeout("timeout")
        return None
    except Exception as exc:
        logger.debug("OS keyring operation failed (%s)", exc)
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


# ── 2. Composite Credential Storage (`Keyring` + `auth.json` + `.env`) ────


class CompositeCredentialStorage:
    """Manages persistent Control Plane sessions and 22+ provider API keys.

    Persists structured JSON data atomically with `0600` permissions in
    `~/.talkops/.state/auth.json` (`paths.AUTH_PATH`), optionally mirrors secrets
    to the OS Keyring when available, and synchronizes provider env vars with
    `~/.talkops/.env` (`paths.GLOBAL_ENV_PATH`).
    """

    def __init__(
        self,
        auth_file_path: Path | None = None,
        env_file_path: Path | None = None,
    ) -> None:
        self._auth_file_path = auth_file_path
        self._env_file_path = env_file_path

    @property
    def auth_path(self) -> Path:
        return self._auth_file_path or paths_mod.AUTH_PATH

    @property
    def env_path(self) -> Path:
        return self._env_file_path or paths_mod.GLOBAL_ENV_PATH

    def _read_auth_json(self) -> dict[str, Any]:
        target = self.auth_path
        if not target.exists():
            return {"version": 1, "session": None, "providers": {}}
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                return {"version": 1, "session": None, "providers": {}}
            data.setdefault("version", 1)
            data.setdefault("session", None)
            if not isinstance(data.get("providers"), dict):
                data["providers"] = {}
            return data
        except Exception as exc:
            logger.debug("Could not read %s: %s", target, exc)
            return {"version": 1, "session": None, "providers": {}}

    def _write_auth_json(self, data: dict[str, Any]) -> bool:
        target = self.auth_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
            fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".auth_", suffix=".tmp")
            try:
                with contextlib.suppress(OSError):
                    os.chmod(fd, 0o600)
                with open(fd, "w", encoding="utf-8") as f:
                    f.write(serialized)
                tmp_p = Path(tmp_path)
                with contextlib.suppress(OSError):
                    tmp_p.chmod(0o600)
                tmp_p.replace(target)
                with contextlib.suppress(OSError):
                    target.chmod(0o600)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
            return True
        except Exception as exc:
            logger.warning("Failed to write %s: %s", target, exc)
            return False

    def _keyring_set(self, account: str, secret: str) -> bool:
        if should_bypass_keyring() or not secret:
            return False

        def _do_set() -> bool:
            import keyring  # type: ignore[import-not-found]

            keyring.set_password(KEYRING_SERVICE_NAME, account, secret)
            return True

        return bool(_run_with_keyring_timeout(_do_set))

    def _keyring_get(self, account: str) -> str | None:
        if should_bypass_keyring():
            return None

        def _do_get() -> str | None:
            import keyring  # type: ignore[import-not-found]

            val = keyring.get_password(KEYRING_SERVICE_NAME, account)
            return str(val) if val else None

        res = _run_with_keyring_timeout(_do_get)
        return str(res) if isinstance(res, str) and res else None

    def _keyring_delete(self, account: str) -> bool:
        if should_bypass_keyring():
            return False

        def _do_del() -> bool:
            import keyring  # type: ignore[import-not-found]

            keyring.delete_password(KEYRING_SERVICE_NAME, account)
            return True

        return bool(_run_with_keyring_timeout(_do_del))

    # ── Session (Control Plane & Central Server Lease) ────────────────────

    def load_session(self) -> StoredSession | None:
        """Load the stored Control Plane / Central Server session if present."""
        data = self._read_auth_json()
        raw_session = data.get("session")
        if not isinstance(raw_session, dict) or not raw_session:
            return None
        session = StoredSession.from_dict(raw_session)
        if not session.api_key:
            kr_key = self._keyring_get("session:api_key")
            if kr_key:
                session.api_key = kr_key
                session.api_key_prefix = session.api_key_prefix or mask_api_key(kr_key)
                session.source_key_hash = session.source_key_hash or hash_api_key(kr_key)
        return session

    def save_session(
        self,
        session: StoredSession,
        *,
        sync_dotenv: bool = False,
    ) -> bool:
        """Persist `StoredSession` to `auth.json` (`0600`), Keyring, and optionally `.env`."""
        session.updated_at = time.time()
        if session.api_key:
            session.api_key_prefix = mask_api_key(session.api_key)
            session.source_key_hash = hash_api_key(session.api_key)
            self._keyring_set("session:api_key", session.api_key)
        if session.access_token:
            self._keyring_set("session:access_token", session.access_token)

        data = self._read_auth_json()
        data["session"] = session.to_dict(include_secrets=True)
        ok = self._write_auth_json(data)

        if sync_dotenv:
            env_updates: dict[str, str] = {}
            if session.api_key:
                env_updates["TALKOPS_API_KEY"] = session.api_key
                os.environ["TALKOPS_API_KEY"] = session.api_key
            if session.server_url:
                env_updates["TALKOPS_SERVER_URL"] = session.server_url
                os.environ["TALKOPS_SERVER_URL"] = session.server_url
            else:
                os.environ.pop("TALKOPS_SERVER_URL", None)
                if self.env_path.exists():
                    with contextlib.suppress(OSError):
                        lines = self.env_path.read_text(encoding="utf-8").splitlines()
                        remaining = [
                            line
                            for line in lines
                            if not (
                                line.strip().startswith("TALKOPS_SERVER_URL=")
                                or line.strip().startswith("export TALKOPS_SERVER_URL=")
                            )
                        ]
                        self.env_path.write_text(
                            "\n".join(remaining).strip() + ("\n" if remaining else ""),
                            encoding="utf-8",
                        )
            if session.control_plane_url:
                env_updates["TALKOPS_CONTROL_PLANE_URL"] = session.control_plane_url
                os.environ["TALKOPS_CONTROL_PLANE_URL"] = session.control_plane_url
            if session.user_id:
                env_updates["TALKOPS_USER_ID"] = session.user_id
                os.environ["TALKOPS_USER_ID"] = session.user_id
            if env_updates:
                paths_mod.upsert_env_vars(env_updates, env_path=self.env_path)
        return ok

    def clear_session(self, *, clear_dotenv: bool = True) -> bool:
        """Remove the stored Control Plane / Central Server session lease."""
        self._keyring_delete("session:api_key")
        self._keyring_delete("session:access_token")
        data = self._read_auth_json()
        had_session = bool(data.get("session"))
        data["session"] = None
        self._write_auth_json(data)

        if clear_dotenv:
            keys_to_clear = {
                "TALKOPS_API_KEY",
                "TALKOPS_SERVER_API_KEY",
                "TALKOPS_SERVER_URL",
                "TALKOPS_CONTROL_PLANE_URL",
                "TALKOPS_USER_ID",
            }
            for k in keys_to_clear:
                os.environ.pop(k, None)
            target_env = self.env_path
            if target_env.exists():
                with contextlib.suppress(OSError):
                    lines = target_env.read_text(encoding="utf-8").splitlines()
                    remaining = [
                        line
                        for line in lines
                        if not any(
                            line.strip().startswith(f"{k}=")
                            or line.strip().startswith(f"export {k}=")
                            for k in keys_to_clear
                        )
                    ]
                    target_env.write_text(
                        "\n".join(remaining).strip() + ("\n" if remaining else ""),
                        encoding="utf-8",
                    )
        return had_session

    # ── Provider API Keys (22+ LLM Providers) ─────────────────────────────

    def get_provider_entry(self, provider: str) -> dict[str, Any] | None:
        """Return stored `{api_key, base_url, env_var, updated_at}` for `provider`."""
        prov_key = provider.strip().lower()
        data = self._read_auth_json()
        providers = data.get("providers", {})
        entry = providers.get(prov_key)
        if not isinstance(entry, dict):
            kr_secret = self._keyring_get(f"provider:{prov_key}")
            if kr_secret:
                return {"api_key": kr_secret, "key_prefix": mask_api_key(kr_secret)}
            return None
        result = dict(entry)
        if not result.get("api_key"):
            kr_secret = self._keyring_get(f"provider:{prov_key}")
            if kr_secret:
                result["api_key"] = kr_secret
        return result

    def get_provider_key(self, provider: str) -> str | None:
        """Return the stored API key for `provider`, or None."""
        entry = self.get_provider_entry(provider)
        if not entry:
            return None
        val = str(entry.get("api_key") or "").strip()
        return val or None

    def set_provider_key(
        self,
        provider: str,
        api_key: str,
        *,
        env_var: str | None = None,
        base_url: str | None = None,
        base_url_env: str | None = None,
        sync_dotenv: bool = True,
    ) -> bool:
        """Save a provider API key to `auth.json` (`0600`), Keyring, and `.env`."""
        prov_key = provider.strip().lower()
        cleaned_key = api_key.strip()
        if not prov_key or not cleaned_key:
            return False

        from talkops.model.config import get_base_url_env_vars, get_credential_env_var

        resolved_env_var = (
            env_var
            or get_credential_env_var(prov_key)
            or ("LANGSMITH_API_KEY" if prov_key == "langsmith" else f"{prov_key.upper()}_API_KEY")
        )
        base_url_candidates = get_base_url_env_vars(prov_key)
        resolved_base_url_env = base_url_env or (
            base_url_candidates[0] if base_url_candidates else f"{prov_key.upper()}_BASE_URL"
        )

        self._keyring_set(f"provider:{prov_key}", cleaned_key)

        data = self._read_auth_json()
        providers = data.setdefault("providers", {})
        entry: dict[str, Any] = {
            "api_key": cleaned_key,
            "key_prefix": mask_api_key(cleaned_key),
            "env_var": resolved_env_var,
            "updated_at": time.time(),
        }
        if base_url:
            entry["base_url"] = base_url.strip()
            entry["base_url_env"] = resolved_base_url_env
        providers[prov_key] = entry
        ok = self._write_auth_json(data)

        os.environ[resolved_env_var] = cleaned_key
        if base_url:
            os.environ[resolved_base_url_env] = base_url.strip()

        if sync_dotenv:
            env_updates = {resolved_env_var: cleaned_key}
            if base_url:
                env_updates[resolved_base_url_env] = base_url.strip()
            paths_mod.upsert_env_vars(env_updates, env_path=self.env_path)

        return ok

    def delete_provider_key(self, provider: str, *, sync_dotenv: bool = True) -> bool:
        """Delete stored credentials for `provider` (or `'all'`)."""
        prov_key = provider.strip().lower()
        data = self._read_auth_json()
        providers = data.get("providers", {})
        removed_any = False

        if prov_key in {"all", "*"}:
            for k in list(providers.keys()):
                self._keyring_delete(f"provider:{k}")
                removed_any = True
            data["providers"] = {}
            self._write_auth_json(data)
        else:
            self._keyring_delete(f"provider:{prov_key}")
            if prov_key in providers:
                providers.pop(prov_key, None)
                removed_any = True
                self._write_auth_json(data)

        if sync_dotenv:
            from talkops.model.config import revoke_provider_credentials

            revoked_list = revoke_provider_credentials(
                None if prov_key in {"all", "*"} else prov_key
            )
            if revoked_list:
                removed_any = True

        return removed_any

    def list_provider_keys(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of stored provider entries in `auth.json`."""
        data = self._read_auth_json()
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            return {}
        return {str(k): dict(v) for k, v in providers.items() if isinstance(v, dict)}


_default_storage: CompositeCredentialStorage | None = None


def get_credential_storage() -> CompositeCredentialStorage:
    """Return the singleton `CompositeCredentialStorage` instance."""
    global _default_storage
    if _default_storage is None:
        _default_storage = CompositeCredentialStorage()
    return _default_storage


__all__ = [
    "CompositeCredentialStorage",
    "KEYRING_SERVICE_NAME",
    "KEYRING_TIMEOUT_SECONDS",
    "KEYRING_UNAVAILABLE_FILENAME",
    "android_detector",
    "container_detector",
    "dbus_detector",
    "get_credential_storage",
    "get_keyring_unavailable_marker",
    "memoize_keyring_timeout",
    "should_bypass_keyring",
    "ssh_detector",
    "wsl_detector",
]
