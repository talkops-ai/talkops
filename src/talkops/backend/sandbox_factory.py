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

# pyright: reportMissingImports=false
"""Factory and provider implementations for remote sandbox backends in TalkOps.

Supports LangSmith, Daytona, Modal, Runloop, AWS Bedrock AgentCore, Vercel,
and third-party / config-declared sandbox providers via `SandboxRegistry`.
"""

from __future__ import annotations

import atexit
import contextlib
from contextlib import contextmanager
from datetime import timedelta
import importlib
import importlib.util
import os
from pathlib import Path
import shlex
import string
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from talkops.backend.local import LocalShellBackend
from talkops.backend.sandbox_config import SandboxConfig
from talkops.backend.sandbox_provider import (
    SandboxNotFoundError,
    SandboxProvider,
)
from talkops.config.paths import find_project_root
from talkops.utils.logger import AgentLogger

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from deepagents.backends.protocol import SandboxBackendProtocol
    from talkops.backend.sandbox_registry import SandboxRegistry

logger = AgentLogger("SandboxFactory")

# Thread-safe global cache for active sandbox backends, keyed by thread_id
_active_sandboxes: dict[str, Any] = {}
_active_sandboxes_lock = threading.Lock()

# Registry of active provider instances and sandbox IDs to delete at exit:
# cache_key -> (provider_name, sandbox_id, provider_instance_or_none)
_active_providers: dict[str, tuple[str, str, Any]] = {}

# Default working directories inside the sandbox containers
WORKING_DIRS: dict[str, str] = {
    "langsmith": "/root",
    "modal": "/workspace",
    "daytona": "/home/daytona",
    "runloop": "/home/user",
    "vercel": "/vercel/sandbox",
    "agentcore": "/workspace",
}

# Directories to exclude from remote sandbox file synchronization
WORKSPACE_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".gemini",
        ".agents",
        ".talkops",
        "tests",
        "scratch",
        ".terraform",
        ".terragrunt-cache",
        "cdk.out",
        "node_modules",
        "dist",
        "build",
    }
)

WORKSPACE_IGNORE_EXTS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".bak",
        ".tmp",
    }
)


def _resolve_env_var(name: str) -> str | None:
    """Resolve an environment variable with TALKOPS_ prefix fallback."""
    for candidate in (f"TALKOPS_{name}", name):
        val = os.environ.get(candidate)
        if val is not None and val.strip():
            return val.strip()
    return None


def _get_registry() -> SandboxRegistry:
    """Build a `SandboxRegistry` reflecting the current config and environment."""
    from talkops.backend.sandbox_registry import SandboxRegistry

    return SandboxRegistry()


def get_default_working_dir(provider: str) -> str:
    """Get the default working directory for a given sandbox provider.

    Args:
        provider: Sandbox provider name (`"langsmith"`, `"daytona"`, `"modal"`,
            `"runloop"`, `"agentcore"`, `"vercel"`, or a third-party/config provider).

    Returns:
        Default working directory path as string.

    Raises:
        ValueError: If provider is unknown.
    """
    return _get_registry().get_working_dir(provider)


def get_sandbox_working_dir(provider: str) -> str:
    """Return default working directory for a given provider (falls back to `/workspace`)."""
    if provider in WORKING_DIRS:
        return WORKING_DIRS[provider]
    try:
        return get_default_working_dir(provider)
    except Exception:
        return "/workspace"


def get_project_root() -> Path:
    """Return the resolved project root or current working directory."""
    root = find_project_root()
    if root is not None:
        return root
    try:
        from talkops.project_utils import get_server_project_context

        ctx = get_server_project_context()
        if ctx is not None:
            return ctx.user_cwd
    except Exception:
        pass
    return Path.cwd().resolve()


