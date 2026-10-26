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

"""Authentication command handlers for TalkOps."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import Any

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _is_env_truthy(var_name: str) -> bool:
    """Return True if environment variable `var_name` is set to an enabled truthy value."""
    return os.environ.get(var_name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _has_stored_key_in_dotenv(env_path: Any, env_keys: set[str]) -> bool:
    """Return True if any key in `env_keys` is persisted in `env_path`."""
    try:
        if not env_path or not env_path.exists():
            return False
        lines = env_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if any(
                stripped.startswith(f"{k}=") or stripped.startswith(f"export {k}=")
                for k in env_keys
            ):
                return True
    except Exception:
        pass
    return False


def check_logout_mode_guard(
    provider_arg: str | None = None,
    *,
    settings: Any = None,
    storage: Any = None,
) -> tuple[str, str] | None:
    """Evaluate pre-logout mode guards for TalkOps authentication.

    Checks whether the current TalkOps authentication state is governed by a
    stateless environment mode (`TALKOPS_ADC_AUTH`, TalkOps Enterprise Gateway,
    or a direct unstored `TALKOPS_API_KEY` / `TALKOPS_SERVER_API_KEY` in the
    shell environment) where there is no interactive stored session to log out of.

    Returns:
        `(alert_title, alert_body)` if logout should be blocked with a warning alert,
        or `None` if normal credential/session revocation should proceed.
    """
    del settings
    cleaned_arg = (provider_arg or "").strip().lower()
    if cleaned_arg in {"--force", "-f", "force"}:
        return None

    # Only apply TalkOps session mode guards when logging out of TalkOps Auth (default `/logout` or explicit `talkops`/`account`)
    is_talkops_target = cleaned_arg in {
        "",
        "talkops",
        "control_plane",
        "account",
        "server",
    }
    if not is_talkops_target:
        return None

    # 1. TalkOps Application Default / Workload Identity Credentials Mode Guard (`TALKOPS_ADC_AUTH`)
    if _is_env_truthy("TALKOPS_ADC_AUTH"):
        return (
            "ADC Logout Instructions",
            (
                "You are logged in to TalkOps using Application Default Credentials (TALKOPS_ADC_AUTH).\n"
                "To log out, please run `unset TALKOPS_ADC_AUTH` in your terminal and restart the CLI."
            ),
        )

    if storage is None:
        try:
            from talkops.auth.storage import get_credential_storage

            storage = get_credential_storage()
        except Exception:
            storage = None

    session = None
    stored_providers: dict[str, Any] = {}
    if storage is not None:
        with contextlib.suppress(Exception):
            session = storage.load_session()
        with contextlib.suppress(Exception):
            stored_providers = storage.list_provider_keys()

    auth_method = str(getattr(session, "auth_method", "") or "").strip().lower()

    # 2. TalkOps Enterprise Gateway Mode Guard (`TALKOPS_LLM_GATEWAY_URL` / `TALKOPS_GATEWAY_URL` / `auth_method == "gateway"`)
    gw_var = ""
    if os.environ.get("TALKOPS_LLM_GATEWAY_URL", "").strip():
        gw_var = "TALKOPS_LLM_GATEWAY_URL"
    elif os.environ.get("TALKOPS_GATEWAY_URL", "").strip():
        gw_var = "TALKOPS_GATEWAY_URL"

    if auth_method == "gateway" or gw_var:
        effective_gw_var = gw_var or "TALKOPS_LLM_GATEWAY_URL"
        return (
            "Enterprise Gateway Mode",
            (
                "You are connected to a TalkOps Enterprise Gateway, so there is no session to log out of.\n"
                f"To switch back to signing in with TalkOps, unset {effective_gw_var} (or remove gateway configuration from settings) and restart the CLI."
            ),
        )

    # 3. Direct TalkOps API Key Mode Guard (`auth_method == "direct_api_key"` or unstored `TALKOPS_API_KEY` / `TALKOPS_SERVER_API_KEY` in shell env)
    if auth_method == "direct_api_key":
        return (
            "TalkOps API Key Mode",
            (
                "You are using TalkOps directly with TALKOPS_API_KEY in your environment, so there is no stored session to log out of.\n"
                "To switch back to signing in with TalkOps, run `unset TALKOPS_API_KEY` in your terminal and restart the CLI."
            ),
        )

    env_talkops_var = ""
    if os.environ.get("TALKOPS_API_KEY", "").strip():
        env_talkops_var = "TALKOPS_API_KEY"
    elif os.environ.get("TALKOPS_SERVER_API_KEY", "").strip():
        env_talkops_var = "TALKOPS_SERVER_API_KEY"

    if (
        env_talkops_var
        and session is None
        and "talkops" not in stored_providers
    ):
        env_path = getattr(storage, "env_path", None) if storage is not None else None
        if not _has_stored_key_in_dotenv(
            env_path, {"TALKOPS_API_KEY", "TALKOPS_SERVER_API_KEY"}
        ):
            return (
                "TalkOps API Key Mode",
                (
                    f"You are using TalkOps directly with {env_talkops_var} in your environment, so there is no stored session to log out of.\n"
                    f"To switch back to signing in with TalkOps, run `unset {env_talkops_var}` in your terminal and restart the CLI."
                ),
            )

    return None


def _stop_streaming_and_reset_conversation(ctx: CommandContext) -> str | None:
    """Cancel active streaming turns and start a fresh conversation session on `ctx.app`."""
    app = ctx.app
    if app is None:
        return None

    # 1. Stop active streaming / background agent or shell workers (`StopStreamingConversation`)
    if hasattr(app, "_force_interrupt_active_work"):
        with contextlib.suppress(Exception):
            app._force_interrupt_active_work()

    prompts_ctrl = getattr(app, "_prompts", None)
    if prompts_ctrl is not None and hasattr(prompts_ctrl, "interrupt_active_prompt"):
        with contextlib.suppress(Exception):
            prompts_ctrl.interrupt_active_prompt()

    if hasattr(app, "clear_active_btw"):
        with contextlib.suppress(Exception):
            app.clear_active_btw()
    if hasattr(app, "_pending_messages") and app._pending_messages is not None:
        with contextlib.suppress(Exception):
            app._pending_messages.clear()
    if hasattr(app, "_queued_widgets") and app._queued_widgets is not None:
        with contextlib.suppress(Exception):
            app._queued_widgets.clear()
    if hasattr(app, "_sync_status_queued"):
        with contextlib.suppress(Exception):
            app._sync_status_queued()
    if hasattr(app, "_image_tracker") and app._image_tracker is not None:
        with contextlib.suppress(Exception):
            app._image_tracker.clear()

    # 2. Clear message history & start a fresh conversation thread (`StartConversation`)
    with contextlib.suppress(Exception):
        from talkops.ui.widgets.messages import MessageList

        messages = app.query_one("#messages", MessageList)
        messages.clear()
        if hasattr(messages, "scroll_home"):
            messages.scroll_home(animate=False)

    if hasattr(app, "_ensure_welcome_banner"):
        with contextlib.suppress(Exception):
            app._ensure_welcome_banner()

    old_tid = getattr(app, "_agent_thread_id", None)
    new_thread_id = str(uuid.uuid4())
    session_state = getattr(app, "_session_state", None)
    if session_state is not None and hasattr(session_state, "reset_thread"):
        with contextlib.suppress(Exception):
            new_thread_id = session_state.reset_thread()
    app._agent_thread_id = new_thread_id

    with contextlib.suppress(Exception):
        from talkops.state.session import (
            acquire_thread_presence,
            release_thread_presence,
        )
        from talkops.utils.logger import bind_logging_to_thread

        if old_tid and old_tid != new_thread_id:
            release_thread_presence(old_tid)
        if new_thread_id:
            acquire_thread_presence(new_thread_id)
        bind_logging_to_thread(new_thread_id)

    if hasattr(app, "_reset_ephemeral_mode_states"):
        with contextlib.suppress(Exception):
            app._reset_ephemeral_mode_states()
    if hasattr(app, "_restore_goal_rubric_state"):
        with contextlib.suppress(Exception):
            app._restore_goal_rubric_state({})
    if hasattr(app, "_reset_thread_usage"):
        with contextlib.suppress(Exception):
            app._reset_thread_usage(0.0, 0)

    with contextlib.suppress(Exception):
        from talkops.ui.widgets.status import StatusBar

        status_bar = app.query_one("#status-bar", StatusBar)
        status_bar.set_status("Ready")

    return new_thread_id


class LoginHandler(BaseCommandHandler):
    """Handler for /login — open auth manager to manage API keys."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/login"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/auth", "/connect")

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        from talkops.ui.widgets.auth_manager import AuthManagerScreen

        initial_provider = ctx.args.strip() or None
        screen = AuthManagerScreen(initial_provider=initial_provider)

        def _on_close(_result: Any) -> None:
            # Refocus chat input after auth manager closes
            if hasattr(ctx.app, "_chat_input") and ctx.app._chat_input:
                ctx.app._chat_input.focus_input()
            try:
                from talkops.ui.widgets.welcome import WelcomeBanner

                ctx.app.query_one(WelcomeBanner).refresh_auth()
            except Exception:
                pass
            if hasattr(ctx.app, "maybe_start_deferred_server"):
                asyncio.create_task(ctx.app.maybe_start_deferred_server())

        ctx.app.push_screen(screen, _on_close)
        return CommandResult(success=True, message="", mount_as_app_message=False)


