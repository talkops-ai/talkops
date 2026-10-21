"""Browser-Loopback + Paste PKCE OAuth 2.0 / SSO Token Acquirer for TalkOps CLI.

Modeled after Antigravity CLI's `BrowserAndPasteTokenAcquirer`:
1. Generates an RFC 7636 PKCE (`S256`) `code_verifier` and `code_challenge`.
2. Starts an ephemeral local HTTP listener on `127.0.0.1:0/auth/callback`.
3. Opens the user's browser to `{control_plane_url}/api/v1/auth/authorize?...` AND
   simultaneously listens for a pasted authorization code (`oc_code_...` or callback URL)
   on stdin for headless/SSH environments.
4. Whichever completes first cancels the other and exchanges `(code, code_verifier)` at
   `POST {control_plane_url}/api/v1/auth/token` (`grant_type="authorization_code"`)
   for a 15-minute `RS256` JWT lease and operator `oc_live_...` API key.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import html
import os
import secrets
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import httpx

from talkops.auth.models import (
    AuthMethodType,
    LicenseTier,
    StoredSession,
    hash_api_key,
    mask_api_key,
)
from talkops.auth.storage import container_detector, ssh_detector


@dataclass(frozen=True)
class PKCEChallenge:
    """RFC 7636 Proof Key for Code Exchange (PKCE) S256 parameters."""

    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"
    state: str = ""


def generate_pkce_challenge() -> PKCEChallenge:
    """Generate a high-entropy PKCE `code_verifier`, `S256` `code_challenge`, and CSRF `state`."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(16)
    return PKCEChallenge(
        code_verifier=verifier,
        code_challenge=challenge,
        code_challenge_method="S256",
        state=state,
    )


def extract_code_from_paste(raw_input: str, *, expected_state: str | None = None) -> str:
    """Extract an `oc_code_...` authorization code from either a raw code or a pasted callback URL."""
    cleaned = (raw_input or "").strip()
    if not cleaned:
        raise ValueError("Empty authorization code input.")

    if cleaned.startswith(("http://", "https://", "/")) or "code=" in cleaned:
        parsed = urlparse(cleaned)
        qs = parse_qs(parsed.query)
        if "error" in qs and qs["error"]:
            raise ValueError(f"SSO authorization failed: {qs['error'][0]}")
        if expected_state and "state" in qs and qs["state"]:
            actual_state = qs["state"][0]
            if actual_state != expected_state:
                raise ValueError("CSRF state mismatch in pasted callback URL.")
        if "code" in qs and qs["code"]:
            return qs["code"][0].strip()

    return cleaned


