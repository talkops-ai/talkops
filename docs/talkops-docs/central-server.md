# Central Server & Multi-Client Architecture

TalkOps can run as a centralized, multi-tenant execution server (`talkops serve`) that hosts the LangGraph agent runtime, persists threads and long-term memory in PostgreSQL, ingests production webhooks, and accepts concurrent connections from multiple terminal UI (TUI) and headless CLI clients.

Instead of isolating operational context on individual engineer laptops, deploying a Central Server gives your entire SRE, DevOps, and platform team a shared operational control plane where engineers can inspect live webhook-triggered incident threads, attach from their local terminal, fork investigations (`/fork`), and run headless CI/CD checks against a single governed runtime.

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/central-server-multi-client.png`**
> **What to capture:** Split-screen screenshot showing `talkops serve` running in a server terminal (logging incoming `/api/v1/threads` and `/webhooks/pagerduty` requests) alongside two connected operator terminals (`talkops --server-url http://talkops.internal:2024 --user-id alice` and `--user-id bob`) viewing and resuming threads via `/threads`.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>🌐 Shared LangGraph Runtime</h3>
      <p>Centralize LLM credentials, cloud IAM roles, MCP servers, and Jev Smart Mode routing behind a single hardened server endpoint on port <code>2024</code>.</p>
    </td>
    <td width="50%" valign="top">
      <h3>🐘 PostgreSQL State &amp; Store</h3>
      <p>Persist conversation checkpoints (<code>TalkOpsPostgresCheckpointer</code>) and long-term cross-thread memory in PostgreSQL with automatic SQLite fallback for single-node setups.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🔐 1-Step JWT Token Exchange</h3>
      <p>Clients exchange long-lived API keys (<code>oc_live_...</code>) once at <code>POST /api/v1/auth/token</code> for short-lived signed JWTs (<code>eyJ...</code>) verified locally in <code>&lt;1ms</code>.</p>
    </td>
    <td width="50%" valign="top">
      <h3>🛡️ Per-User Tenant Isolation</h3>
      <p>Every thread is stamped with <code>metadata["owner"] = user_id</code>, and memory store namespaces are automatically isolated per user (<code>(user_id, ...)</code>).</p>
    </td>
  </tr>
</table>

---

## Architecture overview

When you launch `talkops serve`, TalkOps starts a unified ASGI server (powered by LangGraph Server and Starlette) that exposes four logical surfaces:

```mermaid
flowchart LR
    subgraph Clients["Operator &amp; CI Clients"]
        C1["TUI Client (Alice)\ntalkops --server-url ..."]
        C2["TUI Client (Bob)\ntalkops --server-url ..."]
        C3["CI Pipeline (Headless)\ntalkops -p ... --server-url ..."]
    end

    subgraph CentralServer["Central TalkOps Server (talkops serve :2024)"]
        Auth["TalkOpsAuthMiddleware\nPOST /api/v1/auth/token\n/.well-known/openid-configuration"]
        ThreadAPI["Session Management API\n/api/v1/server/info\n/api/v1/threads/*"]
        GraphAPI["LangGraph Execution API\n/threads, /runs, /assistants"]
        WebhookAPI["Webhook Dispatcher\n/webhooks/{provider}"]
    end

    subgraph Storage["Persistence Layer"]
        PG[("PostgreSQL\nCheckpointer &amp; Store")]
    end

    Clients -->|Bearer JWT / API Key| Auth
    Auth --> ThreadAPI & GraphAPI
    ThreadAPI & GraphAPI <--> PG
    WebhookAPI --> GraphAPI
```

1. **LangGraph Execution API (`/threads`, `/runs`, `/assistants`, `/store`)**: Streams real-time agent turns, tool calls, subagent delegations, and human-in-the-loop interrupts to connected TUI and CLI clients via `RemoteAgentClient`.
2. **Session & Thread Management API (`/api/v1/*`)**: Provides high-speed thread listing, title updates, checkpoint pruning, and thread forking (`RemoteSessionBackend`) with strict owner filtering.
3. **Authentication & OIDC Discovery (`/api/v1/auth/token`, `/.well-known/*`)**: Handles 1-step API-key-to-JWT token exchange and publishes OpenID Connect / JWKS metadata for cloud workload identity federation.
4. **Inbound Webhook Listener (`/webhooks/*`)**: Ingests real-time DevOps events on the same port (`2024`) or an isolated `--webhook-port`.

---

## Launching the central server

Run `talkops serve` on a VM, Kubernetes pod, or ECS service to start the central runtime.

```bash
talkops serve \
  --host 0.0.0.0 \
  --port 2024 \
  --postgres-uri "postgresql://talkops:password@postgres.internal:5432/talkops" \
  --auth-mode token \
  --approval-mode smart \
  --workspaces-dir /var/lib/talkops/workspaces
```

### Server CLI flags & environment variables

