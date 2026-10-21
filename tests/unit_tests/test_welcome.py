"""Unit tests for Welcome Banner widget."""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult

from talkops.ui.widgets.welcome import (
    WelcomeBanner,
    _BannerInfo,
    _InfoPanel,
    _LeftPanel,
)


def _wordmark_glyph_rows() -> tuple[str, ...]:
    from talkops.ui.widgets.welcome import _WORDMARK_SUBPIXELS, _subpixels_to_glyph_rows

    return _subpixels_to_glyph_rows(_WORDMARK_SUBPIXELS)


class WelcomeHarnessApp(App[None]):
    """App mounting WelcomeBanner to test rendering and lifecycle."""

    def __init__(self):
        super().__init__()
        from talkops.ui.theme import register_app_themes

        register_app_themes(self)
        self.banner = WelcomeBanner()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        from talkops.ui.theme import get_css_variable_defaults, get_theme_colors

        colors = get_theme_colors(self)
        return get_css_variable_defaults(colors=colors)

    def compose(self) -> ComposeResult:
        yield self.banner


@pytest.mark.asyncio
async def test_welcome_banner_renders_cleanly():
    """Verify WelcomeBanner mounts two panels without error."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        assert app.banner is not None
        left = app.banner.query_one(_LeftPanel)
        right = app.banner.query_one(_InfoPanel)
        assert left is not None
        assert right is not None


@pytest.mark.asyncio
async def test_welcome_banner_environment_info_update():
    """Verify WelcomeBanner dynamically updates model and cloud profile."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        app.banner.update_model("google_genai:gemini-3.7-flash")
        app.banner.update_cloud(profile="krayak", region="ap-south-1")
        await pilot.pause()

        assert app.banner._info.model_spec == "google_genai:gemini-3.7-flash"
        assert app.banner._info.cloud_profile == "krayak"
        assert app.banner._info.cloud_region == "ap-south-1"


@pytest.mark.asyncio
async def test_left_panel_animation_continues_and_reflects_health():
    """Verify _LeftPanel animation timer keeps running and sweeps reflect health state."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        left_panel = app.banner.query_one(_LeftPanel)
        left_panel._cycle_count = 2
        left_panel.status = "connected"

        # Advance beam past logo width
        left_panel.beam_x = 64
        left_panel._advance_beam()

        # Timer should keep running to display the continuous light beam
        assert left_panel._anim_timer is not None
        assert left_panel.beam_x == -6
        await pilot.pause()

        # Test status transitions reflect proper health line colors
        app.banner.set_warning()
        await pilot.pause()
        assert left_panel.status == "warning"

        app.banner.set_connected()
        await pilot.pause()
        assert left_panel.status == "connected"


@pytest.mark.asyncio
async def test_welcome_banner_displays_logo_tagline_and_control_plane_user(
    tmp_path, monkeypatch
):
    """Verify WelcomeBanner renders the TalkOps logo, 'DevOps on Demand', and Control Plane user."""
    import json

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "session": {
                    "api_key": "oc_live_test_secret",
                    "email": "sandeepmahto3@gmail.com",
                    "user_id": "usr_01J8Z9X2M4P1",
                    "user_tier": "individual",
                    "control_plane_url": "http://localhost:8000",
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("talkops.config.paths.AUTH_PATH", auth_file)

    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        left_panel = app.banner.query_one(_LeftPanel)
        app.banner.set_connected()
        await pilot.pause()
        rendered = left_panel.render()
        rendered_plain = "".join(getattr(r, "plain", "") for r in rendered.renderables)

        wordmark = _wordmark_glyph_rows()
        assert wordmark[1] in rendered_plain
        assert "D E V O P S   O N   D E M A N D" in rendered_plain
        assert "sandeepmahto3@gmail.com (Individual)" in rendered_plain
        assert "by talkops.ai" not in rendered_plain
        logo_renderable: Any = rendered.renderables[0]
        assert logo_renderable.no_wrap is True
        logo_lines = logo_renderable.plain.splitlines()
        assert len(logo_lines) == 8
        for row in range(3):
            assert wordmark[row] in logo_lines[row]
        assert "D E V O P S   O N   D E M A N D" in logo_lines[3]
        assert "─" * 10 in logo_lines[4]
        assert "sandeepmahto3@gmail.com (Individual)" in logo_lines[5]
        assert "v0." in logo_lines[6] and "ready" in logo_lines[6]

        # Test explicit update_user override
        app.banner.update_user("alice@company.com", "enterprise")
        await pilot.pause()
        updated_plain = "".join(
            getattr(r, "plain", "") for r in left_panel.render().renderables
        )
        assert "alice@company.com (Enterprise)" in updated_plain


def test_welcome_logo_preserves_brand_colors_in_all_status_states():
    """Verify the TalkOps logo keeps its #1F7ACB blue and #7AC143 green brand colors in starting/error states."""
    from talkops.ui.widgets.welcome import _LOGO_BLUE, _LOGO_GREEN, _build_logo_text

    for st in ("connected", "starting", "error"):
        logo = _build_logo_text(beam_x=-10, status=st, user_display="sandeepmahto3@gmail.com (Individual)")
        span_styles = " ".join(str(s.style) for s in logo.spans)
        assert _LOGO_BLUE.lower() in span_styles.lower()
        assert _LOGO_GREEN.lower() in span_styles.lower()