def _sync_workspace_to_sandbox(backend: Any, provider: str) -> None:
    """Upload project workspace files to the remote sandbox container."""
    root = get_project_root()
    working_dir = get_sandbox_working_dir(provider)
    logger.info(f"Syncing workspace {root} to sandbox {working_dir}...")

    files_to_upload: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in WORKSPACE_IGNORE_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in WORKSPACE_IGNORE_EXTS:
                continue

            local_path = Path(dirpath) / fname
            try:
                rel_path = local_path.relative_to(root)
                remote_path = f"{working_dir}/{rel_path}"

                file_size = local_path.stat().st_size
                if file_size > 10 * 1024 * 1024:
                    logger.debug(
                        f"Skipping large file during sync: {rel_path} ({file_size} bytes)"
                    )
                    continue

                content = local_path.read_bytes()
                files_to_upload.append((remote_path, content))
            except Exception as e:
                logger.warning(f"Failed to prepare file {local_path} for upload: {e}")

    batch_size = 20
    logger.info(f"Uploading {len(files_to_upload)} files to sandbox...")
    for i in range(0, len(files_to_upload), batch_size):
        batch = files_to_upload[i : i + batch_size]
        try:
            responses = backend.upload_files(batch)
            for resp in responses:
                if getattr(resp, "error", None):
                    logger.warning(f"Failed to upload {resp.path}: {resp.error}")
        except Exception as e:
            logger.error(f"Error uploading batch {i // batch_size + 1}: {e}")

    logger.info("Workspace synchronization completed.")


def _run_sandbox_setup(backend: Any, setup_script_path: str) -> None:
    """Run user's setup script inside the sandbox after expanding environment variables."""
    script_path = Path(setup_script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"Setup script not found at {setup_script_path}")

    logger.info(f"Executing setup script: {setup_script_path}...")
    script_content = script_path.read_text(encoding="utf-8")

    template = string.Template(script_content)
    expanded_script = template.safe_substitute(os.environ)

    result = backend.execute(f"bash -c {shlex.quote(expanded_script)}")
    if result.exit_code != 0:
        logger.error(
            f"Setup script failed with exit code {result.exit_code}:\n{result.output}"
        )
        raise RuntimeError(
            f"Sandbox setup script execution failed: exit {result.exit_code}"
        )

    logger.info("Sandbox setup complete.")


