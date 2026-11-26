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

"""Model-node retry middleware for TalkOps terminal & TUI execution.

Wraps model invocation with exponential backoff and jitter for transient errors:
- Rate limits (HTTP 429, ResourceExhausted)
- Timeouts (HTTP 408, 504, APITimeoutError)
- Connection drops and resets (Botocore ConnectionClosedError, Anthropic APIConnectionError, OpenAI APIConnectionError)
- Server faults (HTTP 500, 502, 503)

Emits real-time stream events so the terminal status bar updates with countdown/attempt details.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langgraph.errors import GraphBubbleUp

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL_RETRIES = 3
MODEL_RETRY_EVENT_TYPE = "model_retry"

_INITIAL_DELAY_SECONDS = 0.5
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_SECONDS = 10.0
_MAX_INTERACTIVE_TOTAL_DELAY_SECONDS = 60.0
_JITTER_FRACTION = 0.15
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})

_TRANSIENT_SDK_EXC_NAMES = frozenset(
    {
        ("anthropic", "APIConnectionError"),
        ("anthropic", "APIConnectionTimeoutError"),
        ("anthropic", "APITimeoutError"),
        ("botocore", "ConnectionClosedError"),
        ("botocore", "ConnectTimeoutError"),
        ("botocore", "EndpointConnectionError"),
        ("botocore", "ReadTimeoutError"),
        ("google", "Aborted"),
        ("google", "DeadlineExceeded"),
        ("google", "ResourceExhausted"),
        ("google", "ServiceUnavailable"),
        ("openai", "APIConnectionError"),
        ("openai", "APITimeoutError"),
        ("urllib3", "ConnectTimeoutError"),
        ("urllib3", "ReadTimeoutError"),
        ("httpx", "ConnectTimeout"),
        ("httpx", "ReadTimeout"),
        ("httpx", "NetworkError"),
    }
)


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    # Check response object
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def _is_single_exc_transient(exc: BaseException, *, is_root: bool = False) -> bool | None:
    """Classify a single exception node in the causal/group tree."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit, GraphBubbleUp)):
        return False

    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
    except ImportError:
        pass

    if not isinstance(exc, Exception):
        return None

    code = _extract_status_code(exc)
    if code is not None:
        return code in _RETRYABLE_STATUS_CODES or (500 <= code < 600)

    for base in type(exc).__mro__:
        base_mod = base.__module__.split(".")[0].lower()
        for pkg, name in _TRANSIENT_SDK_EXC_NAMES:
            if (pkg == base_mod or pkg in base.__module__.lower()) and base.__name__ == name:
                return True

    if is_root and isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    exc_str = str(exc).lower()
    transient_signals = (
        "rate limit",
        "too many requests",
        "overloaded",
        "connection reset",
        "timeout",
        "timed out",
        "time out",
        "service unavailable",
        "throttling",
    )
    if any(sig in exc_str for sig in transient_signals):
        return True
    return None


def is_transient_error(exc: Exception) -> bool:
    """Return whether the exception (or its cause/group tree) represents a transient, retryable failure."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit, GraphBubbleUp)):
        return False

    pending: list[tuple[BaseException, bool]] = [(exc, True)]
    seen: set[int] = set()
    while pending:
        current, is_root = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        verdict = _is_single_exc_transient(current, is_root=is_root)
        if verdict is not None:
            if verdict:
                return True
            continue
        if isinstance(current, BaseExceptionGroup):
            pending.extend((member, False) for member in current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append((cause, False))
    return False


def compute_delay(attempt: int) -> float:
    """Compute exponential backoff delay with jitter."""
    base = _INITIAL_DELAY_SECONDS * (_BACKOFF_FACTOR ** (attempt - 1))
    bounded = min(base, _MAX_DELAY_SECONDS)
    jitter = bounded * _JITTER_FRACTION * (random.random() * 2 - 1)
    return max(0.1, bounded + jitter)


@register_middleware(name="model_retry")
class CodeModelRetryMiddleware(AgentMiddleware):
    """Retries transient LLM failures with exponential backoff and telemetry events."""

    def __init__(self, max_retries: int = DEFAULT_MODEL_RETRIES) -> None:
        """Initialize CodeModelRetryMiddleware."""
        super().__init__()
        self.max_retries = max_retries

    def _emit_retry_event(
        self,
        request: ModelRequest,
        attempt: int,
        delay: float,
        exc: Exception,
    ) -> None:
        runtime = getattr(request, "runtime", None)
        writer = getattr(runtime, "stream_writer", None) if runtime else None
        code = _extract_status_code(exc)
        status_msg = (
            f"Transient error ({type(exc).__name__}"
            f"{f' {code}' if code else ''}); retrying attempt {attempt}/{self.max_retries} "
            f"in {delay:.1f}s..."
        )
        logger.warning(status_msg)
        if callable(writer):
            try:
                writer(
                    {
                        "type": MODEL_RETRY_EVENT_TYPE,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "delay": delay,
                        "message": status_msg,
                        "error": str(exc),
                    }
                )
            except Exception:
                pass

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Intercept and wrap synchronous model invocation."""
        attempt = 0
        total_delay = 0.0
        while True:
            try:
                return handler(request)
            except Exception as exc:
                if (
                    attempt >= self.max_retries
                    or total_delay >= _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS
                    or not is_transient_error(exc)
                ):
                    raise
                attempt += 1
                remaining = _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS - total_delay
                delay = min(compute_delay(attempt), remaining)
                total_delay += delay
                self._emit_retry_event(request, attempt, delay, exc)
                time.sleep(delay)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Intercept and wrap asynchronous model invocation."""
        attempt = 0
        total_delay = 0.0
        while True:
            try:
                return await handler(request)
            except Exception as exc:
                if (
                    attempt >= self.max_retries
                    or total_delay >= _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS
                    or not is_transient_error(exc)
                ):
                    raise
                attempt += 1
                remaining = _MAX_INTERACTIVE_TOTAL_DELAY_SECONDS - total_delay
                delay = min(compute_delay(attempt), remaining)
                total_delay += delay
                self._emit_retry_event(request, attempt, delay, exc)
                await asyncio.sleep(delay)


__all__ = [
    "DEFAULT_MODEL_RETRIES",
    "MODEL_RETRY_EVENT_TYPE",
    "CodeModelRetryMiddleware",
    "compute_delay",
    "is_transient_error",
]
