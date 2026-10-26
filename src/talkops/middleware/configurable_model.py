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

"""Middleware for runtime model selection via LangGraph runtime context."""
import asyncio
import os
import time
from typing import Any, Awaitable, Callable

from deepagents._models import (
    get_model_identifier,
    model_matches_spec,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langgraph.types import Command

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_ANTHROPIC_ONLY_SETTINGS: set[str] = {"cache_control"}


def _get_ls_provider(model: object) -> str | None:
    """Return the provider name reported by the chat model."""
    try:
        metadata = getattr(model, "metadata", None)
        if isinstance(metadata, dict):
            configured = metadata.get("talkops_configured_provider")
            if isinstance(configured, str) and configured:
                return configured
        ls_params = getattr(model, "_get_ls_params", None)
        if ls_params:
            params = ls_params()
            if isinstance(params, dict):
                provider = params.get("ls_provider")
                if isinstance(provider, str):
                    return provider
    except Exception:
        pass
    return None


def _is_anthropic_model(model: object) -> bool:
    return _get_ls_provider(model) == "anthropic"


def _model_matches_target(
    model: Any,
    model_spec: str,
    requested_effort: str | None = None,
) -> bool:
    """Check whether a model instance matches both the target spec and reasoning effort."""
    if ":" in model_spec:
        target_provider, _, target_model_name = model_spec.partition(":")
        configured_provider = _get_ls_provider(model)
        current_id = get_model_identifier(model)
        if configured_provider and current_id:
            norm_cfg = configured_provider.lower().replace("-", "_")
            norm_tgt = target_provider.lower().replace("-", "_")
            if norm_cfg != norm_tgt or current_id != target_model_name:
                return False
        elif not model_matches_spec(model, model_spec):
            return False
    elif not model_matches_spec(model, model_spec):
        return False

    if requested_effort is not None:
        model_effort = getattr(model, "reasoning_effort", None) or getattr(model, "thinking_level", None)
        norm_req = (
            ""
            if str(requested_effort).lower() in ("off", "none", "clear", "0", "reset", "")
            else str(requested_effort).lower()
        )
        norm_cur = (
            ""
            if model_effort is None or str(model_effort).lower() in ("off", "none", "clear", "0", "reset", "")
            else str(model_effort).lower()
        )
        if norm_req != norm_cur:
            return False

    return True


def _extract_response_usage(response: Any) -> dict[str, int]:
    """Extract token usage counts from a ModelResponse or ExtendedModelResponse."""
    try:
        inner = getattr(response, "model_response", response)
        messages = getattr(inner, "result", None)
        if isinstance(messages, list):
            for msg in reversed(messages):
                usage = getattr(msg, "usage_metadata", None)
                if isinstance(usage, dict):
                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                    tot = int(usage.get("total_tokens") or (inp + out))
                    if inp or out or tot:
                        return {
                            "input_tokens": inp,
                            "output_tokens": out,
                            "total_tokens": tot,
                        }
    except Exception:
        pass
    return {}


@register_middleware(name="configurable_model")
class ConfigurableModelMiddleware(AgentMiddleware):
    """Swap the model or per-call settings from runtime.context."""

    def __init__(self, *, persist_model_state: bool = True) -> None:
        """Initialize ConfigurableModelMiddleware."""
        self._persist_model_state = persist_model_state

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap synchronous model invocation."""
        resolved_req, resolved_spec, resolved_params = self._apply_overrides(request)
        effort = (
            (resolved_params or {}).get("reasoning_effort")
            or getattr(resolved_req.model, "reasoning_effort", None)
            or getattr(resolved_req.model, "thinking_level", None)
        )
        logger.info(
            "Dispatching model call to '%s'",
            resolved_spec or "default",
            extra={
                "model": resolved_spec or "default",
                "reasoning_effort": effort,
            },
        )
        t0 = time.monotonic()
        response = handler(resolved_req)
        elapsed = time.monotonic() - t0
        usage_extra = _extract_response_usage(response)
        logger.info(
            "Completed model call to '%s' in %.2fs",
            resolved_spec or "default",
            elapsed,
            extra={
                "model": resolved_spec or "default",
                "elapsed_s": round(elapsed, 2),
                **usage_extra,
            },
        )

        command = (
            self._checkpoint_command(resolved_spec, resolved_params)
            if self._persist_model_state
            else None
        )
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap asynchronous model invocation."""
        resolved_req, resolved_spec, resolved_params = (
            await self._apply_overrides_async(request)
        )
        effort = (
            (resolved_params or {}).get("reasoning_effort")
            or getattr(resolved_req.model, "reasoning_effort", None)
            or getattr(resolved_req.model, "thinking_level", None)
        )
        logger.info(
            "Dispatching model call to '%s'",
            resolved_spec or "default",
            extra={
                "model": resolved_spec or "default",
                "reasoning_effort": effort,
            },
        )
        t0 = time.monotonic()
        response = await handler(resolved_req)
        elapsed = time.monotonic() - t0
        usage_extra = _extract_response_usage(response)
        logger.info(
            "Completed model call to '%s' in %.2fs",
            resolved_spec or "default",
            elapsed,
            extra={
                "model": resolved_spec or "default",
                "elapsed_s": round(elapsed, 2),
                **usage_extra,
            },
        )

        command = (
            self._checkpoint_command(resolved_spec, resolved_params)
            if self._persist_model_state
            else None
        )
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    def _resolve_requested_effort(
        self, model_spec: str | None, model_params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        requested_effort = model_params.get("reasoning_effort")
        if requested_effort is None:
            from talkops.config.settings import get_settings

            requested_effort = getattr(get_settings(), "reasoning_effort", None) or os.environ.get(
                "TALKOPS_REASONING_EFFORT"
            )
            if requested_effort and model_spec and model_spec not in {"auto", "dynamic"}:
                from talkops.model.reasoning import with_effort_model_params

                model_params = with_effort_model_params(model_spec, model_params, str(requested_effort))
        return (str(requested_effort) if requested_effort is not None else None), model_params

    def _apply_overrides(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:
        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        dyn_route = request.state.get("_dynamic_model_route") if isinstance(request.state, dict) else None
        is_smart = bool(
            ctx.get("smart") is True
            or str(ctx.get("approval_mode") or "").lower() == "smart"
        )
        if is_smart and isinstance(dyn_route, dict) and dyn_route.get("spec"):
            routed_spec = str(dyn_route["spec"])
            updated_request = self._build_overrides(request, None, {})
            return updated_request, routed_spec, {"reasoning_effort": dyn_route.get("effort")}

        model_spec = ctx.get("model")
        model_params = dict(ctx.get("model_params") or {})
        requested_effort, model_params = self._resolve_requested_effort(model_spec, model_params)

        model_result = None
        if (
            model_spec
            and model_spec not in {"auto", "dynamic"}
            and not _model_matches_target(request.model, model_spec, requested_effort)
        ):
            from talkops.exceptions import ModelConfigError
            from talkops.model.factory import create_model

            extra_kwargs = {"reasoning_effort": requested_effort} if requested_effort else None
            try:
                if extra_kwargs:
                    try:
                        model_result = create_model(model_spec, extra_kwargs=extra_kwargs)
                    except TypeError:
                        model_result = create_model(model_spec)
                else:
                    model_result = create_model(model_spec)
            except ModelConfigError:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model.",
                    model_spec,
                )

        updated_request = self._build_overrides(request, model_result, model_params)
        resolved_spec = self._model_spec_from_result(
            model_result, updated_request.model
        )
        return updated_request, resolved_spec, model_params

    async def _apply_overrides_async(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:

        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        dyn_route = request.state.get("_dynamic_model_route") if isinstance(request.state, dict) else None
        is_smart = bool(
            ctx.get("smart") is True
            or str(ctx.get("approval_mode") or "").lower() == "smart"
        )
        if is_smart and isinstance(dyn_route, dict) and dyn_route.get("spec"):
            routed_spec = str(dyn_route["spec"])
            updated_request = self._build_overrides(request, None, {})
            return updated_request, routed_spec, {"reasoning_effort": dyn_route.get("effort")}

        model_spec = ctx.get("model")
        model_params = dict(ctx.get("model_params") or {})
        requested_effort, model_params = self._resolve_requested_effort(model_spec, model_params)

        model_result = None
        if (
            model_spec
            and model_spec not in {"auto", "dynamic"}
            and not _model_matches_target(request.model, model_spec, requested_effort)
        ):
            from talkops.exceptions import ModelConfigError
            from talkops.model.factory import create_model

            extra_kwargs = {"reasoning_effort": requested_effort} if requested_effort else None
            try:
                if extra_kwargs:
                    try:
                        model_result = await asyncio.to_thread(
                            create_model, model_spec, extra_kwargs=extra_kwargs
                        )
                    except TypeError:
                        model_result = await asyncio.to_thread(create_model, model_spec)
                else:
                    model_result = await asyncio.to_thread(create_model, model_spec)
            except ModelConfigError:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model.",
                    model_spec,
                )

        updated_request = self._build_overrides(request, model_result, model_params)
        resolved_spec = self._model_spec_from_result(
            model_result, updated_request.model
        )
        return updated_request, resolved_spec, model_params

    def _build_overrides(
        self, request: ModelRequest, model_result: Any, model_params: dict[str, Any]
    ) -> ModelRequest:
        overrides: dict[str, Any] = {}

        new_model = model_result.model if model_result is not None else None
        if new_model is not None:
            overrides["model"] = new_model

        effective_model = new_model if new_model is not None else request.model
        if model_params:
            merged_params = {**request.model_settings, **model_params}
            provider = _get_ls_provider(effective_model) or (
                model_result.provider if model_result is not None else None
            )
            if provider in {"openai", "openai_codex", "azure_openai"} and "reasoning_effort" in merged_params:
                from talkops.model.factory import _compose_openai_reasoning_effort

                model_name = get_model_identifier(effective_model) or (
                    model_result.model_name if model_result is not None else ""
                )
                if getattr(effective_model, "use_responses_api", False) or isinstance(
                    getattr(effective_model, "reasoning", None), dict
                ):
                    merged_params.setdefault("use_responses_api", True)
                merged_params = _compose_openai_reasoning_effort(
                    provider,
                    merged_params,
                    merged_params.get("reasoning_effort"),
                    model_name=model_name or "",
                )
            overrides["model_settings"] = merged_params

        # Switch away from Anthropic -> strip Anthropic settings
        if new_model is not None and not _is_anthropic_model(new_model):
            settings = overrides.get("model_settings", request.model_settings)
            dropped = settings.keys() & _ANTHROPIC_ONLY_SETTINGS
            if dropped:
                overrides["model_settings"] = {
                    k: v for k, v in settings.items() if k not in dropped
                }

        # Patch the Model Identity section in system_prompt if model changed
        prompt = getattr(request, "system_prompt", None)
        if model_result is not None and isinstance(prompt, str):
            from talkops.prompts import MODEL_IDENTITY_RE, build_model_identity_section

            new_identity = build_model_identity_section(
                model_result.model_name,
                provider=model_result.provider,
                context_limit=model_result.context_limit,
                unsupported_modalities=getattr(model_result, "unsupported_modalities", frozenset()),
            )
            patched = MODEL_IDENTITY_RE.sub(new_identity, prompt, count=1)
            if patched != prompt:
                overrides["system_prompt"] = patched
            elif "### Model Identity" in prompt:
                logger.warning(
                    "System prompt contains '### Model Identity' but regex did not match; "
                    "identity was not updated for model '%s'.",
                    model_result.model_name,
                )

        # Sanitize cross-provider AIMessage content blocks for effective_model
        from talkops.middleware.unified_system_message import sanitize_messages_for_model

        sanitized_msgs = sanitize_messages_for_model(request.messages, effective_model)
        if sanitized_msgs is not None:
            overrides["messages"] = sanitized_msgs

        if not overrides:
            return request

        return request.override(**overrides)

    def _get_context(self, request: ModelRequest) -> dict[str, Any] | None:
        runtime = request.runtime
        if runtime is None or runtime.context is None:
            return None
        ctx = runtime.context
        if isinstance(ctx, dict):
            return ctx
        return {
            "model": getattr(ctx, "model", None),
            "model_params": getattr(ctx, "model_params", None),
            "thread_id": getattr(ctx, "thread_id", None),
            "approval_mode": getattr(ctx, "approval_mode", None),
            "smart": getattr(ctx, "smart", None),
        }

    def _model_spec_from_model(self, model: Any) -> str | None:
        provider = _get_ls_provider(model)
        model_name = get_model_identifier(model)
        if provider and model_name:
            return f"{provider}:{model_name}"
        return None

    def _model_spec_from_result(self, model_result: Any, model: Any) -> str | None:
        if (
            model_result is not None
            and model_result.provider
            and model_result.model_name
        ):
            return f"{model_result.provider}:{model_result.model_name}"
        return self._model_spec_from_model(model)

    def _checkpoint_command(
        self, model_spec: str | None, model_params: dict[str, Any] | None
    ) -> Command[Any] | None:
        update: dict[str, Any] = {}
        if model_spec:
            update["_model_spec"] = model_spec
        if model_params:
            update["_model_params"] = model_params
        if not update:
            return None
        return Command(update=update)
