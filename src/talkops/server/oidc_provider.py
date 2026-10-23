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

"""OIDC Identity Provider & JWKS Manager for TalkOps Central Server Federation.

Enables keyless cross-account federation into customer clouds (GCP Workload Identity
Federation, Azure Entra ID Federated Credentials, and AWS IAM OIDC Identity Providers)
by exposing:
- `GET /.well-known/openid-configuration`
- `GET /.well-known/jwks.json`
and minting short-lived RS256-signed workload JWTs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def _b64url_encode(data: bytes) -> str:
    """Base64url encode bytes without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int) -> str:
    """Convert positive integer to big-endian base64url string (for RSA JWK n/e)."""
    byte_length = max(1, (value.bit_length() + 7) // 8)
    return _b64url_encode(value.to_bytes(byte_length, byteorder="big"))


class OIDCProvider:
    """Manages OIDC discovery metadata, RSA/JWKS public keys, and workload JWT minting."""

    def __init__(self, issuer_url: str | None = None) -> None:
        """Initialize OIDCProvider."""
        self.issuer_url = (
            issuer_url
            or os.environ.get("TALKOPS_OIDC_ISSUER_URL")
            or "https://auth.talkops.io"
        ).rstrip("/")
        self.kid: str = os.environ.get("TALKOPS_OIDC_KID") or "talkops-oidc-k1"
        self._private_key: Any = None
        self._public_jwk: dict[str, Any] | None = None
        self._hmac_secret: bytes = (
            os.environ.get("TALKOPS_OIDC_SECRET") or "talkops-ephemeral-oidc-secret"
        ).encode("utf-8")
        self._init_keys()

    def _init_keys(self) -> None:
        """Initialize RSA-2048 keypair (from PEM env var or ephemeral in-memory keypair)."""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa

            pem_env = os.environ.get("TALKOPS_OIDC_PRIVATE_KEY_PEM")
            key_obj: Any
            if pem_env:
                key_obj = serialization.load_pem_private_key(
                    pem_env.encode("utf-8"),
                    password=None,
                )
            else:
                key_obj = rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=2048,
                )
            self._private_key = key_obj
            pub_key: Any = key_obj.public_key()
            pub_numbers = pub_key.public_numbers()
            self._public_jwk = {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": self.kid,
                "n": _int_to_b64url(pub_numbers.n),
                "e": _int_to_b64url(pub_numbers.e),
            }
        except Exception as exc:
            logger.debug("RSA key initialization unavailable, falling back to HS256: %s", exc)
            self._private_key = None
            self._public_jwk = {
                "kty": "oct",
                "use": "sig",
                "alg": "HS256",
                "kid": self.kid,
            }

    def get_openid_configuration(self, base_url: str | None = None) -> dict[str, Any]:
        """Return OpenID Connect Discovery 1.0 document (`/.well-known/openid-configuration`)."""
        issuer = (base_url or self.issuer_url).rstrip("/")
        return {
            "issuer": issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "jti",
                "execution_mode",
                "tenant_id",
                "project_id",
            ],
        }

    def get_jwks(self) -> dict[str, Any]:
        """Return JSON Web Key Set (`/.well-known/jwks.json`)."""
        if self._public_jwk is None:
            return {"keys": []}
        return {"keys": [self._public_jwk]}

    def mint_workload_jwt(
        self,
        *,
        audience: str,
        subject: str = "system:serviceaccount:talkops:agent",
        ttl_seconds: int = 3600,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Mint a short-lived signed OIDC JWT for cloud Workload Identity Federation."""
        now = int(time.time())
        alg = "RS256" if self._private_key is not None else "HS256"
        header = {
            "alg": alg,
            "typ": "JWT",
            "kid": self.kid,
        }
        payload: dict[str, Any] = {
            "iss": self.issuer_url,
            "sub": subject,
            "aud": audience,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }
        if extra_claims:
            payload.update(extra_claims)

        signing_input = (
            f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
            f"{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
        )

        if self._private_key is not None:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            signature = self._private_key.sign(
                signing_input.encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        else:
            signature = hmac.new(
                self._hmac_secret,
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()

        return f"{signing_input}.{_b64url_encode(signature)}"


_oidc_provider: OIDCProvider | None = None


def get_oidc_provider() -> OIDCProvider:
    """Return the singleton OIDCProvider."""
    global _oidc_provider
    if _oidc_provider is None:
        _oidc_provider = OIDCProvider()
    return _oidc_provider


__all__ = [
    "OIDCProvider",
    "get_oidc_provider",
]
