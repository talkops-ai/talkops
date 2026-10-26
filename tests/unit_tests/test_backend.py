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

"""Unit tests for TalkOps backend execution, registry, and composite routing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from talkops.backend.composite import (
    CompositeBackend,
    TalkOpsCompositeBackend,
)
from talkops.backend.local import LocalShellBackend, _build_shell_env
from talkops.backend.registry import (
    BackendRegistry,
    get_backend_class,
    get_backend_registry,
    register_backend,
)
from talkops.backend.sandbox_config import SandboxConfig
from talkops.backend.sandbox_factory import (
    WORKSPACE_IGNORE_DIRS,
    get_project_root,
    get_sandbox_backend,
    get_sandbox_working_dir,
)


class TestBackendRegistry:
    """Tests for BackendRegistry singleton, registration, and builder."""

    def test_singleton(self) -> None:
        """Verify singleton."""
        r1 = get_backend_registry()
        r2 = get_backend_registry()
        assert r1 is r2

    def test_register_and_build(self) -> None:
        """Verify register and build."""
        registry = BackendRegistry()

        class DummyBackend:
            """Test suite or helper class for dummy backend."""
            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                """Initialize DummyBackend."""
                self.kwargs = kwargs

        registry.register("dummy", DummyBackend)
        backend = registry.build("dummy", root_dir="/tmp/talkops_test")
        assert isinstance(backend, DummyBackend)
        assert backend.kwargs["root_dir"] == "/tmp/talkops_test"

    def test_build_unknown_raises(self) -> None:
        """Verify build unknown raises."""
        registry = BackendRegistry()
        with pytest.raises(ValueError, match="not registered"):
            registry.build("nonexistent_provider")

    def test_list_registered(self) -> None:
        """Verify list registered."""
        registry = BackendRegistry()

        class Alpha:
            """Test suite or helper class for alpha."""
            pass

        class Beta:
            """Test suite or helper class for beta."""
            pass

        registry.register("beta_backend", Beta)
        registry.register("alpha_backend", Alpha)
        assert registry.list_registered() == ["alpha_backend", "beta_backend"]

    def test_decorator_registers(self) -> None:
        """Verify decorator registers."""
        registry = BackendRegistry()

        @register_backend("test_custom_provider")
        class CustomProvider:
            """Test suite or helper class for custom provider."""
            pass

        global_reg = get_backend_registry()
        assert "test_custom_provider" in global_reg.list_registered()
        assert get_backend_class("test_custom_provider") is CustomProvider


class TestLocalShellBackend:
    """Tests for LocalShellBackend AWS and DevOps environment handling."""

    def test_local_backend_registered(self) -> None:
        """Verify local backend registered."""
        registry = get_backend_registry()
        assert "local" in registry.list_registered()

    def test_local_shell_backend_execute(self, tmp_path: Path) -> None:
        """Verify local shell backend execute."""
        backend = LocalShellBackend(root_dir=tmp_path)
        res = backend.execute("echo 'hello talkops'")
        assert res.exit_code == 0
        assert "hello talkops" in res.output

    def test_aws_env_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify aws env preserved."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE123456789")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretkey123456789")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sessiontoken123")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_PROFILE", "production")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/DevOpsRole")
        monkeypatch.setenv("AWS_SDK_LOAD_CONFIG", "1")

        env = _build_shell_env()
        assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE123456789"
        assert env["AWS_SECRET_ACCESS_KEY"] == "secretkey123456789"
        assert env["AWS_SESSION_TOKEN"] == "sessiontoken123"
        assert env["AWS_REGION"] == "us-west-2"
        assert env["AWS_PROFILE"] == "production"
        assert env["AWS_ROLE_ARN"] == "arn:aws:iam::123456789012:role/DevOpsRole"
        assert env["AWS_SDK_LOAD_CONFIG"] == "1"

    def test_aws_pager_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify aws pager disabled by default."""
        monkeypatch.delenv("AWS_PAGER", raising=False)
        env = _build_shell_env()
        assert env["AWS_PAGER"] == ""

    def test_aws_pager_preserved_if_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify aws pager preserved if explicit."""
        monkeypatch.setenv("AWS_PAGER", "cat")
        env = _build_shell_env()
        assert env["AWS_PAGER"] == "cat"

    def test_iac_prefix_env_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify iac prefix env captured."""
        monkeypatch.setenv("TF_VAR_environment", "staging")
        monkeypatch.setenv("TF_IN_AUTOMATION", "1")
        monkeypatch.setenv("TERRAGRUNT_TFPATH", "tofu")
        monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
        monkeypatch.setenv("PULUMI_ACCESS_TOKEN", "pul-secret")
        monkeypatch.setenv("SAM_CLI_TELEMETRY", "0")
        monkeypatch.setenv("KUBECONFIG", "/custom/kube/config")

        env = _build_shell_env()
        assert env["TF_VAR_environment"] == "staging"
        assert env["TF_IN_AUTOMATION"] == "1"
        assert env["TERRAGRUNT_TFPATH"] == "tofu"
        assert env["CDK_DEFAULT_ACCOUNT"] == "123456789012"
        assert env["PULUMI_ACCESS_TOKEN"] == "pul-secret"
        assert env["SAM_CLI_TELEMETRY"] == "0"
        assert env["KUBECONFIG"] == "/custom/kube/config"

    def test_safe_keys_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify safe keys included."""
        monkeypatch.setenv("HOME", "/home/devops")
        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setenv("USER", "devops")

        env = _build_shell_env()
        assert env["HOME"] == "/home/devops"
        assert env["SHELL"] == "/bin/zsh"
        assert env["USER"] == "devops"

    def test_path_prepend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify path prepend."""
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _build_shell_env()
        assert "/usr/local/bin" in env["PATH"]
        assert "/opt/homebrew/bin" in env["PATH"]

    def test_sensitive_env_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify sensitive env not leaked."""
        monkeypatch.setenv("SECRET_PASSWORD", "hunter2")
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pwd@db/prod")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_123456789")

        env = _build_shell_env()
        assert "SECRET_PASSWORD" not in env
        assert "DATABASE_URL" not in env
        assert "GITHUB_TOKEN" not in env

    def test_local_shell_backend_sync_cloud_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify _sync_cloud_env syncs active profile and region, clearing static keys for named profiles."""
        backend = LocalShellBackend(root_dir=tmp_path)
        backend._env["AWS_ACCESS_KEY_ID"] = "OLD_KEY"
        backend._env["AWS_SECRET_ACCESS_KEY"] = "OLD_SECRET"

        monkeypatch.setattr("talkops.config.cloud_profiles.get_active_aws_profile", lambda: "prod_corp")
        monkeypatch.setattr("talkops.config.cloud_profiles.get_active_aws_region", lambda: "eu-west-1")

        backend._sync_cloud_env()

        assert backend._env["AWS_PROFILE"] == "prod_corp"
        assert backend._env["AWS_DEFAULT_PROFILE"] == "prod_corp"
        assert backend._env["AWS_REGION"] == "eu-west-1"
        assert backend._env["AWS_DEFAULT_REGION"] == "eu-west-1"
        # Conflicting static credentials must be cleared for named profiles
        assert "AWS_ACCESS_KEY_ID" not in backend._env
        assert "AWS_SECRET_ACCESS_KEY" not in backend._env

        # Switching to profile without explicit region must clear stale region keys
        monkeypatch.setattr("talkops.config.cloud_profiles.get_active_aws_region", lambda: None)
        backend._sync_cloud_env()
        assert "AWS_REGION" not in backend._env
        assert "AWS_DEFAULT_REGION" not in backend._env


