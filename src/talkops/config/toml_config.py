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

"""Atomic TOML configuration persistence for TalkOps."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from talkops.config.paths import CONFIG_PATH, STATE_DIR
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_config_write_lock = threading.Lock()
_RECENT_MODELS_FILE = STATE_DIR / "recent_models.json"
_RECENT_PROFILES_FILE = STATE_DIR / "recent_cloud_profiles.json"
_MAX_RECENT = 10


def read_config_toml(config_path: Path | None = None) -> dict[str, Any]:
    """Read config.toml and return parsed dict, or empty dict on missing/error."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return {}
    try:
        with target_path.open("rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        logger.warning("Could not read config TOML from %s: %s", target_path, exc)
        return {}


def _save_toml_field(
    section: str,
    field: str,
    value: Any,
    config_path: Path | None = None,
) -> bool:
    """Read-modify-write a key under a section in config.toml atomically."""
    target_path = config_path or CONFIG_PATH
    try:
        with _config_write_lock:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                with target_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            if section not in data or not isinstance(data[section], dict):
                data[section] = {}
            data[section][field] = value

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not save %s.%s preference to %s", section, field, target_path)
        return False
    return True


save_toml_field = _save_toml_field


def delete_toml_field(
    section: str,
    field: str,
    config_path: Path | None = None,
) -> bool:
    """Read-modify-write: delete a key under a section in config.toml atomically."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            sec = data.get(section)
            if not isinstance(sec, dict) or field not in sec:
                return True
            del sec[field]
            if not sec:
                del data[section]

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not delete %s.%s from %s", section, field, target_path)
        return False
    return True


def save_recent_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Persist recent model selection to [models].recent in config.toml and MRU cache."""
    if not isinstance(model_spec, str) or not model_spec:
        return False
    ok = _save_toml_field("models", "recent", model_spec, config_path)
    touch_recent_model(model_spec)
    return ok


def load_recent_model(config_path: Path | None = None) -> str | None:
    """Load [models].recent from config.toml."""
    data = read_config_toml(config_path)
    val = data.get("models", {}).get("recent")
    return val.strip() if isinstance(val, str) and val.strip() else None


def save_default_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Persist default model selection to [models].default in config.toml."""
    if not isinstance(model_spec, str) or not model_spec:
        return False
    return _save_toml_field("models", "default", model_spec, config_path)


def clear_default_model(config_path: Path | None = None) -> bool:
    """Remove default model entry from [models].default in config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            models_sec = data.get("models")
            if not isinstance(models_sec, dict) or "default" not in models_sec:
                return True
            del models_sec["default"]

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not clear default model preference")
        return False
    return True


def load_default_model(config_path: Path | None = None) -> str | None:
    """Load [models].default from config.toml, falling back to [models].recent."""
    data = read_config_toml(config_path)
    models_sec = data.get("models", {})
    val = models_sec.get("default")
    if isinstance(val, str) and val.strip():
        return val.strip()
    recent = models_sec.get("recent")
    if isinstance(recent, str) and recent.strip():
        return recent.strip()
    return None


def load_model_providers(config_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load [models.providers] dictionary from config.toml."""
    data = read_config_toml(config_path)
    models_sec = data.get("models", {})
    if not isinstance(models_sec, dict):
        return {}
    providers = models_sec.get("providers", {})
    if not isinstance(providers, dict):
        return {}
    return {
        str(k).strip(): dict(v)
        for k, v in providers.items()
        if isinstance(k, str) and k.strip() and isinstance(v, dict)
    }


def touch_recent_model(model_spec: str) -> bool:
    """Promote model_spec to the front of recent_models.json."""
    if not isinstance(model_spec, str) or not model_spec:
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        recent: list[str] = []
        if _RECENT_MODELS_FILE.exists():
            try:
                import json

                raw = json.loads(_RECENT_MODELS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    recent = [str(item) for item in raw]
                elif isinstance(raw, dict) and isinstance(raw.get("models"), list):
                    recent = [str(item) for item in raw["models"]]
            except Exception:
                recent = []
        recent = [s for s in recent if s != model_spec]
        recent.insert(0, model_spec)
        recent = recent[:_MAX_RECENT]
        import json

        _RECENT_MODELS_FILE.write_text(json.dumps({"models": recent}, indent=2), encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning("Could not update recent models file: %s", exc)
        return False


def save_effort_for_model(
    model_spec: str,
    effort: str | None,
    config_path: Path | None = None,
) -> bool:
    """Read-modify-write an entry under [effort.by_model] in config.toml."""
    if not isinstance(model_spec, str) or not model_spec:
        return False
    target_path = config_path or CONFIG_PATH

    try:
        with _config_write_lock:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                with target_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            effort_sec = data.setdefault("effort", {})
            if not isinstance(effort_sec, dict):
                effort_sec = {}
                data["effort"] = effort_sec
            by_mod = effort_sec.setdefault("by_model", {})
            if not isinstance(by_mod, dict):
                by_mod = {}
                effort_sec["by_model"] = by_mod

            if effort is None:
                by_mod.pop(model_spec, None)
                if not by_mod:
                    effort_sec.pop("by_model", None)
                if not effort_sec:
                    data.pop("effort", None)
            else:
                by_mod[model_spec] = effort

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not save reasoning effort preference for %s", model_spec)
        return False
    return True


def clear_effort_for_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Remove reasoning effort entry for model_spec from [effort.by_model] in config.toml."""
    return save_effort_for_model(model_spec, None, config_path=config_path)


def load_effort_for_model(model_spec: str, config_path: Path | None = None) -> str | None:
    """Load persisted reasoning effort for a model from [effort.by_model] in config.toml."""
    data = read_config_toml(config_path)
    by_mod = data.get("effort", {}).get("by_model", {})
    if isinstance(by_mod, dict):
        val = by_mod.get(model_spec)
        return val.strip() if isinstance(val, str) and val.strip() else None
    return None


def save_recent_agent(agent_name: str, config_path: Path | None = None) -> bool:
    """Persist recent agent selection to [agents].recent in config.toml."""
    return _save_toml_field("agents", "recent", agent_name, config_path)


def load_recent_agent(config_path: Path | None = None) -> str | None:
    """Load [agents].recent from config.toml."""
    data = read_config_toml(config_path)
    val = data.get("agents", {}).get("recent")
    return val.strip() if isinstance(val, str) and val.strip() else None


def save_default_agent(agent_name: str, config_path: Path | None = None) -> bool:
    """Persist default agent selection to [agents].default in config.toml."""
    return _save_toml_field("agents", "default", agent_name, config_path)


def clear_default_agent(config_path: Path | None = None) -> bool:
    """Remove default agent entry from [agents].default in config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            agents_sec = data.get("agents")
            if not isinstance(agents_sec, dict) or "default" not in agents_sec:
                return True
            del agents_sec["default"]

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not clear default agent preference")
        return False
    return True


def load_default_agent(config_path: Path | None = None) -> str | None:
    """Load [agents].default from config.toml, falling back to [agents].recent."""
    data = read_config_toml(config_path)
    agents_sec = data.get("agents", {})
    val = agents_sec.get("default")
    if isinstance(val, str) and val.strip():
        return val.strip()
    recent = agents_sec.get("recent")
    if isinstance(recent, str) and recent.strip():
        return recent.strip()
    return None


def save_theme_preference(theme: str, config_path: Path | None = None) -> bool:
    """Persist theme preference to [ui].theme in config.toml."""
    return _save_toml_field("ui", "theme", theme, config_path)


def load_theme_preference(config_path: Path | None = None) -> str:
    """Load theme preference from [ui].theme in config.toml, defaulting to 'talkops-dark'."""
    data = read_config_toml(config_path)
    val = data.get("ui", {}).get("theme")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return "talkops-dark"


def apply_stored_credentials() -> None:
    """Apply any stored credentials from config and credential storage into the environment."""
    data = read_config_toml()
    env_sec = data.get("env", {})
    if isinstance(env_sec, dict):
        for k, v in env_sec.items():
            if k not in os.environ and isinstance(v, str):
                os.environ[k] = v
    with contextlib.suppress(Exception):
        from talkops.state.remote_session import _can_use_disk_auth_session

        if _can_use_disk_auth_session():
            from talkops.auth.storage import get_credential_storage

            storage = get_credential_storage()
            for prov in storage.list_provider_keys():
                entry = storage.get_provider_entry(prov)
                if not entry:
                    continue
                env_var = entry.get("env_var")
                api_key = entry.get("api_key")
                if isinstance(env_var, str) and env_var and isinstance(api_key, str) and api_key:
                    if env_var not in os.environ:
                        os.environ[env_var] = api_key
                base_url_env = entry.get("base_url_env")
                base_url = entry.get("base_url")
                if isinstance(base_url_env, str) and base_url_env and isinstance(base_url, str) and base_url:
                    if base_url_env not in os.environ:
                        os.environ[base_url_env] = base_url


def save_agent_pool(
    pool: dict[str, str],
    provider: str | None = None,
    config_path: Path | None = None,
) -> bool:
    """Save user-configured agent model pool to [agent_pool] in config.toml."""
    target_path = config_path or CONFIG_PATH
    try:
        with _config_write_lock:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                with target_path.open("rb") as f:
                    data = tomllib.load(f)
            else:
                data = {}

            pool_sec: dict[str, Any] = {}
            if provider:
                pool_sec["provider"] = provider
            for key in ("fast", "standard", "powerful"):
                if key in pool and pool[key]:
                    pool_sec[key] = str(pool[key]).strip()

            data["agent_pool"] = pool_sec

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not save agent_pool preference to %s", target_path)
        return False
    return True


def load_agent_pool(config_path: Path | None = None) -> dict[str, str] | None:
    """Load user-configured agent model pool from [agent_pool] in config.toml."""
    data = read_config_toml(config_path)
    pool_sec = data.get("agent_pool") or data.get("model_pool")
    if not isinstance(pool_sec, dict) or not pool_sec:
        return None

    res: dict[str, str] = {}
    for key in ("fast", "standard", "powerful", "provider"):
        val = pool_sec.get(key)
        if isinstance(val, str) and val.strip():
            res[key] = val.strip()

    if not any(k in res for k in ("fast", "standard", "powerful")):
        return None
    return res


def format_agent_pool_summary(pool: dict[str, str] | None) -> str:
    """Return a compact human-readable summary of an agent model pool dictionary."""
    if not pool:
        return "default (dynamic discovery)"
    fast = pool.get("fast", "-")
    std = pool.get("standard", "-")
    pow_m = pool.get("powerful", "-")
    prov = pool.get("provider", "multi")
    return f"fast='{fast}', standard='{std}', powerful='{pow_m}' (provider: {prov})"



def clear_agent_pool(config_path: Path | None = None) -> bool:
    """Remove [agent_pool] section from config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            if "agent_pool" not in data and "model_pool" not in data:
                return True
            data.pop("agent_pool", None)
            data.pop("model_pool", None)

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not clear agent_pool preference")
        return False
    return True


def save_aws_profile(profile: str, config_path: Path | None = None) -> bool:
    """Persist AWS profile selection to [aws].profile in config.toml."""
    if not isinstance(profile, str) or not profile.strip():
        return False
    clean_profile = profile.strip()
    ok = _save_toml_field("aws", "profile", clean_profile, config_path)
    save_recent_aws_profile(clean_profile)
    return ok


def load_aws_profile(config_path: Path | None = None) -> str | None:
    """Load [aws].profile from config.toml with fallback to [cloud].aws_profile."""
    data = read_config_toml(config_path)
    val = data.get("aws", {}).get("profile")
    if isinstance(val, str) and val.strip():
        return val.strip()
    val = data.get("cloud", {}).get("aws_profile")
    if isinstance(val, str) and val.strip():
        return val.strip()
    val = data.get("cloud", {}).get("profile")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def save_aws_region(region: str, config_path: Path | None = None) -> bool:
    """Persist AWS region selection to [aws].region in config.toml."""
    if not isinstance(region, str) or not region.strip():
        return False
    return _save_toml_field("aws", "region", region.strip(), config_path)


def load_aws_region(config_path: Path | None = None) -> str | None:
    """Load [aws].region from config.toml with fallback to [cloud].aws_region."""
    data = read_config_toml(config_path)
    val = data.get("aws", {}).get("region")
    if isinstance(val, str) and val.strip():
        return val.strip()
    val = data.get("cloud", {}).get("aws_region")
    if isinstance(val, str) and val.strip():
        return val.strip()
    val = data.get("cloud", {}).get("region")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def clear_aws_profile(config_path: Path | None = None) -> bool:
    """Remove [aws].profile from config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            aws_sec = data.get("aws")
            if not isinstance(aws_sec, dict) or "profile" not in aws_sec:
                return True
            del aws_sec["profile"]
            if not aws_sec:
                data.pop("aws", None)

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not clear aws.profile preference")
        return False
    return True


def clear_aws_region(config_path: Path | None = None) -> bool:
    """Remove [aws].region from config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            aws_sec = data.get("aws")
            if not isinstance(aws_sec, dict) or "region" not in aws_sec:
                return True
            del aws_sec["region"]
            if not aws_sec:
                data.pop("aws", None)

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        logger.exception("Could not clear aws.region preference")
        return False
    return True


def save_recent_aws_profile(profile: str) -> bool:
    """Promote profile to front of recent_cloud_profiles.json."""
    if not isinstance(profile, str) or not profile.strip():
        return False
    clean_profile = profile.strip()
    try:
        import json

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        recent_aws: list[str] = []
        recent_targets: list[str] = []
        if _RECENT_PROFILES_FILE.exists():
            try:
                raw = json.loads(_RECENT_PROFILES_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    recent_aws = [str(x) for x in raw]
                elif isinstance(raw, dict):
                    if isinstance(raw.get("aws_profiles"), list):
                        recent_aws = [str(x) for x in raw["aws_profiles"]]
                    if isinstance(raw.get("cloud_targets"), list):
                        recent_targets = [str(x) for x in raw["cloud_targets"]]
            except Exception:
                recent_aws = []
                recent_targets = []
        recent_aws = [p for p in recent_aws if p != clean_profile]
        recent_aws.insert(0, clean_profile)
        recent_aws = recent_aws[:_MAX_RECENT]

        q_id = clean_profile if ":" in clean_profile else f"aws:{clean_profile}"
        recent_targets = [t for t in recent_targets if t != q_id]
        recent_targets.insert(0, q_id)
        recent_targets = recent_targets[:_MAX_RECENT]

        _RECENT_PROFILES_FILE.write_text(
            json.dumps({"aws_profiles": recent_aws, "cloud_targets": recent_targets}, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger.warning("Could not update recent cloud profiles: %s", exc)
        return False


def load_recent_aws_profiles() -> list[str]:
    """Load recent AWS profiles from recent_cloud_profiles.json."""
    if not _RECENT_PROFILES_FILE.exists():
        return []
    try:
        import json

        raw = json.loads(_RECENT_PROFILES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, dict) and isinstance(raw.get("aws_profiles"), list):
            return [str(x) for x in raw["aws_profiles"]]
        return []
    except Exception:
        return []


def save_recent_cloud_target(target_id: str, provider: str = "aws") -> bool:
    """Promote a multi-cloud target (`<provider>:<target_id>`) to front of recent_cloud_profiles.json."""
    if not isinstance(target_id, str) or not target_id.strip():
        return False
    clean_id = target_id.strip()
    prov = provider.strip().lower() if provider else "aws"
    if ":" in clean_id and clean_id.split(":", 1)[0] in ("aws", "azure", "gcp", "k8s"):
        prov, clean_id = clean_id.split(":", 1)
    qualified = f"{prov}:{clean_id}"

    if prov == "aws":
        return save_recent_aws_profile(clean_id)

    try:
        import json

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        recent_aws: list[str] = []
        recent_targets: list[str] = []
        if _RECENT_PROFILES_FILE.exists():
            try:
                raw = json.loads(_RECENT_PROFILES_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    recent_aws = [str(x) for x in raw]
                    recent_targets = [f"aws:{x}" for x in raw]
                elif isinstance(raw, dict):
                    if isinstance(raw.get("aws_profiles"), list):
                        recent_aws = [str(x) for x in raw["aws_profiles"]]
                    if isinstance(raw.get("cloud_targets"), list):
                        recent_targets = [str(x) for x in raw["cloud_targets"]]
            except Exception:
                pass

        recent_targets = [t for t in recent_targets if t != qualified]
        recent_targets.insert(0, qualified)
        recent_targets = recent_targets[:_MAX_RECENT]

        _RECENT_PROFILES_FILE.write_text(
            json.dumps({"aws_profiles": recent_aws, "cloud_targets": recent_targets}, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger.warning("Could not update recent cloud targets: %s", exc)
        return False


def load_recent_cloud_targets() -> list[str]:
    """Load recent multi-cloud targets (`<provider>:<target_id>`) from recent_cloud_profiles.json."""
    if not _RECENT_PROFILES_FILE.exists():
        return []
    try:
        import json

        raw = json.loads(_RECENT_PROFILES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [f"aws:{x}" if ":" not in str(x) else str(x) for x in raw]
        if isinstance(raw, dict):
            if isinstance(raw.get("cloud_targets"), list):
                return [str(x) for x in raw["cloud_targets"]]
            if isinstance(raw.get("aws_profiles"), list):
                return [f"aws:{x}" if ":" not in str(x) else str(x) for x in raw["aws_profiles"]]
        return []
    except Exception:
        return []


def save_active_cloud_context(
    provider: str,
    target_id: str,
    *,
    region: str | None = None,
    account_or_project: str | None = None,
    config_path: Path | None = None,
) -> bool:
    """Persist active multi-cloud provider and target context to [cloud] in config.toml."""
    prov = provider.strip().lower()
    tid = target_id.strip()
    if not prov or not tid:
        return False

    target_path = config_path or CONFIG_PATH
    try:
        with _config_write_lock:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            data = read_config_toml(target_path) if target_path.exists() else {}
            cloud_sec = data.get("cloud")
            if not isinstance(cloud_sec, dict):
                cloud_sec = {}
                data["cloud"] = cloud_sec

            cloud_sec["active_provider"] = prov
            cloud_sec["target_id"] = tid
            if region and region.strip():
                cloud_sec["region"] = region.strip()
            else:
                cloud_sec.pop("region", None)
            if account_or_project and account_or_project.strip():
                cloud_sec["account_or_project"] = account_or_project.strip()

            # Provider-specific subsection for quick switching back
            prov_sec = cloud_sec.get(prov)
            if not isinstance(prov_sec, dict):
                prov_sec = {}
                cloud_sec[prov] = prov_sec
            prov_sec["target_id"] = tid
            if region and region.strip():
                prov_sec["region"] = region.strip()
            if account_or_project and account_or_project.strip():
                prov_sec["account_or_project"] = account_or_project.strip()

            # Maintain backward-compatible [aws] section when provider is aws
            if prov == "aws":
                aws_sec = data.get("aws")
                if not isinstance(aws_sec, dict):
                    aws_sec = {}
                    data["aws"] = aws_sec
                aws_sec["profile"] = tid
                if region and region.strip():
                    aws_sec["region"] = region.strip()
                else:
                    aws_sec.pop("region", None)

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except Exception:
        logger.exception("Could not save cloud context preference")
        return False

    save_recent_cloud_target(tid, provider=prov)
    return True


def load_active_cloud_context(config_path: Path | None = None) -> dict[str, str | None] | None:
    """Load persisted multi-cloud context from [cloud] or fallback [aws] in config.toml."""
    data = read_config_toml(config_path)
    cloud_sec = data.get("cloud")
    if isinstance(cloud_sec, dict):
        prov = cloud_sec.get("active_provider")
        tid = cloud_sec.get("target_id")
        if isinstance(prov, str) and prov.strip() and isinstance(tid, str) and tid.strip():
            reg = cloud_sec.get("region")
            acct = cloud_sec.get("account_or_project")
            return {
                "provider": prov.strip().lower(),
                "target_id": tid.strip(),
                "region": reg.strip() if isinstance(reg, str) and reg.strip() else None,
                "account_or_project": acct.strip() if isinstance(acct, str) and acct.strip() else None,
            }

    aws_prof = load_aws_profile(config_path)
    if aws_prof:
        aws_reg = load_aws_region(config_path)
        return {
            "provider": "aws",
            "target_id": aws_prof,
            "region": aws_reg,
            "account_or_project": None,
        }
    return None


def clear_active_cloud_context(config_path: Path | None = None) -> bool:
    """Clear persisted cloud target and AWS profile from config.toml."""
    target_path = config_path or CONFIG_PATH
    if not target_path.exists():
        return True
    try:
        with _config_write_lock:
            with target_path.open("rb") as f:
                data = tomllib.load(f)
            data.pop("aws", None)
            cloud_sec = data.get("cloud")
            if isinstance(cloud_sec, dict):
                for k in ("active_provider", "target_id", "region", "account_or_project", "aws", "azure", "gcp", "k8s"):
                    cloud_sec.pop(k, None)
                if not cloud_sec:
                    data.pop("cloud", None)

            fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    tomli_w.dump(data, f)
                Path(tmp_path).replace(target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise
    except Exception:
        logger.exception("Could not clear active cloud context preference")
        return False
    return True


def save_cloud_execution_mode(mode: str, config_path: Path | None = None) -> bool:
    """Persist cloud execution mode ('read_only' or 'elevated') to [cloud].execution_mode."""
    norm = mode.strip().lower().replace("-", "_")
    if norm not in ("read_only", "elevated"):
        return False
    return _save_toml_field("cloud", "execution_mode", norm, config_path)


def load_cloud_execution_mode(config_path: Path | None = None) -> str:
    """Load [cloud].execution_mode from config.toml (defaults to 'elevated')."""
    env_mode = os.environ.get("TALKOPS_CLOUD_EXECUTION_MODE", "").strip().lower().replace("-", "_")
    if env_mode in ("read_only", "elevated"):
        return env_mode
    data = read_config_toml(config_path)
    val = data.get("cloud", {}).get("execution_mode")
    if isinstance(val, str):
        norm = val.strip().lower().replace("-", "_")
        if norm in ("read_only", "elevated"):
            return norm
    return "elevated"


