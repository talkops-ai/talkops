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

"""Browser session and persona lifecycle management for TalkOps.

Implements the Antigravity 3-tier Chrome connection & persona strategy on top of
``stagehand`` (``local_browser`` / ``browserbase``) and ``stagehand_middleware``:

1. **Tier 1 — Explicit / Discovered CDP Endpoint**:
   Connects to ``TALKOPS_BROWSER_WS_URL``, ``AGY_BROWSER_WS_URL``, or an explicit
   ``cdp_url`` via ``await local_browser.connect(cdp_url=...)`` (``keep_alive=True``).
2. **Tier 2 — User's Live Default Chrome Persona via ``DevToolsActivePort`` (Auto-Connect)**:
   Reads ``<ChromeUserDataDir>/DevToolsActivePort`` (macOS, Linux, Windows), constructs
   ``ws://127.0.0.1:<port><ws_path>``, and attaches to the user's running Chrome
   instance without launching a second binary or closing the user's browser on disconnect.
3. **Tier 3 — Persistent TalkOps Chrome Profile (or Browserbase Cloud Sandbox)**:
   Launches Chrome with ``user_data_dir=~/.talkops/browser-profile`` and
   ``preserve_user_data_dir=True`` so cookies and sessions persist across runs, or
   connects to a remote Browserbase session when configured.
"""

from __future__ import annotations

import asyncio
import os
import platform
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from talkops.config.paths import DATA_DIR
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

BrowserConnectionMode = Literal["auto", "auto_connect", "persistent", "isolated", "browserbase"]

DEFAULT_BROWSER_PROFILE_DIR: Path = DATA_DIR / "browser-profile"
DEFAULT_BROWSER_ARTIFACTS_DIR: Path = DATA_DIR / "artifacts" / "browser"


class BrowserConnectionError(RuntimeError):
    """Raised when TalkOps cannot connect to or launch a Chrome browser session."""


def default_chrome_user_data_dirs(system: str | None = None) -> list[Path]:
    """Return candidate default Chrome user-data directories for the current OS.

    Matches the directory lookup used by ``chrome-devtools-mcp`` when resolving
    ``DevToolsActivePort`` for ``--autoConnect``.
    """
    sys_name = (system or platform.system()).lower()
    home = Path.home()

    env_override = os.environ.get("TALKOPS_CHROME_USER_DATA_DIR") or os.environ.get(
        "CHROME_USER_DATA_DIR"
    )
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())

    if sys_name == "darwin":
        app_support = home / "Library" / "Application Support"
        candidates.extend(
            [
                app_support / "Google" / "Chrome",
                app_support / "Google" / "Chrome Canary",
                app_support / "Google" / "Chrome Beta",
                app_support / "Chromium",
            ]
        )
    elif sys_name == "windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        candidates.extend(
            [
                local_app_data / "Google" / "Chrome" / "User Data",
                local_app_data / "Google" / "Chrome SxS" / "User Data",
                local_app_data / "Chromium" / "User Data",
            ]
        )
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        candidates.extend(
            [
                config_home / "google-chrome",
                config_home / "google-chrome-beta",
                config_home / "google-chrome-unstable",
                config_home / "chromium",
            ]
        )
    return candidates


def parse_devtools_active_port_file(port_file: Path) -> str:
    """Parse a Chrome ``DevToolsActivePort`` file into a ``ws://127.0.0.1:<port><path>`` URL.

    Follows the exact parsing contract of ``chrome-devtools-mcp/build/src/browser.js``:
    line 1 is the TCP port (1..65535), line 2 is the WebSocket path (e.g.
    ``/devtools/browser/<uuid>``).

    Raises:
        BrowserConnectionError: If the file does not exist or has invalid content.
    """
    try:
        raw_content = port_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrowserConnectionError(
            f"Could not read DevToolsActivePort at {port_file}. "
            "Check if Chrome is running and remote debugging is enabled by going to "
            "chrome://inspect/#remote-debugging."
        ) from exc

    lines = [line.strip() for line in raw_content.splitlines() if line.strip()]
    if len(lines) < 2:
        raise BrowserConnectionError(
            f"Invalid DevToolsActivePort content in {port_file}: {raw_content!r}"
        )

    raw_port, raw_path = lines[0], lines[1]
    try:
        port = int(raw_port, 10)
    except ValueError as exc:
        raise BrowserConnectionError(
            f"Invalid port {raw_port!r} in {port_file}"
        ) from exc

    if port <= 0 or port > 65535:
        raise BrowserConnectionError(
            f"Port out of range ({port}) in {port_file}"
        )

    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"

    return f"ws://127.0.0.1:{port}{raw_path}"


