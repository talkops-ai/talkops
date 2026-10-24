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

"""LLM Model construction factory and provider detection.

Builds dynamic chat model instances for LangChain agents based on the
active model spec (``provider:model``) and reasoning effort configuration.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import os
import re
import threading
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

# Avoid duplicate protobuf descriptor registration between google-genai, google-cloud-aiplatform, and fireworks-ai
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")


class _SafeDescriptorPoolProxy:
    """DescriptorPool wrapper that tolerates duplicate .proto file registrations.

    ``fireworks-ai`` bundles generated protobuf descriptors for ``google/rpc/status.proto``,
    ``google/type/date.proto``, and ``google/longrunning/operations.proto`` which collide with
    ``googleapis-common-protos`` used by ``langchain-google-vertexai`` and ``langchain-google-genai``.
    """

    _talkops_safe_proxy = True

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def AddSerializedFile(self, serialized_file_desc_proto: bytes) -> Any:  # noqa: N802
        try:
            return self._pool.AddSerializedFile(serialized_file_desc_proto)
        except TypeError as exc:
            msg = str(exc)
            if "duplicate file name" in msg:
                dup_name = msg.split("duplicate file name", 1)[1].strip().rstrip(".")
                with contextlib.suppress(Exception):
                    return self._pool.FindFileByName(dup_name)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


_PROTO_GUARD_INSTALLED = False


def _ensure_protobuf_descriptor_pool_guard() -> None:
    """Install a safe descriptor pool proxy and pre-load canonical googleapis proto descriptors (without gRPC)."""
    global _PROTO_GUARD_INSTALLED
    if _PROTO_GUARD_INSTALLED:
        return
    _PROTO_GUARD_INSTALLED = True
    with contextlib.suppress(Exception):
        import google.protobuf.descriptor_pool as _dp

        orig_default = _dp.Default
        if not getattr(orig_default, "_talkops_patched", False):
            _pool_proxy = _SafeDescriptorPoolProxy(orig_default())

            def _safe_default() -> Any:
                return _pool_proxy

            setattr(_safe_default, "_talkops_patched", True)
            _dp.Default = _safe_default  # type: ignore[assignment]

    # Note: import operations_proto_pb2 directly rather than operations_pb2 to avoid
    # pulling in operations_grpc_pb2 -> grpc._cython.cygrpc (38.6 MB Cython extension).
    for _proto_mod in (
        "google.rpc.status_pb2",
        "google.type.date_pb2",
        "google.longrunning.operations_proto_pb2",
    ):
        with contextlib.suppress(Exception):
            importlib.import_module(_proto_mod)


class _FireworksProtoGuardFinder:
    """Meta-path hook that lazily installs the protobuf descriptor guard only if fireworks is imported."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname in ("fireworks", "langchain_fireworks") or fullname.startswith("fireworks."):
            _ensure_protobuf_descriptor_pool_guard()
        return None


if not any(type(f).__name__ == "_FireworksProtoGuardFinder" for f in sys.meta_path):
    sys.meta_path.insert(0, _FireworksProtoGuardFinder())

from langchain_core.language_models import BaseChatModel