def _render_loopback_html(*, success: bool, detail: str) -> str:
    """Render the HTML response served by the local loopback listener at `127.0.0.1:<port>/auth/callback`."""
    badge = "TalkOps CLI Authenticated" if success else "Authentication Error"
    badge_color = "#34d399" if success else "#f87171"
    heading = "SSO Login Complete" if success else "SSO Login Failed"
    safe_detail = html.escape(detail)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{heading} - TalkOps CLI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #060913;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .card {{
      max-width: 460px;
      width: 90%;
      background: #0f172a;
      border: 1px solid rgba(56, 189, 248, 0.25);
      border-radius: 16px;
      padding: 32px;
      text-align: center;
      box-shadow: 0 24px 64px rgba(0, 0, 0, 0.55);
    }}
    .badge {{
      display: inline-block;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: {badge_color};
      border: 1px solid {badge_color};
      margin-bottom: 14px;
    }}
    h1 {{
      margin: 0 0 10px 0;
      font-size: 22px;
    }}
    p {{
      margin: 0;
      color: #94a3b8;
      font-size: 14px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">{badge}</span>
    <h1>{heading}</h1>
    <p>{safe_detail}</p>
  </div>
</body>
</html>"""


class LoopbackCallbackServer:
    """Ephemeral `127.0.0.1:0` HTTP server that captures `/auth/callback?code=...&state=...`."""

    def __init__(self, *, expected_state: str, host: str = "127.0.0.1", port: int = 0) -> None:
        self.expected_state = expected_state
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._code_future: asyncio.Future[str] | None = None

    @property
    def redirect_uri(self) -> str:
        """Return the loopback callback URI including the bound ephemeral port."""
        return f"http://{self.host}:{self.port}/auth/callback"

    async def start(self) -> str:
        """Bind the server to `127.0.0.1:0` and return the assigned `redirect_uri`."""
        loop = asyncio.get_running_loop()
        self._code_future = loop.create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                host=self.host,
                port=self.port,
            )
            sockets = self._server.sockets or []
            if sockets:
                sockname = sockets[0].getsockname()
                self.port = int(sockname[1])
        except OSError:
            self._server = None
            if self.port == 0:
                self.port = 54321
        return self.redirect_uri

    def deliver_callback_url(self, callback_url_or_code: str) -> str:
        """Validate a callback URL or code against `expected_state` and resolve the loopback future."""
        code_val = extract_code_from_paste(callback_url_or_code, expected_state=self.expected_state)
        if self._code_future and not self._code_future.done():
            self._code_future.set_result(code_val)
        return _render_loopback_html(
            success=True,
            detail="You have signed in to TalkOps CLI. You may close this browser tab and return to your terminal.",
        )

    async def stop(self) -> None:
        """Close the ephemeral HTTP server."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def wait_for_code(self, *, timeout: float = 180.0) -> str:
        """Await the authorization code delivered to `/auth/callback`."""
        if self._code_future is None:
            raise RuntimeError("LoopbackCallbackServer has not been started.")
        return await asyncio.wait_for(self._code_future, timeout=timeout)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line_bytes = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line_bytes:
                return
            request_line = request_line_bytes.decode("utf-8", errors="replace").strip()
            parts = request_line.split(" ")
            path = parts[1] if len(parts) >= 2 else "/"

            # Drain HTTP headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line or line in (b"\r\n", b"\n"):
                    break

            parsed = urlparse(path)
            if not parsed.path.startswith("/auth/callback"):
                body = _render_loopback_html(
                    success=False,
                    detail="Unrecognized callback path.",
                )
                await self._write_http_response(writer, 404, body)
                return

            qs = parse_qs(parsed.query)
            if "error" in qs and qs["error"]:
                err_msg = qs["error"][0]
                body = _render_loopback_html(
                    success=False,
                    detail=f"Identity provider returned error: {err_msg}",
                )
                await self._write_http_response(writer, 400, body)
                if self._code_future and not self._code_future.done():
                    self._code_future.set_exception(RuntimeError(f"SSO error: {err_msg}"))
                return

            state_val = qs.get("state", [""])[0]
            if self.expected_state and state_val != self.expected_state:
                body = _render_loopback_html(
                    success=False,
                    detail="Invalid OAuth state parameter (CSRF check failed).",
                )
                await self._write_http_response(writer, 400, body)
                if self._code_future and not self._code_future.done():
                    self._code_future.set_exception(RuntimeError("Invalid OAuth state parameter."))
                return

            code_val = qs.get("code", [""])[0].strip()
            if not code_val:
                body = _render_loopback_html(
                    success=False,
                    detail="Missing authorization code in callback query.",
                )
                await self._write_http_response(writer, 400, body)
                return

            if self._code_future and not self._code_future.done():
                self._code_future.set_result(code_val)
            body = _render_loopback_html(
                success=True,
                detail="You have signed in to TalkOps CLI. You may close this browser tab and return to your terminal.",
            )
            await self._write_http_response(writer, 200, body)
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write_http_response(
        writer: asyncio.StreamWriter,
        status_code: int,
        html_body: str,
    ) -> None:
        reason = "OK" if status_code == 200 else ("Bad Request" if status_code == 400 else "Not Found")
        encoded = html_body.encode("utf-8")
        headers = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(headers.encode("ascii") + encoded)
        await writer.drain()


def build_authorize_url(
    control_plane_url: str,
    *,
    pkce: PKCEChallenge,
    redirect_uri: str,
    client_id: str = "talkops-cli",
    sso_provider: str | None = None,
    login_hint: str | None = None,
    auto_approve: bool = False,
) -> str:
    """Construct the Control Plane `/api/v1/auth/authorize` URL with PKCE S256 parameters."""
    base = control_plane_url.rstrip("/")
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": pkce.state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": pkce.code_challenge_method,
    }
    if sso_provider:
        params["sso_provider"] = sso_provider
    if login_hint:
        params["login_hint"] = login_hint
    if auto_approve:
        params["auto_approve"] = "1"
    return f"{base}/api/v1/auth/authorize?{urlencode(params)}"


async def exchange_pkce_code_for_session(
    *,
    control_plane_url: str,
    server_url: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
    client_id: str = "talkops-cli",
    sso_provider: str | None = None,
    offline_grace_hours: float = 24.0,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[StoredSession, dict[str, Any]]:
    """Exchange a single-use PKCE authorization code (`oc_code_...`) for a 15m RS256 JWT + API key."""
    base = control_plane_url.rstrip("/")
    url = f"{base}/api/v1/auth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "code_verifier": code_verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(url, json=payload)
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code in (400, 401, 403):
        detail = f"HTTP {response.status_code}"
        try:
            err_body = response.json()
            if isinstance(err_body, dict):
                detail = str(err_body.get("detail") or err_body.get("error") or detail)
        except Exception:
            pass
        raise PermissionError(f"PKCE token exchange rejected by Control Plane ({detail})")

    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON response from Control Plane PKCE token endpoint")

    now = time.time()
    access_token = str(data.get("access_token") or "")
    raw_api_key = str(data.get("api_key") or "")
    expires_in = float(data.get("expires_in") or 900.0)
    expires_at = float(data.get("expires_at") or (now + expires_in))

    raw_scopes = data.get("scopes")
    scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else ["*"]

    provider_method = str(data.get("auth_method") or sso_provider or "").lower()
    auth_method = (
        AuthMethodType.WIF_SSO.value
        if provider_method in ("enterprise", "oidc", "saml", "wif_sso", "work")
        else AuthMethodType.OAUTH_PKCE.value
    )

    session = StoredSession(
        access_token=access_token,
        api_key=raw_api_key,
        api_key_prefix=str(data.get("api_key_prefix") or mask_api_key(raw_api_key)),
        refresh_token=str(data.get("refresh_token") or ""),
        server_url=server_url,
        control_plane_url=base,
        user_id=str(data.get("user_id") or "operator"),
        email=str(data.get("email") or ""),
        org_id=str(data.get("org_id") or "default"),
        role=str(data.get("role") or "operator"),
        user_tier=str(data.get("tier") or data.get("user_tier") or LicenseTier.PRO.value),
        scopes=scopes,
        auth_method=auth_method,
        expires_at=expires_at,
        offline_grace_until=now + (offline_grace_hours * 3600.0),
        source_key_hash=hash_api_key(raw_api_key or access_token),
        updated_at=now,
    )
    return session, data


class BrowserAndPasteTokenAcquirer:
    """Concurrent Browser-Loopback + Terminal Paste PKCE Token Acquirer (`talkops auth login`)."""

    def __init__(
        self,
        *,
        control_plane_url: str,
        server_url: str = "",
        sso_provider: str | None = None,
        login_hint: str | None = None,
        no_browser: bool = False,
        auto_approve: bool = False,
        timeout_seconds: float = 180.0,
        browser_opener: Callable[[str], bool] | None = None,
        paste_reader: Callable[[], str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.server_url = server_url
        self.sso_provider = sso_provider
        self.login_hint = login_hint
        self.no_browser = no_browser
        self.auto_approve = auto_approve
        self.timeout_seconds = timeout_seconds
        self._custom_browser_opener = browser_opener is not None
        self.browser_opener = browser_opener or webbrowser.open
        self.paste_reader = paste_reader
        self.http_client = http_client
        self.output_fn = output_fn or print

    def is_headless_environment(self) -> bool:
        """Return True if running inside SSH, a container, or without a desktop display."""
        if self.no_browser:
            return True
        if self._custom_browser_opener:
            return False
        if ssh_detector() or container_detector():
            return True
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return True
        return False

    async def acquire_session(
        self,
        *,
        provided_code: str | None = None,
        pkce_override: PKCEChallenge | None = None,
    ) -> tuple[StoredSession, dict[str, Any]]:
        """Run the PKCE Browser-Loopback + Paste race and exchange the code for a `StoredSession`."""
        pkce = pkce_override or generate_pkce_challenge()

        # Case 1: Caller already has an authorization code (or callback URL) via `--code`
        if provided_code:
            clean_code = extract_code_from_paste(provided_code, expected_state=None)
            return await exchange_pkce_code_for_session(
                control_plane_url=self.control_plane_url,
                server_url=self.server_url,
                code=clean_code,
                code_verifier=pkce.code_verifier,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob",
                sso_provider=self.sso_provider,
                http_client=self.http_client,
            )

        # Case 2: Non-interactive auto-approve (e.g. sandbox / CI / automated test with `auto_approve=True` and `no_browser=True`)
        if self.auto_approve and self.no_browser and self.paste_reader is None:
            authorize_url = build_authorize_url(
                self.control_plane_url,
                pkce=pkce,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob",
                sso_provider=self.sso_provider,
                login_hint=self.login_hint,
                auto_approve=True,
            )
            owns_client = self.http_client is None
            client = self.http_client or httpx.AsyncClient(timeout=10.0)
            try:
                resp = await client.get(authorize_url, follow_redirects=False)
                location = resp.headers.get("location") or ""
                code = extract_code_from_paste(location, expected_state=pkce.state)
            finally:
                if owns_client:
                    await client.aclose()
            return await exchange_pkce_code_for_session(
                control_plane_url=self.control_plane_url,
                server_url=self.server_url,
                code=code,
                code_verifier=pkce.code_verifier,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob",
                sso_provider=self.sso_provider,
                http_client=self.http_client,
            )

        # Case 3: Concurrent Browser-Loopback (`127.0.0.1:<port>/auth/callback`) + Terminal Paste Race
        loopback = LoopbackCallbackServer(expected_state=pkce.state)
        self.active_loopback = loopback
        redirect_uri = await loopback.start()

        try:
            authorize_url = build_authorize_url(
                self.control_plane_url,
                pkce=pkce,
                redirect_uri=redirect_uri,
                sso_provider=self.sso_provider,
                login_hint=self.login_hint,
                auto_approve=self.auto_approve,
            )
            provider_label = (self.sso_provider or "SSO").upper()
            self.output_fn(f"\nLaunching TalkOps Control Plane {provider_label} login (PKCE S256)...")
            self.output_fn(f"  Authorization URL: {authorize_url}")
            self.output_fn(f"  Loopback Listener: {redirect_uri}")

            browser_opened = False
            if not self.is_headless_environment():
                try:
                    browser_opened = bool(self.browser_opener(authorize_url))
                except Exception:
                    browser_opened = False

            if browser_opened:
                self.output_fn(
                    "  Waiting for browser callback (or paste `oc_code_...` below if on a remote host)..."
                )
            else:
                self.output_fn(
                    "  Open the URL above in your browser, then paste the `oc_code_...` authorization code below:"
                )

            paste_stop_event = asyncio.Event()
            tasks: list[asyncio.Task[str]] = [
                asyncio.create_task(
                    loopback.wait_for_code(timeout=self.timeout_seconds),
                    name="loopback_listener",
                )
            ]
            if self.paste_reader is not None or sys.stdin.isatty():
                tasks.append(
                    asyncio.create_task(
                        self._read_paste_code(
                            expected_state=pkce.state,
                            stop_event=paste_stop_event,
                        ),
                        name="paste_listener",
                    )
                )

            done, pending = await asyncio.wait(
                tasks,
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            paste_stop_event.set()
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if not done:
                raise TimeoutError(
                    f"Timed out after {int(self.timeout_seconds)}s waiting for browser SSO callback or pasted code."
                )

            winning_task = next(iter(done))
            auth_code = winning_task.result()
        finally:
            await loopback.stop()

        return await exchange_pkce_code_for_session(
            control_plane_url=self.control_plane_url,
            server_url=self.server_url,
            code=auth_code,
            code_verifier=pkce.code_verifier,
            redirect_uri=redirect_uri,
            sso_provider=self.sso_provider,
            http_client=self.http_client,
        )

    async def _read_paste_code(
        self,
        *,
        expected_state: str,
        stop_event: asyncio.Event | None = None,
    ) -> str:
        """Read a pasted authorization code or callback URL from stdin without blocking event loop shutdown."""
        loop = asyncio.get_running_loop()
        if self.paste_reader is not None:
            raw = await loop.run_in_executor(None, self.paste_reader)
            return extract_code_from_paste(raw, expected_state=expected_state)

        import select

        sys.stdout.write("  Paste authorization code (or press Ctrl+C to cancel): ")
        sys.stdout.flush()

        while stop_event is None or not stop_event.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            except Exception:
                ready = []
            if ready:
                raw_line = await loop.run_in_executor(None, sys.stdin.readline)
                if raw_line and raw_line.strip():
                    return extract_code_from_paste(raw_line, expected_state=expected_state)
            await asyncio.sleep(0.05)

        raise asyncio.CancelledError()


__all__ = [
    "BrowserAndPasteTokenAcquirer",
    "LoopbackCallbackServer",
    "PKCEChallenge",
    "build_authorize_url",
    "exchange_pkce_code_for_session",
    "extract_code_from_paste",
    "generate_pkce_challenge",
]
