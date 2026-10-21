"""Unit tests for Phase 2: Transactional Non-Regression (TNR) Safety & Undo Middleware."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from talkops.middleware.tnr_safety import (
    ALockManager,
    TransactionalNonRegressionMiddleware,
    is_mutating_tool_call,
    synthesize_k8s_inverse_command,
)


def _make_request(
    tool_name: str,
    args: dict[str, Any],
    *,
    tnr: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> Any:
    req = MagicMock()
    req.tool_call = {"name": tool_name, "args": args, "id": "tc_test_1"}
    runtime = MagicMock()
    runtime.context = {"tnr": tnr}
    runtime.config = {}
    if events is not None:
        runtime.stream_writer = lambda payload: events.append(payload)
    else:
        runtime.stream_writer = None
    req.runtime = runtime
    return req


def test_is_mutating_tool_call_classification() -> None:
    assert is_mutating_tool_call("read_file", {"file_path": "app.py"}) is False
    assert is_mutating_tool_call("execute", {"command": "kubectl get pods -n default"}) is False
    assert is_mutating_tool_call("execute", {"command": "kubectl scale deployment/web --replicas=0 -n prod"}) is True
    assert is_mutating_tool_call("execute", {"command": "helm upgrade api ./chart"}) is True
    assert is_mutating_tool_call("execute", {"command": "rm -f config.yaml"}) is True
    assert is_mutating_tool_call("write_file", {"file_path": "main.tf"}) is True


def test_synthesize_k8s_inverse_command() -> None:
    inv, res = synthesize_k8s_inverse_command(
        "kubectl set image deployment/payment-svc app=payment:broken -n checkout"
    )
    assert inv == "kubectl rollout undo deployment/payment-svc -n checkout"
    assert res == "k8s:checkout/deployment/payment-svc"

    helm_inv, helm_res = synthesize_k8s_inverse_command("helm upgrade billing ./charts/billing")
    assert helm_inv == "helm rollback billing"
    assert helm_res == "helm:billing"


def test_tnr_middleware_commits_successful_mutation_and_releases_lock(tmp_path: Path) -> None:
    target_file = tmp_path / "service.yaml"
    target_file.write_text("replicas: 2\n", encoding="utf-8")

    emitted: list[dict[str, Any]] = []
    mw = TransactionalNonRegressionMiddleware(working_dir=tmp_path, enabled_by_default=True)
    req = _make_request("write_file", {"file_path": str(target_file)}, tnr=True, events=emitted)

    def handler(_r: Any) -> ToolMessage:
        target_file.write_text("replicas: 4\n", encoding="utf-8")
        return ToolMessage(content="Wrote service.yaml successfully", tool_call_id="tc_test_1", name="write_file")

    res = mw.wrap_tool_call(req, handler)
    assert isinstance(res, ToolMessage)
    assert target_file.read_text(encoding="utf-8") == "replicas: 4\n"
    assert mw.lock_manager.active_lock_count() == 0

    statuses = [e["status"] for e in emitted]
    assert statuses == ["lock_acquired", "committed", "lock_released"]


def test_tnr_middleware_rolls_back_file_mutation_on_regression(tmp_path: Path) -> None:
    target_file = tmp_path / "deployment.yaml"
    original_content = "image: nginx:1.25\n"
    target_file.write_text(original_content, encoding="utf-8")

    emitted: list[dict[str, Any]] = []
    mw = TransactionalNonRegressionMiddleware(working_dir=tmp_path, enabled_by_default=True)
    req = _make_request("edit_file", {"file_path": str(target_file)}, tnr=True, events=emitted)

    def regressive_handler(_r: Any) -> ToolMessage:
        # Mutate file to broken state and return error output
        target_file.write_text("image: nginx:nonexistent-tag\n", encoding="utf-8")
        return ToolMessage(
            content="Error from server (Invalid): deployment validation failed",
            tool_call_id="tc_test_1",
            name="edit_file",
        )

    res = mw.wrap_tool_call(req, regressive_handler)
    assert isinstance(res, ToolMessage)
    # Verify alpha_U restored exact original file content!
    assert target_file.read_text(encoding="utf-8") == original_content
    assert "[TNR Safety Rollback (α_U)]" in str(res.content)
    assert mw.lock_manager.active_lock_count() == 0

    statuses = [e["status"] for e in emitted]
    assert statuses == ["lock_acquired", "rolled_back", "lock_released"]


def test_tnr_middleware_restores_deleted_file_on_rm_regression(tmp_path: Path) -> None:
    secret_cfg = tmp_path / "critical.conf"
    secret_cfg.write_text("db_host=prod.internal\n", encoding="utf-8")

    emitted: list[dict[str, Any]] = []
    mw = TransactionalNonRegressionMiddleware(working_dir=tmp_path, enabled_by_default=True)
    req = _make_request("execute", {"command": f"rm {secret_cfg}"}, tnr=True, events=emitted)

    def rm_then_fail(_r: Any) -> ToolMessage:
        secret_cfg.unlink()
        return ToolMessage(
            content="Command failed with exit code: 1 (service CrashLoopBackOff)",
            tool_call_id="tc_test_1",
            name="execute",
        )

    res: Any = mw.wrap_tool_call(req, rm_then_fail)
    assert secret_cfg.exists()
    assert secret_cfg.read_text(encoding="utf-8") == "db_host=prod.internal\n"
    assert "[TNR Safety Rollback (α_U)]" in str(res.content)
    assert [e["status"] for e in emitted] == ["lock_acquired", "rolled_back", "lock_released"]


@pytest.mark.asyncio
async def test_tnr_middleware_async_rollback_and_passthrough_when_disabled(tmp_path: Path) -> None:
    f = tmp_path / "new_bad_manifest.yaml"
    emitted: list[dict[str, Any]] = []
    mw = TransactionalNonRegressionMiddleware(working_dir=tmp_path, enabled_by_default=False)

    # 1. Disabled in context -> passthrough (no events emitted)
    req_off = _make_request("write_file", {"file_path": str(f)}, tnr=False, events=emitted)

    async def ok_handler(_r: Any) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id="tc_test_1", name="write_file")

    await mw.awrap_tool_call(req_off, ok_handler)
    assert emitted == []

    # 2. Enabled in context -> creates new file that causes regression -> alpha_U unlinks it
    req_on = _make_request("write_file", {"file_path": str(f)}, tnr=True, events=emitted)

    async def bad_create_handler(_r: Any) -> ToolMessage:
        f.write_text("broken: ::::\n", encoding="utf-8")
        return ToolMessage(content="syntax error in manifest", tool_call_id="tc_test_1", name="write_file")

    res_async: Any = await mw.awrap_tool_call(req_on, bad_create_handler)
    assert not f.exists()
    assert "Removed newly created file" in str(res_async.content)
    assert [e["status"] for e in emitted] == ["lock_acquired", "rolled_back", "lock_released"]
