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

"""Estimate model cost using ``genai-prices`` in TalkOps.

Provides token-level and session-level pricing calculations for supported
model providers (Anthropic, OpenAI, Google, Bedrock, Azure, Mistral, xAI).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

SESSION_COST_EVENT_TYPE = "session_cost"
"""Custom-stream event type carrying the thread's absolute cumulative cost."""

_PROVIDER_ALIASES: dict[str, str] = {
    "azure_openai": "azure",
    "bedrock": "aws",
    "google_genai": "google",
    "google_vertexai": "google",
    "mistralai": "mistral",
    "xai": "x-ai",
}

_UNPRICEABLE_PROVIDERS: frozenset[str] = frozenset({"openai_codex"})


class TokenCostEstimator:
    """Calculates granular pricing estimates for model completions."""

    _pricing_unavailable: bool = False
    _pricing_contract_broken: bool = False

    @classmethod
    def is_available(cls) -> bool:
        """Report whether pricing engine is currently functional."""
        return not (cls._pricing_unavailable or cls._pricing_contract_broken)

    @classmethod
    def _load_pricing_backend(cls) -> tuple[Any, Any] | None:
        """Lazily load genai-prices calculation backend."""
        try:
            from genai_prices import (  # type: ignore[import-not-found,import-untyped]
                Usage,
                calc_price,
            )
        except Exception:
            if not cls._pricing_unavailable:
                logger.warning(
                    "Could not load genai-prices; cost estimates are unavailable for this session.",
                    exc_info=True,
                )
            cls._pricing_unavailable = True
            return None
        cls._pricing_unavailable = False
        return Usage, calc_price

    @staticmethod
    def _token_count(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    @classmethod
    def _cache_write_counts(cls, details: Mapping[str, Any]) -> tuple[int, int, int]:
        five_minute = cls._token_count(details.get("ephemeral_5m_input_tokens"))
        one_hour = cls._token_count(details.get("ephemeral_1h_input_tokens"))
        if five_minute or one_hour:
            return 0, five_minute, one_hour
        generic = cls._token_count(details.get("cache_creation")) or cls._token_count(details.get("cache_write"))
        return generic, 0, 0

    @classmethod
    def _clamp_cache_counts(
        cls,
        input_tokens: int,
        cache_read: int,
        cache_writes: tuple[int, int, int],
    ) -> tuple[int, tuple[int, int, int]]:
        clamped_read = min(cache_read, input_tokens)
        remaining = input_tokens - clamped_read
        clamped_writes: list[int] = []
        for count in cache_writes:
            w = min(count, remaining)
            clamped_writes.append(w)
            remaining -= w
        return clamped_read, (clamped_writes[0], clamped_writes[1], clamped_writes[2])

    @classmethod
    def estimate_cost(
        cls,
        model_name: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        *,
        input_token_details: Mapping[str, Any] | None = None,
        output_token_details: Mapping[str, Any] | None = None,
    ) -> float | None:
        """Calculate estimated cost in USD for a single model completion."""
        if not model_name or not provider:
            return None
        if provider in _UNPRICEABLE_PROVIDERS:
            return None

        backend = cls._load_pricing_backend()
        if backend is None:
            return None
        usage_cls, calc_price_fn = backend

        provider_key = provider.strip().lower()
        norm_provider = _PROVIDER_ALIASES.get(provider_key, provider_key) or None
        in_details = input_token_details or {}
        out_details = output_token_details or {}

        cache_read = cls._token_count(in_details.get("cache_read"))
        raw_writes = cls._cache_write_counts(in_details)
        cache_read, (generic_write, cache_5m, cache_1h) = cls._clamp_cache_counts(input_tokens, cache_read, raw_writes)
        cache_write = generic_write or (cache_5m + cache_1h)
        input_audio = min(cls._token_count(in_details.get("audio")), input_tokens)
        if input_audio and (cache_read or cache_write or cache_5m or cache_1h):
            input_audio = 0

        reasoning_tokens = cls._token_count(out_details.get("reasoning"))
        if provider_key == "perplexity":
            output_tokens += reasoning_tokens
        output_audio = min(cls._token_count(out_details.get("audio")), output_tokens)
        reasoning_tokens = min(reasoning_tokens, output_tokens)

        try:
            usage = usage_cls(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read or None,
                cache_write_tokens=cache_write or None,
                cache_write_5m_tokens=cache_5m or None,
                cache_write_1h_tokens=cache_1h or None,
                input_audio_tokens=input_audio or None,
                output_audio_tokens=output_audio or None,
                output_reasoning_tokens=reasoning_tokens or None,
            )
        except Exception as exc:
            cls._pricing_contract_broken = True
            logger.debug("genai-prices rejected usage schema for %s:%s: %s", provider, model_name, exc)
            return None
        cls._pricing_contract_broken = False

        try:
            try:
                price_result = calc_price_fn(usage, model_ref=model_name.strip(), provider_id=norm_provider)
            except TypeError:
                price_result = calc_price_fn(usage, model=model_name.strip(), provider=norm_provider)
            if price_result is None:
                return None
            total_cost = getattr(price_result, "total_price", None)
            if total_cost is None:
                total_cost = getattr(price_result, "total_cost", None)
            if total_cost is not None:
                cost_val = float(total_cost)
                if math.isfinite(cost_val) and cost_val >= 0:
                    return cost_val
            return None
        except Exception as exc:
            logger.debug("Cost estimation failed for %s:%s: %s", provider, model_name, exc)
            return None


def estimate_cost(
    model_name: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    *,
    input_token_details: Mapping[str, Any] | None = None,
    output_token_details: Mapping[str, Any] | None = None,
) -> float | None:
    """Convenience function for estimating completion cost."""
    return TokenCostEstimator.estimate_cost(
        model_name,
        provider,
        input_tokens,
        output_tokens,
        input_token_details=input_token_details,
        output_token_details=output_token_details,
    )


__all__ = [
    "SESSION_COST_EVENT_TYPE",
    "TokenCostEstimator",
    "estimate_cost",
]
