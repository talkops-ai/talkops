"""Auth management CLI command suite (`talkops auth`).

Supports:
- `talkops auth` / `talkops auth aws`: AWS STS caller-identity check (backward-compatible default).
- `talkops auth status`: Unified Control Plane session lease, 22+ Model Providers, and AWS STS status.
- `talkops auth login`: Authenticate with the TalkOps Control Plane / Central Server using an API key,
  verify it via temporary JWT exchange (`POST /api/v1/auth/token`), and persist the session to
  `~/.talkops/.state/auth.json` (`0600`) + OS Keyring + `~/.talkops/.env`.
- `talkops auth logout`: Clear stored Control Plane session lease and/or model provider credentials.
- `talkops auth token`: Print or refresh the active short-lived temporary JWT (`eyJ...`).
- `talkops auth list`: List all model providers and their credential status.
- `talkops auth set <provider> [key]`: Save a model provider API key to `CompositeCredentialStorage`.
- `talkops auth remove <provider>`: Remove a model provider API key from `CompositeCredentialStorage`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import time
from typing import Any

from rich.console import Console
from rich.table import Table

from talkops.auth.models import StoredSession, hash_api_key, mask_api_key
from talkops.auth.oauth_acquirer import BrowserAndPasteTokenAcquirer
from talkops.auth.storage import get_credential_storage
from talkops.config.aws import get_aws_context
from talkops.model.config import (
    PROVIDER_API_KEY_ENV,
    PROVIDER_DISPLAY_NAMES,
    get_provider_auth_status,
)
from talkops.output import OutputFormat, write_json
from talkops.state.remote_session import (
    clear_remote_client_context,
    exchange_api_key_for_temporary_token,
    get_cached_temporary_token,
    get_remote_client_config,
    set_remote_client_context,
)


def setup_auth_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    add_output_args: Any,
) -> argparse.ArgumentParser:
    """Register `talkops auth` and its subcommands on the root CLI parser."""
    auth_parser = subparsers.add_parser(
        "auth",
        help="Manage TalkOps Control Plane login, provider API keys, and AWS STS status.",
    )
    add_output_args(auth_parser)
    auth_parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS profile override for authentication check.",
    )
    auth_parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS region override for authentication check.",
    )

    auth_sub = auth_parser.add_subparsers(dest="auth_command")

    # talkops auth status
    status_p = auth_sub.add_parser(
        "status",
        help="Show Control Plane session lease, model providers, and AWS STS status.",
    )
    add_output_args(status_p)
    status_p.add_argument("--aws-profile", dest="aws_profile", type=str, default=argparse.SUPPRESS)
    status_p.add_argument("--aws-region", dest="aws_region", type=str, default=argparse.SUPPRESS)

    # talkops auth login
    login_p = auth_sub.add_parser(
        "login",
        help="Authenticate with TalkOps Control Plane / Central Server via Browser-Loopback SSO (PKCE) or API key.",
    )
    add_output_args(login_p)
    login_p.add_argument(
        "--api-key",
        dest="api_key",
        type=str,
        default=None,
        help="Control Plane API key (`oc_live_...`). Reads TALKOPS_API_KEY if omitted.",
    )
    login_p.add_argument(
        "--sso",
        dest="sso_provider",
        nargs="?",
        const="sso",
        default=None,
        help="Authenticate via Browser-Loopback + Paste PKCE SSO (optional provider: google, github, linkedin, enterprise).",
    )
    login_p.add_argument(
        "--email",
        "--login-hint",
        dest="login_hint",
        type=str,
        default=None,
        help="Optional email or corporate work email hint for SSO login (e.g. name@company.com).",
    )
    login_p.add_argument(
        "--no-browser",
        "--headless",
        dest="no_browser",
        action="store_true",
        default=False,
        help="Do not auto-open browser; print the PKCE authorization URL and wait for callback or pasted code.",
    )
    login_p.add_argument(
        "--code",
        dest="auth_code",
        type=str,
        default=None,
        help="Provide a single-use PKCE authorization code (`oc_code_...`) or callback URL directly.",
    )
    login_p.add_argument(
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help="Auto-approve PKCE SSO authorization against a local/sandbox Control Plane.",
    )
    login_p.add_argument(
        "--server-url",
        dest="server_url",
        type=str,
        default=None,
        help="Central TalkOps Server URL (e.g. http://talkops-server:2024).",
    )
    login_p.add_argument(
        "--control-plane-url",
        dest="control_plane_url",
        type=str,
        default=None,
        help="External Control Plane URL (e.g. http://127.0.0.1:8090 or https://console.talkops.ai).",
    )
    login_p.add_argument(
        "--user-id",
        dest="user_id",
        type=str,
        default=None,
        help="Optional operator user ID or email.",
    )
    login_p.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        default=False,
        help="Store credentials locally without performing an immediate token exchange.",
    )

    # talkops auth logout
    logout_p = auth_sub.add_parser(
        "logout",
        help="Revoke stored Control Plane session lease and/or provider API keys.",
    )
    add_output_args(logout_p)
    logout_p.add_argument(
        "--all",
        dest="logout_all",
        action="store_true",
        default=False,
        help="Clear both Control Plane session and all stored LLM provider API keys.",
    )
    logout_p.add_argument(
        "--account",
        dest="logout_account",
        action="store_true",
        default=False,
        help="Clear only the Control Plane / Central Server session lease.",
    )
    logout_p.add_argument(
        "--provider",
        dest="provider",
        type=str,
        default=None,
        help="Clear stored credentials for a specific LLM provider.",
    )

    # talkops auth token
    token_p = auth_sub.add_parser(
        "token",
        help="Exchange or print the active short-lived temporary JWT access token.",
    )
    add_output_args(token_p)
    token_p.add_argument(
        "--refresh",
        action="store_true",
        default=False,
        help="Force a fresh token exchange against the server / Control Plane.",
    )
    token_p.add_argument(
        "--server-url",
        dest="server_url",
        type=str,
        default=None,
        help="Override server URL for token exchange.",
    )
    token_p.add_argument(
        "--api-key",
        dest="api_key",
        type=str,
        default=None,
        help="Override API key for token exchange.",
    )

    # talkops auth list
    list_p = auth_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all configured and available LLM model providers and Control Plane status.",
    )
    add_output_args(list_p)

    # talkops auth set <provider> [api_key]
    set_p = auth_sub.add_parser(
        "set",
        help="Store an API key for a model provider (or 'talkops' for Control Plane).",
    )
    add_output_args(set_p)
    set_p.add_argument("provider", help="Provider name (e.g. anthropic, openai, google_genai, talkops).")
    set_p.add_argument("api_key_value", nargs="?", default=None, help="API key value (prompts if omitted).")
    set_p.add_argument("--base-url", dest="base_url", default=None, help="Optional custom base URL.")

    # talkops auth remove <provider>
    rem_p = auth_sub.add_parser(
        "remove",
        aliases=["rm", "delete", "unset"],
        help="Remove stored credentials for a model provider.",
    )
    add_output_args(rem_p)
    rem_p.add_argument("provider", help="Provider name (or 'all' / 'talkops').")

    # talkops auth aws
    aws_p = auth_sub.add_parser(
        "aws",
        help="Show AWS STS caller identity authentication status.",
    )
    add_output_args(aws_p)
    aws_p.add_argument("--aws-profile", dest="aws_profile", type=str, default=argparse.SUPPRESS)
    aws_p.add_argument("--aws-region", dest="aws_region", type=str, default=argparse.SUPPRESS)

    return auth_parser


def run_auth_command(
    output_format: OutputFormat = "text",
    *,
    aws_profile: str | None = None,
    aws_region: str | None = None,
) -> int:
    """Backward-compatible default `talkops auth` command (AWS STS check)."""
    if aws_profile is not None or aws_region is not None:
        ctx = get_aws_context(profile=aws_profile, region=aws_region)
    else:
        ctx = get_aws_context()

    if output_format == "json":
        write_json(
            "auth",
            {
                "authenticated": ctx.is_authenticated,
                "account_id": ctx.account_id,
                "arn": ctx.arn,
                "user_id": ctx.user_id,
                "region": ctx.region,
                "profile": ctx.profile,
                "error": ctx.error,
            },
        )
        return 0 if ctx.is_authenticated else 1

    console = Console()
    console.print("\n[bold yellow]AWS STS Authentication Status[/bold yellow]\n")
    if ctx.is_authenticated:
        console.print("[bold green]Authenticated Successfully[/bold green]")
        console.print(f"  Account ID: {ctx.account_id}")
        console.print(f"  Identity ARN: {ctx.arn}")
        console.print(f"  User ID: {ctx.user_id}")
        console.print(f"  Region: {ctx.region}")
        console.print(f"  Profile: {ctx.profile}\n")
        return 0
    else:
        console.print("[bold red]Not Authenticated[/bold red]")
        console.print(f"  Error: {ctx.error or 'No credentials found'}")
        console.print(
            "  Tip: Configure AWS credentials via `aws configure` or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n"
        )
        return 1


def _build_control_plane_status_dict() -> dict[str, Any]:
    """Assemble current Control Plane / Central Server authentication status."""
    storage = get_credential_storage()
    session = storage.load_session()
    env_url, env_key, env_user = get_remote_client_config()
    cp_url = (
        os.environ.get("TALKOPS_CONTROL_PLANE_URL", "").strip()
        or (session.control_plane_url if session else "")
    )
    server_url = env_url or (session.server_url if session else "")
    raw_key = env_key or (session.api_key if session else "")
    user_id = env_user or (session.user_id if session else "")
    key_prefix = (
        mask_api_key(raw_key)
        if raw_key
        else (session.api_key_prefix if session else "")
    )
    temp_jwt = get_cached_temporary_token(raw_key or None)
    lease_valid = bool(temp_jwt) or bool(session and session.is_access_token_valid())
    remaining_ttl = session.remaining_ttl_seconds() if session else 0

    return {
        "authenticated": bool(raw_key or lease_valid),
        "auth_method": (session.auth_method if session else None),
        "server_url": server_url or None,
        "control_plane_url": cp_url or None,
        "user_id": user_id or None,
        "email": (session.email if session and session.email else None),
        "org_id": (session.org_id if session and session.org_id else None),
        "role": (session.role if session else "operator"),
        "tier": (session.user_tier if session else None),
        "api_key_prefix": key_prefix or None,
        "temp_token_active": lease_valid,
        "temp_token_ttl_seconds": remaining_ttl,
        "auth_file": str(storage.auth_path),
    }


def _build_providers_list() -> list[dict[str, Any]]:
    """Return status dicts for all known LLM model providers."""
    storage = get_credential_storage()
    stored_map = storage.list_provider_keys()
    rows: list[dict[str, Any]] = []
    for provider in sorted(PROVIDER_API_KEY_ENV.keys()):
        st = get_provider_auth_status(provider)
        stored_entry = stored_map.get(provider, {})
        rows.append(
            {
                "provider": provider,
                "display_name": PROVIDER_DISPLAY_NAMES.get(provider, provider),
                "state": str(st.state),
                "configured": bool(st.as_legacy_bool()),
                "env_var": st.env_var,
                "key_prefix": stored_entry.get("key_prefix") or (
                    mask_api_key(os.environ.get(st.env_var)) if st.env_var and os.environ.get(st.env_var) else None
                ),
            }
        )
    return rows


def run_auth_status_command(
    output_format: OutputFormat = "text",
    *,
    aws_profile: str | None = None,
    aws_region: str | None = None,
) -> int:
    """Execute `talkops auth status`."""
    cp_status = _build_control_plane_status_dict()
    providers = _build_providers_list()
    configured_providers = [p for p in providers if p["configured"]]
    aws_ctx = (
        get_aws_context(profile=aws_profile, region=aws_region)
        if (aws_profile or aws_region)
        else get_aws_context()
    )

    payload = {
        "control_plane": cp_status,
        "providers": providers,
        "configured_provider_count": len(configured_providers),
        "aws_sts": {
            "authenticated": aws_ctx.is_authenticated,
            "account_id": aws_ctx.account_id,
            "arn": aws_ctx.arn,
            "user_id": aws_ctx.user_id,
            "region": aws_ctx.region,
            "profile": aws_ctx.profile,
            "error": aws_ctx.error,
        },
    }

    if output_format == "json":
        write_json("auth status", payload)
        return 0

    console = Console()
    console.print("\n[bold cyan]1. TalkOps Control Plane & Central Server[/bold cyan]")
    if cp_status["authenticated"]:
        console.print("  Status: [bold green]Configured[/bold green]")
        if cp_status.get("auth_method"):
            console.print(f"  Auth Method: {cp_status['auth_method']}")
        if cp_status["server_url"]:
            console.print(f"  Server URL: {cp_status['server_url']}")
        if cp_status["control_plane_url"]:
            console.print(f"  Control Plane URL: {cp_status['control_plane_url']}")
        if cp_status["user_id"]:
            console.print(f"  User ID: {cp_status['user_id']}")
        if cp_status.get("email"):
            console.print(f"  Email: {cp_status['email']}")
        if cp_status["org_id"]:
            console.print(f"  Org ID: {cp_status['org_id']}")
        if cp_status.get("role"):
            console.print(f"  Role / Tier: {cp_status['role']} ({cp_status.get('tier') or 'pro'})")
        if cp_status["api_key_prefix"]:
            console.print(f"  API Key: {cp_status['api_key_prefix']}")
        if cp_status["temp_token_active"]:
            console.print(
                f"  Temporary JWT Lease: [green]Active[/green] (TTL: {cp_status['temp_token_ttl_seconds']}s)"
            )
        else:
            console.print("  Temporary JWT Lease: [dim]Will exchange on next request[/dim]")
    else:
        console.print("  Status: [dim]Local Mode (not connected to a Central Server)[/dim]")
        console.print("  Tip: Run `talkops auth login --sso` or `talkops auth login --server-url <url> --api-key <key>`")

    console.print(
        f"\n[bold cyan]2. Model Providers ({len(configured_providers)}/{len(providers)} ready)[/bold cyan]"
    )
    for p in configured_providers:
        prefix_str = f" ({p['key_prefix']})" if p.get("key_prefix") else ""
        console.print(f"  [green]●[/green] {p['display_name']} [{p['provider']}]{prefix_str}")

    console.print("\n[bold cyan]3. AWS STS Identity[/bold cyan]")
    if aws_ctx.is_authenticated:
        console.print(f"  [green]●[/green] Account {aws_ctx.account_id} ({aws_ctx.arn})")
    else:
        console.print(f"  [dim]○ Not authenticated ({aws_ctx.error or 'no credentials'})[/dim]\n")
    return 0


def run_auth_login_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute `talkops auth login` (supports Browser-Loopback + Paste PKCE SSO and direct API Key exchange)."""
    storage = get_credential_storage()
    existing = storage.load_session()

    server_url = (
        getattr(args, "server_url", None)
        or os.environ.get("TALKOPS_SERVER_URL", "")
        or (existing.server_url if existing else "")
    ).strip().rstrip("/")
    cp_url = (
        getattr(args, "control_plane_url", None)
        or os.environ.get("TALKOPS_CONTROL_PLANE_URL", "")
        or (existing.control_plane_url if existing else "")
    ).strip().rstrip("/")
    user_id = (
        getattr(args, "user_id", None)
        or os.environ.get("TALKOPS_USER_ID", "")
        or (existing.user_id if existing else "")
    ).strip()
    explicit_api_key = (getattr(args, "api_key", None) or "").strip()
    sso_provider = getattr(args, "sso_provider", None)
    login_hint = getattr(args, "login_hint", None)
    no_browser = bool(getattr(args, "no_browser", False))
    auth_code = getattr(args, "auth_code", None)
    auto_approve = bool(getattr(args, "auto_approve", False))

    use_sso_flow = bool(sso_provider or auth_code or auto_approve or login_hint)

    api_key = (
        explicit_api_key
        if (explicit_api_key or use_sso_flow)
        else (
            os.environ.get("TALKOPS_API_KEY", "")
            or os.environ.get("TALKOPS_SERVER_API_KEY", "")
        ).strip()
    )

    if not api_key and not use_sso_flow and sys.stdin.isatty() and output_format != "json":
        try:
            prompt_val = getpass.getpass(
                "Enter TalkOps API Key (oc_live_...) or press Enter for Browser SSO login: "
            ).strip()
            if prompt_val:
                api_key = prompt_val
            else:
                use_sso_flow = True
        except (EOFError, KeyboardInterrupt):
            return 1

    # Browser-Loopback + Paste PKCE SSO Flow (`BrowserAndPasteTokenAcquirer`)
    if use_sso_flow and not api_key:
        from talkops._constants import DEFAULT_CONTROL_PLANE_URL

        effective_cp_url = cp_url or server_url or DEFAULT_CONTROL_PLANE_URL
        effective_server_url = server_url or ""
        normalized_provider = None if sso_provider in (None, "sso", "default") else str(sso_provider).strip().lower()
        effective_hint = login_hint or (user_id if "@" in user_id else None)

        acquirer = BrowserAndPasteTokenAcquirer(
            control_plane_url=effective_cp_url,
            server_url=effective_server_url,
            sso_provider=normalized_provider,
            login_hint=effective_hint,
            no_browser=no_browser or (output_format == "json"),
            auto_approve=auto_approve,
            output_fn=(lambda _msg: None) if output_format == "json" else print,
        )
        try:
            session, token_data = asyncio.run(
                acquirer.acquire_session(provided_code=auth_code)
            )
        except Exception as exc:
            err_msg = str(exc)
            if output_format == "json":
                write_json("auth login", {"authenticated": False, "error": err_msg})
            else:
                print(f"Error: {err_msg}", file=sys.stderr)
            return 1

        set_remote_client_context(
            session.server_url or None,
            api_key=session.api_key or session.access_token,
            user_id=session.user_id or None,
        )
        storage.save_session(session, sync_dotenv=True)

        result_data = {
            "authenticated": True,
            "auth_method": session.auth_method,
            "verified_temp_token": bool(session.access_token),
            "api_key_prefix": session.api_key_prefix,
            "server_url": session.server_url or None,
            "control_plane_url": session.control_plane_url or None,
            "user_id": session.user_id or None,
            "email": session.email or None,
            "org_id": session.org_id or None,
            "role": session.role,
            "tier": session.user_tier,
            "account_type": token_data.get("account_type"),
            "capabilities": token_data.get("capabilities", []),
            "allowed_agents": token_data.get("allowed_agents", []),
            "auth_file": str(storage.auth_path),
        }
        if output_format == "json":
            write_json("auth login", result_data)
            return 0

        console = Console()
        console.print("\n[bold green]Successfully authenticated via TalkOps PKCE SSO![/bold green]")
        console.print(f"  Auth Method: [cyan]{session.auth_method}[/cyan]")
        if session.email:
            console.print(f"  Email: {session.email}")
        if session.user_id:
            console.print(f"  User ID: {session.user_id}")
        if session.org_id:
            console.print(f"  Org ID: {session.org_id} (Role: {session.role}, Tier: {session.user_tier})")
        if session.api_key_prefix:
            console.print(f"  Provisioned CLI Key: {session.api_key_prefix}")
        if session.control_plane_url:
            console.print(f"  Control Plane URL: {session.control_plane_url}")
        if session.server_url:
            console.print(f"  Server URL: {session.server_url}")
        if session.access_token:
            console.print(
                f"  Temporary RS256 JWT Lease: [bold green]Verified[/bold green] (TTL: {session.remaining_ttl_seconds()}s)"
            )
        console.print(f"  Stored in: {storage.auth_path}")
        return 0

    if not api_key:
        err_msg = "API key is required. Pass --api-key <key>, --sso, or set TALKOPS_API_KEY."
        if output_format == "json":
            write_json("auth login", {"authenticated": False, "error": err_msg})
        else:
            print(f"Error: {err_msg}", file=sys.stderr)
        return 1

    clean_key = api_key[7:].strip() if api_key.lower().startswith("bearer ") else api_key
    no_verify = bool(getattr(args, "no_verify", False))

    set_remote_client_context(server_url or None, api_key=clean_key, user_id=user_id or None)
    temp_jwt: str | None = None
    if not no_verify and (server_url or cp_url):
        try:
            temp_jwt = asyncio.run(
                exchange_api_key_for_temporary_token(
                    server_url or None,
                    api_key=clean_key,
                    user_id=user_id or None,
                    control_plane_url=cp_url or None,
                    force_refresh=True,
                    raise_on_unauthorized=True,
                )
            )
        except PermissionError as exc:
            err_msg = str(exc)
            if output_format == "json":
                write_json("auth login", {"authenticated": False, "error": err_msg})
            else:
                print(f"Error: {err_msg}", file=sys.stderr)
            return 1

    # Load any session updated during exchange, or build a fresh one
    updated = storage.load_session()
    now = time.time()
    session = StoredSession(
        access_token=temp_jwt or (updated.access_token if updated else ""),
        api_key=clean_key,
        api_key_prefix=mask_api_key(clean_key),
        server_url=server_url,
        control_plane_url=cp_url,
        user_id=(updated.user_id if updated and updated.user_id else user_id),
        email=(updated.email if updated else ""),
        org_id=(updated.org_id if updated else ""),
        role=(updated.role if updated else "operator"),
        user_tier=(updated.user_tier if updated else "pro"),
        expires_at=(updated.expires_at if (updated and temp_jwt) else (now + 900 if temp_jwt else 0.0)),
        offline_grace_until=now + 7 * 86400,
        source_key_hash=hash_api_key(clean_key),
    )
    storage.save_session(session, sync_dotenv=True)

    result_data = {
        "authenticated": True,
        "verified_temp_token": bool(temp_jwt),
        "api_key_prefix": session.api_key_prefix,
        "server_url": session.server_url or None,
        "control_plane_url": session.control_plane_url or None,
        "user_id": session.user_id or None,
        "org_id": session.org_id or None,
        "auth_file": str(storage.auth_path),
    }

    if output_format == "json":
        write_json("auth login", result_data)
        return 0

    console = Console()
    console.print("[bold green]Successfully saved TalkOps authentication![/bold green]")
    console.print(f"  API Key: {session.api_key_prefix}")
    if session.server_url:
        console.print(f"  Server URL: {session.server_url}")
    if session.control_plane_url:
        console.print(f"  Control Plane URL: {session.control_plane_url}")
    if session.user_id:
        console.print(f"  User ID: {session.user_id}")
    if temp_jwt:
        console.print("  Temporary JWT Exchange: [bold green]Verified[/bold green]")
    console.print(f"  Stored in: {storage.auth_path}")
    return 0