from talkops.config.settings import get_settings, resolve_env_var
from talkops.exceptions import (
    MissingCredentialsError,
    MissingProviderPackageError,
    ModelConfigError,
    NoCredentialsConfiguredError,
)
from talkops.model.config import (
    IMPLICIT_AUTH_PROVIDERS,
    PROVIDER_KEY_ALIASES,
    PROVIDER_SETTINGS_FIELD_MAP,
    ModelConfig,
    ModelSpec,
    _is_google_api_key_value,
    apply_stored_credentials,
    canonicalize_provider,
    detect_provider,
    get_credential_env_var,
    get_model_profile,
    has_provider_credentials,
    normalize_model_spec,
    provider_package_name,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelResult:
    """Chat model instance with associated metadata."""

    model: BaseChatModel
    model_name: str
    provider: str
    context_limit: int | None = None
    unsupported_modalities: frozenset[str] = frozenset()

    def apply_to_settings(self) -> None:
        """Commit this result's metadata to the global settings singleton."""
        s = get_settings()
        s.model = self.model_name
        s.model_name = self.model_name
        s.model_provider = self.provider
        s.model_context_limit = self.context_limit
        s.model_unsupported_modalities = self.unsupported_modalities


def _get_default_model_spec() -> str:
    """Resolve the default model spec to use based on settings or configured credentials."""
    from talkops.config.settings import _load_dotenv

    _load_dotenv(refresh_loaded=True)
    config = ModelConfig.load()
    settings = get_settings()

    def _has_creds(spec: str) -> bool:
        provider = spec.split(":", 1)[0] if ":" in spec else detect_provider(spec)
        return bool(provider and has_provider_credentials(provider))

    # 1. If explicit model set in settings and has credentials
    if settings.model_name and settings.model_name not in ("auto", "dynamic") and _has_creds(settings.model_name):
        return normalize_model_spec(settings.model_name)
    if settings.model and settings.model not in ("auto", "dynamic") and _has_creds(settings.model):
        return normalize_model_spec(settings.model)

    # 2. If default / recent model set in config and has credentials
    if config.default_model and config.default_model not in ("auto", "dynamic") and _has_creds(config.default_model):
        return config.default_model
    if config.recent_model and config.recent_model not in ("auto", "dynamic") and _has_creds(config.recent_model):
        return config.recent_model

    # 3. Auto-detect from available credentials in environment
    # Note: bedrock is intentionally excluded here because
    # ambient ~/.aws/credentials are commonly present for AWS CLI / DevOps use
    # rather than explicit LLM configuration. Bedrock is used when configured
    # in [models].default / [models].recent or passed via --model.
    for prov, default_model in (
        ("anthropic", "anthropic:claude-sonnet-4-5"),
        ("openai", "openai:gpt-5.4"),
        ("openai_codex", "openai_codex:gpt-5.4"),
        ("google_genai", "google_genai:gemini-3.7-flash"),
        ("groq", "groq:llama-3.3-70b-versatile"),
        ("deepseek", "deepseek:deepseek-v4-flash"),
        ("openrouter", "openrouter:anthropic/claude-sonnet-5"),
    ):
        if has_provider_credentials(prov) is True:
            return default_model

    try:
        from talkops.state.remote_session import _can_use_disk_auth_session

        if _can_use_disk_auth_session():
            from talkops.auth.storage import get_credential_storage
            from talkops.model.config import (
                DEFAULT_PROVIDER_PRIORITY,
                get_curated_models_for_provider,
            )

            storage = get_credential_storage()
            for prov in DEFAULT_PROVIDER_PRIORITY:
                if prov in ("ollama", "dynamic"):
                    continue
                if storage.get_provider_key(prov) and has_provider_credentials(prov) is True:
                    curated = get_curated_models_for_provider(prov)
                    if curated:
                        return f"{prov}:{curated[0][0]}"
    except Exception:
        pass

    # 4. If explicit model set without credentials (e.g. testing / local provider)
    if settings.model_name and settings.model_name not in ("auto", "dynamic"):
        return normalize_model_spec(settings.model_name)
    if settings.model and settings.model not in ("auto", "dynamic"):
        return normalize_model_spec(settings.model)

    raise NoCredentialsConfiguredError(
        "No model is configured yet. Run /model to choose one."
    )


def _get_provider_kwargs(provider: str, *, model_name: str | None = None) -> dict[str, Any]:
    """Retrieve all configuration arguments for the provider/model."""
    config = ModelConfig.load()
    if provider and provider not in config.providers:
        provider = canonicalize_provider(provider)
    result = config.get_kwargs(provider, model_name=model_name)

    base_url = config.get_base_url(provider)
    if base_url:
        if provider == "azure_openai":
            result["azure_endpoint"] = base_url
            api_version = (
                resolve_env_var("AZURE_OPENAI_API_VERSION")
                or resolve_env_var("OPENAI_API_VERSION")
                or "2024-08-01-preview"
            )
            result.setdefault("api_version", api_version)
        elif provider in ("bedrock", "bedrock_converse", "anthropic_bedrock"):
            result["endpoint_url"] = base_url
            result.pop("base_url", None)
        elif provider == "ibm":
            result["url"] = base_url
            result.pop("base_url", None)
        elif provider == "litellm":
            result["api_base"] = base_url
            result.pop("base_url", None)
        elif provider == "perplexity":
            # ChatPerplexity does not accept base_url directly in kwargs; it reads PERPLEXITY_BASE_URL
            result.pop("base_url", None)
            os.environ["PERPLEXITY_BASE_URL"] = base_url
        elif provider in ("google_vertexai", "google_anthropic_vertex"):
            result["api_endpoint"] = base_url
            result.pop("base_url", None)
        elif provider == "anthropic":
            anthropic_base = base_url[:-3] if base_url.endswith("/v1") else base_url
            result["anthropic_api_url"] = anthropic_base
            result["base_url"] = anthropic_base
        else:
            result["base_url"] = base_url
            if provider == "mistralai":
                result["endpoint"] = base_url
            elif provider == "deepseek":
                result["api_base"] = base_url
            elif provider == "groq":
                result["groq_api_base"] = base_url
            elif provider == "fireworks":
                result["fireworks_api_base"] = base_url
            elif provider == "xai":
                result["xai_api_base"] = base_url

    settings = get_settings()
    env_var = get_credential_env_var(provider)
    api_key = None
    if env_var:
        api_key = resolve_env_var(env_var)

    if not api_key:
        for alias_var in PROVIDER_KEY_ALIASES.get(provider, ()):
            api_key = resolve_env_var(alias_var)
            if api_key:
                break

    if not api_key:
        for field in PROVIDER_SETTINGS_FIELD_MAP.get(provider, ()):
            if hasattr(settings, field):
                api_key = getattr(settings, field)
                if api_key:
                    break

    if not api_key:
        field_map = {
            "openai": "openai_api_key",
            "anthropic": "anthropic_api_key",
            "google_genai": "google_api_key",
            "google": "google_api_key",
            "groq": "groq_api_key",
            "deepseek": "deepseek_api_key",
            "openrouter": "openrouter_api_key",
            "mistralai": "mistral_api_key",
            "mistral": "mistral_api_key",
            "fireworks": "fireworks_api_key",
            "together": "together_api_key",
            "xai": "xai_api_key",
            "cohere": "cohere_api_key",
            "perplexity": "perplexity_api_key",
            "nvidia": "nvidia_api_key",
            "huggingface": "huggingface_api_key",
            "bedrock": "aws_access_key_id",
            "azure_openai": "azure_openai_api_key",
            "typesafe": "typesafe_api_key",
            "ibm": "watsonx_apikey",
        }
        field = field_map.get(provider)
        if field and hasattr(settings, field):
            api_key = getattr(settings, field)

    if not api_key and provider in ("google_genai", "google"):
        api_key = resolve_env_var("GEMINI_API_KEY") or getattr(settings, "google_api_key", None)
    elif not api_key and provider == "google_vertexai":
        g_key = resolve_env_var("GOOGLE_API_KEY") or getattr(settings, "google_api_key", None)
        if g_key and str(g_key).strip().startswith("AQ."):
            api_key = g_key

    if api_key and provider in ("google_genai", "google"):
        stored_genai_key = None
        with contextlib.suppress(Exception):
            from talkops.state.remote_session import _can_use_disk_auth_session

            if _can_use_disk_auth_session():
                from talkops.auth.storage import get_credential_storage

                stored_genai_key = get_credential_storage().get_provider_key("google_genai")
        if not stored_genai_key and str(api_key).strip().startswith("AQ."):
            api_key = None

    if api_key:
        if provider not in (
            "google_vertexai",
            "google_anthropic_vertex",
            "bedrock",
            "bedrock_converse",
            "anthropic_bedrock",
            "cohere",
            "perplexity",
            "ibm",
        ):
            result["api_key"] = api_key
        if provider in ("google_genai", "google"):
            result["google_api_key"] = api_key
        elif provider == "huggingface":
            result["huggingfacehub_api_token"] = api_key
        elif provider == "cohere":
            result["cohere_api_key"] = api_key
            result.pop("api_key", None)
        elif provider == "perplexity":
            result["pplx_api_key"] = api_key
            result.pop("api_key", None)
        elif provider == "ibm":
            result["apikey"] = api_key
            result.pop("api_key", None)
        elif provider == "anthropic":
            result["anthropic_api_key"] = api_key
            if base_url and "api.anthropic.com" not in base_url:
                result.setdefault("default_headers", {})["Authorization"] = f"Bearer {api_key}"
        if env_var:
            if provider in ("google_vertexai", "google_anthropic_vertex") and _is_google_api_key_value(str(api_key)):
                os.environ["VERTEXAI_API_KEY"] = str(api_key).strip()
                os.environ["GOOGLE_VERTEXAI_API_KEY"] = str(api_key).strip()
                if _is_google_api_key_value(os.environ.get("GOOGLE_CLOUD_PROJECT")):
                    os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
            else:
                os.environ[env_var] = str(api_key)

    # AWS Bedrock handling
    if provider in ("bedrock", "bedrock_converse", "anthropic_bedrock"):
        from talkops.config.aws import get_aws_model_kwargs, validate_aws_credentials

        is_valid, err = validate_aws_credentials()
        if not is_valid and err:
            logger.warning(err)
        aws_kwargs = get_aws_model_kwargs(settings)
        result.update(aws_kwargs)
        result.pop("api_key", None)
        if base_url:
            result.setdefault("endpoint_url", base_url)
            result.pop("base_url", None)

    # IBM watsonx handling
    if provider == "ibm":
        project_id = resolve_env_var("WATSONX_PROJECT_ID") or getattr(settings, "watsonx_project_id", None)
        if project_id:
            result["project_id"] = project_id
        space_id = resolve_env_var("WATSONX_SPACE_ID") or getattr(settings, "watsonx_space_id", None)
        if space_id:
            result["space_id"] = space_id
        watsonx_url = (
            config.get_base_url("ibm")
            or resolve_env_var("WATSONX_URL")
            or getattr(settings, "watsonx_url", None)
        )
        if watsonx_url:
            result["url"] = watsonx_url
        result.pop("base_url", None)

    # Google Gemini vs Vertex AI handling (strictly separated like dcode)
    if provider in ("google_genai", "google"):
        result.setdefault("vertexai", False)
    elif provider == "google_vertexai":
        vertex_api_key = (
            resolve_env_var("VERTEXAI_API_KEY")
            or resolve_env_var("GOOGLE_VERTEXAI_API_KEY")
            or (str(api_key).strip() if api_key and _is_google_api_key_value(str(api_key)) else None)
        )
        project_env = (
            resolve_env_var("GOOGLE_CLOUD_PROJECT")
            or resolve_env_var("GCP_PROJECT_ID")
            or resolve_env_var("VERTEXAI_PROJECT")
            or getattr(settings, "google_cloud_project", None)
        )
        if project_env and _is_google_api_key_value(str(project_env)):
            vertex_api_key = vertex_api_key or str(project_env).strip()
            project_env = None
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)

        if vertex_api_key and not project_env:
            result["vertexai"] = True
            result["google_api_key"] = str(vertex_api_key).strip()
            result.pop("project", None)
            result.pop("location", None)
        else:
            if project_env:
                result["project"] = project_env
                os.environ["GOOGLE_CLOUD_PROJECT"] = str(project_env)
            location_env = (
                resolve_env_var("GOOGLE_CLOUD_LOCATION")
                or resolve_env_var("GCP_LOCATION")
                or resolve_env_var("CLOUD_ML_REGION")
                or resolve_env_var("VERTEXAI_LOCATION")
                or getattr(settings, "google_cloud_location", None)
            )
            if location_env:
                result["location"] = location_env
                os.environ["GOOGLE_CLOUD_LOCATION"] = str(location_env)
    elif provider == "google_anthropic_vertex":
        project_env = (
            resolve_env_var("GOOGLE_CLOUD_PROJECT")
            or resolve_env_var("GCP_PROJECT_ID")
            or resolve_env_var("VERTEXAI_PROJECT")
            or getattr(settings, "google_cloud_project", None)
        )
        if project_env and not _is_google_api_key_value(str(project_env)):
            result["project"] = project_env
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(project_env)
        location_env = (
            resolve_env_var("GOOGLE_CLOUD_LOCATION")
            or resolve_env_var("GCP_LOCATION")
            or resolve_env_var("CLOUD_ML_REGION")
            or resolve_env_var("VERTEXAI_LOCATION")
            or getattr(settings, "google_cloud_location", None)
        )
        if location_env:
            result["location"] = location_env
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(location_env)

    # Ollama bearer token injection for remote / hosted Ollama instances
    if provider == "ollama":
        result.pop("api_key", None)
        ollama_key = resolve_env_var("OLLAMA_API_KEY")
        if ollama_key:
            client_kwargs = result.get("client_kwargs")
            if isinstance(client_kwargs, dict) or client_kwargs is None:
                client_kwargs = dict(client_kwargs) if client_kwargs else {}
                headers = client_kwargs.get("headers")
                headers = dict(headers) if isinstance(headers, dict) else {}
                if not any(isinstance(k, str) and k.lower() == "authorization" for k in headers):
                    headers["Authorization"] = f"Bearer {ollama_key}"
                    client_kwargs["headers"] = headers
                    result["client_kwargs"] = client_kwargs

    # Reasoning effort injection
    from talkops.model.reasoning import (
        is_effort_supported_for_model,
        with_effort_model_params,
    )

    effort = getattr(settings, "reasoning_effort", None)
    if effort:
        spec = f"{provider}:{model_name}" if model_name else provider
        if is_effort_supported_for_model(spec, str(effort)):
            result = with_effort_model_params(spec, result, str(effort))

    return result