class TestTalkOpsCompositeBackend:
    """Tests for TalkOpsCompositeBackend routing, cleanup, and compatibility."""

    def test_composite_backend_virtual_routing(self, tmp_path: Path) -> None:
        """Verify composite backend virtual routing."""
        local_backend = LocalShellBackend(root_dir=tmp_path)
        composite = TalkOpsCompositeBackend(default=local_backend)

        # Write file to virtual /large_tool_results/ prefix
        write_res = composite.write("/large_tool_results/test.txt", "evidence data")
        assert write_res.error is None

        # Read back through composite backend
        read_res = composite.read("/large_tool_results/test.txt")
        assert read_res.file_data is not None
        assert read_res.file_data["content"] == "evidence data"

        # Write file to virtual /conversation_history/ prefix
        history_res = composite.write("/conversation_history/session_001.json", '{"history": []}')
        assert history_res.error is None
        read_hist = composite.read("/conversation_history/session_001.json")
        assert read_hist.file_data is not None
        assert read_hist.file_data["content"] == '{"history": []}'

        composite.cleanup()

    def test_composite_cleanup(self) -> None:
        """Verify composite cleanup."""
        backend = TalkOpsCompositeBackend(default=MagicMock())
        temp_dir = backend._large_results_dir
        assert Path(temp_dir).exists()
        backend.cleanup()
        assert not Path(temp_dir).exists()

    def test_context_manager(self) -> None:
        """Verify context manager."""
        with TalkOpsCompositeBackend(default=MagicMock()) as backend:
            temp_dir = backend._large_results_dir
            assert Path(temp_dir).exists()
        assert not Path(temp_dir).exists()

    def test_custom_routes_merged(self) -> None:
        """Verify custom routes merged."""
        custom_backend = MagicMock()
        backend = TalkOpsCompositeBackend(
            default=MagicMock(),
            routes={"/custom/": custom_backend},
        )
        assert "/custom/" in backend.routes
        assert "/large_tool_results/" in backend.routes
        assert "/conversation_history/" in backend.routes
        backend.cleanup()

    def test_aliases(self) -> None:
        """Verify aliases."""
        assert CompositeBackend is TalkOpsCompositeBackend


