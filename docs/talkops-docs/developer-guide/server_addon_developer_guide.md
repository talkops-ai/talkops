# TalkOps Server Addon Framework — End-to-End Developer & Operator Guide

TalkOps Server Mode (`talkops serve`) is built around a pluggable **Server Addon Microkernel** ([`src/talkops/addons/`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/__init__.py)). Every HTTP route, webhook listener, authentication provider, background consumer, and ChatOps integration in `talkops serve` is a modular **Server Addon**.

By default, TalkOps ships and enables three built-in Open-Source (OSS) addons so the standard CLI, server, and webhook workflows work out of the box with zero configuration:
1. **`core-auth`** ([`CoreAuthAddon`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/builtin/core_auth.py)) — Static API Key, Bearer token, 1-step JWT exchange, and OIDC/JWKS discovery endpoints.
2. **`client-gateway`** ([`ClientGatewayAddon`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/builtin/client_gateway.py)) — Multi-user `/api/v1/threads/*`, `/api/v1/server/info`, and background task SSE streaming for remote TUI/CLI clients.
3. **`webhooks`** ([`WebhookAddon`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/builtin/webhooks.py)) — Inbound webhook ingestion (`/webhooks/{provider}`) for PagerDuty, AWS SNS, Alertmanager, Grafana, GitHub, Jenkins, Datadog, ServiceNow, Azure Monitor, and CloudEvents.

Beyond these built-in defaults, **TalkOps-supported enterprise addons** and **user-authored custom addons** can live in **completely separate Git repositories** and be hooked into TalkOps at runtime without modifying a single line of core TalkOps code.

---

## 1. Architecture Overview

```mermaid
flowchart TB
    subgraph Discovery["1. Runtime Addon Discovery (4 Channels)"]
        D1["Built-in OSS Addons\n(core-auth, client-gateway, webhooks)"]
        D2["Declarative Manifest\n(.talkops/addons.json / --addons-config)"]
        D3["Python Entry Points\n(entry_points group='talkops.addons')"]
        D4["Drop-in & CLI Specs\n(.talkops/addons/*.py / --custom-addon)"]
    end

    subgraph Config["2. 4-Tier Config & Secret Resolution"]
        C1["Tier 1: AddonSettings Pydantic Defaults"]
        C2["Tier 2: Server Profile (default, api-only, custom)"]
        C3["Tier 3: addons.json & settings.json"]
        C4["Tier 4: .talkops/.env + OS Env Vars + CLI Flags"]
    end

    subgraph Kernel["3. TalkOps Server Microkernel (AddonManager)"]
        AM["AddonManager\n• Dependency & Conflict DAG Sort\n• Route & Middleware Assembly\n• Multi-Port uvicorn.Server Orchestration\n• Companion OS Subprocess Supervision"]
        GW["AgentRunGateway (In-Memory Pregel)\n• trigger_run() / submit_run()\n• stream_run()\n• resume_run() (HITL Command)\n• get_thread_state() / update_thread_state()"]
        EB["HookEventBus (Async Pub/Sub)\n• addon.run.started\n• addon.run.interrupted (HITL)\n• addon.run.completed / failed"]
    end

    Discovery --> AM
    Config --> AM
    AM <--> GW
    GW <--> EB
```