_TALKOPS_PROFILES_REGISTERED = False


def _ensure_talkops_provider_profiles() -> None:
    """Register built-in ProviderProfile defaults for thinking/reasoning summaries."""
    global _TALKOPS_PROFILES_REGISTERED
    if _TALKOPS_PROFILES_REGISTERED:
        return
    try:
        from deepagents.profiles.provider import (
            ProviderProfile,
            register_provider_profile,
        )

        register_provider_profile(
            "openai",
            ProviderProfile(init_kwargs={"use_responses_api": True, "reasoning": {"summary": "auto"}}),
        )
        register_provider_profile(
            "openai_codex",
            ProviderProfile(init_kwargs={"use_responses_api": True, "reasoning": {"summary": "auto"}}),
        )
        register_provider_profile(
            "azure_openai",
            ProviderProfile(init_kwargs={"use_responses_api": True, "reasoning": {"summary": "auto"}}),
        )
        register_provider_profile(
            "google_genai",
            ProviderProfile(init_kwargs={"include_thoughts": True}),
        )
        register_provider_profile(
            "google_vertexai",
            ProviderProfile(init_kwargs={"include_thoughts": True}),
        )
        _TALKOPS_PROFILES_REGISTERED = True
    except Exception as exc:
        logger.debug("Could not register talkops provider profiles: %s", exc)


