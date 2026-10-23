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