def test_ensure_cli_authenticated_clears_revoked_key_and_triggers_sso(tmp_path, monkeypatch):
    """Verify _ensure_cli_authenticated detects a 401 revoked key from Control Plane and re-authenticates via SSO."""
    import argparse
    import json
    from talkops.cli.main import _ensure_cli_authenticated

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "session": {
                    "api_key": "oc_live_revoked_old_key",
                    "email": "sandeepmahto3@gmail.com",
                    "user_id": "usr_01J8Z9X2M4P1",
                    "user_tier": "individual",
                    "control_plane_url": "http://localhost:8000",
                    "expires_at": 0.0,
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("talkops.config.paths.AUTH_PATH", auth_file)
    monkeypatch.setenv("TALKOPS_ENFORCE_CLI_AUTH_IN_TESTS", "1")
    monkeypatch.setenv("TALKOPS_CONTROL_PLANE_URL", "http://localhost:8000")
    monkeypatch.setenv("TALKOPS_API_KEY", "oc_live_revoked_old_key")

    async def _fake_exchange(*_args, **_kwargs):
        raise PermissionError("Invalid or revoked API token (rejected by Control Plane) [token_revoked]")

    def _fake_sso_login(login_args, output_format="text"):
        assert login_args.sso_provider == "sso"
        assert login_args.login_hint == "sandeepmahto3@gmail.com"
        auth_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "session": {
                        "access_token": "eyJhbGciOiJSUzI1NiJ9.fresh.sig",
                        "api_key": "oc_live_fresh_key",
                        "email": "sandeepmahto3@gmail.com",
                        "user_id": "usr_01J8Z9X2M4P1",
                        "user_tier": "individual",
                        "control_plane_url": "http://localhost:8000",
                        "expires_at": 9999999999.0,
                    },
                    "providers": {},
                }
            ),
            encoding="utf-8",
        )
        return 0

    import talkops.cli.commands.auth  # noqa: F401 — import before monkeypatching remote_session

    monkeypatch.setattr(
        "talkops.state.remote_session.exchange_api_key_for_temporary_token",
        _fake_exchange,
    )
    monkeypatch.setattr(
        "talkops.cli.commands.auth.run_auth_login_command",
        _fake_sso_login,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    args = argparse.Namespace(api_key=None, user_id=None, server_url=None)
    rc = _ensure_cli_authenticated(args)
    assert rc == 0
    assert args.api_key == "eyJhbGciOiJSUzI1NiJ9.fresh.sig"
    assert args.user_id == "usr_01J8Z9X2M4P1"




# ── Logo icon: block-art fallback geometry + graphics-protocol mode ──────────


def test_icon_subpixel_canvas_matches_grid():
    """The sub-pixel art must be exactly 2x the 20x8 cell grid with only B/G/. codes."""
    from talkops.ui.widgets.welcome import (
        _ICON_ASCII,
        _ICON_ASCII_COLORS,
        _ICON_COLS,
        _ICON_ROWS,
        _ICON_SUBPIXELS,
        _ICON_UNICODE,
    )

    assert len(_ICON_SUBPIXELS) == _ICON_ROWS * 2
    assert all(len(row) == _ICON_COLS * 2 for row in _ICON_SUBPIXELS)
    assert set("".join(_ICON_SUBPIXELS)) <= {"B", "G", "."}
    assert len(_ICON_UNICODE) == _ICON_ROWS
    assert all(len(row) == _ICON_COLS for row in _ICON_UNICODE)
    assert len(_ICON_ASCII) == len(_ICON_ASCII_COLORS) == _ICON_ROWS
    for art, mask in zip(_ICON_ASCII, _ICON_ASCII_COLORS):
        assert len(art) == len(mask) == _ICON_COLS
        # every visible ASCII glyph has a colour, every blank has none
        assert all((ch == " ") == (m == " ") for ch, m in zip(art, mask))


def test_icon_rows_have_uniform_width_so_text_stack_aligns():
    """Every rendered icon row is exactly _ICON_COLS cells before the brand text."""
    from talkops.ui.widgets.welcome import _ICON_COLS, _build_logo_text

    logo = _build_logo_text(beam_x=-10, status="connected", user_display="a@b.co (Team)")
    lines = logo.plain.splitlines()
    assert len(lines) == 8
    for line in lines:
        assert len(line) == _ICON_COLS or line[_ICON_COLS : _ICON_COLS + 2] == "  "
    wordmark = _wordmark_glyph_rows()
    for row in range(3):
        assert lines[row][_ICON_COLS + 2 :] == wordmark[row]


def test_full_icon_cells_paint_background_to_avoid_line_spacing_seams():
    """Solid single-colour cells must set bg too, so line-spacing pads are filled."""
    from talkops.ui.widgets.welcome import _LOGO_BLUE, _build_logo_text

    logo = _build_logo_text(beam_x=-100, status="connected", user_display="")
    full_block_styles = [
        str(span.style).lower()
        for span in logo.spans
        if logo.plain[span.start : span.end] == "█"
    ]
    assert full_block_styles, "side walls / wordmark stems should render as full blocks"
    assert any(f"on {_LOGO_BLUE}".lower() in s for s in full_block_styles)
    for style in full_block_styles:
        fg = style.split()[1]
        assert style.endswith(f"on {fg}"), style


def test_build_logo_text_without_icon_renders_only_text_stack():
    from talkops.ui.widgets.welcome import _build_logo_text

    logo = _build_logo_text(status="connected", user_display="a@b.co", show_icon=False)
    lines = logo.plain.split("\n")
    assert len(lines) == 8
    wordmark = _wordmark_glyph_rows()
    assert lines[:3] == list(wordmark)
    assert lines[3].strip() == "D E V O P S   O N   D E M A N D"
    assert lines[5] == "a@b.co"
    # no icon: nothing to the left of the wordmark
    assert all(not line.startswith("  ") for line in lines[:3])


def test_logo_image_probe_is_noop_without_tty(monkeypatch):
    """Under pytest stdin/stdout are not TTYs: never probe, never create a widget."""
    from talkops.ui.widgets import logo_image

    logo_image._reset_for_tests()
    try:
        assert logo_image.probe_graphics_protocol() is None
        assert logo_image.graphics_protocol() is None
        assert logo_image.create_logo_image_widget() is None
    finally:
        logo_image._reset_for_tests()


def test_logo_image_env_var_disables_probe(monkeypatch):
    from talkops.ui.widgets import logo_image

    monkeypatch.setenv("TALKOPS_LOGO_IMAGE", "0")
    assert logo_image._eligible_for_probe() is False


def test_logo_image_asset_is_bundled():
    from PIL import Image

    from talkops.ui.widgets.logo_image import LOGO_ICON_PATH

    assert LOGO_ICON_PATH.is_file()
    with Image.open(LOGO_ICON_PATH) as img:
        assert img.mode == "RGBA"
        assert img.width > 200 and img.height > 200


def test_tgp_only_trusted_in_kitty_and_ghostty(monkeypatch):
    from talkops.ui.widgets import logo_image

    for var in ("KITTY_WINDOW_ID", "GHOSTTY_RESOURCES_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    assert logo_image._tgp_placeholders_trusted() is False
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    assert logo_image._tgp_placeholders_trusted() is True
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
    monkeypatch.setenv("TERM", "xterm-kitty")
    assert logo_image._tgp_placeholders_trusted() is True


@pytest.mark.asyncio
async def test_welcome_banner_uses_image_widget_when_graphics_supported(monkeypatch):
    """With a detected protocol the banner shows the PNG + a text-only _LeftPanel."""
    from textual.widget import Widget

    from talkops.ui.widgets import welcome

    class _FakeImage(Widget):
        pass

    monkeypatch.setattr(
        welcome, "create_logo_image_widget", lambda: _FakeImage(id="welcome-logo-image")
    )
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        assert app.banner.query_one("#welcome-logo-image") is not None
        left = app.banner.query_one(_LeftPanel)
        assert left._show_icon is False
        plain = "".join(getattr(r, "plain", "") for r in left.render().renderables)
        assert plain.splitlines()[0] == _wordmark_glyph_rows()[0]


def test_wordmark_is_larger_than_tagline_and_fits_panel():
    """Wordmark spans 3 rows (3x the 1-row tagline) and the lockup fits beside the icon."""
    from talkops.ui.widgets.welcome import (
        _ICON_COLS,
        _TAGLINE_LOCKUP,
        _WORDMARK_COLS,
        _WORDMARK_FONT,
        _WORDMARK_ROWS,
        _WORDMARK_SUBPIXELS,
        _WORDMARK_TEXT,
    )

    assert _WORDMARK_ROWS == 3
    assert len(_WORDMARK_SUBPIXELS) == 6
    assert set(_WORDMARK_TEXT) <= set(_WORDMARK_FONT)
    assert all(len(g) == 6 and len(set(map(len, g))) == 1 for g in _WORDMARK_FONT.values())
    assert len(_TAGLINE_LOCKUP) <= _WORDMARK_COLS
    # left panel is 60 wide with 1 col padding
    assert 1 + _ICON_COLS + 2 + _WORDMARK_COLS <= 60


def test_wordmark_switches_to_dark_ink_on_light_backgrounds():
    from talkops.ui.widgets.welcome import _build_logo_text

    dark = _build_logo_text(beam_x=-100, user_display="", panel_bg="#11121D")
    light = _build_logo_text(beam_x=-100, user_display="", panel_bg="#F5F5F7")
    assert "#ffffff" in " ".join(str(s.style).lower() for s in dark.spans)
    assert "#0f172a" in " ".join(str(s.style).lower() for s in light.spans)


@pytest.mark.asyncio
async def test_inprocess_server_restart_silently_refreshes_expired_jwt(tmp_path, monkeypatch):
    """When config.api_key holds an expired temporary JWT on restart, astart() silently refreshes using stored.api_key."""
    import json
    import time
    from types import SimpleNamespace
    import jwt

    from talkops.cli.server_manager import InProcessServerHandle
    from talkops.server import ServerConfig
    from talkops.server.auth import get_jwt_secret, issue_temporary_jwt
    from talkops.state.remote_session import (
        clear_remote_client_context,
        stop_background_token_refresh_task,
    )

    clear_remote_client_context()
    expired_jwt = jwt.encode(
        {
            "sub": "usr_01J8Z9X2M4P1",
            "user_id": "usr_01J8Z9X2M4P1",
            "iat": int(time.time()) - 1800,
            "exp": int(time.time()) - 300,
        },
        get_jwt_secret(),
        algorithm="HS256",
    )
    fresh_jwt_info = issue_temporary_jwt("usr_01J8Z9X2M4P1", ttl_seconds=900)
    fresh_jwt = fresh_jwt_info["access_token"]

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "session": {
                    "access_token": expired_jwt,
                    "api_key": "oc_live_valid_long_lived_key",
                    "email": "sandeepmahto3@gmail.com",
                    "user_id": "usr_01J8Z9X2M4P1",
                    "user_tier": "pro",
                    "control_plane_url": "http://localhost:8000",
                    "expires_at": time.time() - 300.0,
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("talkops.config.paths.AUTH_PATH", auth_file)
    monkeypatch.setenv("TALKOPS_ENFORCE_CLI_AUTH_IN_TESTS", "1")
    monkeypatch.setenv("TALKOPS_CONTROL_PLANE_URL", "http://localhost:8000")
    monkeypatch.setenv("TALKOPS_API_KEY", expired_jwt)

    exchanged_with_keys: list[str] = []

    async def _fake_exchange(
        server_url=None,
        *,
        api_key=None,
        user_id=None,
        control_plane_url=None,
        timeout=10.0,
        force_refresh=False,
        raise_on_unauthorized=False,
    ):
        exchanged_with_keys.append(str(api_key))
        assert api_key == "oc_live_valid_long_lived_key"
        return fresh_jwt

    async def _fake_build_runtime(*, checkpointer=None, store=None):
        return SimpleNamespace(agent="compiled_graph", backend="local_backend")

    monkeypatch.setattr(
        "talkops.state.remote_session.exchange_api_key_for_temporary_token",
        _fake_exchange,
    )
    monkeypatch.setattr(
        "talkops.server.server_graph._build_default_runtime_async",
        _fake_build_runtime,
    )

    config = ServerConfig(api_key=expired_jwt, user_id="usr_01J8Z9X2M4P1")
    handle = InProcessServerHandle(config)
    try:
        url = await handle.restart()
        assert url == "inprocess://local"
        assert exchanged_with_keys == ["oc_live_valid_long_lived_key"]
        assert handle.config.api_key == fresh_jwt
    finally:
        stop_background_token_refresh_task()
        await handle.astop()
        clear_remote_client_context()