class TestSandboxConfig:
    """Tests for SandboxConfig parsing and defaults."""

    def test_default_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default config."""
        monkeypatch.delenv("TALKOPS_SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("TALKOPS_SANDBOX_SNAPSHOT", raising=False)
        monkeypatch.delenv("SANDBOX_SNAPSHOT", raising=False)

        cfg = SandboxConfig.from_env()
        assert cfg.provider == "local"
        assert cfg.sync_workspace is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify env override."""
        monkeypatch.setenv("TALKOPS_SANDBOX_PROVIDER", "langsmith")
        monkeypatch.setenv("TALKOPS_SANDBOX_ID", "sb-12345")
        monkeypatch.setenv("TALKOPS_SANDBOX_IMAGE", "custom-aws-image:latest")
        monkeypatch.setenv("TALKOPS_SANDBOX_SETUP_SCRIPT", "/scripts/init.sh")
        monkeypatch.setenv("TALKOPS_SANDBOX_SNAPSHOT", "aws-devops-v1")
        monkeypatch.setenv("TALKOPS_SANDBOX_SYNC_WORKSPACE", "false")

        cfg = SandboxConfig.from_env()
        assert cfg.provider == "langsmith"
        assert cfg.sandbox_id == "sb-12345"
        assert cfg.image == "custom-aws-image:latest"
        assert cfg.setup_script_path == "/scripts/init.sh"
        assert cfg.snapshot_name == "aws-devops-v1"
        assert cfg.sync_workspace is False

    def test_toml_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify toml config."""
        monkeypatch.delenv("TALKOPS_SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("TALKOPS_SANDBOX_SYNC_WORKSPACE", raising=False)
        monkeypatch.delenv("SANDBOX_SYNC_WORKSPACE", raising=False)

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            """
[sandboxes]
default = "daytona"
image = "python:3.12-alpine"
sync_workspace = false