def discover_devtools_active_port(
    user_data_dir: str | Path | None = None,
) -> tuple[str, Path] | None:
    """Discover a live Chrome ``ws://127.0.0.1:<port>/<path>`` endpoint from ``DevToolsActivePort``.

    Args:
        user_data_dir: Optional explicit Chrome user-data directory to inspect first.

    Returns:
        ``(ws_endpoint, port_file_path)`` if found and valid, else ``None``.
    """
    search_dirs: list[Path] = []
    if user_data_dir is not None:
        search_dirs.append(Path(user_data_dir).expanduser())
    search_dirs.extend(default_chrome_user_data_dirs())

    for candidate_dir in search_dirs:
        port_file = candidate_dir / "DevToolsActivePort"
        if not port_file.is_file():
            continue
        try:
            ws_endpoint = parse_devtools_active_port_file(port_file)
            return ws_endpoint, port_file
        except BrowserConnectionError as exc:
            logger.debug("Skipping invalid DevToolsActivePort at %s: %s", port_file, exc)
            continue

    return None


@dataclass
class BrowserConnectionConfig:
    """Configuration for how TalkOps connects to or launches Chrome."""

    mode: BrowserConnectionMode = "auto"
    """Connection mode:
    - ``"auto"``: Try Tier 1 (env/explicit CDP URL) -> Tier 2 (``DevToolsActivePort`` live user
      Chrome persona) -> Tier 3 (launch persistent ``~/.talkops/browser-profile``).
    - ``"auto_connect"``: Strict Antigravity ``--autoConnect`` mode — only connect to existing
      Chrome via ``TALKOPS_BROWSER_WS_URL`` / ``AGY_BROWSER_WS_URL`` or ``DevToolsActivePort``;
      if unavailable, raise ``BrowserConnectionError`` instructing the user to enable
      ``chrome://inspect/#remote-debugging``.
    - ``"persistent"``: Launch Chrome with a persistent profile at ``user_data_dir``
      (default ``~/.talkops/browser-profile``).
    - ``"isolated"``: Launch a fresh headless/ephemeral Chrome profile (for CI/evals).
    - ``"browserbase"``: Connect to or launch a remote Browserbase cloud browser session.
    """

    cdp_url: str | None = None
    """Explicit CDP WebSocket or HTTP endpoint (e.g. ``ws://127.0.0.1:9222/...``)."""

    user_data_dir: Path | None = None
    """Chrome user-data directory for ``auto_connect`` or ``persistent`` modes."""

    headless: bool = False
    """Whether to launch Chrome in headless mode when launching a local browser."""

    port: int | None = None
    """Optional remote debugging port when launching a local browser."""

    chrome_path: str | None = None
    """Optional path to Chrome executable."""

    launch_args: list[str] = field(default_factory=lambda: ["--hide-crash-restore-bubble"])
    """Additional Chrome CLI arguments when launching locally."""

    browserbase_api_key: str | None = None
    """Browserbase API key when ``mode == "browserbase"``."""

    browserbase_project_id: str | None = None
    """Browserbase project ID when ``mode == "browserbase"``."""

    browserbase_session_id: str | None = None
    """Optional existing Browserbase session ID to attach to."""

    artifacts_dir: Path = field(default_factory=lambda: DEFAULT_BROWSER_ARTIFACTS_DIR)
    """Directory where snapshots and screenshots are persisted."""

    @classmethod
    def from_env(cls) -> BrowserConnectionConfig:
        """Build a ``BrowserConnectionConfig`` from environment variables and defaults."""
        raw_mode = (
            os.environ.get("TALKOPS_BROWSER_MODE")
            or os.environ.get("TALKOPS_BROWSER_PROVIDER")
            or "auto"
        ).strip().lower()
        if raw_mode not in {"auto", "auto_connect", "persistent", "isolated", "browserbase"}:
            raw_mode = "auto"

        cdp_url = (
            os.environ.get("TALKOPS_BROWSER_WS_URL")
            or os.environ.get("AGY_BROWSER_WS_URL")
            or os.environ.get("TALKOPS_BROWSER_CDP_URL")
            or None
        )
        raw_user_data_dir = os.environ.get("TALKOPS_BROWSER_USER_DATA_DIR")
        user_data_dir = Path(raw_user_data_dir).expanduser() if raw_user_data_dir else None

        raw_headless = os.environ.get("TALKOPS_BROWSER_HEADLESS", "").strip().lower()
        headless = raw_headless in {"1", "true", "yes"} or raw_mode == "isolated"

        raw_port = os.environ.get("TALKOPS_BROWSER_PORT")
        port = int(raw_port) if raw_port and raw_port.isdigit() else None

        chrome_path = (
            os.environ.get("TALKOPS_CHROME_PATH")
            or os.environ.get("JETSKI_EVAL_CHROME_EXECUTABLE_PATH")
            or os.environ.get("CHROME_PATH")
            or None
        )

        bb_api_key = os.environ.get("BROWSERBASE_API_KEY") or None
        bb_project_id = os.environ.get("BROWSERBASE_PROJECT_ID") or None
        bb_session_id = os.environ.get("BROWSERBASE_SESSION_ID") or None

        if bb_api_key and raw_mode == "auto" and os.environ.get("TALKOPS_BROWSER_PREFER_CLOUD") == "1":
            raw_mode = "browserbase"

        return cls(
            mode=raw_mode,  # type: ignore[arg-type]
            cdp_url=cdp_url,
            user_data_dir=user_data_dir,
            headless=headless,
            port=port,
            chrome_path=chrome_path,
            browserbase_api_key=bb_api_key,
            browserbase_project_id=bb_project_id,
            browserbase_session_id=bb_session_id,
        )