| Flag | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | `TALKOPS_WEBHOOK_HOST` | `0.0.0.0` | Bind address for the Central Server HTTP listener. |
| `--port` | `TALKOPS_WEBHOOK_PORT` | `2024` | Primary port for LangGraph client connections, `/api/v1/*`, and `/webhooks/*`. |
| `--webhook-port` | — | Same as `--port` | Optional separate port dedicated exclusively to inbound `/webhooks/*` traffic. |
| `--webhook-host` | — | Same as `--host` | Optional bind address when `--webhook-port` is set. |
| `--postgres-uri` | `TALKOPS_POSTGRES_URI` / `POSTGRES_URI` | `None` (SQLite) | PostgreSQL connection string for multi-user checkpointing and store persistence. |
| `--auth-mode` | `TALKOPS_AUTH_MODE` | `noop` (`token` if keys set) | Authentication enforcement mode: `token` (enforces JWT/API key auth) or `noop` (trusted local network). |
| `--approval-mode` | `TALKOPS_SERVER_APPROVAL_MODE` | `smart` | Tool safety policy for unattended server/webhook runs (`smart`, `auto`, `yolo`, `manual`). |
| `--workspaces-dir` | `TALKOPS_WORKSPACES_DIR` | Current directory | Root directory for per-project or per-tenant execution workspaces. |
| `--model` | `TALKOPS_MODEL` | Active default | Override the default LLM model used by the server's orchestrator graph. |

> [!TIP]
> **Isolating public webhook ingress from internal operator traffic:** Pass `--port 2024 --webhook-port 8080` to `talkops serve`. You can then expose port `8080` to your public API gateway or load balancer for PagerDuty/GitHub webhooks while keeping port `2024` strictly on your private tailnet or VPN for engineer TUI clients.

---

## Connecting multiple clients to the server

Once `talkops serve` is running, any number of engineers or CI/CD runners can connect their local `talkops` binary to the central server. Connected clients run a thin local UI while delegating graph execution, tool calls, and checkpoint storage to the remote server.

### Step 1: Log in once with `talkops auth login`

Persist your server URL and exchange your API key for a cached, auto-refreshing JWT:

```bash
talkops auth login \
  --server-url https://talkops.internal:2024 \
  --api-key oc_live_9f8e7d6c5b4a... \
  --user-id alice@company.com
```

Verify your active session and JWT expiration at any time:

```bash
talkops auth status
```

### Step 2: Launch interactive TUI or headless CLI sessions

If you logged in via `talkops auth login` or exported `TALKOPS_SERVER_URL`, simply run `talkops`. You can also pass connection flags explicitly per invocation:

```bash
# Launch interactive TUI connected to the central server
talkops --server-url https://talkops.internal:2024 --user-id alice@company.com

# Resume a specific remote thread (including a thread created by a webhook alert!)
talkops --server-url https://talkops.internal:2024 -r 4e8b12a0-9c3d-4f11-8a22-7b6c5d4e3f21

# Run a headless check in CI/CD against the central server
talkops -p "Verify staging Kubernetes deployments after release v2.14.0" \
  --server-url https://talkops.internal:2024 \
  --api-key "$TALKOPS_API_KEY" \
  --user-id ci-deploy-bot
```

### Step 3: Manage remote threads across the team

Use `talkops threads` from your shell or `/threads` and `/fork` inside the TUI to collaborate on remote sessions:

```bash
# List threads stored on the central server
talkops threads list --server-url https://talkops.internal:2024 --user-id alice@company.com -n 20

# Delete a remote thread
talkops threads delete 4e8b12a0-9c3d-4f11-8a22-7b6c5d4e3f21 --server-url https://talkops.internal:2024
```

Inside an active TUI session connected to the central server:
- Run **`/threads`** to browse and switch between your remote threads or inspect webhook-created incident threads.
- Run **`/fork [new-title]`** to clone the current remote thread checkpoint into a new independent branch (`POST /api/v1/threads/{id}/fork`) so you can test an alternative remediation hypothesis without disrupting a teammate's session.
- Run **`/compact`** to summarize conversation history and prune superseded remote checkpoints (`POST /api/v1/threads/{id}/prune`).

---

## Multi-tier authentication & JWT token exchange

When `--auth-mode token` is enabled (or when `TALKOPS_SERVER_API_KEYS` / `TALKOPS_JWT_SECRET` / `TALKOPS_CONTROL_PLANE_URL` is set), `TalkOpsAuthMiddleware` protects all client endpoints using a high-performance multi-tier authentication pipeline (`src/talkops/server/auth.py`).

### How the 1-step API key → JWT exchange works

1. **Initial Exchange (`POST /api/v1/auth/token`)**: When a client authenticates with a long-lived API key (`oc_live_...`), `TalkOpsClientAuth` exchanges it once at `/api/v1/auth/token` for a short-lived signed JWT (`eyJ...`, default TTL `900s` / 15 minutes).
2. **Local Cache & Offline Grace Period**: The client stores the JWT in `~/.talkops/.state/auth.json` (`0600` permissions) with a 7-day offline grace period (`TALKOPS_AUTH_GRACE_SECONDS=604800`).
3. **Sub-Millisecond Verification (`<1ms`)**: On every subsequent request, the client sends `Authorization: Bearer eyJ...` and `X-TalkOps-User-Id: <user_id>`. The server verifies the JWT cryptographic signature locally in `<1ms` without hitting an external database.
4. **Transparent Auto-Refresh**: When the JWT is within 60 seconds of expiry or receives a `401 Unauthorized`, the client transparently exchanges its stored API key for a fresh JWT and retries the request.