[sandboxes.daytona]
workspace_type = "docker"
"""
        )
        cfg = SandboxConfig.load(cfg_file)
        assert cfg.provider == "daytona"
        assert cfg.image == "python:3.12-alpine"
        assert cfg.sync_workspace is False
        assert "daytona" in cfg.providers
        assert cfg.providers["daytona"]["workspace_type"] == "docker"


class TestSandboxFactory:
    """Tests for sandbox factory, working dirs, and ignore lists."""

    def test_working_dirs(self) -> None:
        """Verify working dirs."""
        assert get_sandbox_working_dir("langsmith") == "/root"
        assert get_sandbox_working_dir("modal") == "/workspace"
        assert get_sandbox_working_dir("daytona") == "/home/daytona"
        assert get_sandbox_working_dir("runloop") == "/home/user"
        assert get_sandbox_working_dir("vercel") == "/vercel/sandbox"
        assert get_sandbox_working_dir("agentcore") == "/workspace"
        assert get_sandbox_working_dir("unknown") == "/workspace"

    def test_workspace_ignore_includes_iac(self) -> None:
        """Verify workspace ignore includes iac."""
        assert ".terraform" in WORKSPACE_IGNORE_DIRS
        assert ".terragrunt-cache" in WORKSPACE_IGNORE_DIRS
        assert "cdk.out" in WORKSPACE_IGNORE_DIRS
        assert ".git" in WORKSPACE_IGNORE_DIRS

    def test_get_sandbox_backend_local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify get sandbox backend local."""
        monkeypatch.setenv("TALKOPS_SANDBOX_PROVIDER", "local")
        monkeypatch.setattr("talkops.backend.sandbox_factory.get_project_root", lambda: tmp_path)

        backend = get_sandbox_backend(thread_id="test_thread")
        assert isinstance(backend, LocalShellBackend)
        res = backend.execute("echo 'sandbox local test'")
        assert res.exit_code == 0
        assert "sandbox local test" in res.output

    def test_sandbox_registry_builtins_and_deps(self) -> None:
        """Verify all 6 built-in providers are registered and their packages are installed."""
        from talkops.backend.sandbox_factory import (
            get_default_working_dir,
            verify_sandbox_deps,
        )
        from talkops.backend.sandbox_registry import SandboxRegistry

        registry = SandboxRegistry.load()
        providers = registry.available_providers()
        for expected in ("agentcore", "daytona", "langsmith", "modal", "runloop", "vercel"):
            assert expected in providers
            assert registry.is_available(expected)
            verify_sandbox_deps(expected)

        assert get_default_working_dir("langsmith") == "/root"
        assert get_default_working_dir("daytona") == "/home/daytona"
        assert get_default_working_dir("modal") == "/workspace"
        assert get_default_working_dir("runloop") == "/home/user"
        assert get_default_working_dir("vercel") == "/vercel/sandbox"

    def test_sandbox_registry_custom_provider_config(self, tmp_path: Path) -> None:
        """Verify config-declared [sandboxes.providers.<name>] is loaded into SandboxRegistry."""
        from talkops.backend.sandbox_registry import SandboxRegistry

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            """
[sandboxes]
default = "acme"

[sandboxes.providers.acme]
class_path = "talkops.backend.sandbox_factory:_ModalProvider"
working_dir = "/custom/acme"
supports_sandbox_id = true
supports_snapshot_name = true

[sandboxes.providers.acme.params]
region = "us-east-1"
"""
        )
        cfg = SandboxConfig.load(cfg_file)
        registry = SandboxRegistry(cfg)
        assert registry.default == "acme"
        assert registry.is_available("acme")
        assert registry.get_working_dir("acme") == "/custom/acme"
        assert registry.get_config_params("acme") == {"region": "us-east-1"}
        meta = registry.get_metadata("acme")
        assert meta is not None
        assert meta.supports_sandbox_id is True
        assert meta.supports_snapshot_name is True

    def test_create_sandbox_validation(self) -> None:
        """Verify create_sandbox rejects incompatible snapshot_name / sandbox_id combinations."""
        from talkops.backend.sandbox_factory import create_sandbox

        with pytest.raises(ValueError, match="snapshot_name cannot be combined with sandbox_id"):
            with create_sandbox("langsmith", sandbox_id="sb-1", snapshot_name="snap-1"):
                pass

        with pytest.raises(ValueError, match="snapshot_name is not supported by provider 'modal'"):
            with create_sandbox("modal", snapshot_name="snap-1"):
                pass

    def test_cli_sandbox_flags_validation(self) -> None:
        """Verify CLI parser and _resolve_and_validate_sandbox handle --sandbox flags."""
        from talkops.cli.main import _resolve_and_validate_sandbox, build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["--sandbox", "runloop", "--sandbox-id", "dbx_123", "--sandbox-setup", "./setup.sh"]
        )
        _resolve_and_validate_sandbox(args, parser)
        assert args.sandbox == "runloop"
        assert args.sandbox_id == "dbx_123"
        assert args.sandbox_setup == "./setup.sh"

        # --sandbox-id without --sandbox raises SystemExit(2) via parser.error
        args_no_sb = parser.parse_args(["--sandbox-id", "dbx_123"])
        with pytest.raises(SystemExit):
            _resolve_and_validate_sandbox(args_no_sb, parser)

        # --sandbox-id with daytona (unsupported) raises SystemExit(2)
        args_daytona_id = parser.parse_args(["--sandbox", "daytona", "--sandbox-id", "sb_1"])
        with pytest.raises(SystemExit):
            _resolve_and_validate_sandbox(args_daytona_id, parser)

        # --sandbox-snapshot-name with modal (unsupported) raises SystemExit(2)
        args_modal_snap = parser.parse_args(
            ["--sandbox", "modal", "--sandbox-snapshot-name", "snap_1"]
        )
        with pytest.raises(SystemExit):
            _resolve_and_validate_sandbox(args_modal_snap, parser)
