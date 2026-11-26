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

"""Microsoft Teams Central HTTP Webhook Channel Adapter (`TeamsWebhookChannel`).

Handles Microsoft Bot Framework Activity webhooks (`POST /channels/teams/events`
and `POST /channels/teams/interactions`), Adaptive Card v1.4 HITL tool approval
submissions, reactions, and Bot Connector REST API v3 outbound delivery.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from talkops.channels.formatting import (
    TEAMS_MAX_TEXT_CHARS,
    ChannelExposure,
    build_teams_incident_card,
    chunk_text,
    exposure_from_mapping,
    format_markdown_for_teams,
    split_csv,
)
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ChannelCapabilities,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelReaction,
    ChannelStatus,
    InteractionHandler,
    MessageHandler,
    ReactionHandler,
    SendResult,
    WebhookHTTPResponse,
)
from talkops.channels.state_store import ChannelStateStore
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/amer"
BOTFRAMEWORK_OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOTFRAMEWORK_ISSUER = "https://api.botframework.com"
_TRUSTED_SERVICE_EXACT_HOSTS = frozenset(
    {
        "smba.trafficmanager.net",
        "api.botframework.com",
        "state.botframework.com",
    }
)
_TRUSTED_SERVICE_HOST_SUFFIXES = (
    ".botframework.com",
    ".botframework.azure.us",
    ".botframework.azure.cn",
    ".teams.microsoft.com",
    ".teams.microsoft.us",
    ".skype.com",
)
_JWKS_CACHE_TTL_SECONDS = 86400.0
_JWKS_REFRESH_COOLDOWN_SECONDS = 60.0
_AT_MENTION_TAG_RE = re.compile(r"<at>.*?</at>", flags=re.IGNORECASE | re.DOTALL)
_CHANNEL_MESSAGEID_RE = re.compile(r";\s*messageid=([^;]+)", flags=re.IGNORECASE)
_SEEN_ACTIVITY_CAP = 1024


def is_trusted_teams_service_url(
    url: str,
    *,
    extra_allowed_hosts: Sequence[str] = (),
) -> bool:
    """Validate that `url` is a trusted Microsoft Bot Connector `serviceUrl` (SSRF guard)."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return False

    allowed_extras = {h.strip().rstrip(".").lower() for h in extra_allowed_hosts if h and h.strip()}
    if host in allowed_extras:
        if host in {"localhost", "127.0.0.1", "::1"}:
            return parsed.scheme in {"https", "http"}
        return parsed.scheme == "https"

    if parsed.scheme != "https":
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    if host in _TRUSTED_SERVICE_EXACT_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _TRUSTED_SERVICE_HOST_SUFFIXES)


def _base_teams_conversation_id(conversation_id: str) -> str:
    """Strip `;messageid=...` suffix from a Teams channel thread conversation ID."""
    return conversation_id.split(";", 1)[0].strip()


def build_teams_approval_card(spec: ApprovalCardSpec) -> dict[str, Any]:
    """Build a Microsoft Teams Adaptive Card v1.4 attachment for HITL tool approval."""
    facts: list[dict[str, str]] = []
    for idx, tc in enumerate(spec.tool_calls, start=1):
        args_str = ", ".join(f"{k}={v!r}" for k, v in list(tc.args.items())[:5])
        facts.append(
            {
                "title": f"{idx}. {tc.name}",
                "value": f"`{args_str or '(no args)'}`"
                + (f" — {tc.description}" if tc.description else ""),
            }
        )

    card_content: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "size": "Medium",
                "weight": "Bolder",
                "text": f"🛡️ {spec.title}",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"{len(spec.tool_calls)} tool call(s) require approval before execution. "
                    "Click Approve/Reject below or reply `approve` / `deny <reason>`."
                ),
                "wrap": True,
                "isSubtle": True,
            },
            {
                "type": "FactSet",
                "facts": facts,
            },
            {
                "type": "Input.Text",
                "id": "reason",
                "placeholder": "Optional reason if rejecting...",
                "isMultiline": False,
            },
        ],
        "actions": [
            {
                "type": "Action.Execute",
                "verb": "talkops_approve",
                "title": "✅ Approve",
                "style": "positive",
                "data": {
                    "talkops_action": "approval",
                    "decision": "approve",
                    "approval_id": spec.approval_id,
                    "lg_thread_id": spec.lg_thread_id,
                    "run_id": spec.run_id,
                    "conversation_id": spec.conversation_id,
                },
            },
            {
                "type": "Action.Execute",
                "verb": "talkops_reject",
                "title": "🛑 Reject",
                "style": "destructive",
                "data": {
                    "talkops_action": "approval",
                    "decision": "reject",
                    "approval_id": spec.approval_id,
                    "lg_thread_id": spec.lg_thread_id,
                    "run_id": spec.run_id,
                    "conversation_id": spec.conversation_id,
                },
            },
        ],
    }
    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": card_content,
    }