class BrowserSessionManager:
    """Thread-safe lifecycle manager for TalkOps Stagehand browser sessions and facade tools.

    Maintains persistent connection state across turns within a conversation/subagent run,
    reopens the facade if agent code calls ``browser.close()``, and detaches cleanly without
    terminating the user's default Chrome browser when connected via ``DevToolsActivePort``.
    """

    _instance: BrowserSessionManager | None = None
    _instance_lock = threading.Lock()

    def __init__(self, config: BrowserConnectionConfig | None = None) -> None:
        """Initialize BrowserSessionManager."""
        self.config = config or BrowserConnectionConfig.from_env()
        self._async_lock = asyncio.Lock()
        self._browser: Any | None = None
        self._stagehand: Any | None = None
        self._facade: Any | None = None
        self._connection_tier: str | None = None
        self._connected_endpoint: str | None = None

    @classmethod
    def get_instance(cls, config: BrowserConnectionConfig | None = None) -> BrowserSessionManager:
        """Return the singleton ``BrowserSessionManager`` instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(config=config)
        elif config is not None:
            cls._instance.config = config
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for unit tests)."""
        with cls._instance_lock:
            cls._instance = None

    @property
    def is_connected(self) -> bool:
        """Return True if a Stagehand browser session is currently active."""
        return self._stagehand is not None and self._facade is not None

    @property
    def connection_tier(self) -> str | None:
        """Return the active connection tier label (``tier1_cdp``, ``tier2_auto_connect``, ``tier3_persistent``, ``tier3_isolated``, ``tier3_browserbase``)."""
        return self._connection_tier

    @property
    def connected_endpoint(self) -> str | None:
        """Return the active CDP URL or profile path."""
        return self._connected_endpoint

    def status_summary(self) -> dict[str, Any]:
        """Return a diagnostic dictionary describing current browser session state."""
        discovered = discover_devtools_active_port(self.config.user_data_dir)
        return {
            "connected": self.is_connected,
            "mode": self.config.mode,
            "connection_tier": self._connection_tier,
            "endpoint": self._connected_endpoint,
            "devtools_active_port_detected": discovered is not None,
            "devtools_active_port_path": str(discovered[1]) if discovered else None,
            "persistent_profile_dir": str(self.config.user_data_dir or DEFAULT_BROWSER_PROFILE_DIR),
            "artifacts_dir": str(self.config.artifacts_dir),
        }

    async def _connect_stagehand_browser(self) -> tuple[Any, str, str]:
        """Resolve and connect/launch a ``StagehandBrowser`` using the 3-tier cascade.

        Returns:
            ``(stagehand_browser, tier_label, endpoint_description)``
        """
        from stagehand.browser import browserbase, local_browser

        cfg = self.config

        # ── Mode: Browserbase Cloud ──────────────────────────────────────────
        if cfg.mode == "browserbase":
            api_key = cfg.browserbase_api_key or ""
            if cfg.browserbase_session_id:
                browser = await browserbase.connect(
                    api_key=api_key,
                    session_id=cfg.browserbase_session_id,
                )
                return browser, "tier3_browserbase", f"browserbase:{cfg.browserbase_session_id}"
            browser = await browserbase.launch(
                api_key=api_key,
                keep_alive=True,
            )
            return browser, "tier3_browserbase", "browserbase:new_session"

        # ── Tier 1: Explicit CDP URL / TALKOPS_BROWSER_WS_URL / AGY_BROWSER_WS_URL ──
        explicit_cdp = (
            cfg.cdp_url
            or os.environ.get("TALKOPS_BROWSER_WS_URL")
            or os.environ.get("AGY_BROWSER_WS_URL")
            or os.environ.get("TALKOPS_BROWSER_CDP_URL")
        )
        if explicit_cdp and cfg.mode in {"auto", "auto_connect"}:
            logger.info("[BrowserSession] Tier 1: Connecting to explicit CDP endpoint %s", explicit_cdp)
            browser = await local_browser.connect(cdp_url=explicit_cdp)
            return browser, "tier1_cdp", explicit_cdp

        # ── Tier 2: User's Live Default Chrome via DevToolsActivePort (--autoConnect) ──
        if cfg.mode in {"auto", "auto_connect"}:
            discovered = discover_devtools_active_port(cfg.user_data_dir)
            if discovered is not None:
                ws_url, port_file = discovered
                try:
                    logger.info(
                        "[BrowserSession] Tier 2: Auto-connecting to user Chrome via %s (%s)",
                        port_file,
                        ws_url,
                    )
                    browser = await local_browser.connect(cdp_url=ws_url)
                    return browser, "tier2_auto_connect", ws_url
                except Exception as exc:
                    logger.warning(
                        "[BrowserSession] DevToolsActivePort found at %s (%s) but connection failed: %s",
                        port_file,
                        ws_url,
                        exc,
                    )
                    if cfg.mode == "auto_connect":
                        raise BrowserConnectionError(
                            f"Could not connect to Chrome via {port_file} ({ws_url}). "
                            "Check if Chrome is running and remote debugging is enabled by going to "
                            "chrome://inspect/#remote-debugging."
                        ) from exc
            elif cfg.mode == "auto_connect":
                target_dir = cfg.user_data_dir or (
                    default_chrome_user_data_dirs()[0]
                    if default_chrome_user_data_dirs()
                    else Path("~/.config/google-chrome")
                )
                raise BrowserConnectionError(
                    f"Could not connect to Chrome in {target_dir}. "
                    "Check if Chrome is running and remote debugging is enabled by going to "
                    "chrome://inspect/#remote-debugging."
                )

        # ── Tier 3a: Isolated Ephemeral Launch (CI / Eval mode) ──────────────
        if cfg.mode == "isolated":
            isolated_args = [
                *cfg.launch_args,
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
            logger.info("[BrowserSession] Tier 3 (isolated): Launching headless isolated Chrome")
            browser = await local_browser.launch(
                executable_path=cfg.chrome_path,
                headless=True,
                port=cfg.port,
                preserve_user_data_dir=False,
                args=isolated_args,
                keep_alive=False,
            )
            return browser, "tier3_isolated", "isolated:headless"

        # ── Tier 3b: Persistent TalkOps Browser Profile (~/.talkops/browser-profile) ──
        profile_dir = (cfg.user_data_dir or DEFAULT_BROWSER_PROFILE_DIR).expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "[BrowserSession] Tier 3 (persistent): Launching Chrome with persistent profile at %s",
            profile_dir,
        )
        browser = await local_browser.launch(
            executable_path=cfg.chrome_path,
            user_data_dir=str(profile_dir),
            preserve_user_data_dir=True,
            headless=cfg.headless,
            port=cfg.port,
            args=list(cfg.launch_args) if cfg.launch_args else None,
            keep_alive=True,
        )
        return browser, "tier3_persistent", str(profile_dir)

    async def get_facade(self) -> Any:
        """Return the active ``StagehandFacadeTools`` instance, initializing it lazily if needed."""
        if (
            self._facade is not None
            and self._stagehand is not None
            and (self._browser is None or getattr(self._browser, "connected", True))
        ):
            return self._facade

        async with self._async_lock:
            if (
                self._facade is not None
                and self._stagehand is not None
                and (self._browser is None or getattr(self._browser, "connected", True))
            ):
                return self._facade

            self._facade = None
            self._stagehand = None
            self._browser = None

            from stagehand import Stagehand
            from stagehand_middleware.facade.tools import StagehandFacadeTools

            browser, tier, endpoint = await self._connect_stagehand_browser()
            stagehand = await Stagehand.create(browser=browser)

            artifacts_root = self.config.artifacts_dir.expanduser().resolve()
            artifacts_root.mkdir(parents=True, exist_ok=True)

            async def _reopen_on_close() -> None:
                """Intercept ``browser.close()`` inside ``run`` so the shared browser stays alive."""
                self._facade = StagehandFacadeTools(
                    self._stagehand,
                    artifact_root=str(artifacts_root),
                    on_close_requested=_reopen_on_close,
                )

            facade = StagehandFacadeTools(
                stagehand,
                artifact_root=str(artifacts_root),
                on_close_requested=_reopen_on_close,
            )

            self._browser = browser
            self._stagehand = stagehand
            self._facade = facade
            self._connection_tier = tier
            self._connected_endpoint = endpoint
            return self._facade

    async def disconnect(self) -> None:
        """Disconnect from the browser session without closing the user's running Chrome."""
        async with self._async_lock:
            browser = self._browser
            self._facade = None
            self._stagehand = None
            self._browser = None
            self._connection_tier = None
            self._connected_endpoint = None

            if browser is not None:
                try:
                    await browser.disconnect()
                except Exception as exc:
                    logger.debug("[BrowserSession] Error disconnecting StagehandBrowser: %s", exc)

    async def close(self) -> None:
        """Close the browser session (detaches if connected to user Chrome; closes if isolated)."""
        async with self._async_lock:
            tier = self._connection_tier
            stagehand = self._stagehand
            browser = self._browser
            self._facade = None
            self._stagehand = None
            self._browser = None
            self._connection_tier = None
            self._connected_endpoint = None

            if tier in {"tier1_cdp", "tier2_auto_connect"}:
                if browser is not None:
                    try:
                        await browser.disconnect()
                    except Exception as exc:
                        logger.debug("[BrowserSession] Error disconnecting StagehandBrowser: %s", exc)
            else:
                if stagehand is not None:
                    try:
                        await stagehand.close()
                    except Exception as exc:
                        logger.debug("[BrowserSession] Error closing Stagehand client: %s", exc)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception as exc:
                        logger.debug("[BrowserSession] Error closing StagehandBrowser: %s", exc)


__all__ = [
    "BrowserConnectionConfig",
    "BrowserConnectionError",
    "BrowserConnectionMode",
    "BrowserSessionManager",
    "DEFAULT_BROWSER_ARTIFACTS_DIR",
    "DEFAULT_BROWSER_PROFILE_DIR",
    "default_chrome_user_data_dirs",
    "discover_devtools_active_port",
    "parse_devtools_active_port_file",
]