def _compose_openai_reasoning_effort(
    provider: str,
    kwargs: dict[str, Any],
    effort_override: object = None,
    reasoning_override: object = None,
    *,
    model_name: str = "",
) -> dict[str, Any]:
    """Compose a session effort override with an OpenAI reasoning mapping.

    Args:
        provider: Resolved model provider.
        kwargs: Layered model constructor parameters.
        effort_override: High-priority `reasoning_effort` from session params.
        reasoning_override: High-priority native `reasoning` from session params.
        model_name: Resolved model identifier for capability checks.

    Returns:
        Constructor parameters with one native `reasoning` mapping when
        composition is needed.
    """
    if provider not in {"openai", "openai_codex", "azure_openai"}:
        return kwargs

    from talkops.model.reasoning import supported_efforts_for_model

    spec = f"{provider}:{model_name}" if model_name else provider
    supported_efforts = supported_efforts_for_model(spec)
    composed = dict(kwargs)

    if composed.get("use_responses_api") is False:
        composed.pop("reasoning", None)
        return composed

    if model_name and not supported_efforts:
        composed.pop("reasoning_effort", None)
        composed.pop("reasoning", None)
        return composed

    effort = effort_override if isinstance(effort_override, str) else composed.get("reasoning_effort")
    if isinstance(effort, str):
        eff_lower = effort.strip().lower()
        if eff_lower in {"off", "clear", "0", "reset", ""}:
            composed.pop("reasoning_effort", None)
            composed.pop("reasoning", None)
            return composed
        if eff_lower == "none":
            composed.pop("reasoning_effort", None)
            if "none" in supported_efforts:
                composed["reasoning"] = {"effort": "none"}
            else:
                composed.pop("reasoning", None)
            return composed

    reasoning = composed.get("reasoning")
    if isinstance(reasoning_override, Mapping):
        reasoning = {**(reasoning if isinstance(reasoning, Mapping) else {}), **reasoning_override}

    if isinstance(effort, str):
        if isinstance(reasoning, Mapping):
            if not (isinstance(reasoning_override, Mapping) and "effort" in reasoning_override):
                composed["reasoning"] = {"summary": "auto", **reasoning, "effort": effort}
            else:
                composed["reasoning"] = dict(reasoning)
            composed.pop("reasoning_effort", None)
        elif composed.get("use_responses_api"):
            composed["reasoning"] = {"summary": "auto", "effort": effort}
            composed.pop("reasoning_effort", None)
    elif isinstance(reasoning, Mapping):
        composed["reasoning"] = dict(reasoning)

    return composed


