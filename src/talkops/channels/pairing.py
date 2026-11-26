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

"""Dynamic DM sender pairing for `talkops.channels`.

Unknown DM senders receive a short
8-character one-time code (`K7QM-3XRD`). The channel operator approves the code
from their own DM using `/pair approve <code>`.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

PAIRING_FILENAME = "channel_pairing.json"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 3600
MAX_PENDING_PER_CHANNEL = 32
MAX_APPROVED_PER_CHANNEL = 1024


def generate_code() -> str:
    """Generate an 8-character pairing code without ambiguous characters (`0 O 1 I L`)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """Format an 8-char code with a middle hyphen (`K7QM-3XRD`)."""
    norm = normalize_code(code)
    half = len(norm) // 2
    return f"{norm[:half]}-{norm[half:]}"


def normalize_code(value: str) -> str:
    """Normalize user-entered code by upper-casing and stripping hyphens/spaces."""
    return "".join(ch for ch in value.upper() if ch not in "- \t\n")


@dataclass(frozen=True, slots=True)
class PendingPairingRequest:
    """One unapproved DM pairing request."""

    channel: str
    sender_id: str
    sender_name: str
    code: str
    conversation_id: str
    created_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class PairedSender:
    """One approved sender admitted via operator pairing."""

    channel: str
    sender_id: str
    sender_name: str
    conversation_id: str
    approved_at: int
    role: str = "operator"