def run_auth_logout_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute `talkops auth logout`."""
    storage = get_credential_storage()
    logout_all = bool(getattr(args, "logout_all", False))
    provider = getattr(args, "provider", None)

    cleared_session = False
    cleared_providers: list[str] = []

    if provider and not logout_all:
        if provider.lower() in {"talkops", "control_plane", "account", "server"}:
            cleared_session = storage.clear_session(clear_dotenv=True)
            clear_remote_client_context()
        else:
            if storage.delete_provider_key(provider, sync_dotenv=True):
                cleared_providers.append(provider.lower())
    elif logout_all:
        cleared_session = storage.clear_session(clear_dotenv=True)
        clear_remote_client_context()
        storage.delete_provider_key("all", sync_dotenv=True)
        cleared_providers.append("all")
    else:
        cleared_session = storage.clear_session(clear_dotenv=True)
        clear_remote_client_context()

    payload = {
        "session_cleared": cleared_session,
        "providers_cleared": cleared_providers,
    }
    if output_format == "json":
        write_json("auth logout", payload)
        return 0

    console = Console()
    if cleared_session:
        console.print("[green]Cleared TalkOps Control Plane / Central Server session.[/green]")
    if cleared_providers:
        console.print(f"[green]Cleared provider credentials: {', '.join(cleared_providers)}[/green]")
    if not cleared_session and not cleared_providers:
        console.print("[dim]No stored session or matching credentials found to clear.[/dim]")
    return 0


def run_auth_token_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute `talkops auth token` to print or refresh the temporary JWT access token."""
    srv_url = getattr(args, "server_url", None)
    api_key = getattr(args, "api_key", None)
    force_refresh = bool(getattr(args, "refresh", False))

    token = asyncio.run(
        exchange_api_key_for_temporary_token(
            srv_url,
            api_key=api_key,
            force_refresh=force_refresh,
        )
    )
    if not token:
        err = "Could not obtain temporary token. Configure TALKOPS_API_KEY and TALKOPS_SERVER_URL (or run `talkops auth login`)."
        if output_format == "json":
            write_json("auth token", {"access_token": None, "error": err})
        else:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    session = get_credential_storage().load_session()
    if output_format == "json":
        write_json(
            "auth token",
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": session.remaining_ttl_seconds() if session else 900,
                "user_id": session.user_id if session else None,
            },
        )
        return 0

    print(token)
    return 0


