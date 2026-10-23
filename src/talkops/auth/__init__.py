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

"""TalkOps Client Authentication & Resilient Credential Storage (`talkops.auth`)."""

from __future__ import annotations

from talkops.auth.models import (
    AuthMethodType,
    LicenseTier,
    StoredSession,
    hash_api_key,
    mask_api_key,
)
from talkops.auth.oauth_acquirer import (
    BrowserAndPasteTokenAcquirer,
    LoopbackCallbackServer,
    PKCEChallenge,
    build_authorize_url,
    exchange_pkce_code_for_session,
    extract_code_from_paste,
    generate_pkce_challenge,
)
from talkops.auth.storage import (
    KEYRING_SERVICE_NAME,
    KEYRING_TIMEOUT_SECONDS,
    KEYRING_UNAVAILABLE_FILENAME,
    CompositeCredentialStorage,
    android_detector,
    container_detector,
    dbus_detector,
    get_credential_storage,
    get_keyring_unavailable_marker,
    memoize_keyring_timeout,
    should_bypass_keyring,
    ssh_detector,
    wsl_detector,
)

__all__ = [
    "AuthMethodType",
    "BrowserAndPasteTokenAcquirer",
    "CompositeCredentialStorage",
    "KEYRING_SERVICE_NAME",
    "KEYRING_TIMEOUT_SECONDS",
    "KEYRING_UNAVAILABLE_FILENAME",
    "LicenseTier",
    "LoopbackCallbackServer",
    "PKCEChallenge",
    "StoredSession",
    "android_detector",
    "build_authorize_url",
    "container_detector",
    "dbus_detector",
    "exchange_pkce_code_for_session",
    "extract_code_from_paste",
    "generate_pkce_challenge",
    "get_credential_storage",
    "get_keyring_unavailable_marker",
    "hash_api_key",
    "mask_api_key",
    "memoize_keyring_timeout",
    "should_bypass_keyring",
    "ssh_detector",
    "wsl_detector",
]