### Supported server authentication backends

| Tier | Environment Variables | How Verification Works |
| :--- | :--- | :--- |
| **Tier 1: Signed JWT (`<1ms` local)** | `TALKOPS_CONTROL_PLANE_JWKS_URL`, `TALKOPS_JWT_PUBLIC_KEY`, or `TALKOPS_JWT_SECRET` | Verifies `RS256`/`ES256` tokens against cached Control Plane JWKS or a static PEM public key, or `HS256` tokens using `TALKOPS_JWT_SECRET`. Extracts `sub`, `org_id`, `roles`, and `tier`. |
| **Tier 2: Control Plane Introspection** | `TALKOPS_CONTROL_PLANE_URL` | Validates opaque `oc_live_...` API keys against `POST {CONTROL_PLANE_URL}/v1/auth/introspect` and caches valid identities for `TALKOPS_AUTH_CACHE_TTL` (default `300s`). |
| **Tier 3: Static Multi-User API Keys** | `TALKOPS_SERVER_API_KEYS` or `TALKOPS_SERVER_API_KEY` | Maps static keys directly to user IDs (e.g., `TALKOPS_SERVER_API_KEYS="alice:oc_key_1,bob:oc_key_2"`) and issues local `HS256` JWTs on `/api/v1/auth/token`. |

---

## Multi-tenant thread & memory isolation

When multiple engineers connect to the same Central Server, TalkOps enforces tenant boundaries across both conversation threads and the LangGraph long-term store:

- **Thread Ownership Stamping**: When a user creates or runs a thread, the server stamps `metadata["owner"] = user_id` (and `org_id` when present). Calls to `GET /api/v1/threads` filter results to the authenticated `user_id` unless the caller holds the `admin` role or belongs to the `system` namespace.
- **Per-User Store Namespace Isolation**: Long-term memory operations (`/store`) automatically scope non-system namespaces under `(user_id, ...)` while allowing read access to shared namespaces (`system`, `global`, `shared`, `skills`, `plugins`).

---

## HTTP API endpoints reference

In addition to standard LangGraph Server routes (`/threads`, `/runs`, `/assistants`, `/store`), `talkops serve` exposes the following TalkOps native endpoints:

| Method & Path | Auth Required | Description |
| :--- | :--- | :--- |
| `GET /api/v1/server/info` | Optional | Returns server version, `checkpoint_backend` (`postgres` or `sqlite`), `auth_mode`, and multi-tenant capabilities. |
| `POST /api/v1/auth/token` | API Key (`Bearer`) | Exchanges a long-lived API key (`oc_live_...`) for a short-lived signed JWT (`eyJ...`). |
| `GET /api/v1/threads` | Yes (`token` mode) | Lists threads owned by the caller with title, updated timestamp, model, and message count. |
| `GET /api/v1/threads/{thread_id}` | Yes (`token` mode) | Retrieves metadata and message history for a specific thread. |
| `PATCH /api/v1/threads/{thread_id}/title` | Yes (`token` mode) | Renames a remote thread (`{"title": "..."}`). |
| `POST /api/v1/threads/{thread_id}/fork` | Yes (`token` mode) | Clones a remote thread's latest checkpoint into a new `thread_id`. |
| `POST /api/v1/threads/{thread_id}/prune` | Yes (`token` mode) | Deletes superseded checkpoints while keeping the latest `keep_latest` states. |
| `DELETE /api/v1/threads/{thread_id}` | Yes (`token` mode) | Permanently deletes a remote thread and all associated checkpoints. |
| `GET /.well-known/openid-configuration` | Public | OpenID Connect discovery metadata for cloud workload federation (AWS STS / GCP / Azure). |
| `GET /.well-known/jwks.json` | Public | JSON Web Key Set (`JWKS`) public keys for verifying server-issued tokens. |
| `GET /webhooks` | Public | Lists all registered inbound webhook provider adapters (`sns`, `pagerduty`, `alertmanager`, etc.). |
| `GET /webhooks/health` | Public | Health check for the webhook ingestion subsystem. |
| `GET /webhooks/deliveries` | Public / Token | Lists the 100 most recent webhook deliveries, status codes, and spawned LangGraph `thread_id`s. |
| `POST /webhooks/{provider}` | Provider Secret / HMAC | Ingests an inbound event from a supported DevOps or observability platform. |

---

## Next steps

- **[Webhooks & Event-Driven Automation](webhooks.md)** — Configure PagerDuty, Alertmanager, Datadog, AWS SNS, and GitHub webhook ingestion on your Central Server
- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — Understand how `--approval-mode smart` governs unattended server runs and optimizes model costs
- **[Credentials & Authentication](credentials.md)** — Configure `talkops auth login`, Control Plane JWKS, and cloud provider credentials