def _create_model_from_class(
    class_path: str,
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Instantiate a chat model dynamically via class reflection."""
    if ":" not in class_path:
        raise ModelConfigError(f"Invalid class_path '{class_path}': must be module:Class format")

    module_path, class_name = class_path.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ModelConfigError(f"Module '{module_path}' not found for class_path '{class_path}': {e}") from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ModelConfigError(f"Class '{class_name}' not found in module '{module_path}'")

    if not (isinstance(cls, type) and issubclass(cls, BaseChatModel)):
        raise ModelConfigError(f"'{class_path}' is not a BaseChatModel subclass")

    cls_kwargs = dict(kwargs)

    cls_any = cast(Any, cls)
    try:
        return cls_any(model=model_name, **cls_kwargs)
    except TypeError:
        return cls_any(model_name=model_name, **cls_kwargs)


def _create_model_via_init(
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Delegate to langchain's dynamic init_chat_model function."""
    if provider == "fireworks":
        _ensure_protobuf_descriptor_pool_guard()
    from langchain.chat_models import init_chat_model

    init_kwargs = dict(kwargs)

    target_provider = provider
    vertex_express_api_key: str | None = None
    if provider in ("openai_codex", "typesafe"):
        target_provider = "openai"
    elif provider == "google_vertexai" and (
        init_kwargs.get("vertexai") is True or init_kwargs.get("google_api_key")
    ):
        target_provider = "google_genai"
        init_kwargs["vertexai"] = True
        init_kwargs.pop("project", None)
        init_kwargs.pop("location", None)
        if init_kwargs.get("google_api_key"):
            vertex_express_api_key = str(init_kwargs["google_api_key"]).strip()
    elif provider in ("cerebras", "sambanova"):
        try:
            has_dedicated_pkg = importlib.util.find_spec(f"langchain_{provider}") is not None
        except (ImportError, ValueError):
            has_dedicated_pkg = False
        if not has_dedicated_pkg:
            target_provider = "openai"

    if target_provider == "huggingface":
        try:
            from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

            hf_kwargs = dict(init_kwargs)
            token = hf_kwargs.pop("huggingfacehub_api_token", None) or hf_kwargs.pop("api_key", None)
            endpoint_url = hf_kwargs.pop("endpoint_url", None) or hf_kwargs.pop("base_url", None)
            ep_args: dict[str, Any] = {}
            if token:
                ep_args["huggingfacehub_api_token"] = token
            if endpoint_url:
                ep_args["endpoint_url"] = endpoint_url
            else:
                ep_args["repo_id"] = model_name
            endpoint = HuggingFaceEndpoint(**ep_args, **hf_kwargs)
            return ChatHuggingFace(llm=endpoint)
        except Exception as hf_err:
            logger.debug("HuggingFaceEndpoint initialization skipped (%s); trying standard init", hf_err)

    saved_env: dict[str, str | None] = {}
    if vertex_express_api_key:
        for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
            saved_env[k] = os.environ.get(k)
        os.environ["GOOGLE_API_KEY"] = vertex_express_api_key
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GOOGLE_CLOUD_LOCATION", None)

    try:
        if target_provider:
            model = init_chat_model(model_name, model_provider=target_provider, **init_kwargs)
        else:
            model = init_chat_model(model_name, **init_kwargs)
        if vertex_express_api_key:
            _configure_vertex_express_model(model, vertex_express_api_key)
        return model
    except ImportError as e:
        package = provider_package_name(provider)
        module_name = package.replace("-", "_")
        try:
            spec_found = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):
            spec_found = False
        if spec_found:
            raise ModelConfigError(
                f"Provider package '{package}' is installed but failed to import for provider '{provider}': {e}"
            ) from e
        raise MissingProviderPackageError(
            f"Missing package for '{provider}'. Install: pip install {package}",
            provider=provider,
            package=package,
        ) from e
    except (ValueError, TypeError) as e:
        raise ModelConfigError(f"Invalid configuration for '{provider}:{model_name}': {e}") from e
    finally:
        if vertex_express_api_key:
            for k, old_val in saved_env.items():
                if k == "GOOGLE_CLOUD_PROJECT" and _is_google_api_key_value(old_val):
                    os.environ.pop(k, None)
                elif old_val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_val


_MODEL_CACHE: dict[str, ModelResult] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_ANTHROPIC_CACHE_CONTROL_PATCHED = False
_OLLAMA_MESSAGE_COMPAT_PATCHED = False
_VERTEX_EXPRESS_PROJECT_CACHE: dict[str, str] = {}
_VERTEX_EXPRESS_CLIENT_PATCHED = False
_VERTEX_PROBE_LOG_FILTER_INSTALLED = False
_VERTEX_PROJECT_PATH_RE = re.compile(r"projects/([0-9A-Za-z_-]+)/locations/([0-9A-Za-z_-]+)/publishers/")