def run_auth_list_command(output_format: OutputFormat = "text") -> int:
    """Execute `talkops auth list`."""
    cp_status = _build_control_plane_status_dict()
    providers = _build_providers_list()
    if output_format == "json":
        write_json(
            "auth list",
            {
                "control_plane": cp_status,
                "providers": providers,
            },
        )
        return 0

    console = Console()
    table = Table(title="TalkOps Authentication & Model Providers")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Display Name")
    table.add_column("Status")
    table.add_column("Env Var", style="dim")
    table.add_column("Stored Key", style="dim")

    cp_badge = "[bold green]CONFIGURED[/bold green]" if cp_status["authenticated"] else "[dim]MISSING[/dim]"
    table.add_row(
        "talkops",
        "TalkOps Control Plane",
        cp_badge,
        "TALKOPS_API_KEY",
        cp_status.get("api_key_prefix") or "-",
    )
    for p in providers:
        state = p["state"]
        if state == "configured":
            badge = "[bold green]CONFIGURED[/bold green]"
        elif state in {"implicit", "not_required"}:
            badge = f"[blue]{state.upper()}[/blue]"
        else:
            badge = "[dim]MISSING[/dim]"
        table.add_row(
            p["provider"],
            p["display_name"],
            badge,
            p["env_var"] or "-",
            p["key_prefix"] or "-",
        )
    console.print(table)
    return 0


