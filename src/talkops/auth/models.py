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

"""Data models for TalkOps client authentication, sessions, and token leases."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LicenseTier(StrEnum):
    """Subscription or organization tier returned by the Control Plane."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class AuthMethodType(StrEnum):
    """Authentication mechanism used to establish the current session."""

    API_KEY = "api_key"
    GATEWAY = "gateway"
    LICENSE_KEY = "license_key"
    KEYRING_SESSION = "keyring_session"
    OAUTH_PKCE = "oauth_pkce"
    WIF_SSO = "wif_sso"


def mask_api_key(raw_key: str | None) -> str:
    """Return a safe, masked prefix representation of an API key for display/storage.

    Examples:
        `oc_live_k9x2m4p1_secret123456` -> `oc_live_k9x2m4p1_***`
        `sk-ant-api03-abcdef123456789` -> `sk-ant-a***6789`
    """
    if not raw_key:
        return ""
    cleaned = raw_key.strip()
    if not cleaned:
        return ""
    if cleaned.startswith(("oc_live_", "oc_test_")):
        parts = cleaned.split("_")
        if len(parts) >= 4:
            return f"{parts[0]}_{parts[1]}_{parts[2]}_***"
    if len(cleaned) <= 8:
        return "***"
    return f"{cleaned[:8]}***{cleaned[-4:]}"


def hash_api_key(raw_key: str | None) -> str:
    """Return a deterministic SHA-256 hex digest of an API key."""
    if not raw_key:
        return ""
    cleaned = raw_key.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


@dataclass
class StoredSession:
    """Persisted Control Plane / Central Server authentication session and JWT lease.

    Stored in `~/.talkops/.state/auth.json` (mode `0600`) and optionally OS Keyring
    via `CompositeCredentialStorage`.
    """

    access_token: str = ""
    api_key: str = ""
    api_key_prefix: str = ""
    refresh_token: str = ""
    server_url: str = ""
    control_plane_url: str = ""
    user_id: str = ""
    email: str = ""
    org_id: str = ""
    role: str = "operator"
    user_tier: str = LicenseTier.PRO.value
    scopes: list[str] = field(default_factory=lambda: ["*"])
    auth_method: str = AuthMethodType.API_KEY.value
    expires_at: float = 0.0
    offline_grace_until: float = 0.0
    source_key_hash: str = ""
    updated_at: float = field(default_factory=time.time)

    def is_access_token_valid(self, *, buffer_seconds: float = 30.0) -> bool:
        """Return True if `access_token` is present and not expiring within `buffer_seconds`."""
        if not self.access_token or self.expires_at <= 0:
            return False
        return (time.time() + buffer_seconds) < self.expires_at

    def is_within_offline_grace(self) -> bool:
        """Return True if the session is within its offline grace window."""
        if self.offline_grace_until <= 0:
            return False
        return time.time() < self.offline_grace_until

    def remaining_ttl_seconds(self) -> int:
        """Return remaining seconds until `access_token` expires (0 if expired)."""
        if not self.access_token or self.expires_at <= 0:
            return 0
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self, *, include_secrets: bool = True) -> dict[str, Any]:
        """Serialize session to a JSON-compatible dictionary."""
        prefix = self.api_key_prefix or mask_api_key(self.api_key)
        data: dict[str, Any] = {
            "api_key_prefix": prefix,
            "server_url": self.server_url,
            "control_plane_url": self.control_plane_url,
            "user_id": self.user_id,
            "email": self.email,
            "org_id": self.org_id,
            "role": self.role,
            "user_tier": self.user_tier,
            "scopes": list(self.scopes),
            "auth_method": self.auth_method,
            "expires_at": self.expires_at,
            "offline_grace_until": self.offline_grace_until,
            "source_key_hash": self.source_key_hash or hash_api_key(self.api_key),
            "updated_at": self.updated_at,
        }
        if include_secrets:
            data["access_token"] = self.access_token
            data["api_key"] = self.api_key
            data["refresh_token"] = self.refresh_token
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StoredSession:
        """Construct a `StoredSession` from a dictionary."""
        raw_scopes = data.get("scopes")
        if isinstance(raw_scopes, list):
            scopes = [str(s) for s in raw_scopes]
        else:
            scopes = ["*"]
        api_key = str(data.get("api_key") or "")
        return cls(
            access_token=str(data.get("access_token") or ""),
            api_key=api_key,
            api_key_prefix=str(data.get("api_key_prefix") or mask_api_key(api_key)),
            refresh_token=str(data.get("refresh_token") or ""),
            server_url=str(data.get("server_url") or ""),
            control_plane_url=str(data.get("control_plane_url") or ""),
            user_id=str(data.get("user_id") or ""),
            email=str(data.get("email") or ""),
            org_id=str(data.get("org_id") or ""),
            role=str(data.get("role") or "operator"),
            user_tier=str(data.get("user_tier") or LicenseTier.PRO.value),
            scopes=scopes,
            auth_method=str(data.get("auth_method") or AuthMethodType.API_KEY.value),
            expires_at=float(data.get("expires_at") or 0.0),
            offline_grace_until=float(data.get("offline_grace_until") or 0.0),
            source_key_hash=str(data.get("source_key_hash") or hash_api_key(api_key)),
            updated_at=float(data.get("updated_at") or time.time()),
        )


__all__ = [
    "AuthMethodType",
    "LicenseTier",
    "StoredSession",
    "hash_api_key",
    "mask_api_key",
]
