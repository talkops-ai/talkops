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

"""Unit tests for CodeModelRetryMiddleware and transient error resilience."""
import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from langgraph.errors import GraphBubbleUp

from talkops.middleware.model_retry import (
    MODEL_RETRY_EVENT_TYPE,
    CodeModelRetryMiddleware,
    compute_delay,
    is_transient_error,
)


class DummyExceptionWithStatus(Exception):
    """Test suite or helper class for dummy exception with status."""
    def __init__(self, message: str, status_code: int):
        """Initialize DummyExceptionWithStatus."""
        super().__init__(message)
        self.status_code = status_code


class DummyExceptionWithResponse(Exception):
    """Test suite or helper class for dummy exception with response."""
    def __init__(self, message: str, status_code: int):
        """Initialize DummyExceptionWithResponse."""
        super().__init__(message)
        self.response = MagicMock()
        self.response.status_code = status_code


def test_is_transient_error_status_codes():
    # Transient HTTP status codes
    """Verify is transient error status codes."""
    assert is_transient_error(DummyExceptionWithStatus("Rate limited", 429)) is True
    assert is_transient_error(DummyExceptionWithStatus("Server error", 500)) is True
    assert is_transient_error(DummyExceptionWithStatus("Bad gateway", 502)) is True
    assert is_transient_error(DummyExceptionWithStatus("Unavailable", 503)) is True
    assert is_transient_error(DummyExceptionWithStatus("Gateway timeout", 504)) is True
    assert is_transient_error(DummyExceptionWithStatus("Request timeout", 408)) is True
    assert is_transient_error(DummyExceptionWithStatus("Conflict", 409)) is True

    # Check via response object
    assert is_transient_error(DummyExceptionWithResponse("Too many requests", 429)) is True

    # Non-transient status codes
    assert is_transient_error(DummyExceptionWithStatus("Bad request", 400)) is False
    assert is_transient_error(DummyExceptionWithStatus("Unauthorized", 401)) is False
    assert is_transient_error(DummyExceptionWithStatus("Forbidden", 403)) is False
    assert is_transient_error(DummyExceptionWithStatus("Not found", 404)) is False


def test_is_transient_error_control_flow_and_signals():

    # Never retry control flow or termination exceptions
    """Verify is transient error control flow and signals."""
    kb_exc: Any = KeyboardInterrupt()
    sys_exc: Any = SystemExit()
    assert is_transient_error(kb_exc) is False
    assert is_transient_error(sys_exc) is False
    assert is_transient_error(GraphBubbleUp()) is False

    # String signal detection
    assert is_transient_error(Exception("Connection reset by peer")) is True
    assert is_transient_error(Exception("Server overloaded, try again later")) is True
    assert is_transient_error(Exception("API rate limit exceeded")) is True
    assert is_transient_error(Exception("Request timed out waiting for response")) is True
    assert is_transient_error(Exception("Throttling exception from AWS STS")) is True

    # Plain non-transient error
    assert is_transient_error(ValueError("Invalid syntax in argument")) is False


def test_compute_delay():
    # Delay should increase with attempts and stay within bounds
    """Verify compute delay."""
    d1 = compute_delay(1)
    d2 = compute_delay(2)
    d3 = compute_delay(3)

    assert d1 >= 0.1
    assert d2 >= d1 * 0.5  # accounting for jitter
    assert d3 <= 12.0  # max delay capped at 10.0 + jitter


def test_model_retry_middleware_success():
    """Verify model retry middleware success."""
    mw = CodeModelRetryMiddleware(max_retries=3)
    request = MagicMock()
    handler = MagicMock(return_value="success_response")

    res = mw.wrap_model_call(request, handler)
    assert res == "success_response"
    assert handler.call_count == 1


def test_model_retry_middleware_retries_and_recovers(monkeypatch):
    """Verify model retry middleware retries and recovers."""
    monkeypatch.setattr("time.sleep", lambda _: None)

    mw = CodeModelRetryMiddleware(max_retries=3)
    emitted_events = []

    mock_runtime = MagicMock()
    mock_runtime.stream_writer = lambda event: emitted_events.append(event)
    request = MagicMock()
    request.runtime = mock_runtime

    calls = 0

    def failing_then_succeeding_handler(req: Any) -> Any:
        """Provide failing then succeeding handler test helper or fixture."""
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DummyExceptionWithStatus("Rate limited", 429)
        return "recovered"

    res = mw.wrap_model_call(request, failing_then_succeeding_handler)
    assert res == "recovered"
    assert calls == 3
    assert len(emitted_events) == 2
    assert emitted_events[0]["type"] == MODEL_RETRY_EVENT_TYPE
    assert emitted_events[0]["attempt"] == 1
    assert emitted_events[1]["attempt"] == 2


def test_model_retry_middleware_exhausts_retries(monkeypatch):
    """Verify model retry middleware exhausts retries."""
    monkeypatch.setattr("time.sleep", lambda _: None)

    mw = CodeModelRetryMiddleware(max_retries=2)
    request = MagicMock()
    request.runtime = None

    handler = MagicMock(side_effect=DummyExceptionWithStatus("Rate limited", 429))

    with pytest.raises(DummyExceptionWithStatus):
        mw.wrap_model_call(request, handler)

    # 1 initial + 2 retries = 3 calls total
    assert handler.call_count == 3


def test_model_retry_middleware_non_transient_no_retry():
    """Verify model retry middleware non transient no retry."""
    mw = CodeModelRetryMiddleware(max_retries=3)
    request = MagicMock()
    handler = MagicMock(side_effect=ValueError("Non transient parameter error"))

    with pytest.raises(ValueError):
        mw.wrap_model_call(request, handler)

    assert handler.call_count == 1


@pytest.mark.asyncio
async def test_async_model_retry_middleware(monkeypatch):
    """Verify async model retry middleware."""
    async def dummy_sleep(_):
        """Provide dummy sleep test helper or fixture."""
        pass

    monkeypatch.setattr(asyncio, "sleep", dummy_sleep)

    mw = CodeModelRetryMiddleware(max_retries=2)
    request = MagicMock()
    request.runtime = None

    calls = 0

    async def async_handler(req: Any) -> Any:
        """Provide async handler test helper or fixture."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DummyExceptionWithStatus("Service unavailable", 503)
        return "async_success"

    res = await mw.awrap_model_call(request, async_handler)
    assert res == "async_success"
    assert calls == 2