def run_auth_set_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute `talkops auth set <provider> [api_key]`."""
    provider = str(getattr(args, "provider", "") or "").strip().lower()
    api_key = getattr(args, "api_key_value", None)
    base_url = getattr(args, "base_url", None)

    if not api_key and sys.stdin.isatty() and output_format != "json":
        try:
            api_key = getpass.getpass(f"Enter API key for {provider}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1

    if not api_key:
        err = f"API key value is required for provider '{provider}'."
        if output_format == "json":
            write_json("auth set", {"saved": False, "provider": provider, "error": err})
        else:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    if provider in {"talkops", "control_plane", "server"}:
        login_ns = argparse.Namespace(
            api_key=api_key,
            server_url=base_url,
            control_plane_url=None,
            user_id=None,
            no_verify=False,
        )
        return run_auth_login_command(login_ns, output_format=output_format)

    storage = get_credential_storage()
    ok = storage.set_provider_key(provider, api_key, base_url=base_url, sync_dotenv=True)
    if output_format == "json":
        write_json(
            "auth set",
            {
                "saved": ok,
                "provider": provider,
                "key_prefix": mask_api_key(api_key),
                "base_url": base_url,
            },
        )
        return 0 if ok else 1

    if ok:
        Console().print(
            f"[bold green]Saved credentials for {provider} ({mask_api_key(api_key)}).[/bold green]"
        )
        return 0
    print(f"Error: Failed to save credentials for {provider}.", file=sys.stderr)
    return 1


def run_auth_remove_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute `talkops auth remove <provider>`."""
    provider = str(getattr(args, "provider", "") or "").strip().lower()
    storage = get_credential_storage()

    if provider in {"talkops", "control_plane", "server"}:
        removed = storage.clear_session(clear_dotenv=True)
        clear_remote_client_context()
    else:
        removed = storage.delete_provider_key(provider, sync_dotenv=True)

    if output_format == "json":
        write_json("auth remove", {"provider": provider, "removed": removed})
        return 0 if removed else 1

    if removed:
        Console().print(f"[green]Removed stored credentials for {provider}.[/green]")
        return 0
    print(f"No stored credentials found for {provider}.", file=sys.stderr)
    return 1


def dispatch_auth_subcommand(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Route `talkops auth [subcommand]` invocations."""
    subcmd = getattr(args, "auth_command", None)
    aws_prof = getattr(args, "aws_profile", None)
    aws_reg = getattr(args, "aws_region", None)

    if subcmd in (None, "aws"):
        if aws_prof or aws_reg:
            return run_auth_command(output_format, aws_profile=aws_prof, aws_region=aws_reg)
        return run_auth_command(output_format)
    if subcmd == "status":
        return run_auth_status_command(output_format, aws_profile=aws_prof, aws_region=aws_reg)
    if subcmd == "login":
        return run_auth_login_command(args, output_format=output_format)
    if subcmd == "logout":
        return run_auth_logout_command(args, output_format=output_format)
    if subcmd == "token":
        return run_auth_token_command(args, output_format=output_format)
    if subcmd in {"list", "ls"}:
        return run_auth_list_command(output_format=output_format)
    if subcmd == "set":
        return run_auth_set_command(args, output_format=output_format)
    if subcmd in {"remove", "rm", "delete", "unset"}:
        return run_auth_remove_command(args, output_format=output_format)
    return run_auth_command(output_format)