class _VertexProbeLogFilter(logging.Filter):
    """Suppress httpx INFO log lines for internal Vertex AI Express project-discovery probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "gemini-2.5-flash-probe" not in record.getMessage()


def _ensure_vertex_probe_log_filter() -> None:
    global _VERTEX_PROBE_LOG_FILTER_INSTALLED
    if _VERTEX_PROBE_LOG_FILTER_INSTALLED:
        return
    logging.getLogger("httpx").addFilter(_VertexProbeLogFilter())
    _VERTEX_PROBE_LOG_FILTER_INSTALLED = True


def _resolve_vertex_express_project(api_key: str) -> str | None:
    """Resolve the underlying GCP project number for a Vertex AI Express Mode API key ('AQ...').

    Google's Vertex AI Express Mode edge gateway routes bare ``publishers/google/models/...``
    requests to regional backends (e.g. ``us-central1`` or ``asia-southeast1``) where global-only
    models such as ``gemini-4-argon`` fail after ~11.7s with a 404 Not Found error.
    Including ``projects/<project>/locations/global/`` in the request path routes directly
    to the global endpoint while still authenticating via ``x-goog-api-key``.
    """
    key = api_key.strip()
    if not key:
        return None
    cached = _VERTEX_EXPRESS_PROJECT_CACHE.get(key)
    if cached:
        return cached
    env_proj = os.environ.get("TALKOPS_VERTEX_EXPRESS_PROJECT", "").strip()
    if env_proj and not _is_google_api_key_value(env_proj):
        _VERTEX_EXPRESS_PROJECT_CACHE[key] = env_proj
        return env_proj
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        import httpx

        _ensure_vertex_probe_log_filter()
        resp = httpx.post(
            "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-2.5-flash-probe:countTokens",
            headers={"x-goog-api-key": key},
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            timeout=3.0,
        )
        m = _VERTEX_PROJECT_PATH_RE.search(resp.text)
        if m:
            proj = m.group(1)
            _VERTEX_EXPRESS_PROJECT_CACHE[key] = proj
            os.environ["TALKOPS_VERTEX_EXPRESS_PROJECT"] = proj
            return proj
    except Exception as exc:
        logger.debug("Could not pre-resolve Vertex AI Express project number: %s", exc)
    return None


def _ensure_vertex_express_api_client_patch() -> None:
    """Install fallback URL rewrite on google.genai BaseApiClient for Vertex AI Express Mode 404s."""
    global _VERTEX_EXPRESS_CLIENT_PATCHED
    if _VERTEX_EXPRESS_CLIENT_PATCHED:
        return
    try:
        from google.genai import _api_client as _genai_api_client
        from google.genai import errors as _genai_errors
    except ImportError:
        return

    base_cls = getattr(_genai_api_client, "BaseApiClient", None)
    if base_cls is None:
        return

    orig_sync = getattr(base_cls, "_request_once", None)
    orig_async = getattr(base_cls, "_async_request_once", None)

    def _rewrite_express_url_on_404(client_self: Any, http_request: Any, exc: Exception) -> bool:
        if not (getattr(client_self, "vertexai", False) and getattr(client_self, "api_key", None)):
            return False
        if getattr(exc, "code", None) != 404:
            return False
        url = str(getattr(http_request, "url", "") or "")
        msg = str(exc)
        m = _VERTEX_PROJECT_PATH_RE.search(msg)
        proj = m.group(1) if m else getattr(client_self, "project", None)
        if proj and not _is_google_api_key_value(str(proj)):
            client_self.project = str(proj)
            if not getattr(client_self, "location", None):
                client_self.location = "global"
            api_key_str = str(getattr(client_self, "api_key", "")).strip()
            if api_key_str:
                _VERTEX_EXPRESS_PROJECT_CACHE[api_key_str] = str(proj)
                os.environ["TALKOPS_VERTEX_EXPRESS_PROJECT"] = str(proj)

        if "/projects/" not in url and "/publishers/" in url and proj:
            loc = getattr(client_self, "location", None) or "global"
            http_request.url = url.replace(
                "/publishers/",
                f"/projects/{proj}/locations/{loc}/publishers/",
                1,
            )
            return True
        if "/locations/global/publishers/" in url:
            http_request.url = re.sub(
                r"/projects/[^/]+/locations/global/publishers/",
                "/publishers/",
                url,
                count=1,
            )
            return True
        return False

    if orig_sync is not None and not getattr(orig_sync, "_talkops_vertex_express_patched", False):
        def _patched_request_once(
            self: Any,
            http_request: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            try:
                return orig_sync(self, http_request, *args, **kwargs)
            except _genai_errors.ClientError as exc:
                if _rewrite_express_url_on_404(self, http_request, exc):
                    return orig_sync(self, http_request, *args, **kwargs)
                raise

        setattr(_patched_request_once, "_talkops_vertex_express_patched", True)
        base_cls._request_once = _patched_request_once  # type: ignore[method-assign]

    if orig_async is not None and not getattr(orig_async, "_talkops_vertex_express_patched", False):
        async def _patched_async_request_once(
            self: Any,
            http_request: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            try:
                return await orig_async(self, http_request, *args, **kwargs)
            except _genai_errors.ClientError as exc:
                if _rewrite_express_url_on_404(self, http_request, exc):
                    return await orig_async(self, http_request, *args, **kwargs)
                raise

        setattr(_patched_async_request_once, "_talkops_vertex_express_patched", True)
        base_cls._async_request_once = _patched_async_request_once  # type: ignore[method-assign]

    _VERTEX_EXPRESS_CLIENT_PATCHED = True


def _configure_vertex_express_model(model: BaseChatModel, api_key: str) -> None:
    """Configure ChatGoogleGenerativeAI's underlying BaseApiClient for Vertex AI Express Mode."""
    _ensure_vertex_express_api_client_patch()
    proj = _resolve_vertex_express_project(api_key)
    if not proj:
        return
    for client_attr in ("client", "async_client"):
        client_obj = getattr(model, client_attr, None)
        api_client = getattr(client_obj, "_api_client", None) if client_obj is not None else None
        if api_client is not None and getattr(api_client, "vertexai", False) and getattr(api_client, "api_key", None):
            api_client.project = proj
            if not getattr(api_client, "location", None):
                api_client.location = "global"


def _sanitize_content_for_ollama(content: Any) -> Any:
    """Normalize list-based message content so ChatOllama never raises on non-text/image blocks."""
    if not isinstance(content, list):
        return content

    from langchain_core.messages import is_data_content_block

    cleaned: list[Any] = []
    for part in content:
        if isinstance(part, str):
            cleaned.append(part)
        elif isinstance(part, dict):
            ptype = part.get("type")
            if ptype == "text":
                cleaned.append({"type": "text", "text": str(part.get("text", ""))})
            elif ptype in ("tool_use", "image_url"):
                cleaned.append(part)
            elif ptype == "non_standard":
                val = part.get("value")
                if val is not None:
                    cleaned.append({"type": "text", "text": str(val)})
            elif ptype in (
                "thinking",
                "redacted_thinking",
                "reasoning",
                "reasoning_content",
                "thought",
                "tool_call",
                "tool_call_chunk",
                "function_call",
                "server_tool_use",
                "web_search_tool_result",
                "code_execution_tool_result",
                "mcp_tool_use",
                "mcp_tool_result",
            ):
                # Drop provider-specific reasoning/tool blocks; tool_calls are already on AIMessage.tool_calls
                continue
            elif is_data_content_block(part) and part.get("type") == "image":
                cleaned.append(part)
            elif "text" in part and isinstance(part.get("text"), str):
                cleaned.append({"type": "text", "text": part["text"]})
            # Drop any other unsupported block types rather than crashing ChatOllama
    return cleaned


