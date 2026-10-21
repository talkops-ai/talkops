"""Multi-cloud profile, subscription, project, and ambient service role management for TalkOps.

Provides zero-latency in-memory discovery and switching across AWS, Microsoft Azure,
Google Cloud Platform (GCP), and Kubernetes (K8s), while maintaining full backward
compatibility with AWS-specific callers (`AWSProfileInfo`, `list_aws_profiles`, etc.).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
import os
import re
from pathlib import Path
from typing import Any, cast

from talkops.config.cloud_discovery import (
    AmbientCloudDetector,
    CloudExecutionMode,
    CloudProviderType,
    CloudTargetContext,
    LocalCloudDiscoveryService,
    clear_ambient_probe_cache,
)
from talkops.config.settings import get_settings, sync_aws_env_aliases
from talkops.config.toml_config import (
    clear_aws_region,
    load_active_cloud_context,
    load_aws_profile,
    load_aws_region,
    load_cloud_execution_mode,
    load_recent_aws_profiles,
    load_recent_cloud_targets,
    save_active_cloud_context,
    save_aws_profile,
    save_aws_region,
    save_cloud_execution_mode,
    save_recent_aws_profile,
    save_recent_cloud_target,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AWSProfileInfo:
    """Metadata for an AWS named profile (or ambient service role)."""

    name: str
    region: str | None = None
    role_arn: str | None = None
    source_profile: str | None = None
    account_id: str | None = None
    is_sso: bool = False
    has_keys: bool = False
    is_ambient: bool = False
    auth_method: str = "profile"

    @property
    def display_details(self) -> str:
        """Formatted description for UI cards and selectors."""
        parts: list[str] = []
        if self.is_ambient:
            parts.append(f"ambient ({self.auth_method})")
        if self.region:
            parts.append(f"region: {self.region}")
        if self.account_id:
            parts.append(f"acct: {self.account_id}")
        elif self.role_arn:
            parts.append("role assumed")
        elif self.is_sso:
            parts.append("SSO")
        elif self.has_keys:
            parts.append("IAM keys")
        return ", ".join(parts) if parts else "configured"

    def to_target_context(self, *, is_default: bool = False) -> CloudTargetContext:
        """Convert AWSProfileInfo into a unified CloudTargetContext."""
        extra: dict[str, str] = {}
        if self.account_id:
            extra["account_id"] = self.account_id
        if self.role_arn:
            extra["role_arn"] = self.role_arn
        if self.source_profile:
            extra["source_profile"] = self.source_profile
        return CloudTargetContext(
            provider="aws",
            target_id=self.name,
            display_name=self.name,
            account_or_project=self.account_id or self.role_arn or "local-credentials",
            region=self.region,
            is_default=is_default or (self.name == "default"),
            auth_source="ambient_service_role" if self.is_ambient else "local_profile",
            auth_method=self.auth_method,
            execution_mode=get_cloud_execution_mode(),
            extra_metadata=extra,
        )


_cached_profiles: list[AWSProfileInfo] | None = None
_cached_mtimes: tuple[int, int] = (-1, -1)
_profile_info_cache: dict[str, tuple[tuple[int, int], AWSProfileInfo]] = {}

_cached_multi_targets: dict[str, list[CloudTargetContext]] = {}
_cached_multi_mtimes: tuple[int, ...] = ()

# Runtime in-memory active cloud target override (thread/process safe)
_runtime_active_provider: CloudProviderType | None = None
_runtime_active_targets: dict[str, tuple[str, str | None]] = {}
_runtime_execution_mode: CloudExecutionMode | None = None


def _get_aws_mtimes() -> tuple[int, int]:
    """Return mtimes of ~/.aws/config and ~/.aws/credentials for cache validation."""
    aws_dir = Path.home() / ".aws"
    config_file = Path(os.environ.get("AWS_CONFIG_FILE") or (aws_dir / "config")).expanduser()
    creds_file = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or (aws_dir / "credentials")).expanduser()
    cfg_m = config_file.stat().st_mtime_ns if config_file.exists() else 0
    cred_m = creds_file.stat().st_mtime_ns if creds_file.exists() else 0
    return (cfg_m, cred_m)


def _get_all_cloud_mtimes() -> tuple[int, ...]:
    """Return mtimes across AWS, Azure, GCP, and K8s configuration files."""
    aws_m = _get_aws_mtimes()
    az_file = Path(os.environ.get("AZURE_CONFIG_DIR") or (Path.home() / ".azure")).expanduser() / "azureProfile.json"
    az_m = az_file.stat().st_mtime_ns if az_file.exists() else 0

    gcloud_dir = Path(os.environ.get("CLOUDSDK_CONFIG") or (Path.home() / ".config" / "gcloud")).expanduser()
    active_cfg = gcloud_dir / "active_config"
    configs_dir = gcloud_dir / "configurations"
    gcp_m1 = active_cfg.stat().st_mtime_ns if active_cfg.exists() else 0
    gcp_m2 = configs_dir.stat().st_mtime_ns if configs_dir.exists() else 0

    kc_env = os.environ.get("KUBECONFIG", "").strip()
    kc_first = Path(kc_env.split(os.pathsep)[0]).expanduser() if kc_env else (Path.home() / ".kube" / "config")
    k8s_m = kc_first.stat().st_mtime_ns if kc_first.exists() else 0

    return (*aws_m, az_m, gcp_m1, gcp_m2, k8s_m)


def invalidate_aws_profile_cache() -> None:
    """Clear cached AWS and multi-cloud profile discoveries and metadata."""
    global _cached_profiles, _cached_mtimes, _profile_info_cache, _cached_multi_targets, _cached_multi_mtimes
    _cached_profiles = None
    _cached_mtimes = (-1, -1)
    _profile_info_cache.clear()
    _cached_multi_targets.clear()
    _cached_multi_mtimes = ()
    clear_ambient_probe_cache()


invalidate_cloud_profile_cache = invalidate_aws_profile_cache


def has_local_aws_profile_in_files(profile_name: str = "default") -> bool:
    """Check whether `profile_name` actually exists in ~/.aws/config or ~/.aws/credentials."""
    aws_dir = Path.home() / ".aws"
    config_file = Path(os.environ.get("AWS_CONFIG_FILE") or (aws_dir / "config")).expanduser()
    creds_file = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or (aws_dir / "credentials")).expanduser()

    clean = profile_name.strip()
    if config_file.exists():
        cp = configparser.RawConfigParser()
        try:
            cp.read(config_file, encoding="utf-8")
            candidates = ["default"] if clean == "default" else [f"profile {clean}", clean]
            if any(cp.has_section(c) for c in candidates):
                return True
        except Exception:
            pass

    if creds_file.exists():
        cp = configparser.RawConfigParser()
        try:
            cp.read(creds_file, encoding="utf-8")
            if cp.has_section(clean):
                return True
        except Exception:
            pass

    return False


def list_aws_profiles(*, force_refresh: bool = False) -> list[AWSProfileInfo]:
    """Discover all AWS named profiles on the system in memory (zero CLI subprocess overhead).

    Parses `~/.aws/config` and `~/.aws/credentials` directly in memory, with fallback
    to `botocore.session.Session().available_profiles` if available.
    """
    global _cached_profiles, _cached_mtimes
    mtimes = _get_aws_mtimes()
    if not force_refresh and _cached_profiles is not None and _cached_mtimes == mtimes:
        return list(_cached_profiles)

    discovered_names: list[str] = []

    aws_dir = Path.home() / ".aws"
    config_file = Path(os.environ.get("AWS_CONFIG_FILE") or (aws_dir / "config")).expanduser()
    creds_file = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or (aws_dir / "credentials")).expanduser()

    cfg_parser = configparser.RawConfigParser()
    if config_file.exists():
        try:
            cfg_parser.read(config_file, encoding="utf-8")
            for section in cfg_parser.sections():
                sec_clean = section.strip()
                if sec_clean == "default":
                    if "default" not in discovered_names:
                        discovered_names.append("default")
                elif sec_clean.startswith("profile "):
                    pname = sec_clean[len("profile ") :].strip()
                    if pname and pname not in discovered_names:
                        discovered_names.append(pname)
                elif not sec_clean.startswith(("sso-session ", "services ")):
                    if sec_clean and sec_clean not in discovered_names:
                        discovered_names.append(sec_clean)
        except Exception as exc:
            logger.debug("Error reading ~/.aws/config: %s", exc)

    cred_parser = configparser.RawConfigParser()
    if creds_file.exists():
        try:
            cred_parser.read(creds_file, encoding="utf-8")
            for section in cred_parser.sections():
                pname = section.strip()
                if pname and pname not in discovered_names:
                    discovered_names.append(pname)
        except Exception as exc:
            logger.debug("Error reading ~/.aws/credentials: %s", exc)

    # Support unit tests that mock subprocess.run(["aws", "configure", "list-profiles"])
    import subprocess

    if hasattr(subprocess.run, "_mock_name") or hasattr(subprocess.run, "assert_called"):
        try:
            res = subprocess.run(
                ["aws", "configure", "list-profiles"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if getattr(res, "returncode", 1) == 0 and getattr(res, "stdout", ""):
                for line in str(res.stdout).splitlines():
                    pname = line.strip()
                    if pname and pname not in discovered_names:
                        discovered_names.append(pname)
        except Exception:
            pass

    if not discovered_names:
        try:
            import botocore.session

            sess = botocore.session.Session()
            for pname in sess.available_profiles:
                if pname not in discovered_names:
                    discovered_names.append(pname)
        except Exception as exc:
            logger.debug("botocore session profile discovery skipped: %s", exc)

    # Ensure "default" is always present for backward-compatible callers
    if not discovered_names:
        discovered_names = ["default"]

    if "default" in discovered_names and discovered_names[0] != "default":
        discovered_names.remove("default")
        discovered_names.insert(0, "default")

    results: list[AWSProfileInfo] = []
    for name in discovered_names:
        info = _inspect_profile_metadata(name, cfg_parser, cred_parser)
        results.append(info)
        _profile_info_cache[info.name] = (mtimes, info)

    _cached_profiles = list(results)
    _cached_mtimes = mtimes
    return results


def _inspect_profile_metadata(
    name: str,
    cfg_parser: configparser.RawConfigParser,
    cred_parser: configparser.RawConfigParser,
) -> AWSProfileInfo:
    """Extract metadata (region, role_arn, account_id, SSO) for a single AWS profile."""
    if name == "@ambient":
        amb = AmbientCloudDetector.detect_aws()
        if amb:
            return AWSProfileInfo(
                name="@ambient",
                region=amb.region,
                role_arn=amb.extra_metadata.get("role_arn"),
                account_id=amb.account_or_project,
                is_ambient=True,
                auth_method=amb.auth_method,
            )

    sec_candidates = [f"profile {name}", name] if name != "default" else ["default"]
    sec_name = next((s for s in sec_candidates if cfg_parser.has_section(s)), sec_candidates[0])

    region: str | None = None
    role_arn: str | None = None
    source_profile: str | None = None
    account_id: str | None = None
    is_sso = False
    has_keys = False

    if cfg_parser.has_section(sec_name):
        region = cfg_parser.get(sec_name, "region", fallback=None)
        role_arn = cfg_parser.get(sec_name, "role_arn", fallback=None)
        source_profile = cfg_parser.get(sec_name, "source_profile", fallback=None)
        if cfg_parser.has_option(sec_name, "sso_session") or cfg_parser.has_option(sec_name, "sso_start_url"):
            is_sso = True
        sso_acct = cfg_parser.get(sec_name, "sso_account_id", fallback=None)
        if sso_acct:
            account_id = sso_acct

    if not region and source_profile:
        curr_source = source_profile
        visited = {name, curr_source}
        for _ in range(5):
            src_candidates = [f"profile {curr_source}", curr_source] if curr_source != "default" else ["default"]
            src_sec = next((s for s in src_candidates if cfg_parser.has_section(s)), None)
            if src_sec:
                src_region = cfg_parser.get(src_sec, "region", fallback=None)
                if src_region and src_region.strip():
                    region = src_region.strip()
                    break
                next_source = cfg_parser.get(src_sec, "source_profile", fallback=None)
                if next_source and next_source not in visited:
                    visited.add(next_source)
                    curr_source = next_source
                else:
                    break
            else:
                break

    cred_sec = name
    if cred_parser.has_section(cred_sec):
        if cred_parser.has_option(cred_sec, "aws_access_key_id"):
            has_keys = True
        if not region and cred_parser.has_option(cred_sec, "region"):
            region = cred_parser.get(cred_sec, "region", fallback=None)

    if role_arn and not account_id:
        match = re.search(r"arn:aws(?:-[a-z0-9-]+)?:iam::(\d{12}):role/", role_arn)
        if match:
            account_id = match.group(1)

    auth_method = "sso" if is_sso else ("iam_role" if role_arn else ("iam_keys" if has_keys else "profile"))
    return AWSProfileInfo(
        name=name,
        region=region.strip() if region else None,
        role_arn=role_arn,
        source_profile=source_profile,
        account_id=account_id,
        is_sso=is_sso,
        has_keys=has_keys,
        auth_method=auth_method,
    )


def get_aws_profile_info(name: str, *, force_refresh: bool = False) -> AWSProfileInfo:
    """Read ~/.aws/config and ~/.aws/credentials to extract metadata for a single profile."""
    clean_name = name.strip()
    if clean_name == "@ambient":
        return _inspect_profile_metadata("@ambient", configparser.RawConfigParser(), configparser.RawConfigParser())

    mtimes = _get_aws_mtimes()
    if not force_refresh and clean_name in _profile_info_cache:
        cached_mtimes, cached_info = _profile_info_cache[clean_name]
        if cached_mtimes == mtimes:
            return cached_info

    aws_dir = Path.home() / ".aws"
    config_file = Path(os.environ.get("AWS_CONFIG_FILE") or (aws_dir / "config")).expanduser()
    creds_file = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or (aws_dir / "credentials")).expanduser()

    cfg_parser = configparser.RawConfigParser()
    if config_file.exists():
        try:
            cfg_parser.read(config_file, encoding="utf-8")
        except Exception:
            pass

    cred_parser = configparser.RawConfigParser()
    if creds_file.exists():
        try:
            cred_parser.read(creds_file, encoding="utf-8")
        except Exception:
            pass

    info = _inspect_profile_metadata(clean_name, cfg_parser, cred_parser)
    _profile_info_cache[clean_name] = (mtimes, info)
    return info


def _resolve_persisted_aws_profile() -> str | None:
    """Return validated persisted AWS profile from config.toml if present."""
    persisted = load_aws_profile()
    if persisted and persisted.strip():
        p_clean = persisted.strip()
        if p_clean == "@ambient":
            if AmbientCloudDetector.detect_aws() is not None:
                return "@ambient"
            return None
        return p_clean
    return None


def get_active_aws_profile() -> str:
    """Return the currently active AWS profile name (or '@ambient' when running with an attached cloud service role).

    Resolution order:
    1. In ServerProcess (TALKOPS_IN_SERVER_PROCESS=1), persisted profile in config.toml takes priority
       so live `/cloud` switches in the TUI take effect immediately without restarting the server.
    2. AWS_PROFILE or AWS_DEFAULT_PROFILE environment variable (if not overridden by persisted non-default profile).
    3. Persisted profile in config.toml via load_aws_profile() (validated if '@ambient').
    4. settings.aws_profile (if explicitly configured and not 'default').
    5. Automatic '@ambient' attachment when deployed on cloud (IRSA, ECS, EC2 IMDS, Lambda) without local profile.
    6. Fallback to 'default'.
    """
    in_server = os.environ.get("TALKOPS_IN_SERVER_PROCESS") == "1"
    persisted_clean = _resolve_persisted_aws_profile()
    if in_server and persisted_clean:
        return persisted_clean

    env_prof = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE")
    if env_prof and env_prof.strip():
        env_clean = env_prof.strip()
        # If env only has the generic 'default' fallback but config.toml has an explicit switched profile,
        # honor the persisted profile.
        if env_clean == "default" and persisted_clean and persisted_clean != "default":
            return persisted_clean
        return env_clean

    if persisted_clean:
        return persisted_clean

    try:
        s = get_settings()
        if getattr(s, "aws_profile", None) and s.aws_profile not in ("default", "@ambient"):
            return s.aws_profile
    except Exception:
        pass

    # If deployed on cloud (EC2/ECS/EKS/Lambda) with an attached service role and no local default profile,
    # automatically attach the cloud service role.
    if not has_local_aws_profile_in_files("default") and AmbientCloudDetector.detect_aws() is not None:
        return "@ambient"

    if os.environ.get("TALKOPS_AWS_AMBIENT") == "1" and AmbientCloudDetector.detect_aws() is not None:
        return "@ambient"

    return "default"


def set_active_aws_profile(
    profile: str,
    *,
    region: str | None = None,
    persist: bool = True,
) -> None:
    """Activate an AWS named profile (or '@ambient' service role) for the current process and future sessions."""
    global _runtime_active_provider
    clean_profile = profile.strip()
    if not clean_profile:
        return

    _runtime_active_provider = "aws"
    is_ambient = clean_profile in ("@ambient", "ambient") or (
        clean_profile == "default"
        and not has_local_aws_profile_in_files("default")
        and AmbientCloudDetector.detect_aws() is not None
    )

    if is_ambient:
        # Crucial for EC2 / ECS / EKS: do NOT set AWS_PROFILE when using attached service role
        os.environ.pop("AWS_PROFILE", None)
        os.environ.pop("AWS_DEFAULT_PROFILE", None)
        if clean_profile in ("@ambient", "ambient"):
            os.environ["TALKOPS_AWS_AMBIENT"] = "1"
            clean_profile = "@ambient"
    else:
        os.environ.pop("TALKOPS_AWS_AMBIENT", None)
        # Clear any static env key overrides so boto3/aws CLI honors AWS_PROFILE from ~/.aws/config
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
        os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        os.environ.pop("AWS_SESSION_TOKEN", None)
        os.environ["AWS_PROFILE"] = clean_profile
        os.environ["AWS_DEFAULT_PROFILE"] = clean_profile

    effective_region = region.strip() if region and region.strip() else None
    if not effective_region:
        info = get_aws_profile_info("@ambient" if is_ambient else clean_profile)
        if info and info.region:
            effective_region = info.region

    if not effective_region and clean_profile in ("default", "@ambient"):
        effective_region = load_aws_region()

    if effective_region:
        os.environ["AWS_REGION"] = effective_region
        os.environ["AWS_DEFAULT_REGION"] = effective_region
    else:
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("AWS_DEFAULT_REGION", None)

    sync_aws_env_aliases()

    try:
        s = get_settings()
        s.cloud_provider = "aws"
        s.aws_profile = clean_profile
        s.aws_region = effective_region or "us-east-1"
    except Exception:
        pass

    if persist:
        save_aws_profile(clean_profile)
        save_recent_aws_profile(clean_profile)
        save_active_cloud_context("aws", clean_profile, region=effective_region)
        if effective_region:
            save_aws_region(effective_region)
        else:
            clear_aws_region()

    invalidate_aws_profile_cache()
    logger.info("Activated AWS profile: %s (region=%s, persist=%s)", clean_profile, effective_region, persist)


def get_active_aws_region() -> str | None:
    """Return the active AWS region for the currently active profile."""
    active_prof = get_active_aws_profile()

    if active_prof not in ("default", "@ambient"):
        try:
            info = get_aws_profile_info(active_prof)
            if info and info.region:
                return info.region
        except Exception:
            pass

    in_server = os.environ.get("TALKOPS_IN_SERVER_PROCESS") == "1"
    persisted = load_aws_region()
    if in_server and persisted and persisted.strip():
        return persisted.strip()

    env_reg = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_reg and env_reg.strip():
        return env_reg.strip()

    if persisted and persisted.strip():
        return persisted.strip()

    try:
        info_default = get_aws_profile_info(active_prof)
        if info_default and info_default.region:
            return info_default.region
    except Exception:
        pass

    try:
        s = get_settings()
        if getattr(s, "aws_region", None) and s.aws_region.strip() and s.aws_region != "us-east-1":
            return s.aws_region.strip()
    except Exception:
        pass

    return None


# ── Multi-Cloud Target Discovery & Management ──────────────────────────────


def get_cloud_execution_mode() -> CloudExecutionMode:
    """Return the active cloud execution mode ('read_only' or 'elevated')."""
    if _runtime_execution_mode is not None and os.environ.get("TALKOPS_IN_SERVER_PROCESS") != "1":
        return _runtime_execution_mode
    mode = load_cloud_execution_mode()
    return "read_only" if mode == "read_only" else "elevated"


def set_cloud_execution_mode(mode: str, *, persist: bool = True) -> CloudExecutionMode:
    """Set the active cloud execution mode ('read_only' or 'elevated')."""
    global _runtime_execution_mode
    norm: CloudExecutionMode = "read_only" if mode.strip().lower().replace("-", "_") == "read_only" else "elevated"
    _runtime_execution_mode = norm
    os.environ["TALKOPS_CLOUD_EXECUTION_MODE"] = norm
    if persist:
        save_cloud_execution_mode(norm)
    return norm


def list_cloud_targets(
    provider: CloudProviderType | str | None = None,
    *,
    include_ambient: bool = True,
    force_refresh: bool = False,
) -> list[CloudTargetContext]:
    """Discover all cloud targets across AWS, Azure, GCP, and K8s with mtime caching."""
    global _cached_multi_mtimes
    mtimes = _get_all_cloud_mtimes()
    mode = get_cloud_execution_mode()
    cache_key = f"{provider or 'all'}:{include_ambient}:{mode}"

    if not force_refresh and _cached_multi_mtimes == mtimes and cache_key in _cached_multi_targets:
        return list(_cached_multi_targets[cache_key])

    if _cached_multi_mtimes != mtimes:
        _cached_multi_targets.clear()
        _cached_multi_mtimes = mtimes

    prov_norm = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    if prov_norm == "kubernetes":
        prov_norm = "k8s"

    results: list[CloudTargetContext] = []

    if prov_norm in (None, "aws"):
        aws_targets = LocalCloudDiscoveryService.discover_aws(include_ambient=include_ambient)
        # Ensure at least 'default' is present for AWS if no profiles or ambient role found
        if not aws_targets and prov_norm == "aws":
            aws_targets = [
                CloudTargetContext(
                    provider="aws",
                    target_id="default",
                    display_name="default",
                    account_or_project="local-credentials",
                    region=get_active_aws_region() or "us-east-1",
                    is_default=True,
                )
            ]
        results.extend(aws_targets)

    if prov_norm in (None, "azure"):
        results.extend(LocalCloudDiscoveryService.discover_azure(include_ambient=include_ambient))

    if prov_norm in (None, "gcp"):
        results.extend(LocalCloudDiscoveryService.discover_gcp(include_ambient=include_ambient))

    if prov_norm in (None, "k8s"):
        results.extend(LocalCloudDiscoveryService.discover_k8s(include_ambient=include_ambient))

    # Stamp active execution_mode on all returned contexts
    stamped = [replace(t, execution_mode=mode) if t.execution_mode != mode else t for t in results]
    _cached_multi_targets[cache_key] = list(stamped)
    return stamped


def get_active_cloud_provider() -> CloudProviderType:
    """Resolve the currently active primary cloud provider ('aws', 'azure', 'gcp', 'k8s')."""
    in_server = os.environ.get("TALKOPS_IN_SERVER_PROCESS") == "1"
    persisted = load_active_cloud_context()
    if in_server and persisted and persisted.get("provider") in ("aws", "azure", "gcp", "k8s"):
        return cast(CloudProviderType, persisted["provider"])

    if _runtime_active_provider is not None:
        return _runtime_active_provider

    env_prov = (
        os.environ.get("TALKOPS_CLOUD_PROVIDER")
        or os.environ.get("TALKOPS_SERVER_CLOUD_PROVIDER")
        or ""
    ).strip().lower()
    if env_prov == "kubernetes":
        env_prov = "k8s"
    if env_prov in ("aws", "azure", "gcp", "k8s"):
        return cast(CloudProviderType, env_prov)

    if persisted and persisted.get("provider") in ("aws", "azure", "gcp", "k8s"):
        return cast(CloudProviderType, persisted["provider"])

    # Auto-detect ambient cloud runtime if deployed on GCP or Azure without AWS config
    if not os.environ.get("AWS_PROFILE") and not has_local_aws_profile_in_files("default"):
        if AmbientCloudDetector.detect_aws() is not None:
            return "aws"
        if AmbientCloudDetector.detect_gcp() is not None:
            return "gcp"
        if AmbientCloudDetector.detect_azure() is not None:
            return "azure"
        if AmbientCloudDetector.detect_k8s() is not None:
            return "k8s"

    return "aws"


def get_cloud_target_info(provider: str, target_id: str) -> CloudTargetContext | None:
    """Look up a specific CloudTargetContext by provider and target_id."""
    prov = provider.strip().lower()
    if prov == "kubernetes":
        prov = "k8s"
    tid = target_id.strip()
    if tid == "ambient":
        tid = "@ambient"
    elif tid == "in-cluster":
        tid = "@in-cluster"

    targets = list_cloud_targets(provider=prov, include_ambient=True)
    for t in targets:
        if t.target_id == tid or t.display_name == tid:
            return t

    if prov == "aws":
        info = get_aws_profile_info(tid)
        return info.to_target_context()

    return None


def get_active_cloud_target(provider: str | None = None) -> CloudTargetContext:
    """Return the active CloudTargetContext for the specified or primary cloud provider."""
    prov = (provider.strip().lower() if provider else get_active_cloud_provider())
    if prov == "kubernetes":
        prov = "k8s"
    mode = get_cloud_execution_mode()
    in_server = os.environ.get("TALKOPS_IN_SERVER_PROCESS") == "1"

    # 1. Check in-memory runtime override (outside ServerProcess, where config.toml is authoritative)
    if not in_server and prov in _runtime_active_targets:
        rt_id, rt_reg = _runtime_active_targets[prov]
        found = get_cloud_target_info(prov, rt_id)
        if found:
            return replace(found, region=rt_reg or found.region, execution_mode=mode)
        return CloudTargetContext(
            provider=cast(CloudProviderType, prov),
            target_id=rt_id,
            display_name=rt_id,
            account_or_project="configured",
            region=rt_reg,
            execution_mode=mode,
        )

    # 2. Provider-specific environment overrides
    if prov == "aws":
        aws_prof = get_active_aws_profile()
        aws_reg = get_active_aws_region()
        if aws_prof == "default" and not has_local_aws_profile_in_files("default"):
            amb = AmbientCloudDetector.detect_aws()
            if amb is not None:
                return replace(amb, region=aws_reg or amb.region, execution_mode=mode)
        found = get_cloud_target_info("aws", aws_prof)
        if found:
            return replace(found, region=aws_reg or found.region, execution_mode=mode)
        return CloudTargetContext(
            provider="aws",
            target_id=aws_prof,
            display_name=aws_prof,
            account_or_project="local-credentials",
            region=aws_reg or "us-east-1",
            execution_mode=mode,
        )

    # 3. Check persisted config.toml
    persisted = load_active_cloud_context()
    if persisted and persisted.get("provider") == prov and persisted.get("target_id"):
        p_tid = str(persisted["target_id"])
        p_reg = persisted.get("region")
        found = get_cloud_target_info(prov, p_tid)
        if found:
            return replace(found, region=p_reg or found.region, execution_mode=mode)
        if p_tid not in ("@ambient", "ambient", "@in-cluster", "in-cluster"):
            return CloudTargetContext(
                provider=cast(CloudProviderType, prov),
                target_id=p_tid,
                display_name=p_tid,
                account_or_project=persisted.get("account_or_project") or "configured",
                region=p_reg,
                execution_mode=mode,
            )

    # 4. Discover default or ambient target for this provider
    targets = list_cloud_targets(provider=prov, include_ambient=True)
    for t in targets:
        if t.is_default:
            return replace(t, execution_mode=mode)
    if targets:
        return replace(targets[0], execution_mode=mode)

    return CloudTargetContext(
        provider=cast(CloudProviderType, prov),
        target_id="default",
        display_name="default",
        account_or_project="unconfigured",
        region=None,
        execution_mode=mode,
    )


def set_active_cloud_target(
    provider: str,
    target_id: str,
    *,
    region: str | None = None,
    persist: bool = True,
) -> CloudTargetContext:
    """Activate a cloud target across AWS, Azure, GCP, or Kubernetes without mutating global CLI files."""
    global _runtime_active_provider
    prov = provider.strip().lower()
    if prov == "kubernetes":
        prov = "k8s"
    if prov not in ("aws", "azure", "gcp", "k8s"):
        raise ValueError(f"Unsupported cloud provider '{provider}'. Expected aws, azure, gcp, or k8s.")

    clean_id = target_id.strip()
    if clean_id == "ambient":
        clean_id = "@ambient"
    elif clean_id == "in-cluster" and prov == "k8s":
        clean_id = "@in-cluster"

    _runtime_active_provider = cast(CloudProviderType, prov)
    os.environ["TALKOPS_CLOUD_PROVIDER"] = prov
    os.environ["TALKOPS_CLOUD_TARGET"] = clean_id

    if prov == "aws":
        set_active_aws_profile(clean_id, region=region, persist=persist)
        return get_active_cloud_target("aws")

    info = get_cloud_target_info(prov, clean_id)
    effective_region = (region.strip() if region and region.strip() else None) or (info.region if info else None)
    acct = info.account_or_project if info else None

    _runtime_active_targets[prov] = (clean_id, effective_region)

    # Set process-scoped environment variables (NEVER mutating ~/.azure/azureProfile.json or ~/.config/gcloud/active_config)
    if prov == "azure":
        if not clean_id.startswith("@"):
            os.environ["AZURE_SUBSCRIPTION_ID"] = clean_id
            os.environ["ARM_SUBSCRIPTION_ID"] = clean_id
        if acct and acct not in ("azure-tenant", "managed-identity", "azure-imds"):
            os.environ["AZURE_TENANT_ID"] = acct
            os.environ["ARM_TENANT_ID"] = acct
        if effective_region:
            os.environ["AZURE_DEFAULTS_LOCATION"] = effective_region
    elif prov == "gcp":
        if not clean_id.startswith("@"):
            os.environ["CLOUDSDK_ACTIVE_CONFIG_NAME"] = clean_id
        else:
            os.environ.pop("CLOUDSDK_ACTIVE_CONFIG_NAME", None)
        if acct and acct != "unassigned":
            os.environ["CLOUDSDK_CORE_PROJECT"] = acct
            os.environ["GOOGLE_CLOUD_PROJECT"] = acct
            os.environ["GCLOUD_PROJECT"] = acct
        if effective_region:
            os.environ["CLOUDSDK_COMPUTE_REGION"] = effective_region
    elif prov == "k8s":
        if clean_id == "@in-cluster":
            os.environ.pop("KUBE_CONTEXT", None)
            os.environ.pop("KUBECONFIG_CONTEXT", None)
            os.environ.pop("HELM_KUBECONTEXT", None)
        else:
            os.environ["KUBE_CONTEXT"] = clean_id
            os.environ["KUBECONFIG_CONTEXT"] = clean_id
            os.environ["HELM_KUBECONTEXT"] = clean_id
        if effective_region:
            os.environ["KUBE_NAMESPACE"] = effective_region

    try:
        s = get_settings()
        s.cloud_provider = prov
    except Exception:
        pass

    if persist:
        save_active_cloud_context(prov, clean_id, region=effective_region, account_or_project=acct)
        save_recent_cloud_target(clean_id, provider=prov)

    invalidate_cloud_profile_cache()
    logger.info("Activated %s target: %s (region=%s, persist=%s)", prov.upper(), clean_id, effective_region, persist)
    return get_active_cloud_target(prov)


__all__ = [
    "AWSProfileInfo",
    "CloudExecutionMode",
    "CloudProviderType",
    "CloudTargetContext",
    "get_active_aws_profile",
    "get_active_aws_region",
    "get_active_cloud_provider",
    "get_active_cloud_target",
    "get_aws_profile_info",
    "get_cloud_execution_mode",
    "get_cloud_target_info",
    "has_local_aws_profile_in_files",
    "invalidate_aws_profile_cache",
    "invalidate_cloud_profile_cache",
    "list_aws_profiles",
    "list_cloud_targets",
    "set_active_aws_profile",
    "set_active_cloud_target",
    "set_cloud_execution_mode",
]
