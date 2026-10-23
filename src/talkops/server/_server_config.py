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

"""Typed configuration for app-to-server subprocess communication."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from talkops._constants import DEFAULT_ASSISTANT_ID, SERVER_ENV_PREFIX


def _read_env_bool(suffix: str, *, default: bool = False) -> bool:
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    return raw.lower() == "true"


def _read_env_str(suffix: str) -> str | None:
    return os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")


@dataclass
class ServerConfig:
    """Configuration payload passed to the LangGraph server subprocess."""

    model: str | None = None
    assistant_id: str = DEFAULT_ASSISTANT_ID
    system_prompt: str | None = None
    auto_approve: bool = False
    interactive: bool = True
    enable_shell: bool = True
    cwd: str | None = None
    project_root: str | None = None
    approval_mode: str = "manual"
    shell_allow_list: tuple[str, ...] | None = None
    read_only: bool = False
    cloud_provider: str | None = None
    cloud_target: str | None = None
    cloud_execution_mode: str = "read_only"
    aws_profile: str | None = None
    aws_region: str | None = None
    mcp_config_path: str | None = None
    no_mcp: bool = False
    trust_project_mcp: bool = False
    server_log_level: str = "WARNING"
    postgres_uri: str | None = None
    auth_mode: str = "token"
    webhook_port: int | None = None
    webhook_host: str | None = None
    workspaces_dir: str | None = None
    server_url: str | None = None
    api_key: str | None = None
    user_id: str | None = None
    no_subagents: bool = False
    tnr: bool = False
    profile: str | None = None
    addons: tuple[str, ...] | None = None
    disable_addons: tuple[str, ...] | None = None
    custom_addons: tuple[str, ...] | None = None
    addons_config_path: str | None = None

    def to_env(self) -> dict[str, str]:
        """Convert instance to env."""
        env: dict[str, str] = {
            f"{SERVER_ENV_PREFIX}ASSISTANT_ID": self.assistant_id,
            f"{SERVER_ENV_PREFIX}AUTO_APPROVE": str(self.auto_approve).lower(),
            f"{SERVER_ENV_PREFIX}INTERACTIVE": str(self.interactive).lower(),
            f"{SERVER_ENV_PREFIX}ENABLE_SHELL": str(self.enable_shell).lower(),
            f"{SERVER_ENV_PREFIX}APPROVAL_MODE": self.approval_mode,
            f"{SERVER_ENV_PREFIX}READ_ONLY": str(self.read_only).lower(),
            f"{SERVER_ENV_PREFIX}NO_MCP": str(self.no_mcp).lower(),
            f"{SERVER_ENV_PREFIX}TRUST_PROJECT_MCP": str(self.trust_project_mcp).lower(),
            f"{SERVER_ENV_PREFIX}LOG_LEVEL": self.server_log_level,
            f"{SERVER_ENV_PREFIX}AUTH_MODE": self.auth_mode,
            "TALKOPS_AUTH_MODE": self.auth_mode,
            f"{SERVER_ENV_PREFIX}NO_SUBAGENTS": str(self.no_subagents).lower(),
            f"{SERVER_ENV_PREFIX}TNR": str(self.tnr).lower(),
        }
        if self.model:
            env[f"{SERVER_ENV_PREFIX}MODEL"] = self.model
        if self.system_prompt:
            env[f"{SERVER_ENV_PREFIX}SYSTEM_PROMPT"] = self.system_prompt
        effective_cwd = self.cwd or str(Path.cwd().resolve())
        env[f"{SERVER_ENV_PREFIX}CWD"] = effective_cwd
        from talkops.config.paths import find_project_root

        resolved_root = self.project_root or find_project_root(effective_cwd)
        if resolved_root:
            env[f"{SERVER_ENV_PREFIX}PROJECT_ROOT"] = str(resolved_root)
        if self.shell_allow_list:
            env[f"{SERVER_ENV_PREFIX}SHELL_ALLOW_LIST"] = ",".join(self.shell_allow_list)
        if self.cloud_provider:
            env[f"{SERVER_ENV_PREFIX}CLOUD_PROVIDER"] = self.cloud_provider
        if self.cloud_target:
            env[f"{SERVER_ENV_PREFIX}CLOUD_TARGET"] = self.cloud_target
        if self.cloud_execution_mode:
            env[f"{SERVER_ENV_PREFIX}CLOUD_EXECUTION_MODE"] = self.cloud_execution_mode
        if self.aws_profile:
            env[f"{SERVER_ENV_PREFIX}AWS_PROFILE"] = self.aws_profile
        if self.aws_region:
            env[f"{SERVER_ENV_PREFIX}AWS_REGION"] = self.aws_region
        if self.mcp_config_path:
            env[f"{SERVER_ENV_PREFIX}MCP_CONFIG_PATH"] = self.mcp_config_path
        if self.postgres_uri:
            env[f"{SERVER_ENV_PREFIX}POSTGRES_URI"] = self.postgres_uri
            env["TALKOPS_POSTGRES_URI"] = self.postgres_uri
        if self.webhook_port is not None:
            env[f"{SERVER_ENV_PREFIX}WEBHOOK_PORT"] = str(self.webhook_port)
        if self.webhook_host:
            env[f"{SERVER_ENV_PREFIX}WEBHOOK_HOST"] = self.webhook_host
        if self.workspaces_dir:
            env[f"{SERVER_ENV_PREFIX}WORKSPACES_DIR"] = self.workspaces_dir
            env["TALKOPS_WORKSPACES_DIR"] = self.workspaces_dir
        if self.server_url:
            env["TALKOPS_SERVER_URL"] = self.server_url
        if self.api_key:
            env["TALKOPS_API_KEY"] = self.api_key
        if self.user_id:
            env["TALKOPS_USER_ID"] = self.user_id
        if self.profile:
            env["TALKOPS_SERVER_PROFILE"] = self.profile
        if self.addons:
            env["TALKOPS_ADDONS"] = ",".join(self.addons)
        if self.disable_addons:
            env["TALKOPS_DISABLE_ADDONS"] = ",".join(self.disable_addons)
        if self.custom_addons:
            env["TALKOPS_CUSTOM_ADDONS"] = ",".join(self.custom_addons)
        if self.addons_config_path:
            env["TALKOPS_ADDONS_CONFIG"] = self.addons_config_path
        return env

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Construct instance from env."""
        raw_sal = _read_env_str("SHELL_ALLOW_LIST")
        shell_allow_list = tuple(s.strip() for s in raw_sal.split(",") if s.strip()) if raw_sal else None
        server_log_level = _read_env_str("LOG_LEVEL") or "WARNING"
        raw_wh_port = _read_env_str("WEBHOOK_PORT") or os.environ.get("TALKOPS_WEBHOOK_PORT")
        webhook_port = int(raw_wh_port) if raw_wh_port and raw_wh_port.strip().isdigit() else None
        from talkops.state.postgres_session import get_postgres_session_uri

        postgres_uri = _read_env_str("POSTGRES_URI") or get_postgres_session_uri()
        auth_mode = "token"
        workspaces_dir = (
            _read_env_str("WORKSPACES_DIR")
            or os.environ.get("TALKOPS_WORKSPACES_DIR")
        )
        raw_addons = os.environ.get("TALKOPS_ADDONS")
        addons = tuple(s.strip() for s in raw_addons.split(",") if s.strip()) if raw_addons else None
        raw_disable = os.environ.get("TALKOPS_DISABLE_ADDONS")
        disable_addons = tuple(s.strip() for s in raw_disable.split(",") if s.strip()) if raw_disable else None
        raw_custom = os.environ.get("TALKOPS_CUSTOM_ADDONS")
        custom_addons = tuple(s.strip() for s in raw_custom.split(",") if s.strip()) if raw_custom else None

        return cls(
            model=_read_env_str("MODEL"),
            assistant_id=_read_env_str("ASSISTANT_ID") or DEFAULT_ASSISTANT_ID,
            system_prompt=_read_env_str("SYSTEM_PROMPT"),
            auto_approve=_read_env_bool("AUTO_APPROVE", default=False),
            interactive=_read_env_bool("INTERACTIVE", default=True),
            enable_shell=_read_env_bool("ENABLE_SHELL", default=True),
            cwd=_read_env_str("CWD"),
            project_root=_read_env_str("PROJECT_ROOT"),
            approval_mode=_read_env_str("APPROVAL_MODE") or "manual",
            shell_allow_list=shell_allow_list,
            read_only=_read_env_bool("READ_ONLY", default=False),
            cloud_provider=_read_env_str("CLOUD_PROVIDER"),
            cloud_target=_read_env_str("CLOUD_TARGET"),
            cloud_execution_mode=_read_env_str("CLOUD_EXECUTION_MODE") or "read_only",
            aws_profile=_read_env_str("AWS_PROFILE"),
            aws_region=_read_env_str("AWS_REGION"),
            mcp_config_path=_read_env_str("MCP_CONFIG_PATH"),
            no_mcp=_read_env_bool("NO_MCP", default=False),
            trust_project_mcp=_read_env_bool("TRUST_PROJECT_MCP", default=False),
            server_log_level=server_log_level,
            postgres_uri=postgres_uri,
            auth_mode=auth_mode,
            webhook_port=webhook_port,
            webhook_host=_read_env_str("WEBHOOK_HOST"),
            workspaces_dir=workspaces_dir,
            server_url=os.environ.get("TALKOPS_SERVER_URL"),
            api_key=os.environ.get("TALKOPS_API_KEY"),
            user_id=os.environ.get("TALKOPS_USER_ID"),
            no_subagents=_read_env_bool("NO_SUBAGENTS", default=False),
            tnr=_read_env_bool("TNR", default=False),
            profile=os.environ.get("TALKOPS_SERVER_PROFILE") or os.environ.get("TALKOPS_PROFILE"),
            addons=addons,
            disable_addons=disable_addons,
            custom_addons=custom_addons,
            addons_config_path=os.environ.get("TALKOPS_ADDONS_CONFIG"),
        )

    @classmethod
    def from_cli_args(
        cls,
        *,
        project_context: Any = None,
        model_name: str | None = None,
        model: str | None = None,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        system_prompt: str | None = None,
        auto_approve: bool = False,
        interactive: bool = True,
        enable_shell: bool = True,
        cwd: str | None = None,
        project_root: str | None = None,
        approval_mode: str = "manual",
        shell_allow_list: tuple[str, ...] | list[str] | None = None,
        read_only: bool = False,
        cloud_provider: str | None = None,
        cloud_target: str | None = None,
        cloud_execution_mode: str = "read_only",
        aws_profile: str | None = None,
        aws_region: str | None = None,
        mcp_config_path: str | None = None,
        no_mcp: bool = False,
        trust_project_mcp: bool = False,
        server_log_level: str = "WARNING",
        postgres_uri: str | None = None,
        auth_mode: str | None = None,
        webhook_port: int | None = None,
        webhook_host: str | None = None,
        workspaces_dir: str | None = None,
        server_url: str | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        no_subagents: bool = False,
        tnr: bool = False,
        profile: str | None = None,
        addons: tuple[str, ...] | list[str] | None = None,
        disable_addons: tuple[str, ...] | list[str] | None = None,
        custom_addons: tuple[str, ...] | list[str] | None = None,
        addons_config: str | None = None,
        addons_config_path: str | None = None,
    ) -> ServerConfig:
        """Construct instance from cli args."""
        from talkops.state.postgres_session import get_postgres_session_uri

        resolved_cwd = (
            str(project_context.user_cwd)
            if project_context is not None
            else (cwd or str(Path.cwd().resolve()))
        )
        resolved_project_root = (
            str(project_context.project_root)
            if project_context is not None and project_context.project_root is not None
            else project_root
        )
        sal = tuple(shell_allow_list) if shell_allow_list else None
        effective_postgres_uri = postgres_uri or get_postgres_session_uri()
        del auth_mode
        effective_auth_mode = "token"
        effective_server_url = server_url or os.environ.get("TALKOPS_SERVER_URL")
        effective_api_key = api_key or os.environ.get("TALKOPS_API_KEY")
        effective_user_id = user_id or os.environ.get("TALKOPS_USER_ID")
        return cls(
            model=model or model_name,
            assistant_id=assistant_id,
            system_prompt=system_prompt,
            auto_approve=auto_approve,
            interactive=interactive,
            enable_shell=enable_shell,
            cwd=resolved_cwd,
            project_root=resolved_project_root,
            approval_mode=approval_mode,
            shell_allow_list=sal,
            read_only=read_only,
            cloud_provider=cloud_provider,
            cloud_target=cloud_target,
            cloud_execution_mode=cloud_execution_mode or "read_only",
            aws_profile=aws_profile,
            aws_region=aws_region,
            mcp_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            server_log_level=server_log_level,
            postgres_uri=effective_postgres_uri,
            auth_mode=effective_auth_mode,
            webhook_port=webhook_port,
            webhook_host=webhook_host,
            workspaces_dir=workspaces_dir or os.environ.get("TALKOPS_WORKSPACES_DIR"),
            server_url=effective_server_url,
            api_key=effective_api_key,
            user_id=effective_user_id,
            no_subagents=no_subagents,
            tnr=tnr,
            profile=profile,
            addons=tuple(addons) if addons else None,
            disable_addons=tuple(disable_addons) if disable_addons else None,
            custom_addons=tuple(custom_addons) if custom_addons else None,
            addons_config_path=addons_config or addons_config_path or os.environ.get("TALKOPS_ADDONS_CONFIG"),
        )


