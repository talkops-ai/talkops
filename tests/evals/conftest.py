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

"""Pytest & LangSmith Harness Configuration (`evals/conftest.py`).

Implements `docs/agent_evals_enhancement.md` wiring:
1. Loads `.env` via `python-dotenv` and attaches `langsmith_experiment_metadata`
   (`git_sha`, `agent_model`, `environment`, `eval_architecture`).
2. Strict local-only protection (`GITHUB_ACTIONS=true` skips automatically).
3. Supports `LANGSMITH_TEST_CACHE=tests/evals/cassettes` disk-cached replay and `vcrpy`
   header scrubbing (`authorization`, `x-api-key`, `x-goog-api-key`).
4. Permanent fix for `langsmith`'s non-daemon `ThreadPoolExecutor` exit hang.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from talkops.model.config import apply_stored_credentials
from talkops.model.factory import create_model
from tests.evals.framework.reliability import EvalTelemetry
from tests.evals.framework.serve_harness import build_serve_harness_lifecycle

load_dotenv(Path.home() / ".talkops" / ".env")
load_dotenv()
with contextlib.suppress(Exception):
    apply_stored_credentials()

DEFAULT_EVAL_MODEL = "google_vertexai:gemini-3.8-flash"

VALID_SCOPES = {
    "router_logic",
    "single_node",
    "partial_subflow",
    "deep_agent_outcome",
    "multiturn_simulation",
}

VALID_CATEGORIES = {
    "tool_use",
    "retrieval",
    "file_operations",
    "memory",
    "conversation",
    "summarization",
    "profile_isolation",
    "guardrails",
    "graph_routing",
    "webhook_rca",
    "calibration",
}

VALID_PROFILES = {
    "default",
    "api-only",
    "webhook-worker",
    "sre",
    "enterprise-full",
}


def pytest_configure(config: pytest.Config) -> None:
    """Configure LangSmith tracking cleanly so local runs exit immediately without hanging."""
    wants_ls_output = bool(getattr(config.option, "langsmith_output", False))
    explicit_ls_sync = os.environ.get("TALKOPS_LANGSMITH_SYNC", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not wants_ls_output and not explicit_ls_sync:
        os.environ["LANGSMITH_TEST_TRACKING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
    else:
        os.environ["LANGSMITH_TEST_TRACKING"] = "true"
        os.environ.setdefault("LANGSMITH_PROJECT", "talkops")
        os.environ.setdefault("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

    with contextlib.suppress(Exception):
        import langsmith.utils as ls_utils

        ls_utils.get_env_var.cache_clear()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain or shut down any active LangSmith background `ThreadPoolExecutor`s at exit."""
    with contextlib.suppress(Exception):
        from langsmith.testing import _internal as ls_internal

        suites = getattr(ls_internal, "_TEST_SUITES", None) or {}
        for suite in list(suites.values()):
            executor = getattr(suite, "_executor", None)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    with contextlib.suppress(Exception):
        import langsmith.client as ls_client_mod

        global_client = getattr(ls_client_mod, "_GLOBAL_CLIENT", None)
        if global_client is not None and hasattr(global_client, "cleanup"):
            global_client.cleanup()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI options for scoping, profiling, model overrides, and `pass^k` trials."""
    parser.addoption(
        "--eval-scope",
        action="store",
        default=None,
        choices=sorted(VALID_SCOPES),
        help="Run only scenarios targeting one of the Five Isolation Scopes.",
    )
    parser.addoption(
        "--eval-category",
        action="store",
        default=None,
        choices=sorted(VALID_CATEGORIES),
        help="Run only scenarios matching a specific capability domain.",
    )
    parser.addoption(
        "--serve-profile",
        action="store",
        default=None,
        choices=sorted(VALID_PROFILES),
        help="Run only scenarios targeting a specific `talkops serve` profile.",
    )
    parser.addoption(
        "--eval-model",
        action="store",
        default=None,
        help=f"Model spec for subject & judge in scenario evals (default: {DEFAULT_EVAL_MODEL}).",
    )
    parser.addoption(
        "--eval-trials",
        action="store",
        type=int,
        default=1,
        help="Number of trials `k` for `pass@k` / `pass^k` multi-trial consistency checks.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Enforce local-only isolation (`GITHUB_ACTIONS`) and filter by scope/category/profile."""
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        for item in items:
            item.add_marker(
                pytest.mark.skip(reason="Scenario evals are local-only and never run in GitHub Actions")
            )
        return

    target_scope = config.getoption("--eval-scope")
    target_category = config.getoption("--eval-category")
    target_profile = config.getoption("--serve-profile")
    pg_uri = os.environ.get("TALKOPS_EVAL_POSTGRES_URI", "").strip()

    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []

    for item in items:
        if item.get_closest_marker("postgres") and not pg_uri:
            item.add_marker(pytest.mark.skip(reason="TALKOPS_EVAL_POSTGRES_URI not set"))

        keep = True
        if target_scope:
            sc_marker = item.get_closest_marker("eval_scope")
            if not sc_marker or target_scope not in sc_marker.args:
                keep = False

        if keep and target_category:
            cat_marker = item.get_closest_marker("eval_category")
            if not cat_marker or target_category not in cat_marker.args:
                keep = False

        if keep and target_profile:
            prof_marker = item.get_closest_marker("serve_profile")
            if not prof_marker or target_profile not in prof_marker.args:
                keep = False

        if keep:
            selected.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(scope="session")