class PairingStore:
    """Atomic file-backed store for pending and approved channel senders."""

    def __init__(self, path: Path) -> None:
        """Initialize `PairingStore` backed by `path`."""
        self._path = path.expanduser().resolve()
        self._lock = FileLock(str(self._path) + ".lock", timeout=5.0)

    @property
    def path(self) -> Path:
        """Return resolved backing JSON file path."""
        return self._path

    def _read_unlocked(self) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        channels = raw.get("channels") if isinstance(raw, dict) else None
        return channels if isinstance(channels, dict) else {}

    def _write_unlocked(self, channels: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "channels": dict(channels)}, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".pairing-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(self._path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    def is_approved(self, channel: str, sender_id: str) -> bool:
        """Return whether `(channel, sender_id)` has been approved via pairing."""
        channels = self._read_unlocked()
        ch_state = channels.get(channel, {})
        approved = ch_state.get("approved", {}) if isinstance(ch_state, dict) else {}
        return sender_id in approved

    def get_approved(self, channel: str, sender_id: str) -> PairedSender | None:
        """Return the `PairedSender` record if approved."""
        channels = self._read_unlocked()
        ch_state = channels.get(channel, {})
        approved = ch_state.get("approved", {}) if isinstance(ch_state, dict) else {}
        entry = approved.get(sender_id)
        if not isinstance(entry, dict):
            return None
        return PairedSender(
            channel=channel,
            sender_id=sender_id,
            sender_name=str(entry.get("sender_name", "")),
            conversation_id=str(entry.get("conversation_id", "")),
            approved_at=int(entry.get("approved_at", 0)),
            role=str(entry.get("role", "operator")),
        )

    def issue_or_get_code(
        self,
        *,
        channel: str,
        sender_id: str,
        sender_name: str,
        conversation_id: str,
        now: int | None = None,
    ) -> PendingPairingRequest:
        """Return an active pairing code for `(channel, sender_id)` or issue a new one."""
        ts = int(time.time()) if now is None else now
        with self._lock:
            channels = self._read_unlocked()
            ch_state = channels.setdefault(channel, {"pending": {}, "approved": {}})
            pending_map = ch_state.setdefault("pending", {})

            expired_keys = [
                k
                for k, v in pending_map.items()
                if isinstance(v, dict) and int(v.get("expires_at", 0)) <= ts
            ]
            for k in expired_keys:
                pending_map.pop(k, None)

            existing = pending_map.get(sender_id)
            if isinstance(existing, dict) and int(existing.get("expires_at", 0)) > ts:
                return PendingPairingRequest(
                    channel=channel,
                    sender_id=sender_id,
                    sender_name=str(existing.get("sender_name", sender_name)),
                    code=str(existing["code"]),
                    conversation_id=str(existing.get("conversation_id", conversation_id)),
                    created_at=int(existing.get("created_at", ts)),
                    expires_at=int(existing["expires_at"]),
                )

            if len(pending_map) >= MAX_PENDING_PER_CHANNEL:
                oldest_key = min(
                    pending_map,
                    key=lambda k: int(pending_map[k].get("created_at", 0))
                    if isinstance(pending_map[k], dict)
                    else 0,
                )
                pending_map.pop(oldest_key, None)

            code = generate_code()
            expires_at = ts + CODE_TTL_SECONDS
            pending_map[sender_id] = {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "code": code,
                "conversation_id": conversation_id,
                "created_at": ts,
                "expires_at": expires_at,
            }
            self._write_unlocked(channels)
            return PendingPairingRequest(
                channel=channel,
                sender_id=sender_id,
                sender_name=sender_name,
                code=code,
                conversation_id=conversation_id,
                created_at=ts,
                expires_at=expires_at,
            )

    def approve_code(self, raw_code: str, *, now: int | None = None) -> PairedSender | None:
        """Approve a pending request matching `raw_code` across any channel."""
        target = normalize_code(raw_code)
        if not target:
            return None
        ts = int(time.time()) if now is None else now
        with self._lock:
            channels = self._read_unlocked()
            for channel_name, ch_state in channels.items():
                if not isinstance(ch_state, dict):
                    continue
                pending_map = ch_state.get("pending", {})
                approved_map = ch_state.setdefault("approved", {})
                if not isinstance(pending_map, dict):
                    continue
                for sender_id, req in list(pending_map.items()):
                    if not isinstance(req, dict):
                        continue
                    if int(req.get("expires_at", 0)) <= ts:
                        pending_map.pop(sender_id, None)
                        continue
                    candidate = normalize_code(str(req.get("code", "")))
                    if hmac.compare_digest(candidate, target):
                        pending_map.pop(sender_id, None)
                        record = {
                            "sender_id": sender_id,
                            "sender_name": str(req.get("sender_name", "")),
                            "conversation_id": str(req.get("conversation_id", "")),
                            "approved_at": ts,
                            "role": "operator",
                        }
                        approved_map[sender_id] = record
                        self._write_unlocked(channels)
                        return PairedSender(
                            channel=channel_name,
                            sender_id=sender_id,
                            sender_name=record["sender_name"],
                            conversation_id=record["conversation_id"],
                            approved_at=ts,
                            role="operator",
                        )
            self._write_unlocked(channels)
        return None

    def revoke(self, channel: str, sender_id: str) -> PairedSender | None:
        """Revoke an approved sender on `channel`."""
        with self._lock:
            channels = self._read_unlocked()
            ch_state = channels.get(channel)
            if not isinstance(ch_state, dict):
                return None
            approved_map = ch_state.get("approved", {})
            if not isinstance(approved_map, dict) or sender_id not in approved_map:
                return None
            removed = approved_map.pop(sender_id)
            self._write_unlocked(channels)
            if not isinstance(removed, dict):
                return None
            return PairedSender(
                channel=channel,
                sender_id=sender_id,
                sender_name=str(removed.get("sender_name", "")),
                conversation_id=str(removed.get("conversation_id", "")),
                approved_at=int(removed.get("approved_at", 0)),
                role=str(removed.get("role", "operator")),
            )

    def list_all(
        self, *, now: int | None = None
    ) -> tuple[list[PendingPairingRequest], list[PairedSender]]:
        """Return all active pending requests and approved senders across channels."""
        ts = int(time.time()) if now is None else now
        pending_list: list[PendingPairingRequest] = []
        approved_list: list[PairedSender] = []
        channels = self._read_unlocked()
        for channel_name, ch_state in sorted(channels.items()):
            if not isinstance(ch_state, dict):
                continue
            for sender_id, req in sorted((ch_state.get("pending") or {}).items()):
                if not isinstance(req, dict):
                    continue
                if int(req.get("expires_at", 0)) <= ts:
                    continue
                pending_list.append(
                    PendingPairingRequest(
                        channel=channel_name,
                        sender_id=sender_id,
                        sender_name=str(req.get("sender_name", "")),
                        code=str(req.get("code", "")),
                        conversation_id=str(req.get("conversation_id", "")),
                        created_at=int(req.get("created_at", 0)),
                        expires_at=int(req.get("expires_at", 0)),
                    )
                )
            for sender_id, app in sorted((ch_state.get("approved") or {}).items()):
                if not isinstance(app, dict):
                    continue
                approved_list.append(
                    PairedSender(
                        channel=channel_name,
                        sender_id=sender_id,
                        sender_name=str(app.get("sender_name", "")),
                        conversation_id=str(app.get("conversation_id", "")),
                        approved_at=int(app.get("approved_at", 0)),
                        role=str(app.get("role", "operator")),
                    )
                )
        return pending_list, approved_list
