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

"""Local shell and filesystem execution backend for AWS operations.

Extends ``LocalShellBackend`` with a curated shell environment that preserves
AWS CLI, AWS CDK, Terraform/OpenTofu, Terragrunt, Kubernetes, and DevOps tool
credentials and environment configurations.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend as SDKLocalShellBackend
from deepagents.backends.filesystem import _DEFAULT_GLOB_TIMEOUT
from deepagents.backends.protocol import (
    DEFAULT_GREP_TIMEOUT,
    FileInfo,
    GlobResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    compile_grep_include_glob,
)

from talkops.backend.registry import register_backend
from talkops.config.paths import AWS_PRESERVE_ENV_VARS
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Heavy cache/vendor/virtualenv directories pruned by default during workspace
# `glob` and `grep` walks so 50k+ `.venv`/`node_modules`/`.git` trees do not
# cause 5-second glob timeouts or pollute search results.
IGNORED_SEARCH_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".terraform",
        ".terragrunt-cache",
    }
)

# Tool directories to prepend to PATH for CLI tool discovery
COMMON_TOOL_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "~/.aws/bin",
)

# Prefix matching for AWS, Azure, GCP, Kubernetes, and Cloud IaC ecosystem tools
ALLOWED_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "AZURE_",
    "ARM_",
    "CLOUDSDK_",
    "GOOGLE_",
    "GCLOUD_",
    "TF_VAR_",
    "TF_",
    "TERRAGRUNT_",
    "KUBE_",
    "KUBERNETES_",
    "HELM_",
    "CDK_",
    "PULUMI_",
    "SAM_",
    "TALKOPS_CLOUD_",
)


def _build_shell_env() -> dict[str, str]:
    """Curate environment preserving safe shell vars, tool PATHs, and AWS/cloud credentials."""
    safe_keys = {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "LANG",
        "USER",
        "LOGNAME",
        "PWD",
        "EDITOR",
        "VISUAL",
        "LC_ALL",
        "LOCALE",
    }
    env: dict[str, str] = {}
    for key in safe_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    # Prepend standard binary locations to PATH
    path_val = env.get("PATH", "")
    for tool_dir in COMMON_TOOL_DIRS:
        expanded = os.path.expanduser(tool_dir)
        if expanded not in path_val.split(os.pathsep):
            path_val = f"{expanded}{os.pathsep}{path_val}" if path_val else expanded
    env["PATH"] = path_val

    # Ensure AWS_PAGER is disabled by default so aws CLI commands do not hang waiting for paging
    if "AWS_PAGER" in os.environ:
        env["AWS_PAGER"] = os.environ["AWS_PAGER"]
    else:
        env["AWS_PAGER"] = ""

    # Prevent interactive terminal prompts in subshells (e.g. Git credentials, CLI pagers)
    env["GIT_TERMINAL_PROMPT"] = os.environ.get("GIT_TERMINAL_PROMPT", "0")
    if "PAGER" not in env:
        env["PAGER"] = os.environ.get("PAGER", "cat")

    # Preserve all AWS & Cloud IaC environment variables
    for key, val in os.environ.items():
        if key in AWS_PRESERVE_ENV_VARS or key.startswith(ALLOWED_ENV_PREFIXES):
            env[key] = val

    return env


@register_backend("local")
class LocalShellBackend(SDKLocalShellBackend):
    """Local shell and filesystem backend specialized for AWS DevOps/SRE operations."""

    def __init__(
        self,
        root_dir: Path | str,
        virtual_mode: bool = False,
        inherit_env: bool = False,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize LocalShellBackend with sanitized AWS cloud environment.

        Args:
            root_dir: Root directory for file and command operations.
            virtual_mode: Whether to restrict operations to virtual filesystem.
            inherit_env: Whether to inherit parent process environment variables.
            env: Explicit environment variables dictionary.
            **kwargs: Additional keyword arguments for SDKLocalShellBackend.
        """
        effective_env = env if env is not None else _build_shell_env()
        super().__init__(
            root_dir=Path(root_dir),
            virtual_mode=virtual_mode,
            inherit_env=inherit_env,
            env=effective_env,
            **kwargs,
        )

    def _sync_cloud_env(self) -> None:
        """Dynamically sync active multi-cloud target and credentials into the shell execution environment.

        Reads latest persisted cloud target from config.toml, respects ambient service roles
        (@ambient / @in-cluster), and synchronizes AWS, Azure, GCP, and Kubernetes env vars.
        When an explicit named AWS profile is active, purges any static conflicting
        AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY from self._env so AWS CLI and boto3
        properly use the profile's authentic credentials rather than failing.
        """
        from talkops.config.cloud_profiles import (
            _resolve_persisted_aws_profile,
            get_active_aws_profile,
            get_active_aws_region,
            get_active_cloud_target,
            has_local_aws_profile_in_files,
        )
        from talkops.config.credential_broker import get_credential_broker

        persisted_profile = _resolve_persisted_aws_profile()
        active_profile = persisted_profile or get_active_aws_profile()
        is_ambient_aws = (
            active_profile == "@ambient"
            or (active_profile == "default" and not has_local_aws_profile_in_files("default"))
        )

        if active_profile and not is_ambient_aws:
            self._env["AWS_PROFILE"] = active_profile
            self._env["AWS_DEFAULT_PROFILE"] = active_profile
            os.environ["AWS_PROFILE"] = active_profile
            os.environ["AWS_DEFAULT_PROFILE"] = active_profile

            # Clear static credentials when a named profile is selected so AWS CLI
            # authentication precedence does not prioritize stale or unauthenticated env keys.
            if active_profile != "default":
                for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                    self._env.pop(key, None)
                    os.environ.pop(key, None)
        elif is_ambient_aws:
            if self._env.get("AWS_PROFILE") in ("default", "@ambient"):
                self._env.pop("AWS_PROFILE", None)
            if self._env.get("AWS_DEFAULT_PROFILE") in ("default", "@ambient"):
                self._env.pop("AWS_DEFAULT_PROFILE", None)
            if os.environ.get("AWS_PROFILE") in ("default", "@ambient"):
                os.environ.pop("AWS_PROFILE", None)
            if os.environ.get("AWS_DEFAULT_PROFILE") in ("default", "@ambient"):
                os.environ.pop("AWS_DEFAULT_PROFILE", None)

        active_region = get_active_aws_region()
        if active_region:
            self._env["AWS_REGION"] = active_region
            self._env["AWS_DEFAULT_REGION"] = active_region
            os.environ["AWS_REGION"] = active_region
            os.environ["AWS_DEFAULT_REGION"] = active_region
        else:
            self._env.pop("AWS_REGION", None)
            self._env.pop("AWS_DEFAULT_REGION", None)
            os.environ.pop("AWS_REGION", None)
            os.environ.pop("AWS_DEFAULT_REGION", None)

        # Synchronize multi-cloud environment variables (Azure, GCP, K8s, and execution mode)
        try:
            active_prov, active_target, active_reg = get_active_cloud_target()
            if active_prov in ("azure", "gcp", "k8s"):
                broker_vars = get_credential_broker().get_process_env(
                    provider=active_prov,
                    target_id=active_target,
                    region=active_reg,
                    base_env={},
                )
                for k, v in broker_vars.items():
                    if k.startswith(("AZURE_", "ARM_", "CLOUDSDK_", "GOOGLE_", "GCLOUD_", "KUBE_", "KUBECONFIG_", "HELM_", "TALKOPS_CLOUD_")):
                        self._env[k] = v
                        os.environ[k] = v
        except Exception:
            pass

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        """Execute a shell command with dynamically synchronized cloud profile environment."""
        self._sync_cloud_env()
        return super().execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        """Asynchronously execute shell command with dynamically synchronized cloud profile environment."""
        self._sync_cloud_env()
        return await super().aexecute(command, timeout=timeout)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files matching *pattern* while pruning heavy virtualenv/cache directories."""
        try:
            matches_pattern = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError as e:
            return GlobResult(error=str(e), matches=None)

        try:
            search_path = self.cwd if path is None or path == "/" else self._resolve_path(path)
            if not search_path.exists() or not search_path.is_dir():
                return GlobResult(matches=[])
        except (OSError, RuntimeError) as e:
            display_path = path if path is not None else "<default>"
            return GlobResult(error=f"Error globbing path '{display_path}': {e}", matches=[])

        # Only descend into an ignored dir if the pattern explicitly names it
        pattern_parts = set(pattern.replace("\\", "/").split("/"))
        active_ignored_dirs = IGNORED_SEARCH_DIRS - pattern_parts

        deadline = time.monotonic() + _DEFAULT_GLOB_TIMEOUT
        truncated = False
        results: list[FileInfo] = []
        root_str = str(search_path)

        try:
            for dirpath, dirnames, filenames in os.walk(root_str):
                if time.monotonic() > deadline:
                    logger.warning(
                        "Glob of '%s' timed out after %ss with %d match(es); returning partial results",
                        path if path is not None else "<default>",
                        _DEFAULT_GLOB_TIMEOUT,
                        len(results),
                    )
                    truncated = True
                    break

                # Prune ignored directories in-place so os.walk skips their subtrees
                dirnames[:] = [d for d in dirnames if d not in active_ignored_dirs]

                rel_dir = os.path.relpath(dirpath, root_str)
                rel_prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"

                for fname in filenames:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "Glob of '%s' timed out after %ss with %d match(es); returning partial results",
                            path if path is not None else "<default>",
                            _DEFAULT_GLOB_TIMEOUT,
                            len(results),
                        )
                        truncated = True
                        break

                    rel_path = f"{rel_prefix}{fname}"
                    if not matches_pattern(rel_path):
                        continue

                    matched_path = Path(dirpath) / fname
                    try:
                        if not matched_path.is_file():
                            continue
                    except (PermissionError, OSError, RuntimeError):
                        continue

                    if self.virtual_mode:
                        try:
                            matched_path.resolve().relative_to(self.cwd)
                            virt = self._to_virtual_path(matched_path)
                        except (OSError, RuntimeError, ValueError):
                            continue
                        target_path_str = virt
                    else:
                        target_path_str = str(matched_path)

                    try:
                        st = matched_path.stat()
                        results.append(
                            {
                                "path": target_path_str,
                                "is_dir": False,
                                "size": int(st.st_size),
                                "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                            }
                        )
                    except OSError:
                        results.append({"path": target_path_str, "is_dir": False})

                if truncated:
                    break
        except (OSError, RuntimeError, ValueError) as e:
            display_path = path if path is not None else "<default>"
            msg = f"Glob of '{display_path}' aborted partway: {e}"
            logger.warning("%s", msg, exc_info=True)
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(error=msg, matches=results)

        results.sort(key=lambda x: x.get("path", ""))
        return GlobResult(
            matches=results,
            truncated=truncated,
            truncation_reason="budget" if truncated else None,
        )

    def _python_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        *,
        max_count: int | None = None,
        timeout: int = DEFAULT_GREP_TIMEOUT,
    ) -> tuple[dict[str, list[tuple[int, str]]], bool, str | None]:
        """Fallback Python grep search that prunes `.venv`, `.git`, `node_modules`, and `__pycache__`."""
        if base_full.is_file():
            return super()._python_search(
                pattern,
                base_full,
                include_glob,
                max_count=max_count,
                timeout=timeout,
            )

        deadline = time.monotonic() + timeout
        glob_matcher = compile_grep_include_glob(include_glob) if include_glob else None
        total = 0
        results: dict[str, list[tuple[int, str]]] = {}
        file_errors: list[str] = []
        root_str = str(base_full)

        glob_parts = set((include_glob or "").replace("\\", "/").split("/"))
        active_ignored_dirs = IGNORED_SEARCH_DIRS - glob_parts

        def _file_errors_msg() -> str | None:
            if not file_errors:
                return None
            return "One or more files could not be fully searched:\n" + "\n".join(file_errors)

        try:
            for dirpath, dirnames, filenames in os.walk(root_str):
                if time.monotonic() > deadline:
                    return results, True, None

                dirnames[:] = [d for d in dirnames if d not in active_ignored_dirs]
                rel_dir = os.path.relpath(dirpath, root_str)
                rel_prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"

                for fname in filenames:
                    if time.monotonic() > deadline:
                        return results, True, None

                    rel_path = f"{rel_prefix}{fname}"
                    if glob_matcher is not None and not glob_matcher(rel_path):
                        continue

                    fp = Path(dirpath) / fname
                    try:
                        if not fp.is_file() or fp.stat().st_size > self.max_file_size_bytes:
                            continue
                    except (PermissionError, OSError, RuntimeError):
                        continue

                    if self.virtual_mode:
                        try:
                            virt_path = self._to_virtual_path(fp)
                        except (OSError, RuntimeError, ValueError):
                            continue
                    else:
                        virt_path = str(fp)

                    scanned_lines = 0
                    try:
                        with fp.open(encoding="utf-8", errors="strict") as handle:
                            for line_num, raw_line in enumerate(handle, 1):
                                scanned_lines = line_num
                                if line_num % 2048 == 0 and time.monotonic() > deadline:
                                    return results, True, None
                                if pattern not in raw_line:
                                    continue
                                if max_count is not None and total >= max_count:
                                    return results, True, _file_errors_msg()
                                results.setdefault(virt_path, []).append((line_num, raw_line.rstrip("\n")))
                                total += 1
                    except UnicodeDecodeError as e:
                        if scanned_lines > 0 or virt_path in results:
                            file_errors.append(f"- {virt_path}: {type(e).__name__}")
                        continue
                    except (OSError, RuntimeError) as e:
                        file_errors.append(f"- {virt_path}: {type(e).__name__}")
                        continue
        except (OSError, RuntimeError) as e:
            msg = f"Grep of '{self._display_path(base_full)}' aborted after {len(results)} matching file(s): {type(e).__name__}"
            return results, False, msg

        return results, False, _file_errors_msg()


__all__ = [
    "ALLOWED_ENV_PREFIXES",
    "COMMON_TOOL_DIRS",
    "IGNORED_SEARCH_DIRS",
    "LocalShellBackend",
    "_build_shell_env",
]