@dataclass(frozen=True, slots=True)
class TeamsChannelConfig:
    """Configuration for the Microsoft Teams Bot Framework HTTP webhook adapter."""

    app_id: str = ""
    app_password: str = field(default="", repr=False)
    tenant_id: str = "botframework.com"
    webhook_secret: str = field(default="", repr=False)
    default_service_url: str = DEFAULT_TEAMS_SERVICE_URL
    openid_metadata_url: str = BOTFRAMEWORK_OPENID_METADATA_URL
    allowed_service_url_hosts: tuple[str, ...] = ()
    exposure: ChannelExposure = field(default_factory=ChannelExposure)
    verify_signatures: bool = True
    request_timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(
        cls,
        cfg: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> TeamsChannelConfig:
        """Construct `TeamsChannelConfig` from addon config mapping and environment variables."""
        app_id = str(
            cfg.get("app_id")
            or env.get("TALKOPS_CHANNELS_TEAMS_APP_ID")
            or env.get("TALKOPS_TEAMS_APP_ID")
            or env.get("TEAMS_APP_ID")
            or env.get("MICROSOFT_APP_ID")
            or ""
        ).strip()
        app_password = str(
            cfg.get("app_password")
            or env.get("TALKOPS_CHANNELS_TEAMS_APP_PASSWORD")
            or env.get("TALKOPS_TEAMS_APP_PASSWORD")
            or env.get("TEAMS_APP_PASSWORD")
            or env.get("MICROSOFT_APP_PASSWORD")
            or ""
        ).strip()
        tenant_id = str(
            cfg.get("tenant_id")
            or env.get("TALKOPS_CHANNELS_TEAMS_TENANT_ID")
            or env.get("TALKOPS_TEAMS_TENANT_ID")
            or env.get("TEAMS_TENANT_ID")
            or env.get("MICROSOFT_APP_TENANT_ID")
            or "botframework.com"
        ).strip()
        webhook_secret = str(
            cfg.get("webhook_secret")
            or env.get("TALKOPS_CHANNELS_TEAMS_WEBHOOK_SECRET")
            or env.get("TALKOPS_TEAMS_WEBHOOK_SECRET")
            or env.get("TEAMS_WEBHOOK_SECRET")
            or ""
        ).strip()
        default_service_url = str(
            cfg.get("service_url")
            or env.get("TALKOPS_CHANNELS_TEAMS_SERVICE_URL")
            or DEFAULT_TEAMS_SERVICE_URL
        ).rstrip("/")
        openid_metadata_url = str(
            cfg.get("openid_metadata_url")
            or env.get("TALKOPS_CHANNELS_TEAMS_OPENID_METADATA_URL")
            or BOTFRAMEWORK_OPENID_METADATA_URL
        ).strip()
        allowed_service_url_hosts = split_csv(
            cfg.get("allowed_service_url_hosts")
            or env.get("TALKOPS_CHANNELS_TEAMS_ALLOWED_SERVICE_URL_HOSTS")
        )
        verify_sig = cfg.get("verify_signatures")
        if verify_sig is None:
            verify_sig = env.get("TALKOPS_CHANNELS_TEAMS_VERIFY_SIGNATURES", "1")
        verify_signatures = str(verify_sig).strip().lower() not in {"0", "false", "no", "off"}

        exposure = exposure_from_mapping("teams", cfg, env)
        return cls(
            app_id=app_id,
            app_password=app_password,
            tenant_id=tenant_id,
            webhook_secret=webhook_secret,
            default_service_url=default_service_url,
            openid_metadata_url=openid_metadata_url,
            allowed_service_url_hosts=allowed_service_url_hosts,
            exposure=exposure,
            verify_signatures=verify_signatures,
        )


class TeamsWebhookChannel:
    """Microsoft Teams channel adapter operating over central Bot Framework HTTP webhooks."""

    name = "teams"
    capabilities = ChannelCapabilities(
        max_text_chars=TEAMS_MAX_TEXT_CHARS,
        edit_debounce_seconds=1.0,
        typing_refresh_seconds=4.0,
        supports_threads=True,
        auto_spawn_thread_on_mention=False,
        supports_streaming_edits=True,
        supports_reactions=True,
        supports_attachments=True,
        supports_ephemeral_interactions=False,
        interaction_token_ttl_seconds=0.0,
        supported_transports=("webhook",),
    )

    def __init__(
        self,
        config: TeamsChannelConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        state_store: ChannelStateStore | None = None,
    ) -> None:
        """Initialize `TeamsWebhookChannel` with `config`, optional `http_client`, and `state_store`."""
        self.config = config
        self._client = http_client
        self._owns_client = http_client is None
        self._state_store = state_store
        self._message_handler: MessageHandler | None = None
        self._reaction_handler: ReactionHandler | None = None
        self._interaction_handler: InteractionHandler | None = None
        self._service_urls: dict[str, str] = {}
        self._bot_accounts: dict[str, dict[str, str]] = {}
        self._conversation_types: dict[str, str] = {}
        self._seen_activities: OrderedDict[str, None] = OrderedDict()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._jwks_by_kid: dict[str, Any] = {}
        self._jwks_fetched_at: float = 0.0
        self._jwks_last_refresh_attempt: float = 0.0
        self._jwks_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register inbound message callback."""
        self._message_handler = handler

    def set_reaction_handler(self, handler: ReactionHandler) -> None:
        """Register inbound reaction callback."""
        self._reaction_handler = handler

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Register inbound interaction callback."""
        self._interaction_handler = handler

    async def start(self) -> None:
        """Initialize HTTP client on server startup."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    async def stop(self) -> None:
        """Cancel background tasks and close owned HTTP client."""
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _dedup_activity(self, activity_id: str) -> bool:
        if not activity_id:
            return True
        if activity_id in self._seen_activities:
            return False
        self._seen_activities[activity_id] = None
        while len(self._seen_activities) > _SEEN_ACTIVITY_CAP:
            self._seen_activities.popitem(last=False)
        return True

    def _extra_allowed_service_hosts(self) -> tuple[str, ...]:
        hosts = list(self.config.allowed_service_url_hosts)
        default_host = (urllib.parse.urlparse(self.config.default_service_url).hostname or "").strip().lower()
        if (
            default_host
            and default_host not in _TRUSTED_SERVICE_EXACT_HOSTS
            and not any(default_host.endswith(suffix) for suffix in _TRUSTED_SERVICE_HOST_SUFFIXES)
        ):
            hosts.append(default_host)
        return tuple(hosts)

    def register_jwks_keys(self, keys: Sequence[Mapping[str, Any]]) -> None:
        """Preload JWK public keys (useful for offline tests or air-gapped deployments)."""
        for jwk_dict in keys:
            kid = str(jwk_dict.get("kid") or "").strip()
            if not kid:
                continue
            self._jwks_by_kid[kid] = jwt.PyJWK.from_dict(dict(jwk_dict)).key
        if self._jwks_by_kid:
            self._jwks_fetched_at = time.monotonic()

    def _is_trusted_service_url(self, url: str) -> bool:
        return is_trusted_teams_service_url(
            url,
            extra_allowed_hosts=self._extra_allowed_service_hosts(),
        )

    def _is_single_tenant(self) -> bool:
        tid = (self.config.tenant_id or "").strip().lower()
        return bool(tid and tid not in {"botframework.com", "common", "organizations"})

    def _allowed_jwt_issuers(self) -> frozenset[str]:
        issuers = {BOTFRAMEWORK_ISSUER}
        if self._is_single_tenant():
            tid = self.config.tenant_id.strip()
            issuers.add(f"https://sts.windows.net/{tid}")
            issuers.add(f"https://sts.windows.net/{tid}/")
            issuers.add(f"https://login.microsoftonline.com/{tid}/v2.0")
        return frozenset(issuers)

    def _openid_metadata_urls(self) -> tuple[str, ...]:
        urls: list[str] = []
        primary = (self.config.openid_metadata_url or BOTFRAMEWORK_OPENID_METADATA_URL).strip()
        if primary:
            urls.append(primary)
        if self._is_single_tenant():
            entra_url = (
                f"https://login.microsoftonline.com/{self.config.tenant_id.strip()}"
                "/v2.0/.well-known/openid-configuration"
            )
            if entra_url not in urls:
                urls.append(entra_url)
        return tuple(urls)

    async def _refresh_jwks(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if (
            not force
            and self._jwks_by_kid
            and (now - self._jwks_fetched_at) < _JWKS_CACHE_TTL_SECONDS
        ):
            return
        async with self._jwks_lock:
            now = time.monotonic()
            if (
                not force
                and self._jwks_by_kid
                and (now - self._jwks_fetched_at) < _JWKS_CACHE_TTL_SECONDS
            ):
                return
            if (
                self._jwks_last_refresh_attempt > 0.0
                and (now - self._jwks_last_refresh_attempt) < _JWKS_REFRESH_COOLDOWN_SECONDS
                and self._jwks_by_kid
            ):
                return
            self._jwks_last_refresh_attempt = now

            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

            loaded_keys: dict[str, Any] = {}
            for meta_url in self._openid_metadata_urls():
                try:
                    meta_resp = await self._client.get(meta_url)
                    meta_resp.raise_for_status()
                    meta_doc = meta_resp.json()
                    jwks_uri = str(meta_doc.get("jwks_uri") or "").strip() if isinstance(meta_doc, Mapping) else ""
                    if not jwks_uri:
                        continue
                    jwks_resp = await self._client.get(jwks_uri)
                    jwks_resp.raise_for_status()
                    jwks_doc = jwks_resp.json()
                    raw_keys = jwks_doc.get("keys") if isinstance(jwks_doc, Mapping) else None
                    if not isinstance(raw_keys, Sequence):
                        continue
                    for jwk_dict in raw_keys:
                        if not isinstance(jwk_dict, Mapping):
                            continue
                        kid = str(jwk_dict.get("kid") or "").strip()
                        if not kid:
                            continue
                        try:
                            loaded_keys[kid] = jwt.PyJWK.from_dict(dict(jwk_dict)).key
                        except Exception:
                            continue
                except Exception as exc:
                    logger.debug("Teams JWKS metadata fetch failed for %s: %s", meta_url, exc)

            if loaded_keys:
                self._jwks_by_kid.update(loaded_keys)
                self._jwks_fetched_at = time.monotonic()

    def _verify_hmac_header(self, auth_header: str, body: bytes) -> bool:
        if not self.config.webhook_secret or not auth_header.lower().startswith("hmac "):
            return False
        provided_b64 = auth_header[5:].strip()
        try:
            key_bytes = base64.b64decode(self.config.webhook_secret)
        except Exception:
            key_bytes = self.config.webhook_secret.encode("utf-8")
        computed = base64.b64encode(
            hmac.new(key_bytes, body, hashlib.sha256).digest()
        ).decode("ascii")
        return hmac.compare_digest(computed, provided_b64)

    def _verify_bearer_jwt_with_cached_keys(
        self,
        token: str,
        *,
        expected_service_url: str | None = None,
    ) -> bool:
        """Cryptographically verify a Bot Framework / Entra RS256 JWT using cached JWKS keys."""
        if not token:
            return False
        try:
            header = jwt.get_unverified_header(token)
        except Exception:
            return False

        if str(header.get("alg") or "") != "RS256":
            return False
        kid = str(header.get("kid") or "").strip()
        if not kid:
            return False
        signing_key = self._jwks_by_kid.get(kid)
        if signing_key is None:
            return False

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                audience=self.config.app_id or None,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": bool(self.config.app_id),
                    "verify_iss": False,
                },
                leeway=300,
            )
        except Exception:
            return False

        issuer = str(claims.get("iss") or "").strip()
        allowed_issuers = self._allowed_jwt_issuers()
        if issuer not in allowed_issuers and issuer.rstrip("/") not in {
            item.rstrip("/") for item in allowed_issuers
        }:
            return False

        if self._is_single_tenant():
            token_tid = str(claims.get("tid") or "").strip()
            if token_tid and token_tid.lower() != self.config.tenant_id.strip().lower():
                return False

        token_service_url = str(claims.get("serviceurl") or "").strip().rstrip("/")
        if token_service_url and expected_service_url:
            if token_service_url.lower() != expected_service_url.strip().rstrip("/").lower():
                return False

        return True

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Verify Teams Outgoing Webhook HMAC or RS256 Bot Framework JWT using cached JWKS."""
        if not self.config.verify_signatures:
            return True

        lowered = {k.lower(): v for k, v in headers.items()}
        auth_header = lowered.get("authorization", "").strip()

        if self._verify_hmac_header(auth_header, body):
            return True

        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            return self._verify_bearer_jwt_with_cached_keys(token)

        return False

    async def verify_request_async(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        expected_service_url: str | None = None,
    ) -> bool:
        """Asynchronously verify Teams HMAC or Bot Framework RS256 JWT (fetching/rotating JWKS)."""
        if not self.config.verify_signatures:
            return True

        lowered = {k.lower(): v for k, v in headers.items()}
        auth_header = lowered.get("authorization", "").strip()

        if self._verify_hmac_header(auth_header, body):
            return True

        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if not token:
                return False
            try:
                header = jwt.get_unverified_header(token)
            except Exception:
                return False
            if str(header.get("alg") or "") != "RS256":
                return False
            kid = str(header.get("kid") or "").strip()
            if not kid:
                return False

            if kid not in self._jwks_by_kid:
                await self._refresh_jwks(force=bool(self._jwks_by_kid))
            elif (time.monotonic() - self._jwks_fetched_at) >= _JWKS_CACHE_TTL_SECONDS:
                await self._refresh_jwks(force=False)

            return self._verify_bearer_jwt_with_cached_keys(
                token,
                expected_service_url=expected_service_url,
            )

        return False

    def _persist_conversation_ref(
        self,
        conversation_id: str,
        *,
        service_url: str = "",
        bot_account: Mapping[str, str] | None = None,
        tenant_id: str = "",
        conversation_type: str = "",
    ) -> None:
        """Cache and durably persist Teams routing metadata (`serviceUrl`, `bot`, `tenantId`)."""
        if not conversation_id:
            return

        trusted_service_url = (
            service_url.rstrip("/") if service_url and self._is_trusted_service_url(service_url) else ""
        )
        base_conv = _base_teams_conversation_id(conversation_id)
        keys = [conversation_id] if base_conv == conversation_id else [conversation_id, base_conv]

        for key in keys:
            existing = self._load_conversation_ref(key)
            merged = dict(existing)
            if trusted_service_url:
                merged["service_url"] = trusted_service_url
                self._service_urls[key] = trusted_service_url
            if bot_account and bot_account.get("id"):
                bot_copy = {
                    "id": str(bot_account["id"]),
                    "name": str(bot_account.get("name") or "TalkOps"),
                }
                merged["bot"] = bot_copy
                self._bot_accounts[key] = bot_copy
            if tenant_id:
                merged["tenant_id"] = tenant_id
            if conversation_type:
                merged["conversation_type"] = conversation_type
                self._conversation_types[key] = conversation_type

            if self._state_store is not None and merged:
                try:
                    self._state_store.put_conversation_ref("teams", key, merged)
                except Exception:
                    pass

    def _load_conversation_ref(self, conversation_id: str) -> dict[str, Any]:
        """Load cached or durably persisted Teams routing metadata for `conversation_id`."""
        base_conv = _base_teams_conversation_id(conversation_id)
        candidates = (conversation_id,) if base_conv == conversation_id else (conversation_id, base_conv)

        result: dict[str, Any] = {}
        for key in candidates:
            if key in self._service_urls and "service_url" not in result:
                result["service_url"] = self._service_urls[key]
            if key in self._bot_accounts and "bot" not in result:
                result["bot"] = dict(self._bot_accounts[key])
            if key in self._conversation_types and "conversation_type" not in result:
                result["conversation_type"] = self._conversation_types[key]

        if self._state_store is not None and ("service_url" not in result or "bot" not in result):
            for key in candidates:
                try:
                    stored = self._state_store.get_conversation_ref("teams", key)
                except Exception:
                    stored = None
                if not isinstance(stored, Mapping):
                    continue
                if stored.get("service_url") and "service_url" not in result:
                    s_url = str(stored["service_url"]).rstrip("/")
                    if self._is_trusted_service_url(s_url):
                        result["service_url"] = s_url
                        self._service_urls[key] = s_url
                if isinstance(stored.get("bot"), Mapping) and "bot" not in result:
                    b_id = str(stored["bot"].get("id") or "").strip()
                    if b_id:
                        bot_copy = {
                            "id": b_id,
                            "name": str(stored["bot"].get("name") or "TalkOps"),
                        }
                        result["bot"] = bot_copy
                        self._bot_accounts[key] = bot_copy
                if stored.get("conversation_type") and "conversation_type" not in result:
                    c_type = str(stored["conversation_type"]).strip().lower()
                    if c_type:
                        result["conversation_type"] = c_type
                        self._conversation_types[key] = c_type
                if stored.get("tenant_id") and "tenant_id" not in result:
                    result["tenant_id"] = str(stored["tenant_id"]).strip()
        return result

    def _resolve_service_url(
        self,
        conversation_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        meta_url = str((metadata or {}).get("service_url") or "").strip().rstrip("/")
        if meta_url and self._is_trusted_service_url(meta_url):
            return meta_url

        stored_ref = self._load_conversation_ref(conversation_id)
        stored_url = str(stored_ref.get("service_url") or "").strip().rstrip("/")
        if stored_url and self._is_trusted_service_url(stored_url):
            return stored_url

        default_url = self.config.default_service_url.strip().rstrip("/")
        if not self._is_trusted_service_url(default_url):
            raise ValueError(f"Refusing to send Bot Connector request to untrusted serviceUrl: {default_url!r}")
        return default_url

    def _is_channel_conversation(self, conversation_id: str) -> bool:
        lowered = conversation_id.lower()
        if "@thread.tacv2" in lowered or "@thread.skype" in lowered:
            return True
        ref = self._load_conversation_ref(conversation_id)
        return str(ref.get("conversation_type") or "").lower() == "channel"

    async def handle_webhook_event(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/teams/events` (Bot Framework Activity JSON)."""
        del query_params
        lowered = {k.lower(): v for k, v in headers.items()}

        try:
            activity = json.loads(body.decode("utf-8"))
        except Exception:
            if not await self.verify_request_async(lowered, body):
                return WebhookHTTPResponse(
                    status_code=401,
                    body={"ok": False, "error": "invalid_teams_authorization"},
                )
            return WebhookHTTPResponse(status_code=400, body={"ok": False, "error": "invalid_json"})

        raw_service_url = (
            str(activity.get("serviceUrl") or "").strip().rstrip("/")
            if isinstance(activity, Mapping)
            else ""
        )
        if not await self.verify_request_async(
            lowered,
            body,
            expected_service_url=raw_service_url or None,
        ):
            return WebhookHTTPResponse(
                status_code=401,
                body={"ok": False, "error": "invalid_teams_authorization"},
            )

        if not isinstance(activity, Mapping):
            return WebhookHTTPResponse(
                status_code=400, body={"ok": False, "error": "invalid_activity"}
            )

        activity_id = str(activity.get("id") or "")
        if activity_id and not self._dedup_activity(activity_id):
            return WebhookHTTPResponse(status_code=200, body={"ok": True, "deduplicated": True})

        raw_conv = activity.get("conversation")
        conv: Mapping[str, Any] = raw_conv if isinstance(raw_conv, Mapping) else {}
        conv_id = str(conv.get("id") or "").strip()
        service_url = raw_service_url
        if service_url and not self._is_trusted_service_url(service_url):
            logger.warning("Ignoring untrusted Teams serviceUrl %r on activity %s", service_url, activity_id)
            service_url = ""

        raw_recipient = activity.get("recipient")
        recipient: Mapping[str, Any] = raw_recipient if isinstance(raw_recipient, Mapping) else {}
        bot_account = (
            {
                "id": str(recipient["id"]),
                "name": str(recipient.get("name") or "TalkOps"),
            }
            if recipient.get("id")
            else None
        )
        if conv_id:
            self._persist_conversation_ref(
                conv_id,
                service_url=service_url,
                bot_account=bot_account,
                tenant_id=str(conv.get("tenantId") or "").strip(),
                conversation_type=str(conv.get("conversationType") or "").strip().lower(),
            )

        act_type = str(activity.get("type") or "")
        act_name = str(activity.get("name") or "")

        if act_type == "invoke" or (
            act_type == "message" and isinstance(activity.get("value"), Mapping)
        ):
            interaction = self._parse_card_interaction(activity, trusted_service_url=service_url)
            ack_reply: str | None = None
            if interaction is not None and self._interaction_handler is not None:
                if act_type == "invoke" and interaction.interaction_kind == "feedback":
                    ack_reply = await self._interaction_handler(interaction)
                else:
                    self._spawn(self._interaction_handler(interaction))
            if act_type == "invoke":
                if act_name == "adaptiveCard/action" or interaction is not None:
                    ack_msg = (
                        ack_reply
                        if ack_reply
                        else (
                            f"Decision '{interaction.decision}' received."
                            if interaction is not None and interaction.decision
                            else "Action received."
                        )
                    )
                    return WebhookHTTPResponse(
                        status_code=200,
                        body={
                            "statusCode": 200,
                            "type": "application/vnd.microsoft.activity.message",
                            "value": ack_msg,
                        },
                    )
                return WebhookHTTPResponse(status_code=200, body={"status": 200})
            if interaction is not None:
                return WebhookHTTPResponse(status_code=200, body={"ok": True})

        if act_type == "message":
            msg = self._parse_message_activity(activity, trusted_service_url=service_url)
            if msg is not None and self._message_handler is not None:
                self._spawn(self._message_handler(msg))
        elif act_type == "messageReaction":
            reactions = self._parse_reaction_activity(activity, trusted_service_url=service_url)
            if self._reaction_handler is not None:
                for react in reactions:
                    self._spawn(self._reaction_handler(react))

        return WebhookHTTPResponse(status_code=200, body={"ok": True})

    async def handle_webhook_interaction(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/teams/interactions` (delegates to Activity parser)."""
        return await self.handle_webhook_event(headers, body, query_params)

    def _parse_message_activity(
        self,
        activity: Mapping[str, Any],
        *,
        trusted_service_url: str = "",
    ) -> ChannelMessage | None:
        raw_from = activity.get("from")
        from_acct: Mapping[str, Any] = raw_from if isinstance(raw_from, Mapping) else {}
        sender_id = str(from_acct.get("id") or "").strip()
        sender_name = str(from_acct.get("name") or "").strip()
        if not sender_id:
            return None

        raw_recipient = activity.get("recipient")
        recipient: Mapping[str, Any] = raw_recipient if isinstance(raw_recipient, Mapping) else {}
        bot_id = str(recipient.get("id") or "").strip()
        if bot_id and sender_id == bot_id:
            return None

        raw_conv = activity.get("conversation")
        conv: Mapping[str, Any] = raw_conv if isinstance(raw_conv, Mapping) else {}
        conv_id = str(conv.get("id") or "").strip()
        if not conv_id:
            return None

        conv_type = str(conv.get("conversationType") or "").lower()
        is_dm = conv_type == "personal" or (not conv.get("isGroup", False) and conv_type != "channel")

        raw_text = str(activity.get("text") or "")
        entities = activity.get("entities")
        mentioned = False
        if isinstance(entities, Sequence):
            for ent in entities:
                if isinstance(ent, Mapping) and str(ent.get("type") or "") == "mention":
                    mentioned_acct = ent.get("mentioned")
                    if isinstance(mentioned_acct, Mapping):
                        if not bot_id or str(mentioned_acct.get("id") or "") == bot_id:
                            mentioned = True
                    else:
                        mentioned = True

        if "<at>" in raw_text.lower():
            mentioned = True

        cleaned_text = _AT_MENTION_TAG_RE.sub("", raw_text).strip()
        activity_id = str(activity.get("id") or "").strip() or None
        msg_id_match = _CHANNEL_MESSAGEID_RE.search(conv_id)
        conv_thread_root = msg_id_match.group(1).strip() if msg_id_match else ""
        reply_to_id = (
            str(activity.get("replyToId") or "").strip()
            or conv_thread_root
            or activity_id
            or None
        )

        return ChannelMessage(
            channel="teams",
            conversation_id=conv_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=cleaned_text,
            message_id=activity_id,
            thread_id=reply_to_id,
            parent_channel_id=_base_teams_conversation_id(conv_id),
            is_dm=is_dm,
            mentioned=mentioned,
            metadata={
                "service_url": trusted_service_url,
                "reply_to_id": reply_to_id,
                "channel_id": activity.get("channelId"),
                "tenant_id": conv.get("tenantId"),
            },
        )

    def _parse_card_interaction(
        self,
        activity: Mapping[str, Any],
        *,
        trusted_service_url: str = "",
    ) -> ChannelInteraction | None:
        raw_val = activity.get("value")
        if not isinstance(raw_val, Mapping):
            return None
        action_obj = raw_val.get("action") if isinstance(raw_val.get("action"), Mapping) else None
        verb = str(action_obj.get("verb") or "").strip().lower() if action_obj is not None else ""
        val: Mapping[str, Any] = (
            action_obj["data"]
            if action_obj is not None and isinstance(action_obj.get("data"), Mapping)
            else raw_val
        )

        raw_from = activity.get("from")
        from_acct: Mapping[str, Any] = raw_from if isinstance(raw_from, Mapping) else {}
        sender_id = str(from_acct.get("id") or "").strip()
        sender_name = str(from_acct.get("name") or "").strip()
        raw_conv = activity.get("conversation")
        conv: Mapping[str, Any] = raw_conv if isinstance(raw_conv, Mapping) else {}
        conv_id = str(val.get("conversation_id") or conv.get("id") or "").strip()
        reply_to_id = str(activity.get("replyToId") or "").strip() or None

        decision = str(val.get("decision") or "").strip().lower()
        if not decision and verb:
            if verb in {"talkops_approve", "approve"}:
                decision = "approve"
            elif verb in {"talkops_reject", "reject"}:
                decision = "reject"
        approval_id = str(val.get("approval_id") or "").strip() or None
        run_id = str(val.get("run_id") or "").strip() or None
        reason = str(val.get("reason") or raw_val.get("reason") or "").strip() or None

        if decision in {"approve", "reject"}:
            return ChannelInteraction(
                channel="teams",
                conversation_id=conv_id,
                sender_id=sender_id,
                sender_name=sender_name,
                interaction_kind="approval",
                message_id=reply_to_id,
                thread_id=reply_to_id,
                action_id=verb or f"teams_card_{decision}",
                decision=decision,  # type: ignore[arg-type]
                approval_id=approval_id,
                run_id=run_id,
                reason=reason,
                is_dm=str(conv.get("conversationType") or "").lower() == "personal",
                metadata={
                    "service_url": trusted_service_url,
                    "parent_channel_id": _base_teams_conversation_id(conv_id),
                },
            )

        raw_action_id = str(val.get("action_id") or "").strip()
        raw_verdict = str(val.get("verdict") or "").strip().lower()
        is_feedback = (
            verb in {"talkops_confirm", "talkops_dispute"}
            or raw_action_id.startswith(("talkops_confirm:", "talkops_dispute:"))
            or raw_verdict in {"helped", "rejected", "confirmed", "disputed"}
        )
        if is_feedback:
            if not raw_verdict:
                if verb == "talkops_confirm" or raw_action_id.startswith("talkops_confirm:"):
                    raw_verdict = "helped"
                else:
                    raw_verdict = "rejected"
            lg_tid = str(val.get("lg_thread_id") or "").strip()
            if not lg_tid and ":" in raw_action_id:
                lg_tid = raw_action_id.split(":", 1)[1].strip()
            return ChannelInteraction(
                channel="teams",
                conversation_id=conv_id,
                sender_id=sender_id,
                sender_name=sender_name,
                interaction_kind="feedback",
                message_id=reply_to_id,
                thread_id=reply_to_id,
                action_id=raw_action_id or verb or f"teams_feedback_{raw_verdict}",
                reason=reason,
                is_dm=str(conv.get("conversationType") or "").lower() == "personal",
                metadata={
                    "service_url": trusted_service_url,
                    "parent_channel_id": _base_teams_conversation_id(conv_id),
                    "lg_thread_id": lg_tid,
                    "verdict": raw_verdict,
                },
            )
        return None

    def _parse_reaction_activity(
        self,
        activity: Mapping[str, Any],
        *,
        trusted_service_url: str = "",
    ) -> list[ChannelReaction]:
        raw_from = activity.get("from")
        from_acct: Mapping[str, Any] = raw_from if isinstance(raw_from, Mapping) else {}
        sender_id = str(from_acct.get("id") or "").strip()
        sender_name = str(from_acct.get("name") or "").strip()
        raw_conv = activity.get("conversation")
        conv: Mapping[str, Any] = raw_conv if isinstance(raw_conv, Mapping) else {}
        conv_id = str(conv.get("id") or "").strip()
        reply_to_id = str(activity.get("replyToId") or "").strip()
        added = activity.get("reactionsAdded")
        if not sender_id or not conv_id or not reply_to_id or not isinstance(added, Sequence):
            return []
        results: list[ChannelReaction] = []
        for item in added:
            if isinstance(item, Mapping) and item.get("type"):
                results.append(
                    ChannelReaction(
                        channel="teams",
                        conversation_id=conv_id,
                        sender_id=sender_id,
                        message_id=reply_to_id,
                        emoji=str(item["type"]),
                        metadata={
                            "service_url": trusted_service_url,
                            "sender_name": sender_name,
                            "parent_channel_id": _base_teams_conversation_id(conv_id),
                        },
                    )
                )
        return results

    async def _get_bot_token(self) -> str | None:
        if not self.config.app_id or not self.config.app_password:
            return None
        now = time.time()
        if self._access_token and now < (self._token_expires_at - 60):
            return self._access_token

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

        token_url = (
            f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token"
        )
        resp = await self._client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.app_id,
                "client_secret": self.config.app_password,
                "scope": "https://api.botframework.com/.default",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = str(data.get("access_token") or "")
        self._token_expires_at = now + float(data.get("expires_in", 3600))
        return self._access_token

    async def _connector_request(
        self,
        method: str,
        path: str,
        *,
        conversation_id: str,
        payload: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        service_url = self._resolve_service_url(conversation_id, metadata=metadata)
        url = f"{service_url}/{path.lstrip('/')}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = await self._get_bot_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resp = await self._client.request(
            method,
            url,
            headers=headers,
            json=dict(payload) if payload is not None else None,
        )
        resp.raise_for_status()
        if not resp.content:
            return {"ok": True}
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": True}

    def _enrich_outbound_activity(
        self,
        activity_body: dict[str, Any],
        conversation_id: str,
    ) -> dict[str, Any]:
        """Attach Bot Connector `conversation` and cached/persisted `from` bot identity."""
        activity_body.setdefault("conversation", {"id": conversation_id})
        ref = self._load_conversation_ref(conversation_id)
        bot_acct = ref.get("bot")
        if isinstance(bot_acct, Mapping) and bot_acct.get("id"):
            activity_body.setdefault(
                "from",
                {
                    "id": str(bot_acct["id"]),
                    "name": str(bot_acct.get("name") or "TalkOps"),
                },
            )
        return activity_body

    @staticmethod
    def _normalize_card_attachments(blocks_or_cards: Any) -> list[dict[str, Any]]:
        if not blocks_or_cards:
            return []
        if isinstance(blocks_or_cards, list):
            return [item for item in blocks_or_cards if isinstance(item, dict)]
        if isinstance(blocks_or_cards, dict):
            return [blocks_or_cards]
        return []

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send chunked Markdown text or Adaptive Card attachments to Teams without duplication."""
        del media
        formatted = format_markdown_for_teams(text)
        chunks = chunk_text(formatted, max_chars=TEAMS_MAX_TEXT_CHARS) or [formatted]
        attachments = self._normalize_card_attachments(blocks_or_cards)
        last_id: str | None = None
        last_resp: dict[str, Any] = {}
        encoded_conv = urllib.parse.quote(conversation_id, safe=":@;=-._~")

        for idx, chunk in enumerate(chunks):
            attach_here = idx == len(chunks) - 1 and bool(attachments)
            activity_body: dict[str, Any] = {"type": "message"}
            if attach_here:
                # Omit `text` when sending Adaptive Cards so Teams does not render the full
                # Markdown message duplicated above the card; use `summary` for notifications.
                activity_body["attachments"] = attachments
                if chunk.strip():
                    activity_body["summary"] = chunk.strip()[:200]
            else:
                activity_body["textFormat"] = "markdown"
                activity_body["text"] = chunk

            if thread_id:
                activity_body["replyToId"] = thread_id
            self._enrich_outbound_activity(activity_body, conversation_id)

            encoded_thread = (
                urllib.parse.quote(thread_id, safe=":@;=-._~") if thread_id else ""
            )
            path = (
                f"v3/conversations/{encoded_conv}/activities/{encoded_thread}"
                if encoded_thread
                else f"v3/conversations/{encoded_conv}/activities"
            )
            last_resp = await self._connector_request(
                "POST",
                path,
                conversation_id=conversation_id,
                payload=activity_body,
                metadata=metadata,
            )
            last_id = str(last_resp.get("id") or "") or last_id

        return SendResult(
            channel="teams",
            conversation_id=conversation_id,
            message_id=last_id,
            thread_id=thread_id or last_id,
            raw_response=last_resp,
        )

    async def send_in_thread(
        self,
        conversation_id: str,
        thread_id: str,
        text: str,
        *,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send a reply anchored to `thread_id` in Teams."""
        return await self.send(
            conversation_id,
            text,
            thread_id=thread_id,
            media=media,
            blocks_or_cards=blocks_or_cards,
            metadata=metadata,
        )

    async def update_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
        *,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Update an existing Teams message activity in place without duplicating Adaptive Card text."""
        formatted = format_markdown_for_teams(text)[:TEAMS_MAX_TEXT_CHARS]
        attachments = self._normalize_card_attachments(blocks_or_cards)
        activity_body: dict[str, Any] = {
            "id": message_id,
            "type": "message",
        }
        if attachments:
            activity_body["attachments"] = attachments
            if formatted.strip():
                activity_body["summary"] = formatted.strip()[:200]
        else:
            activity_body["textFormat"] = "markdown"
            activity_body["text"] = formatted
            if blocks_or_cards is not None:
                # Explicit empty list (`blocks_or_cards=[]`) clears an existing Adaptive Card.
                activity_body["attachments"] = []
        self._enrich_outbound_activity(activity_body, conversation_id)

        encoded_conv = urllib.parse.quote(conversation_id, safe=":@;=-._~")
        encoded_msg = urllib.parse.quote(message_id, safe=":@;=-._~")
        resp = await self._connector_request(
            "PUT",
            f"v3/conversations/{encoded_conv}/activities/{encoded_msg}",
            conversation_id=conversation_id,
            payload=activity_body,
            metadata=metadata,
        )
        return SendResult(
            channel="teams",
            conversation_id=conversation_id,
            message_id=message_id,
            raw_response=resp,
        )

    def resolve_thread_conversation_id(
        self,
        conversation_id: str,
        sent: SendResult,
    ) -> str:
        """Return the canonical Teams thread conversation key (`<channel_id>;messageid=<root_id>`)."""
        if ";messageid=" in conversation_id.lower():
            return conversation_id
        root_id = str(sent.thread_id or sent.message_id or "").strip()
        if root_id and self._is_channel_conversation(conversation_id):
            threaded_id = f"{conversation_id};messageid={root_id}"
            existing = self._load_conversation_ref(conversation_id)
            if existing:
                self._persist_conversation_ref(
                    threaded_id,
                    service_url=str(existing.get("service_url") or ""),
                    bot_account=existing.get("bot") if isinstance(existing.get("bot"), Mapping) else None,
                    tenant_id=str(existing.get("tenant_id") or ""),
                    conversation_type=str(existing.get("conversation_type") or "channel"),
                )
            return threaded_id
        return conversation_id

    async def create_incident_thread(
        self,
        conversation_id: str,
        *,
        title: str,
        initial_text: str,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Create an incident root message in `conversation_id` with a Teams Adaptive Card v1.4 attachment."""
        meta = dict(metadata or {})
        cards = blocks_or_cards
        if cards is None:
            cards = build_teams_incident_card(
                title=title,
                summary=str(meta.get("summary") or initial_text),
                provider=str(meta.get("provider") or ""),
                severity=str(meta.get("severity") or ""),
                service_name=str(meta.get("service_name") or ""),
                source_url=str(meta.get("source_url") or ""),
                lg_thread_id=str(meta.get("lg_thread_id") or ""),
                investigation_state=str(meta.get("investigation_state") or "investigating"),  # type: ignore[arg-type]
                rca_preview=str(meta.get("rca_preview") or ""),
            )
        fallback_text = f"**{title}**\n\n{initial_text}".strip()
        return await self.send(
            conversation_id,
            fallback_text,
            blocks_or_cards=cards,
            metadata=metadata,
        )

    async def send_approval_prompt(
        self,
        spec: ApprovalCardSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Render and send a Microsoft Teams Adaptive Card HITL approval prompt."""
        card_attachment = build_teams_approval_card(spec)
        return await self.send(
            spec.conversation_id,
            spec.prompt_text or spec.title,
            thread_id=spec.thread_id,
            blocks_or_cards=[card_attachment],
            metadata=metadata,
        )

    async def send_typing(
        self,
        conversation_id: str,
        *,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a Bot Framework `typing` activity to `conversation_id`."""
        payload: dict[str, Any] = {"type": "typing"}
        if thread_id:
            payload["replyToId"] = thread_id
        self._enrich_outbound_activity(payload, conversation_id)
        encoded_conv = urllib.parse.quote(conversation_id, safe=":@;=-._~")
        try:
            await self._connector_request(
                "POST",
                f"v3/conversations/{encoded_conv}/activities",
                conversation_id=conversation_id,
                payload=payload,
                metadata=metadata,
            )
        except Exception:
            pass

    async def fetch_thread_context(
        self,
        conversation_id: str,
        thread_id: str | None = None,
        *,
        limit: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Return `None` (Bot Connector v3 does not expose full history read without Graph permissions)."""
        del conversation_id, thread_id, limit, metadata
        return None

    def status(self) -> ChannelStatus:
        """Return Teams adapter configuration and endpoint health status."""
        return ChannelStatus(
            channel="teams",
            configured=bool(
                (self.config.app_id and self.config.app_password) or self.config.webhook_secret
            ),
            webhook_events_path="/channels/teams/events",
            webhook_interactions_path="/channels/teams/interactions",
            exposure_mode=str(self.config.exposure.mode),
            detail="Microsoft Teams Bot Framework + Adaptive Cards HTTP Webhook",
            metadata={
                "has_app_credentials": bool(self.config.app_id and self.config.app_password),
                "has_webhook_secret": bool(self.config.webhook_secret),
                "tenant_id": self.config.tenant_id,
            },
        )