def langsmith_experiment_metadata(pytestconfig: pytest.Config) -> dict[str, Any]:
    """Attach Git commit SHA, model spec, and architecture metadata to LangSmith experiments."""
    git_sha = "unknown"
    with contextlib.suppress(Exception):
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    model_spec = str(
        pytestconfig.getoption("--eval-model")
        or os.environ.get("TALKOPS_EVAL_MODEL")
        or DEFAULT_EVAL_MODEL
    ).strip()

    return {
        "git_sha": git_sha,
        "agent_model": model_spec,
        "environment": os.environ.get("ENV", "local"),
        "architecture": "five-scope-agent-evals",
    }


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, Any]:
    """Scrub authentication headers from any recorded VCR / cassette files."""
    return {
        "filter_headers": [
            ("authorization", "REDACTED"),
            ("x-api-key", "REDACTED"),
            ("x-goog-api-key", "REDACTED"),
            ("x-control-plane-key", "REDACTED"),
        ],
        "record_mode": "once",
    }


@pytest.fixture(scope="session")
def eval_model_spec(pytestconfig: pytest.Config) -> str:
    """Return active subject model spec (`google_vertexai:gemini-3.8-flash` by default)."""
    cli_val = pytestconfig.getoption("--eval-model")
    return str(cli_val or os.environ.get("TALKOPS_EVAL_MODEL") or DEFAULT_EVAL_MODEL).strip()


@pytest.fixture(scope="session")
def eval_trials_k(pytestconfig: pytest.Config) -> int:
    """Return requested trial count `k` (`--eval-trials`)."""
    return max(1, int(pytestconfig.getoption("--eval-trials") or 1))


@pytest.fixture(scope="session")
def eval_judge_model(eval_model_spec: str) -> Any:
    """Instantiate fast deterministic judge & simulator `BaseChatModel` (`reasoning_effort='off'`)."""
    return create_model(eval_model_spec, extra_kwargs={"reasoning_effort": "off"}).model


@pytest.fixture
def eval_telemetry() -> EvalTelemetry:
    """Provide a fresh per-test `EvalTelemetry` recorder forwarding to `langsmith.testing`."""
    return EvalTelemetry()


@pytest.fixture
async def serve_harness_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    eval_model_spec: str,
) -> AsyncIterator[Callable[..., Any]]:
    """Spin up isolated `ServeEvalHarness` instances for any `talkops serve` profile."""
    async for factory in build_serve_harness_lifecycle(tmp_path, monkeypatch, eval_model_spec):
        yield factory