> [!IMPORTANT]
> **Key Design Guarantees**
> - **Zero Disturbance to Default Flow**: Running `talkops serve` with no flags starts `core-auth`, `client-gateway`, and `webhooks` identically to legacy behavior.
> - **Zero HTTP Loopback**: Addons never need to make HTTP requests back to `127.0.0.1:2024`. Every addon receives an [`AddonContext`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/protocol.py#L84-L115) containing [`ctx.gateway`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/gateway.py#L149-L668) (`AgentRunGateway`) and [`ctx.event_bus`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/gateway.py#L40-L115) (`HookEventBus`), which execute directly against the in-memory compiled LangGraph `Pregel` graph.
> - **No Hardcoded Secrets**: Core TalkOps never hardcodes third-party secret vaults or enterprise credentials. Users supply secrets via `.talkops/.env`, `~/.talkops/.env`, or environment variables, and reference them in `addons.json` using `${ENV_VAR}` or `<field>_env`.

---

## 2. Addon Conventions & Protocol Reference

### 2.1 Naming & Metadata Conventions

Every addon subclasses [`BaseServerAddon`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/protocol.py#L204-L291) (or implements the [`ServerAddon`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/protocol.py#L118-L201) protocol) and defines the following class attributes:

| Attribute | Type | Convention / Requirement | Example |
| :--- | :--- | :--- | :--- |
| `name` | `str` | **Required.** Unique, lowercase `kebab-case` identifier. Env vars map `<NAME>` by uppercasing and replacing `-` with `_`. | `"talkops-slack"` |
| `version` | `str` | Semantic version string (`MAJOR.MINOR.PATCH`). | `"1.0.0"` |
| `api_version` | `int` | Must be `1` (`ADDON_API_VERSION`). Checked by `AddonManager` at load time. | `1` |
| `tier` | `Literal["oss", "enterprise", "custom"]` | `"oss"` for built-in core, `"enterprise"` for TalkOps commercial packages, `"custom"` for user-authored plugins. | `"enterprise"` |
| `description` | `str` | Concise human-readable summary shown in `talkops serve addons` and `GET /api/v1/addons`. | `"Two-way Slack ChatOps & HITL approvals"` |
| `enabled_by_default` | `bool` | `True` only for built-in OSS addons or drop-in `.talkops/addons/*.py`. External repo addons should set `False` and be enabled via `addons.json`, profile, or `--addons`. | `False` |
| `dependencies` | `tuple[str, ...]` | Tuple of addon names that must start *before* this addon. `AddonManager` topologically sorts addons using Kahn's algorithm. | `("core-auth",)` |
| `conflicts_with` | `tuple[str, ...]` | Tuple of addon names that cannot be active at the same time (or that an enterprise addon supersedes). | `("core-auth",)` |
| `settings_schema` | `type[AddonSettings]` | Pydantic v2 model subclassing `AddonSettings` used to validate and type-check the addon's configuration. | `SlackAddonSettings` |
| `capabilities` | `AddonCapabilities` | Declares which extension surfaces the addon uses. | `AddonCapabilities(routes=True, gateway_runner=True)` |

### 2.2 `AddonCapabilities` Flags

Declare which extension hooks your addon implements via [`AddonCapabilities`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/protocol.py#L40-L51):

```python
from talkops.addons import AddonCapabilities

capabilities = AddonCapabilities(
    routes=True,               # Exposes Starlette Route / Mount endpoints via get_routes()
    middleware=False,          # Wraps the ASGI app via get_middlewares()
    background_workers=False,  # Runs async polling/queue tasks via get_background_workers()
    companion_processes=False, # Spawns OS sidecar processes via get_companion_processes()
    auth_provider=False,       # Provides an IdentityMapper via get_identity_mapper()
    event_sinks=False,         # Publishes outbound notifications on run completion
    mcp_provider=False,        # Registers or proxies MCP tool servers
    gateway_runner=True,       # Triggers or resumes agent runs via ctx.gateway
    event_subscriber=True,     # Subscribes to HookEventBus events (e.g. HITL interrupts)
)
```

### 2.3 Configuration Schema Convention (`AddonSettings`)

Every addon defines its typed configuration by subclassing [`AddonSettings`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/config.py#L52-L86). `AddonSettings` automatically provides common base fields (`enabled`, `port`, `mount_prefix`, `package`, `path`, `python_path`, `config`):

```python
from pydantic import Field
from talkops.addons import AddonSettings


class TalkOpsSlackSettings(AddonSettings):
    """Typed settings for the TalkOps Slack Addon."""

    bot_token: str = Field(default="", description="Slack Bot OAuth Token (xoxb-...)")
    signing_secret: str = Field(default="", description="Slack HMAC Signing Secret")
    default_channel: str = Field(default="#sre-alerts", description="Default channel for incident threads")
    max_concurrent_threads: int = Field(default=10, ge=1, le=100)
```

When `AddonManager` resolves configuration, it validates the merged dictionary against `settings_schema.model_validate(raw_config)` and passes the validated instance to `addon.configure(config)`. Inside your addon, `self.config` (or `self._config`) holds this typed `TalkOpsSlackSettings` object.

### 2.4 Addon Lifecycle Hooks (`BaseServerAddon`)

[`AddonManager`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/manager.py#L96-L620) orchestrates every enabled addon through a strict lifecycle:

1. **`addon.configure(config: AddonSettings) -> None`**
   Called during `resolve_and_validate()` before the ASGI app is constructed. Store `self._config = config` and validate any required settings.
2. **`addon.get_routes() -> list[BaseRoute]`**
   Return Starlette `Route` or `Mount` objects.
   - If `config.mount_prefix` is set (e.g. `"/custom/v1"`), `AddonManager` automatically prefixes your routes.
   - If `config.port` is set to a different port than the main server port (e.g. `port: 8090`), `AddonManager` automatically mounts your routes on a dedicated `uvicorn.Server` bound to port `8090` sharing the same `asyncio` event loop.
3. **`addon.get_middlewares() -> list[Middleware]`**
   Return Starlette `Middleware(MyMiddlewareClass, ...)` entries to wrap the server's HTTP stack.
4. **`await addon.on_startup(ctx: AddonContext) -> None`**
   Called inside the Starlette lifespan startup phase. Save `self._ctx = ctx`, subscribe to `ctx.event_bus`, initialize HTTP clients or DB pools.
5. **`addon.get_background_workers(ctx: AddonContext) -> list[Callable[[], Coroutine]]`**
   Return zero-argument async callables (`async def _poll_queue() -> None: ...`). `AddonManager` launches each worker as a supervised `asyncio.Task` on startup and cancels/awaits it cleanly on shutdown.
6. **`addon.get_companion_processes(ctx: AddonContext) -> list[CompanionProcessSpec]`**
   Return optional OS subprocess specifications (`CompanionProcessSpec(name=..., command=[...], restart_policy="on-failure")`) that `AddonManager` spawns and supervises alongside the main server.
7. **`await addon.healthcheck() -> AddonHealthStatus`**
   Called by `GET /api/v1/addons` to report real-time addon health (`status="ok" | "degraded" | "error"`, `details={...}`).
8. **`await addon.on_shutdown() -> None`**
   Called in **reverse dependency order** when `talkops serve` shuts down. Close any open connections or clients here.

---

## 3. Step-by-Step: Creating an Addon in a Separate Repository

Suppose you are creating a separate repository `talkops-enterprise-addons` (or a customer is creating `acme-talkops-addons`) to house a **Slack ChatOps + Human-in-the-Loop (HITL) Approval Addon**.

### Step 1: Repository Directory Structure

```text
talkops-enterprise-addons/
├── pyproject.toml
└── src/
    └── talkops_addons/
        ├── __init__.py
        └── slack_chatops.py
```

### Step 2: `pyproject.toml` (Declaring the `talkops.addons` Entry Point)

By declaring the `[project.entry-points."talkops.addons"]` group in `pyproject.toml`, any environment that installs this package (`pip install` / `uv pip install`) will automatically register the addon with TalkOps:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "talkops-enterprise-addons"
version = "1.0.0"
description = "TalkOps Enterprise Server Addons for TalkOps"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "talkops>=0.1.0",
    "httpx>=0.27.0",
]

# Automatic discovery hook for TalkOps AddonManager
[project.entry-points."talkops.addons"]
talkops-slack = "talkops_addons.slack_chatops:TalkOpsSlackAddon"
```

### Step 3: Implementing the Addon (`src/talkops_addons/slack_chatops.py`)

Below is a complete, production-grade implementation showing:
- Typed `AddonSettings` configuration
- HMAC signature verification using secrets from `.talkops/.env`
- Triggering background agent runs via `ctx.gateway.submit_run()` with deterministic thread mapping (`slack:{channel}:{thread_ts}`)
- Subscribing to `addon.run.interrupted` on `ctx.event_bus` to capture Human-in-the-Loop tool approvals
- Resuming paused agent runs via `ctx.gateway.resume_run()` when an operator clicks **Approve** or **Reject** in Slack

```python
"""TalkOps Slack ChatOps & HITL Approval Addon (External Repository Example)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from talkops.addons import (
    AddonCapabilities,
    AddonContext,
    AddonEvent,
    AddonHealthStatus,
    AddonSettings,
    BaseServerAddon,
)


class TalkOpsSlackSettings(AddonSettings):
    """Configuration schema for `talkops-slack`."""

    bot_token: str = Field(default="", description="Slack Bot OAuth token (xoxb-...)")
    signing_secret: str = Field(default="", description="Slack app signing secret")
    default_channel: str = Field(default="#sre-war-room", description="Default Slack channel")
    verify_signatures: bool = Field(default=True, description="Enforce X-Slack-Signature HMAC check")


class TalkOpsSlackAddon(BaseServerAddon):
    """Two-way Slack ChatOps & interactive Human-in-the-Loop approval addon."""

    name = "talkops-slack"
    version = "1.0.0"
    tier = "enterprise"
    description = "Slack ChatOps bot with interactive HITL tool approval cards"
    enabled_by_default = False
    dependencies = ("core-auth",)
    settings_schema = TalkOpsSlackSettings
    capabilities = AddonCapabilities(
        routes=True,
        gateway_runner=True,
        event_subscriber=True,
    )

    def __init__(self) -> None:
        super().__init__()
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._unsubscribe_bus: list[Any] = []

    async def on_startup(self, ctx: AddonContext) -> None:
        await super().on_startup(ctx)
        # Subscribe to HITL interrupt and run completion events from HookEventBus
        unsub_int = ctx.event_bus.subscribe("addon.run.interrupted", self._on_run_interrupted)
        unsub_done = ctx.event_bus.subscribe("addon.run.completed", self._on_run_completed)
        self._unsubscribe_bus.extend([unsub_int, unsub_done])

    async def on_shutdown(self) -> None:
        for unsub in self._unsubscribe_bus:
            unsub()
        self._unsubscribe_bus.clear()
        await super().on_shutdown()

    async def _on_run_interrupted(self, event: AddonEvent) -> None:
        """Capture HITL interrupts so Slack can render Approve / Reject buttons."""
        if not event.thread_id:
            return
        self._pending_approvals[event.thread_id] = {
            "thread_id": event.thread_id,
            "run_id": event.run_id,
            "interrupts": event.payload.get("interrupts", []),
            "status": "awaiting_approval",
        }

    async def _on_run_completed(self, event: AddonEvent) -> None:
        """Clear pending approval state once a thread finishes executing."""
        if event.thread_id:
            self._pending_approvals.pop(event.thread_id, None)

    def _verify_slack_signature(self, headers: dict[str, str], raw_body: bytes) -> bool:
        cfg: TalkOpsSlackSettings = self.config  # type: ignore[assignment]
        if not cfg.verify_signatures or not cfg.signing_secret:
            return True
        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            return False
        if abs(time.time() - int(timestamp)) > 300:
            return False
        sig_basestring = f"v0:{timestamp}:{raw_body.decode('utf-8')}".encode("utf-8")
        expected = "v0=" + hmac.new(
            cfg.signing_secret.encode("utf-8"),
            sig_basestring,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def get_routes(self) -> list[BaseRoute]:
        return [
            Route("/addons/slack/events", self._handle_slack_events, methods=["POST"]),
            Route("/addons/slack/interactions", self._handle_slack_interactions, methods=["POST"]),
            Route("/addons/slack/status", self._handle_status, methods=["GET"]),
        ]

    async def _handle_slack_events(self, request: Request) -> JSONResponse:
        raw_body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not self._verify_slack_signature(headers, raw_body):
            return JSONResponse({"error": "Invalid Slack signature"}, status_code=401)

        payload = await request.json()
        # 1. Slack URL Verification Challenge
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge", "")})

        # 2. App Mention -> Trigger or Continue an TalkOps Investigation Thread
        event = payload.get("event") or {}
        if event.get("type") == "app_mention":
            channel = event.get("channel", "C000")
            thread_ts = event.get("thread_ts") or event.get("ts") or "0"
            text = str(event.get("text") or "").strip()

            # Deterministic source_key maps the same Slack thread to the same LangGraph UUID thread!
            source_key = f"slack:{channel}:{thread_ts}"
            handle = await self.context.gateway.submit_run(
                prompt=text,
                addon_name=self.name,
                source_key=source_key,
                metadata={"slack_channel": channel, "slack_thread_ts": thread_ts},
                wait=False,
            )
            return JSONResponse(
                {
                    "status": "accepted",
                    "run_id": handle.run_id,
                    "thread_id": handle.thread_id,
                },
                status_code=202,
            )

        return JSONResponse({"status": "ignored"}, status_code=200)

    async def _handle_slack_interactions(self, request: Request) -> JSONResponse:
        """Handle interactive button clicks ('approve' / 'reject') from Slack Block Kit cards."""
        payload = await request.json()
        actions = payload.get("actions") or []
        if not actions:
            return JSONResponse({"error": "No actions provided"}, status_code=400)

        action = actions[0]
        decision = str(action.get("action_id") or "approve").lower()
        thread_id = str(action.get("value") or "")
        if not thread_id:
            return JSONResponse({"error": "Missing thread_id in action value"}, status_code=400)

        handle = await self.context.gateway.resume_run(
            thread_id=thread_id,
            decision=decision,
            addon_name=self.name,
            wait=False,
        )
        return JSONResponse(
            {
                "status": "resumed",
                "decision": decision,
                "run_id": handle.run_id,
                "thread_id": handle.thread_id,
            },
            status_code=200,
        )

    async def _handle_status(self, _request: Request) -> JSONResponse:
        cfg: TalkOpsSlackSettings = self.config  # type: ignore[assignment]
        return JSONResponse(
            {
                "addon": self.name,
                "default_channel": cfg.default_channel,
                "bot_token_configured": bool(cfg.bot_token),
                "pending_approvals": list(self._pending_approvals.values()),
            }
        )

    async def healthcheck(self) -> AddonHealthStatus:
        cfg: TalkOpsSlackSettings = self.config  # type: ignore[assignment]
        return AddonHealthStatus(
            name=self.name,
            status="ok" if cfg.bot_token else "degraded",
            tier=self.tier,
            version=self.version,
            details={
                "bot_token_configured": bool(cfg.bot_token),
                "pending_approvals": len(self._pending_approvals),
            },
        )
```

---

## 4. Interacting with the Agent Runtime (`AgentRunGateway` & `HookEventBus`)

Every addon receives `ctx: AddonContext` in `on_startup(ctx)`. Its two most important properties are `ctx.gateway` ([`AgentRunGateway`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/gateway.py#L149-L668)) and `ctx.event_bus` ([`HookEventBus`](file:///Users/structbinary/Documents/work/talkops/talkops/src/talkops/addons/gateway.py#L40-L115)).

### 4.1 `AgentRunGateway` Reference

| Method | Signature | Description |
| :--- | :--- | :--- |
| `resolve_thread_id` | `resolve_thread_id(thread_id=None, *, source_key=None) -> str` | Converts an arbitrary external correlation key (e.g. `"pagerduty:INC-123"` or `"slack:C123:1700.01"`) into a deterministic UUIDv5 `thread_id`. |
| `submit_run` | `await submit_run(prompt, *, thread_id=None, source_key=None, addon_name="custom", context=None, metadata=None, wait=False) -> RunHandle` | Starts an in-memory agent run in the background (`wait=False`) or foreground (`wait=True`). Returns a `RunHandle` with `run_id`, `thread_id`, `status`, `analysis_text`, and `interrupts`. |
| `trigger_run` | `await trigger_run(prompt_or_input, *, thread_id=None, source_key=None, source_addon="webhooks", wait=True) -> RunHandle` | Same as `submit_run`, but defaults to `wait=True` (awaits completion before returning). |
| `stream_run` | `stream_run(input_payload, *, thread_id, run_id=None, source_addon="...", stream_mode=None, subgraphs=True)` | Async generator yielding `(namespace, mode, chunk)` tuples directly from `graph.astream()` for real-time SSE streaming. |
| `resume_run` | `await resume_run(thread_id, resume_payload=None, *, decision=None, addon_name="chatops", wait=True) -> RunHandle` | Resumes a paused HITL interrupt using `langgraph.types.Command(resume=...)`. Pass `decision="approve"` or `decision="reject"`, or a full `{"decisions": [...]}` dict. |
| `get_thread_state` | `await get_thread_state(thread_id) -> Any` | Reads checkpoint state directly via `graph.aget_state()`. |
| `update_thread_state` | `await update_thread_state(thread_id, values, *, as_node=None) -> Any` | Updates checkpoint state directly via `graph.aupdate_state()`. |
| `cancel_run` | `await cancel_run(thread_id, run_id) -> bool` | Cancels an in-flight `asyncio.Task` for the specified run. |

### 4.2 `HookEventBus` Standard Events

Subscribe to any event (or `"*"` for all events) via `ctx.event_bus.subscribe(event_type, callback)`:

| Event Type | When Published | `event.payload` Fields |
| :--- | :--- | :--- |
| `addon.run.started` | When `stream_run` / `submit_run` begins executing a turn | `thread_id`, `run_id`, `source_addon` |
| `addon.run.interrupted` | When a tool call hits a Human-in-the-Loop approval gate (`__interrupt__`) | `thread_id`, `run_id`, `interrupts` (list of action requests) |
| `addon.run.completed` | When a run finishes normally | `thread_id`, `run_id`, `status`, `analysis_text` |
| `addon.run.failed` | When a run raises an unhandled exception | `thread_id`, `run_id`, `error` |
| `addon.run.cancelled` | When a run is cancelled via `cancel_run()` | `thread_id`, `run_id` |

---

## 5. Hooking Your Addon into TalkOps at Runtime (4 Channels)

You never need to fork or edit the core `talkops` repository to load addons from another repository. Choose whichever runtime channel fits your deployment workflow:

### Channel 1: Declarative `addons.json` Manifest (Recommended for Separate Repos & Docker/K8s)

Just like `.talkops/mcp.json` configures MCP servers, TalkOps automatically looks for `addons.json` in:
1. `~/.talkops/addons.json` (User global)
2. `<project_root>/.talkops/addons.json` or `<project_root>/addons.json` (Project workspace)
3. `--addons-config /path/to/addons.json` or `TALKOPS_ADDONS_CONFIG=/path/to/addons.json` (Explicit CLI/Env override)

```json
{
  "profile": "default",
  "addons": {
    "talkops-slack": {
      "enabled": true,
      "package": "talkops_addons.slack_chatops:TalkOpsSlackAddon",
      "python_path": [
        "/opt/talkops-enterprise-addons/src"
      ],
      "config": {
        "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
        "signing_secret_env": "TALKOPS_SLACK_SIGNING_SECRET",
        "default_channel": "#sre-war-room",
        "verify_signatures": false
      }
    },
    "servicenow-bridge": {
      "enabled": true,
      "path": "/opt/custom-addons-repo/servicenow_addon.py:ServiceNowAddon",
      "port": 8091,
      "mount_prefix": "/snow",
      "config": {
        "instance_url": "https://acme.service-now.com",
        "api_key": "${SERVICENOW_API_KEY}"
      }
    }
  }
}
```

- **`package` + `python_path`**: Imports a dotted Python module (`pkg.mod:ClassName`) and optionally prepends one or more external repository directories to `sys.path`.
- **`path`**: Loads a `.py` file or package directory directly from another repository on disk (`/abs/path/to/file.py:ClassName`).
- **`port`**: Optional dedicated port. When set (e.g., `"port": 8091`), `AddonManager` spins up a dedicated `uvicorn.Server` listener on port `8091` for this addon's routes.
- **`mount_prefix`**: Optional URL prefix prepended to all routes returned by `get_routes()`.

Launch the server with:
```bash
talkops serve --addons-config /path/to/addons.json
```

### Channel 2: Python Packaging `entry_points` (Zero-Config `pip` / `uv` Install)

If the external repository is installed into the Python environment:
```bash
uv pip install git+https://github.com/talkops/talkops-enterprise-addons.git
# or for local editable development across two repos:
uv pip install -e ../talkops-enterprise-addons
```
Because `pyproject.toml` declares `[project.entry-points."talkops.addons"]`, TalkOps discovers `talkops-slack` automatically. You can then enable it via CLI flag, environment variable, or `addons.json`:
```bash
talkops serve --addons talkops-slack
# or
TALKOPS_ADDONS=talkops-slack talkops serve
```

### Channel 3: Drop-In `.talkops/addons/*.py` or `--custom-addon` CLI Flag

For rapid prototyping or single-file custom integrations:
- Drop any `.py` file containing a `BaseServerAddon` subclass into `<project_root>/.talkops/addons/` or `~/.talkops/addons/`.
- Or pass `--custom-addon` on the command line:
```bash
talkops serve \
  --custom-addon /opt/talkops-enterprise-addons/src/talkops_addons/slack_chatops.py:TalkOpsSlackAddon \
  --addons talkops-slack
```

### Channel 4: Programmatic Registration (`@register_addon`)

When embedding TalkOps in a custom Python entrypoint or test suite, decorate your class with `@register_addon`:
```python
from talkops.addons import BaseServerAddon, register_addon

@register_addon
class MyEmbeddedAddon(BaseServerAddon):
    name = "my-embedded-addon"
    ...
```

---

## 6. Secrets Management (`.talkops/.env`) & 4-Tier Configuration

### 6.1 Providing Secrets via `.talkops/.env` or Environment Variables

TalkOps **never** hardcodes secret values or cloud secret fetchers inside the addon config package. Instead, operators provide secrets in:
1. **`<project_root>/.talkops/.env`** or **`<project_root>/.env`**
2. **`~/.talkops/.env`**
3. Standard OS / container environment variables (`export ...` or Kubernetes `Secret` env injection)

Example `.talkops/.env`:
```dotenv
# TalkOps Slack Addon Secrets
TALKOPS_SLACK_BOT_TOKEN=xoxb-1234567890-abcdef
TALKOPS_SLACK_SIGNING_SECRET=9876543210fedcba

# Custom ServiceNow Addon Secret
SERVICENOW_API_KEY=snow_live_secret_token
```

In `addons.json` or `.talkops/settings.json`, you can bind those secrets into any addon's settings in **three ways**:
1. **`${ENV_VAR}` interpolation**:
   ```json
   "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}"
   ```
2. **`<field>_env` pointer** (automatically populates `<field>` from the named env var):
   ```json
   "signing_secret_env": "TALKOPS_SLACK_SIGNING_SECRET"
   ```
3. **Direct `TALKOPS_ADDON_<NAME>_<FIELD>` environment variable** (no JSON needed at all):
   ```bash
   export TALKOPS_ADDON_TALKOPS_SLACK_BOT_TOKEN="xoxb-1234567890-abcdef"
   export TALKOPS_ADDON_TALKOPS_SLACK_SIGNING_SECRET="9876543210fedcba"
   export TALKOPS_ADDON_TALKOPS_SLACK_DEFAULT_CHANNEL="#sre-war-room"
   ```

### 6.2 4-Tier Configuration Precedence Hierarchy

When `AddonManager` resolves each addon's settings, higher tiers override lower tiers:

1. **Tier 1 (Lowest) — `AddonSettings` Pydantic Defaults**: Default field values declared on the addon's `settings_schema`.
2. **Tier 2 — Server Profile (`--profile` / `TALKOPS_SERVER_PROFILE`)**:
   - `default`: Enables `core-auth`, `client-gateway`, `webhooks`.
   - `api-only`: Enables `core-auth`, `client-gateway` (disables `webhooks`).
   - `webhook-worker`: Enables `webhooks` only.
   - Custom profiles can also be defined directly inside `addons.json` under `"profiles"`:
     ```json
     {
       "profiles": {
         "talkops-prod": ["core-auth", "client-gateway", "webhooks", "talkops-slack"]
       },
       "profile": "talkops-prod"
     }
     ```
3. **Tier 3 — `addons.json` / `settings.json` / `--addons-config`**: Merged from `~/.talkops/addons.json` -> `.talkops/addons.json` -> `--addons-config`.
4. **Tier 4 (Highest) — Environment Variables & CLI Flags**:
   - `TALKOPS_ADDON_<NAME>_<FIELD>=<value>`
   - `--addons a,b` (`TALKOPS_ADDONS=a,b`)
   - `--disable-addons c` (`TALKOPS_DISABLE_ADDONS=c`)

---

## 7. Testing Your Addon End-to-End

### 7.1 CLI Inspection Commands

Before starting the server, verify that TalkOps discovers your external addon and resolves its status and tier:

```bash
# Pretty table output
talkops serve addons --addons-config ./addons.json

# Machine-readable JSON output
talkops serve addons --addons-config ./addons.json --json
```

Once the server is running, query the live `/api/v1/addons` discovery and healthcheck endpoint:
```bash
curl -s http://127.0.0.1:2024/api/v1/addons | jq .
```

### 7.2 Writing Automated `pytest` Unit Tests for External Addons

You can test your external addon in-memory (including `AgentRunGateway` runs and `HookEventBus` HITL interrupts) in milliseconds using `httpx.ASGITransport` and `create_webhooks_app`:

```python
import httpx
import pytest
from talkops.addons import AgentRunGateway, HookEventBus
from talkops.server import create_webhooks_app
from talkops_addons.slack_chatops import TalkOpsSlackAddon


@pytest.mark.asyncio
async def test_talkops_slack_addon_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKOPS_ADDON_TALKOPS_SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("TALKOPS_ADDON_TALKOPS_SLACK_VERIFY_SIGNATURES", "false")

    bus = HookEventBus()
    gateway = AgentRunGateway(event_bus=bus)

    app = create_webhooks_app(
        extra_addons=[TalkOpsSlackAddon()],
        addons=["talkops-slack"],
        gateway=gateway,
    )
    mgr = app.state.addon_manager
    await mgr.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # 1. Verify /api/v1/addons reports talkops-slack as healthy
            addons_resp = await client.get("/api/v1/addons")
            assert addons_resp.status_code == 200
            assert "talkops-slack" in addons_resp.json()["active_addons"]

            # 2. Verify Slack URL verification challenge
            chal_resp = await client.post(
                "/addons/slack/events",
                json={"type": "url_verification", "challenge": "challenge-ok"},
            )
            assert chal_resp.status_code == 200
            assert chal_resp.json() == {"challenge": "challenge-ok"}

            # 3. Verify addon status endpoint reflects secret from env
            status_resp = await client.get("/addons/slack/status")
            assert status_resp.status_code == 200
            assert status_resp.json()["bot_token_configured"] is True
    finally:
        await mgr.shutdown()
```

---

## 8. Developer Checklist & Quick Reference

When building a new TalkOps Server Addon in an external repository, verify these items:

- [ ] **Subclass `BaseServerAddon`** from `talkops.addons` and set a unique lowercase `kebab-case` `name` (e.g. `"talkops-slack"`), `tier` (`"enterprise"` or `"custom"`), and `description`.
- [ ] **Define a Pydantic `AddonSettings` subclass** as `settings_schema` for all configurable fields and secrets.
- [ ] **Do not hardcode secrets**: Document the environment variable names users should place in `.talkops/.env` and reference via `${ENV_VAR}` or `<field>_env` in `addons.json`.
- [ ] **Use `self.context.gateway` (`AgentRunGateway`)** to trigger/stream/resume agent runs in memory rather than making HTTP requests to `localhost`.
- [ ] **Use `self.context.gateway.resolve_thread_id(source_key=...)`** so repeated events from the same external incident/thread map to the same LangGraph conversation thread.
- [ ] **Implement `healthcheck()`** returning `AddonHealthStatus` so operators can monitor the addon via `GET /api/v1/addons`.
- [ ] **Register the entry point** under `[project.entry-points."talkops.addons"]` in `pyproject.toml` and/or provide a sample `addons.json` snippet for `--addons-config`.