def _import_provider_module(
    module_name: str,
    *,
    provider: str,
    package: str,
) -> Any:
    """Import an optional provider module with a clear install command."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"The '{provider}' sandbox provider requires the '{package}' package. "
            f"Install it with: uv pip install {package}"
        )
        raise ImportError(msg) from exc


# ── Built-in Provider Implementations ───────────────────────────────────────

_DEFAULT_LANGSMITH_SNAPSHOT_NAME = "talkops-aws-agent"
_DEFAULT_LANGSMITH_IMAGE = "python:3.12-slim"
_DEFAULT_LANGSMITH_FS_CAPACITY_BYTES = 16 * 1024**3


class _LangSmithProvider(SandboxProvider):
    """LangSmith sandbox provider implementation."""

    def __init__(self, api_key: str | None = None, endpoint: str | None = None) -> None:
        from langsmith.sandbox import SandboxClient

        sandbox_key = _resolve_env_var("LANGSMITH_SANDBOX_API_KEY")
        resolved_key = (
            api_key
            or sandbox_key
            or _resolve_env_var("LANGSMITH_API_KEY")
            or _resolve_env_var("LANGCHAIN_API_KEY")
        )
        if not resolved_key:
            msg = (
                "No LangSmith API key found. Set LANGSMITH_API_KEY "
                "or LANGSMITH_SANDBOX_API_KEY."
            )
            raise ValueError(msg)

        resolved_endpoint = endpoint or _resolve_env_var("LANGSMITH_SANDBOX_ENDPOINT")
        client_kwargs: dict[str, Any] = {"api_key": resolved_key}
        if resolved_endpoint:
            client_kwargs["api_endpoint"] = resolved_endpoint
        self._client = SandboxClient(**client_kwargs)

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        snapshot_id: str | None = None,
        snapshot_name: str | None = None,
        image: str | None = None,
        fs_capacity_bytes: int | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get existing or create new LangSmith sandbox."""
        from deepagents.backends.langsmith import LangSmithSandbox

        if kwargs:
            msg = f"Received unsupported arguments: {list(kwargs.keys())}"
            raise TypeError(msg)

        if sandbox_id:
            try:
                sandbox = self._client.get_sandbox(name=sandbox_id)
            except Exception as e:
                msg = f"Failed to connect to existing sandbox '{sandbox_id}': {e}"
                raise RuntimeError(msg) from e
            return LangSmithSandbox(sandbox)

        resolved_snapshot_id = snapshot_id or _resolve_env_var("LANGSMITH_SANDBOX_SNAPSHOT_ID")
        resolved_snapshot_name = (
            snapshot_name
            or _resolve_env_var("LANGSMITH_SANDBOX_SNAPSHOT_NAME")
            or _resolve_env_var("SANDBOX_SNAPSHOT")
            or _DEFAULT_LANGSMITH_SNAPSHOT_NAME
        )
        resolved_image = (
            image
            or _resolve_env_var("SANDBOX_IMAGE")
            or _DEFAULT_LANGSMITH_IMAGE
        )
        resolved_capacity = fs_capacity_bytes or _DEFAULT_LANGSMITH_FS_CAPACITY_BYTES

        if not resolved_snapshot_id:
            resolved_snapshot_id = self._ensure_snapshot(
                snapshot_name=resolved_snapshot_name,
                image=resolved_image,
                fs_capacity_bytes=resolved_capacity,
            )

        try:
            sandbox = self._client.create_sandbox(
                snapshot_id=resolved_snapshot_id, timeout=timeout
            )
        except Exception as e:
            msg = f"Failed to create sandbox from snapshot '{resolved_snapshot_name}': {e}"
            raise RuntimeError(msg) from e

        # Poll for readiness if sandbox exposes `.run()`
        if hasattr(sandbox, "run"):
            for _ in range(max(1, timeout // 2)):
                try:
                    result = sandbox.run("echo ready", timeout=5)
                    if getattr(result, "exit_code", 0) == 0:
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                with contextlib.suppress(Exception):
                    self._client.delete_sandbox(sandbox.name)
                msg = f"LangSmith sandbox failed to start within {timeout} seconds"
                raise RuntimeError(msg)

        return LangSmithSandbox(sandbox)

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Delete a LangSmith sandbox."""
        del kwargs
        self._client.delete_sandbox(sandbox_id)

    def _ensure_snapshot(
        self,
        *,
        snapshot_name: str,
        image: str,
        fs_capacity_bytes: int,
    ) -> str:
        """Resolve a snapshot by name, building it from `image` if missing."""
        try:
            snapshots = self._client.list_snapshots()
        except Exception as e:
            msg = f"Failed to list snapshots: {e}"
            raise RuntimeError(msg) from e

        non_ready_status: str | None = None
        for snap in snapshots:
            if snap.name != snapshot_name:
                continue
            if snap.status == "ready":
                return snap.id
            non_ready_status = snap.status

        if non_ready_status is not None:
            msg = (
                f"Snapshot '{snapshot_name}' exists but is in state "
                f"'{non_ready_status}'. Wait for it to finish building, or "
                f"delete it to rebuild."
            )
            raise RuntimeError(msg)

        try:
            snapshot = self._client.create_snapshot(
                name=snapshot_name,
                docker_image=image,
                fs_capacity_bytes=fs_capacity_bytes,
            )
        except Exception as create_err:
            msg = f"Failed to build snapshot '{snapshot_name}': {create_err}"
            raise RuntimeError(msg) from create_err
        return snapshot.id


class _DaytonaProvider(SandboxProvider):
    """Daytona sandbox provider — lifecycle management for Daytona sandboxes."""

    def __init__(self) -> None:
        daytona_module = _import_provider_module(
            "daytona",
            provider="daytona",
            package="langchain-daytona",
        )
        api_key = _resolve_env_var("DAYTONA_API_KEY")
        if not api_key:
            msg = "No Daytona API key found. Set DAYTONA_API_KEY."
            raise ValueError(msg)
        self._daytona_module = daytona_module
        self._client = daytona_module.Daytona(
            daytona_module.DaytonaConfig(
                api_key=api_key,
                api_url=_resolve_env_var("DAYTONA_API_URL"),
            )
        )

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Daytona sandbox."""
        del kwargs
        daytona_backend = _import_provider_module(
            "langchain_daytona",
            provider="daytona",
            package="langchain-daytona",
        )

        if sandbox_id:
            msg = (
                "Connecting to existing Daytona sandbox by ID not yet supported. "
                "Create a new sandbox by omitting sandbox_id parameter."
            )
            raise NotImplementedError(msg)

        sandbox = self._client.create()
        if hasattr(sandbox, "process") and hasattr(sandbox.process, "exec"):
            last_exc: Exception | None = None
            for _ in range(max(1, timeout // 2)):
                try:
                    result = sandbox.process.exec("echo ready", timeout=5)
                    if getattr(result, "exit_code", 0) == 0:
                        break
                except Exception as exc:
                    last_exc = exc
                time.sleep(2)
            else:
                with contextlib.suppress(Exception):
                    sandbox.delete()
                detail = f" Last error: {last_exc}" if last_exc else ""
                msg = f"Daytona sandbox failed to start within {timeout} seconds.{detail}"
                raise RuntimeError(msg)

        return daytona_backend.DaytonaSandbox(sandbox=sandbox)

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Delete a Daytona sandbox by id."""
        del kwargs
        sandbox = self._client.get(sandbox_id)
        self._client.delete(sandbox)


class _ModalProvider(SandboxProvider):
    """Modal sandbox provider — lifecycle management for Modal sandboxes."""

    def __init__(self) -> None:
        self._modal = _import_provider_module(
            "modal",
            provider="modal",
            package="langchain-modal",
        )
        token_id = _resolve_env_var("MODAL_TOKEN_ID")
        token_secret = _resolve_env_var("MODAL_TOKEN_SECRET")
        if token_id and token_secret:
            try:
                self._client = self._modal.Client.from_credentials(
                    token_id, token_secret
                )
            except Exception as exc:
                msg = (
                    "Failed to authenticate with Modal using "
                    "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET. "
                    "Verify your credentials are valid."
                )
                raise ValueError(msg) from exc
        elif token_id or token_secret:
            missing = "MODAL_TOKEN_SECRET" if token_id else "MODAL_TOKEN_ID"
            msg = (
                f"Modal configuration is incomplete: {missing} is not set. "
                "Set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET together, or unset "
                "both to fall back to default Modal authentication."
            )
            raise ValueError(msg)
        else:
            self._client = None

        lookup_kwargs: dict[str, Any] = {
            "name": "talkops-sandbox",
            "create_if_missing": True,
        }
        if self._client is not None:
            lookup_kwargs["client"] = self._client
        self._app = self._modal.App.lookup(**lookup_kwargs)

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Modal sandbox."""
        del kwargs
        modal_backend = _import_provider_module(
            "langchain_modal",
            provider="modal",
            package="langchain-modal",
        )

        client_kwargs: dict[str, Any] = {}
        if self._client is not None:
            client_kwargs["client"] = self._client

        if sandbox_id:
            sandbox = self._modal.Sandbox.from_id(
                sandbox_id=sandbox_id,
                app=self._app,
                **client_kwargs,
            )
        else:
            sandbox = self._modal.Sandbox.create(
                app=self._app, workdir="/workspace", **client_kwargs
            )
            if hasattr(sandbox, "poll") and hasattr(sandbox, "exec"):
                last_exc: Exception | None = None
                for _ in range(max(1, timeout // 2)):
                    if sandbox.poll() is not None:
                        msg = "Modal sandbox terminated unexpectedly during startup"
                        raise RuntimeError(msg)
                    try:
                        process = sandbox.exec("echo", "ready", timeout=5)
                        process.wait()
                        if process.returncode == 0:
                            break
                    except Exception as exc:
                        last_exc = exc
                    time.sleep(2)
                else:
                    sandbox.terminate()
                    detail = f" Last error: {last_exc}" if last_exc else ""
                    msg = f"Modal sandbox failed to start within {timeout} seconds.{detail}"
                    raise RuntimeError(msg)

        return modal_backend.ModalSandbox(sandbox=sandbox)

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Terminate a Modal sandbox by id."""
        del kwargs
        del_kwargs: dict[str, Any] = {"sandbox_id": sandbox_id, "app": self._app}
        if self._client is not None:
            del_kwargs["client"] = self._client
        sandbox = self._modal.Sandbox.from_id(**del_kwargs)
        sandbox.terminate()


class _RunloopProvider(SandboxProvider):
    """Runloop sandbox provider — delegates to `langchain_runloop.RunloopProvider`."""

    def __init__(self) -> None:
        runloop_module = _import_provider_module(
            "langchain_runloop",
            provider="runloop",
            package="langchain-runloop",
        )
        api_key = _resolve_env_var("RUNLOOP_API_KEY")
        if not api_key:
            msg = "No Runloop API key found. Set RUNLOOP_API_KEY."
            raise ValueError(msg)
        self._provider = runloop_module.RunloopProvider(api_key=api_key)

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Runloop devbox."""
        try:
            return self._provider.get_or_create(
                sandbox_id=sandbox_id,
                timeout=timeout,
                **kwargs,
            )
        except KeyError as e:
            if sandbox_id is None:
                raise
            raise SandboxNotFoundError(sandbox_id) from e

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Shut down a Runloop devbox by id."""
        del kwargs
        self._provider.delete(sandbox_id=sandbox_id)


class _AgentCoreProvider(SandboxProvider):
    """AWS Bedrock AgentCore Code Interpreter sandbox provider."""

    def __init__(self, region: str | None = None) -> None:
        from talkops.config.aws import get_boto3_session

        self._session: Any = None
        try:
            self._session = get_boto3_session()
            resolved_region = region or getattr(self._session, "region_name", None)
        except Exception:
            resolved_region = region

        self._region = (
            resolved_region
            or _resolve_env_var("AWS_REGION")
            or _resolve_env_var("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        self._active_interpreters: dict[str, Any] = {}

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Create a new AgentCore Code Interpreter session."""
        del kwargs
        if sandbox_id:
            msg = (
                "AgentCore does not support reconnecting to existing sessions. "
                "Remove the --sandbox-id option."
            )
            raise NotImplementedError(msg)

        agentcore_module = _import_provider_module(
            "bedrock_agentcore.tools.code_interpreter_client",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )
        agentcore_backend = _import_provider_module(
            "langchain_agentcore_codeinterpreter",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )

        interpreter_kwargs: dict[str, Any] = {
            "region": self._region,
            "integration_source": "talkops",
        }
        if self._session is not None:
            interpreter_kwargs["session"] = self._session
        interpreter = agentcore_module.CodeInterpreter(**interpreter_kwargs)
        try:
            interpreter.start()
        except Exception:
            with contextlib.suppress(Exception):
                interpreter.stop()
            raise

        backend = agentcore_backend.AgentCoreSandbox(interpreter=interpreter)
        backend_id = getattr(backend, "id", None)
        if backend_id:
            self._active_interpreters[str(backend_id)] = interpreter
        return backend

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Stop an AgentCore session."""
        del kwargs
        interpreter = self._active_interpreters.pop(sandbox_id, None)
        if interpreter:
            try:
                interpreter.stop()
                logger.info(f"AgentCore session {sandbox_id} stopped")
            except Exception as e:
                logger.warning(
                    f"Failed to stop AgentCore session {sandbox_id}: {e}"
                )


_VercelStatus = Literal[
    "pending",
    "running",
    "stopping",
    "stopped",
    "failed",
    "aborted",
    "snapshotting",
]

_VERCEL_TERMINAL_STATUSES: frozenset[_VercelStatus] = frozenset(
    {"aborted", "failed", "stopped"}
)

_VERCEL_DEFAULT_RUNTIME = "python3.13"
_VERCEL_SANDBOX_TIMEOUT = timedelta(minutes=30)


class _VercelSandboxHandle(Protocol):
    """Minimal Vercel SDK sandbox surface used by the built-in provider."""

    sandbox_id: str
    status: str

    def wait_for_status(self, status: str, *, timeout: int) -> None:
        """Wait for the sandbox to reach the requested status."""

    def stop(self) -> None:
        """Stop the sandbox."""


class _VercelProvider(SandboxProvider):
    """Vercel Sandbox provider implementation."""

    _CREDENTIAL_ENV_NAMES: ClassVar[dict[str, str]] = {
        "token": "VERCEL_TOKEN",
        "project_id": "VERCEL_PROJECT_ID",
        "team_id": "VERCEL_TEAM_ID",
    }

    def __init__(self) -> None:
        self._sdk_kwargs = self._resolve_sdk_kwargs()

    @classmethod
    def _resolve_sdk_kwargs(cls) -> dict[str, str]:
        """Resolve explicit Vercel credentials configured for this workspace."""
        values = {
            key: _resolve_env_var(name)
            for key, name in cls._CREDENTIAL_ENV_NAMES.items()
        }
        if all(
            value == (os.environ.get(cls._CREDENTIAL_ENV_NAMES[key]) or None)
            for key, value in values.items()
        ):
            return {}
        missing = sorted(
            name for key, name in cls._CREDENTIAL_ENV_NAMES.items() if not values[key]
        )
        if missing:
            names = list(cls._CREDENTIAL_ENV_NAMES.values())
            required = f"{', '.join(names[:-1])}, and {names[-1]}"
            msg = (
                "The Vercel configuration is incomplete: "
                f"{', '.join(missing)} not set. Set {required} together, or "
                "unset all three to fall back to default Vercel authentication."
            )
            raise ValueError(msg)
        return {key: value for key, value in values.items() if value}

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Vercel sandbox."""
        if kwargs:
            msg = f"Received unsupported arguments: {list(kwargs.keys())}"
            raise TypeError(msg)

        vercel_backend = _import_provider_module(
            "langchain_vercel_sandbox",
            provider="vercel",
            package="langchain-vercel-sandbox",
        )
        vercel_sandbox = _import_provider_module(
            "vercel.sandbox",
            provider="vercel",
            package="vercel",
        )

        sandbox: _VercelSandboxHandle
        try:
            if sandbox_id:
                sandbox = vercel_sandbox.Sandbox.get(
                    sandbox_id=sandbox_id,
                    **self._sdk_kwargs,
                )
            else:
                sandbox = vercel_sandbox.Sandbox.create(
                    runtime=_VERCEL_DEFAULT_RUNTIME,
                    timeout=_VERCEL_SANDBOX_TIMEOUT,
                    **self._sdk_kwargs,
                )
        except Exception as exc:
            action = "connect to existing" if sandbox_id else "create"
            msg = f"Failed to {action} Vercel sandbox."
            raise RuntimeError(msg) from exc

        try:
            self._wait_until_running(sandbox, timeout=timeout)
        except Exception:
            if sandbox_id is None:
                with contextlib.suppress(Exception):
                    sandbox.stop()
            raise

        return vercel_backend.VercelSandbox(sandbox=sandbox)

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:
        """Stop a Vercel sandbox by id."""
        del kwargs
        vercel_sandbox = _import_provider_module(
            "vercel.sandbox",
            provider="vercel",
            package="vercel",
        )
        try:
            sandbox = vercel_sandbox.Sandbox.get(
                sandbox_id=sandbox_id,
                **self._sdk_kwargs,
            )
            sandbox.stop()
        except Exception as exc:
            msg = "Failed to stop Vercel sandbox."
            raise RuntimeError(msg) from exc

    @staticmethod
    def _wait_until_running(
        sandbox: _VercelSandboxHandle,
        *,
        timeout: int,
    ) -> None:
        """Wait for a Vercel sandbox to become ready."""
        status = str(getattr(sandbox, "status", "running"))
        if status == "running":
            return
        if status in _VERCEL_TERMINAL_STATUSES:
            msg = f"Vercel sandbox {sandbox.sandbox_id} is in terminal state {status!r}"
            raise RuntimeError(msg)

        if hasattr(sandbox, "wait_for_status"):
            try:
                sandbox.wait_for_status("running", timeout=timeout)
            except TimeoutError as exc:
                status = str(getattr(sandbox, "status", "unknown"))
                msg = (
                    f"Vercel sandbox {sandbox.sandbox_id} failed to start within "
                    f"{timeout} seconds; current status is {status!r}"
                )
                raise RuntimeError(msg) from exc
            except Exception as exc:
                msg = "Failed while waiting for Vercel sandbox startup."
                raise RuntimeError(msg) from exc


def _get_provider(
    provider_name: str,
    registry: SandboxRegistry | None = None,
) -> SandboxProvider:
    """Get a `SandboxProvider` instance for the specified provider."""
    reg = registry if registry is not None else _get_registry()
    return reg.create_provider(provider_name)


def verify_sandbox_deps(provider: str) -> None:
    """Check that the required packages for a sandbox provider are installed.

    Uses `importlib.util.find_spec` for a lightweight check with no actual
    imports.
    """
    if not provider or provider in ("none", "local"):
        return

    metadata = _get_registry().get_metadata(provider)
    if metadata is None or metadata.backend_module is None:
        return

    try:
        found = importlib.util.find_spec(metadata.backend_module) is not None
    except (ImportError, ValueError):
        found = False

    if not found:
        if metadata.install is not None:
            install_hint = f"Install with: {metadata.install.command()}"
        else:
            install_hint = "Install the provider's package."
        msg = f"Missing dependencies for '{provider}' sandbox. {install_hint}"
        raise ImportError(msg)


@contextmanager
def create_sandbox(
    provider: str,
    *,
    sandbox_id: str | None = None,
    snapshot_name: str | None = None,
    setup_script_path: str | None = None,
    sync_workspace: bool = False,
    **kwargs: Any,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to a sandbox of the specified provider as a context manager.

    Args:
        provider: Sandbox provider (`"langsmith"`, `"daytona"`, `"modal"`,
            `"runloop"`, `"agentcore"`, `"vercel"`, or a third-party/config provider).
        sandbox_id: Optional existing sandbox ID to reuse.
        snapshot_name: Optional sandbox snapshot name to use or create.
        setup_script_path: Optional path to setup script to run after sandbox starts.
        sync_workspace: Whether to upload local workspace files into the sandbox.
        **kwargs: Extra arguments forwarded to the provider's `get_or_create()`.

    Yields:
        The initialized `SandboxBackendProtocol` instance.
    """
    if snapshot_name is not None and sandbox_id is not None:
        msg = (
            "snapshot_name cannot be combined with sandbox_id; "
            "snapshots only apply when creating a fresh sandbox"
        )
        raise ValueError(msg)

    registry = _get_registry()
    metadata = registry.get_metadata(provider)
    if snapshot_name is not None and (
        metadata is None or not metadata.supports_snapshot_name
    ):
        msg = f"snapshot_name is not supported by provider '{provider}'"
        raise ValueError(msg)

    provider_obj = _get_provider(provider, registry)
    should_cleanup = sandbox_id is None

    provider_kwargs: dict[str, Any] = {
        **registry.get_config_params(provider),
        **kwargs,
    }
    if snapshot_name is not None:
        key = "snapshot_name" if provider == "langsmith" else "snapshot"
        provider_kwargs[key] = snapshot_name

    logger.info(f"Starting {provider} sandbox...")
    backend = provider_obj.get_or_create(sandbox_id=sandbox_id, **provider_kwargs)

    try:
        if sync_workspace:
            _sync_workspace_to_sandbox(backend, provider)
        if setup_script_path:
            _run_sandbox_setup(backend, setup_script_path)
        yield backend
    finally:
        if should_cleanup:
            backend_id = getattr(backend, "id", None)
            if backend_id:
                try:
                    logger.info(f"Terminating {provider} sandbox {backend_id}...")
                    provider_obj.delete(sandbox_id=str(backend_id))
                except Exception as e:
                    logger.warning(
                        f"Cleanup failed for {provider} sandbox {backend_id}: {e}"
                    )


# ── Legacy & Thread-Cached Sandbox Helpers ──────────────────────────────────

def _create_langsmith_sandbox(config: SandboxConfig) -> Any:
    """Create a LangSmith sandbox backend (delegates to `_LangSmithProvider`)."""
    prov = _LangSmithProvider()
    return prov.get_or_create(
        sandbox_id=config.sandbox_id,
        snapshot_name=config.snapshot_name,
        image=config.image,
    )


def _create_other_sandbox(config: SandboxConfig) -> Any:
    """Create a sandbox backend via the provider registry."""
    from talkops.backend.sandbox_registry import SandboxRegistry

    provider = config.provider
    registry = SandboxRegistry(config)
    if not registry.is_available(provider):
        raise NotImplementedError(f"Unsupported sandbox provider: {provider}")

    prov = registry.create_provider(provider)
    kwargs: dict[str, Any] = dict(registry.get_config_params(provider))
    if config.snapshot_name:
        if provider == "runloop":
            kwargs["snapshot"] = config.snapshot_name
        elif provider == "langsmith":
            kwargs["snapshot_name"] = config.snapshot_name
        else:
            kwargs["snapshot"] = config.snapshot_name
    return prov.get_or_create(sandbox_id=config.sandbox_id, **kwargs)


def get_sandbox_backend(thread_id: str | None = None) -> Any:
    """Resolve, create, or fetch the cached sandbox backend for the given session thread."""
    from talkops.backend.sandbox_registry import SandboxRegistry

    config = SandboxConfig.from_env()

    if config.provider == "local":
        root = get_project_root()
        return LocalShellBackend(
            root_dir=root,
            virtual_mode=False,
            inherit_env=True,
        )

    cache_key = thread_id or "__global__"

    with _active_sandboxes_lock:
        if cache_key in _active_sandboxes:
            return _active_sandboxes[cache_key]

        logger.info(
            f"Initializing new sandbox backend for key {cache_key} (provider: {config.provider})..."
        )

        registry = SandboxRegistry(config)
        prov_instance: SandboxProvider | None = None
        if config.provider == "langsmith":
            prov_instance = _LangSmithProvider()
            backend = prov_instance.get_or_create(
                sandbox_id=config.sandbox_id,
                snapshot_name=config.snapshot_name,
                image=config.image,
            )
        elif registry.is_available(config.provider):
            prov_instance = registry.create_provider(config.provider)
            kwargs: dict[str, Any] = dict(registry.get_config_params(config.provider))
            if config.snapshot_name:
                key = "snapshot_name" if config.provider == "langsmith" else "snapshot"
                kwargs[key] = config.snapshot_name
            backend = prov_instance.get_or_create(
                sandbox_id=config.sandbox_id,
                **kwargs,
            )
        else:
            raise NotImplementedError(f"Unsupported sandbox provider: {config.provider}")

        if config.sync_workspace:
            try:
                _sync_workspace_to_sandbox(backend, config.provider)
            except Exception as e:
                logger.error(f"Workspace sync to sandbox failed: {e}")

        if config.setup_script_path:
            try:
                _run_sandbox_setup(backend, config.setup_script_path)
            except Exception as e:
                logger.error(f"Sandbox setup script failed: {e}")

        _active_sandboxes[cache_key] = backend
        # Only register for automatic cleanup if we created a fresh sandbox (not reattached via sandbox_id)
        if config.sandbox_id is None:
            _active_providers[cache_key] = (
                config.provider,
                str(getattr(backend, "id", cache_key)),
                prov_instance,
            )

        logger.info(f"Sandbox backend successfully cached. Key: {cache_key}")
        return backend


def cleanup_sandbox(thread_id: str) -> None:
    """Terminate the sandbox backend associated with a thread ID."""
    with _active_sandboxes_lock:
        backend = _active_sandboxes.pop(thread_id, None)
        provider_info = _active_providers.pop(thread_id, None)

        if backend and provider_info:
            provider, sandbox_id, prov_instance = (
                provider_info
                if len(provider_info) == 3
                else (provider_info[0], provider_info[1], None)
            )
            logger.info(
                f"Terminating sandbox {sandbox_id} ({provider}) for thread {thread_id}..."
            )
            try:
                if prov_instance is not None:
                    prov_instance.delete(sandbox_id=sandbox_id)
                else:
                    prov = _get_provider(provider)
                    prov.delete(sandbox_id=sandbox_id)
                logger.info(f"Sandbox {sandbox_id} terminated.")
            except Exception as e:
                logger.warning(f"Error during sandbox termination for {sandbox_id}: {e}")


def cleanup_all_sandboxes() -> None:
    """Cleanup exit handler to shut down all active sandboxes on process shutdown."""
    keys = list(_active_sandboxes.keys())
    if keys:
        logger.info("Process exit detected. Cleaning up all active sandboxes...")
        for key in keys:
            cleanup_sandbox(key)


atexit.register(cleanup_all_sandboxes)

create_sandbox_backend = get_sandbox_backend
clear_sandbox_cache = cleanup_all_sandboxes

__all__ = [
    "WORKING_DIRS",
    "WORKSPACE_IGNORE_DIRS",
    "WORKSPACE_IGNORE_EXTS",
    "clear_sandbox_cache",
    "cleanup_all_sandboxes",
    "cleanup_sandbox",
    "create_sandbox",
    "create_sandbox_backend",
    "get_default_working_dir",
    "get_project_root",
    "get_sandbox_backend",
    "get_sandbox_working_dir",
    "verify_sandbox_deps",
]