class LogoutHandler(BaseCommandHandler):
    """Handler for /logout — guard stateless modes, revoke session & provider credentials, reset conversation, and transition TUI to AuthManagerScreen."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/logout"

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the `/logout` flow with pre-logout mode guards, session/provider cleanup, conversation reset, and TUI auth screen transition."""
        from talkops.auth.oauth_acquirer import cancel_active_oauth_flows
        from talkops.auth.storage import get_credential_storage
        from talkops.model.config import (
             canonicalize_provider,
            resolve_model_spec,
            revoke_provider_credentials,
        )
        from talkops.state.remote_session import (
            clear_remote_client_context,
            stop_background_token_refresh_task,
        )

        raw_arg = (ctx.args or "").strip()
        tokens = [t for t in raw_arg.split() if t not in {"--force", "-f"}]
        provider_arg = tokens[0] if tokens else None

        storage = get_credential_storage()

        # 1. Pre-Logout Mode Guards (TalkOps ADC, TalkOps Enterprise Gateway, Direct TalkOps API Key Mode)
        guard = check_logout_mode_guard(
            raw_arg or None,
            settings=ctx.settings,
            storage=storage,
        )
        if guard is not None:
            alert_title, alert_body = guard
            formatted = f"**{alert_title}**\n{alert_body}"
            return CommandResult(
                success=True,
                message=formatted,
                data={
                    "logged_out": False,
                    "alert_title": alert_title,
                    "alert_body": alert_body,
                    "is_warning": True,
                },
                mount_as_app_message=True,
                notify=f"{alert_title}: {alert_body.splitlines()[0]}",
                notify_severity="warning",
            )

        # 2. Cancel any in-flight TalkOps OAuth PKCE browser listener & background JWT refresh
        cancelled_oauth = cancel_active_oauth_flows()
        stop_background_token_refresh_task()

        norm_arg = (provider_arg or "").strip().lower().lstrip("-")
        # Default `/logout` (no args) logs out of TalkOps Auth (`talkops` Control Plane / Central Server).
        # Third-party LLM provider keys are only cleared when `/logout all` or `/logout <provider>` is given.
        clear_cp_session = norm_arg in {
            "",
            "all",
            "*",
            "talkops",
            "control_plane",
            "account",
            "server",
        }
        clear_providers = bool(
            norm_arg
            and norm_arg
            not in {
                "talkops",
                "control_plane",
                "account",
                "server",
            }
        )

        session_cleared = False
        if clear_cp_session:
            with contextlib.suppress(Exception):
                session_cleared = bool(storage.clear_session(clear_dotenv=True))
            clear_remote_client_context()
            if ctx.app is not None:
                with contextlib.suppress(Exception):
                    srv_handle = getattr(ctx.app, "_server_handle", None)
                    if srv_handle is not None and hasattr(srv_handle, "config"):
                        srv_handle.config.api_key = None

        cleared_providers: list[str] = []
        if clear_providers:
            target_prov = None if norm_arg in {"all", "*"} else provider_arg
            cleared_providers = revoke_provider_credentials(
                target_prov,
                settings=ctx.settings,
            )

        # Determine whether this logout affects the active TalkOps session or active model
        active_prov = ""
        if ctx.model_spec:
            active_prov, _ = resolve_model_spec(ctx.model_spec)
        elif ctx.app is not None and getattr(ctx.app, "_model", None):
            active_prov, _ = resolve_model_spec(getattr(ctx.app, "_model", None))

        is_full_or_active_logout = clear_cp_session or bool(
            active_prov
            and provider_arg
            and canonicalize_provider(provider_arg) == canonicalize_provider(active_prov)
        )

        new_thread_id: str | None = None
        transitioned_to_auth_screen = False

        # 3. Frontend TUI: Stop streaming conversation, reset thread, update banner & status bar, and transition to AuthManagerScreen
        if ctx.app is not None:
            if is_full_or_active_logout:
                new_thread_id = _stop_streaming_and_reset_conversation(ctx)

            try:
                from talkops.ui.widgets.welcome import WelcomeBanner

                banner = ctx.app.query_one(WelcomeBanner)
                banner.refresh_auth()
                if clear_cp_session:
                    if hasattr(banner, "set_logged_out"):
                        banner.set_logged_out()
                    else:
                        banner.set_warning()
                elif is_full_or_active_logout:
                    banner.set_warning()
            except Exception:
                pass

            if is_full_or_active_logout:
                with contextlib.suppress(Exception):
                    from talkops.ui.widgets.status import StatusBar

                    status_bar = ctx.app.query_one("#status-bar", StatusBar)
                    status_bar.set_status("● Logged out · /login to sign in")

            if is_full_or_active_logout and hasattr(ctx.app, "push_screen"):
                try:
                    from talkops.ui.widgets.auth_manager import AuthManagerScreen

                    initial_prov = (
                        "talkops"
                        if clear_cp_session
                        else (
                            canonicalize_provider(provider_arg)
                            if provider_arg
                            else (active_prov or "talkops")
                        )
                    )
                    screen = AuthManagerScreen(initial_provider=initial_prov)

                    def _on_close(_result: Any) -> None:
                        if hasattr(ctx.app, "_chat_input") and ctx.app._chat_input:
                            with contextlib.suppress(Exception):
                                ctx.app._chat_input.focus_input()
                        with contextlib.suppress(Exception):
                            from talkops.ui.widgets.welcome import WelcomeBanner

                            ctx.app.query_one(WelcomeBanner).refresh_auth()
                        if hasattr(ctx.app, "maybe_start_deferred_server"):
                            with contextlib.suppress(Exception):
                                asyncio.create_task(ctx.app.maybe_start_deferred_server())

                    ctx.app.push_screen(screen, _on_close)
                    transitioned_to_auth_screen = True
                except Exception:
                    logger.debug("Could not push AuthManagerScreen after /logout", exc_info=True)

        saved_wif = storage.get_saved_wif_provider()
        data_payload = {
            "logged_out": bool(session_cleared or cleared_providers),
            "session_cleared": session_cleared,
            "providers_cleared": cleared_providers,
            "cancelled_oauth_flows": cancelled_oauth,
            "new_thread_id": new_thread_id,
            "transitioned_to_auth_screen": transitioned_to_auth_screen,
            "saved_wif_provider": saved_wif,
        }

        next_steps_block = (
            "\n\n**What's next to log back in:**\n"
            "- **In this TUI:** Run `/login` (or `/auth`), select **TalkOps Control Plane**, and enter your API key (`oc_live_...`).\n"
            "- **Via Browser SSO (CLI):** Run `talkops auth login --sso` in your terminal.\n"
            "- **Via API Key (CLI):** Run `talkops auth login --api-key <oc_live_...>`."
        )

        if session_cleared or cleared_providers:
            parts: list[str] = []
            if session_cleared:
                parts.append("TalkOps Control Plane session")
            if cleared_providers:
                parts.append(f"provider credentials (`{', '.join(cleared_providers)}`)")
            summary = " and ".join(parts)
            return CommandResult(
                success=True,
                message=(
                    f"**Logged Out of TalkOps**\n"
                    f"Cleared {summary}."
                    f"{next_steps_block}"
                ),
                data=data_payload,
                mount_as_app_message=True,
                notify="Logged out of TalkOps. Run /login to sign back in.",
                notify_severity="warning",
            )

        return CommandResult(
            success=True,
            message=(
                "**Logged Out of TalkOps**\n"
                "No active stored session or credentials were found."
                f"{next_steps_block}"
            ),
            data=data_payload,
            mount_as_app_message=True,
            notify="Logged out. Run /login to sign in.",
            notify_severity="warning",
        )


__all__ = ["LoginHandler", "LogoutHandler", "check_logout_mode_guard"]