def _ensure_ollama_message_compatibility(model: BaseChatModel | None = None) -> None:
    """Ensure ChatOllama safely handles cross-provider message content blocks (thinking, reasoning, etc.)."""
    global _OLLAMA_MESSAGE_COMPAT_PATCHED
    if _OLLAMA_MESSAGE_COMPAT_PATCHED:
        return
    try:
        import langchain_ollama.chat_models as _ollama_cm
    except ImportError:
        return

    orig_convert = getattr(_ollama_cm.ChatOllama, "_convert_messages_to_ollama_messages", None)
    if orig_convert is not None and not getattr(orig_convert, "_talkops_compat_patched", False):
        def _patched_convert_messages_to_ollama_messages(
            self: Any,
            messages: list[Any],
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            sanitized: list[Any] = []
            for msg in messages:
                content = getattr(msg, "content", None)
                add_kwargs = getattr(msg, "additional_kwargs", None)
                updates: dict[str, Any] = {}
                if isinstance(content, list):
                    new_content = _sanitize_content_for_ollama(content)
                    if new_content != content:
                        updates["content"] = new_content
                if isinstance(add_kwargs, dict) and "reasoning_content" in add_kwargs:
                    rc = add_kwargs.get("reasoning_content")
                    if rc is not None and not isinstance(rc, str):
                        new_ak = dict(add_kwargs)
                        new_ak.pop("reasoning_content", None)
                        updates["additional_kwargs"] = new_ak
                if updates and hasattr(msg, "model_copy"):
                    sanitized.append(msg.model_copy(update=updates))
                else:
                    sanitized.append(msg)
            return orig_convert(self, sanitized, *args, **kwargs)

        setattr(_patched_convert_messages_to_ollama_messages, "_talkops_compat_patched", True)
        _ollama_cm.ChatOllama._convert_messages_to_ollama_messages = (  # type: ignore[method-assign]
            _patched_convert_messages_to_ollama_messages
        )

    _OLLAMA_MESSAGE_COMPAT_PATCHED = True


def _ensure_anthropic_block_level_cache_control(model: BaseChatModel | None = None) -> None:
    """Ensure ChatAnthropic expands top-level `cache_control` into block-level breakpoints.

    `AnthropicPromptCachingMiddleware` sets `model_settings={"cache_control": ...}`
    expecting `ChatAnthropic._get_request_payload` to translate it for the transport.
    By default, `ChatAnthropic` only expands `cache_control` into a block-level
    breakpoint when `_is_direct_anthropic_llm_type` is `False`, leaving a top-level
    `cache_control` field in the JSON body for `ChatAnthropic` instances even when
    routed through Anthropic-compatible gateways/proxies (which reject top-level
    `cache_control` with `400 - cache_control: Extra inputs are not permitted`).
    Block-level `cache_control` breakpoints are universally accepted by both direct
    Anthropic APIs and all gateways.
    """
    global _ANTHROPIC_CACHE_CONTROL_PATCHED
    try:
        import langchain_anthropic.chat_models as _anth_cm
    except ImportError:
        return

    if not _ANTHROPIC_CACHE_CONTROL_PATCHED:
        _anth_cm._is_direct_anthropic_llm_type = lambda _llm_type: False  # type: ignore[assignment]
        orig_get_payload = _anth_cm.ChatAnthropic._get_request_payload

        if not getattr(orig_get_payload, "_talkops_cache_patched", False):
            def _patched_get_request_payload(
                self: Any,
                input_: Any,
                *args: Any,
                stop: list[str] | None = None,
                **kwargs: Any,
            ) -> dict[str, Any]:
                payload = orig_get_payload(self, input_, *args, stop=stop, **kwargs)
                top_cc = payload.pop("cache_control", None)
                if top_cc and isinstance(payload.get("messages"), list) and payload["messages"]:
                    tool_ids = _anth_cm._collect_code_execution_tool_ids(payload["messages"])
                    _anth_cm._apply_cache_control_to_last_eligible_block(
                        payload["messages"], top_cc, tool_ids
                    )
                return payload

            setattr(_patched_get_request_payload, "_talkops_cache_patched", True)
            _anth_cm.ChatAnthropic._get_request_payload = _patched_get_request_payload  # type: ignore[method-assign]

        _ANTHROPIC_CACHE_CONTROL_PATCHED = True

    if model is not None and isinstance(model, _anth_cm.ChatAnthropic):
        if isinstance(getattr(model, "model_kwargs", None), dict):
            model.model_kwargs.pop("cache_control", None)


def clear_model_cache() -> None:
    """Clear cached chat model instances."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


def create_model(
    model_spec: str | None = None,
    *,
    extra_kwargs: dict[str, Any] | None = None,
    profile_overrides: dict[str, Any] | None = None,
) -> ModelResult:
    """Factory function to build a BaseChatModel and return ModelResult."""
    if not model_spec:
        model_spec = _get_default_model_spec()

    settings_effort = getattr(get_settings(), "reasoning_effort", None) or os.environ.get("TALKOPS_REASONING_EFFORT")
    cache_key = json.dumps(
        {
            "spec": model_spec,
            "extra": extra_kwargs or {},
            "profile": profile_overrides or {},
            "settings_effort": settings_effort,
        },
        sort_keys=True,
        default=str,
    )
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

    inferred_provider = detect_provider(model_spec)
    parsed = ModelSpec.try_parse(model_spec)
    config = ModelConfig.load()
    if parsed and parsed.provider in config.providers:
        provider = parsed.provider
        model_name = parsed.model
    elif inferred_provider == "bedrock":
        provider = inferred_provider
        model_name = parsed.model if (parsed and canonicalize_provider(parsed.provider) == "bedrock") else model_spec
    elif parsed:
        provider = canonicalize_provider(parsed.provider)
        model_name = parsed.model
    else:
        model_name = model_spec
        provider = canonicalize_provider(inferred_provider) if inferred_provider else ""

    if provider == "google_vertexai" and model_name and model_name.lower().startswith("claude-"):
        raise ModelConfigError(
            f"Claude model '{model_name}' on Google Vertex AI requires provider 'google_anthropic_vertex' instead of 'google_vertexai' (e.g. google_anthropic_vertex:{model_name})."
        )

    if provider:
        apply_stored_credentials(provider)

    if provider and provider not in IMPLICIT_AUTH_PROVIDERS:
        cred_status = has_provider_credentials(provider)
        if cred_status is False:
            env_var = get_credential_env_var(provider)
            raise MissingCredentialsError(
                f"No credentials configured for provider '{provider}'. Set environment variable {env_var} or configure it in Settings.",
                provider=provider,
                env_var=env_var,
            )

    kwargs = _get_provider_kwargs(provider, model_name=model_name)
    if provider:
        _ensure_talkops_provider_profiles()
        from deepagents.profiles.provider import apply_provider_profile

        spec = f"{provider}:{model_name}" if model_name else provider
        try:
            kwargs = apply_provider_profile(spec, kwargs)
        except Exception as exc:
            logger.debug("ProviderProfile resolution for %r failed: %s", spec, exc)

        base_url_val = str(kwargs.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "")
        if provider == "openai" and (
            os.environ.get("TALKOPS_DISABLE_RESPONSES_API") == "1"
            or (base_url_val and "api.openai.com" not in base_url_val)
        ):
            kwargs["use_responses_api"] = False
            kwargs.pop("reasoning", None)

    reasoning_effort_override: object = None
    reasoning_override: object = None
    if extra_kwargs:
        extra_kwargs = dict(extra_kwargs)
        reasoning_effort_override = extra_kwargs.pop("reasoning_effort", None)
        reasoning_override = extra_kwargs.pop("reasoning", None)
        eff = reasoning_effort_override
        if eff is not None:
            from talkops.model.reasoning import (
                is_effort_supported_for_model,
                with_effort_model_params,
            )

            spec = f"{provider}:{model_name}" if model_name else (model_name or "")
            eff_str = str(eff)
            if eff_str.lower() in ("off", "none", "clear", "0", "reset", "") or is_effort_supported_for_model(spec, eff_str):
                kwargs = with_effort_model_params(spec, kwargs, eff_str)
        kwargs.update(extra_kwargs)

    kwargs = _compose_openai_reasoning_effort(
        provider,
        kwargs,
        reasoning_effort_override,
        reasoning_override,
        model_name=model_name or "",
    )

    if provider == "anthropic":
        # Ensure fixed-budget 'enabled' thinking is never sent for Claude 5+ models
        thinking = kwargs.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            match = re.match(r"^claude-[a-z]+-(\d+)", model_name.lower())
            if match is not None and int(match.group(1)) >= 5:
                kwargs.pop("thinking", None)

    # Sanitize provider-native models that disallow non-literal reasoning_effort strings (e.g. 'off')
    if kwargs.get("reasoning_effort") in ("off", "none", "clear", "0", "reset"):
        kwargs.pop("reasoning_effort", None)
    if provider in ("google_genai", "google", "google_vertexai"):
        spec = f"{provider}:{model_name}" if model_name else (provider or "")
        from talkops.model.reasoning import supported_efforts_for_model

        eff_override_str = str(reasoning_effort_override).lower() if reasoning_effort_override is not None else ""
        explicitly_disabled = eff_override_str in ("off", "none", "clear", "0", "reset")
        prof_entry = get_model_profile(spec)
        supports_thoughts = bool(supported_efforts_for_model(spec)) or bool(
            (prof_entry.get("profile", {}) if prof_entry else {}).get("reasoning_output")
        )
        if not supported_efforts_for_model(spec) or not kwargs.get("reasoning_effort") or explicitly_disabled:
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("thinking_level", None)
            kwargs.pop("thinking_budget", None)
        if explicitly_disabled or not supports_thoughts:
            kwargs.pop("include_thoughts", None)
        with contextlib.suppress(Exception):
            from google.genai import models as _genai_models

            _genai_models.Models._logged_afc_warning = True
            _genai_models.AsyncModels._logged_afc_warning = True

    config = ModelConfig.load()
    class_path = config.get_class_path(provider) if provider else None

    resolved_cache_key = json.dumps(
        {
            "provider": provider,
            "model": model_name,
            "class_path": class_path,
            "kwargs": kwargs,
            "profile": profile_overrides or {},
        },
        sort_keys=True,
        default=str,
    )
    with _MODEL_CACHE_LOCK:
        cached_resolved = _MODEL_CACHE.get(resolved_cache_key)
        if cached_resolved is not None:
            _MODEL_CACHE[cache_key] = cached_resolved
            return cached_resolved

    if provider == "fireworks":
        _ensure_protobuf_descriptor_pool_guard()

    logger.info(
        "Instantiating ChatModel '%s' (provider=%s)",
        model_name,
        provider,
        extra={
            "model": model_name,
            "provider": provider,
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "temperature": kwargs.get("temperature", 0.0),
        },
    )

    if class_path:
        model = _create_model_from_class(class_path, model_name, provider, kwargs)
    else:
        model = _create_model_via_init(model_name, provider, kwargs)

    model_mod = type(model).__module__
    if provider in ("anthropic", "google_anthropic_vertex", "anthropic_bedrock") or "anthropic" in model_mod:
        _ensure_anthropic_block_level_cache_control(model)
    if provider == "ollama" or "ollama" in model_mod:
        _ensure_ollama_message_compatibility(model)
    if provider:
        with contextlib.suppress(Exception):
            from talkops.middleware.cost_tracking import _set_configured_provider_metadata

            _set_configured_provider_metadata(model, provider)

    # Resolve model profile & context limits
    spec_key = f"{provider}:{model_name}" if provider else model_name
    prof_entry = get_model_profile(spec_key)
    profile: dict[str, Any] = dict(prof_entry["profile"]) if prof_entry else {}
    toml_profile_overrides = config.get_profile_overrides(provider, model_name=model_name)
    if toml_profile_overrides:
        profile.update(toml_profile_overrides)
    if profile_overrides:
        profile.update(cast(dict[str, Any], profile_overrides))

    context_limit = profile.get("max_input_tokens")
    unsupported_modalities = frozenset(profile.get("unsupported_modalities", []))

    if profile:
        try:
            setattr(model, "profile", dict(profile))
        except Exception:
            with contextlib.suppress(Exception):
                setattr(type(model), "profile", property(lambda self: dict(profile)))

    result = ModelResult(
        model=model,
        model_name=model_name,
        provider=provider or getattr(model, "_model_provider", ""),
        context_limit=context_limit,
        unsupported_modalities=unsupported_modalities,
    )
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[cache_key] = result
        _MODEL_CACHE[resolved_cache_key] = result
    return result
