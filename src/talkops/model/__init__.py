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

"""Model creation and configuration package for talkops."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.model.config import (
        MODEL_PROFILES,
        PROVIDER_API_KEY_ENV,
        PROVIDER_BASE_URL_ENV,
        ModelConfig,
        ModelSpec,
        apply_stored_credentials,
        clear_default_model,
        detect_provider,
        load_default_model,
        normalize_model_spec,
        save_default_model,
    )
    from talkops.model.factory import (
        ModelResult,
        clear_model_cache,
        create_model,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "MODEL_PROFILES": ("talkops.model.config", "MODEL_PROFILES"),
    "PROVIDER_API_KEY_ENV": ("talkops.model.config", "PROVIDER_API_KEY_ENV"),
    "PROVIDER_BASE_URL_ENV": ("talkops.model.config", "PROVIDER_BASE_URL_ENV"),
    "ModelConfig": ("talkops.model.config", "ModelConfig"),
    "ModelSpec": ("talkops.model.config", "ModelSpec"),
    "apply_stored_credentials": ("talkops.model.config", "apply_stored_credentials"),
    "clear_default_model": ("talkops.model.config", "clear_default_model"),
    "detect_provider": ("talkops.model.config", "detect_provider"),
    "load_default_model": ("talkops.model.config", "load_default_model"),
    "normalize_model_spec": ("talkops.model.config", "normalize_model_spec"),
    "save_default_model": ("talkops.model.config", "save_default_model"),
    "ModelResult": ("talkops.model.factory", "ModelResult"),
    "clear_model_cache": ("talkops.model.factory", "clear_model_cache"),
    "create_model": ("talkops.model.factory", "create_model"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ModelConfig",
    "ModelSpec",
    "ModelResult",
    "apply_stored_credentials",
    "clear_default_model",
    "clear_model_cache",
    "create_model",
    "detect_provider",
    "load_default_model",
    "normalize_model_spec",
    "save_default_model",
    "PROVIDER_API_KEY_ENV",
    "PROVIDER_BASE_URL_ENV",
    "MODEL_PROFILES",
]
