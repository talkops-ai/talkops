# CLI & Slash Command Reference

TalkOps provides two complementary command surfaces defined in `src/talkops/cli/` and `src/talkops/ui/command_registry.py`:
1. **Terminal CLI Subcommands (`talkops <subcommand>`)** for launching interactive TUI sessions, running headless CI/CD checks, starting the Central Server and webhook listener (`talkops serve`), authenticating (`talkops auth`), managing remote/local threads (`talkops threads`), and administering MCP servers, plugins, and skills.
2. **Interactive Slash Commands (`/<command>`)** for controlling the live session, switching Jev Smart Mode and model pools, inspecting cloud context, running `/review` or `/goal` loops, and managing subagents without leaving the TUI.

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/cli-command-palette.png`**
> **What to capture:** Screenshot of the TalkOps TUI command palette open when typing `/`, showing fuzzy autocomplete for `/smart`, `/pool`, `/cloud`, `/threads`, `/fork`, and `/review` with keyboard shortcut hints.

---

## Root CLI Invocation & Flags

### `talkops` / `ops` (Root Command & Alias)

Launches the interactive Textual terminal UI by default, or executes a non-interactive headless run when `-p` / `--prompt` is passed. You can invoke the CLI using either `talkops` or the `ops` shorthand alias.

```bash
talkops [FLAGS]          # or: ops [FLAGS]
talkops -p "<prompt>"    # or: ops -p "<prompt>"
```

#### Execution & Approval Mode Flags

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--prompt <text>` | `-p` | `None` | Execute `<text>` in headless non-interactive mode and print the response to stdout (`src/talkops/cli/main.py`). |
| `--smart` | `-s` | `False` | Enable **Jev Smart Mode**: `<70ms` 3-tier dynamic model/reasoning routing + `<100ms` System One tool safety classification. |
| `--auto-approve` | `-y` | `False` | Enable **Auto Mode**: auto-approve routine tools while still blocking catastrophic shell destructors (`rm -rf /`, `mkfs`). |
| `--yolo` | — | `False` | Enable **YOLO Mode**: bypass all tool approval prompts (use strictly inside disposable sandboxes). |
| `--max-turns <int>` | — | Unlimited | Maximum number of agentic turns allowed in a headless (`-p`) or `/goal` run. |
| `--criteria <text>` | — | `None` | Success rubric evaluated after each turn in headless (`-p`) mode; the agent iterates until the criteria are met or `--max-turns` is reached. |

#### Model & Sandbox Flags

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--model <spec>` | `-M`, `--models` | Active default | Primary model or comma-separated model pool (e.g., `claude-sonnet-4-6` or `claude-haiku-4-5,claude-sonnet-4-6,claude-opus-4-6`). |
| `--agent <name>` | `-a` | `talkops` | Named agent configuration or subagent profile to launch. |
| `--sandbox <type>` | `-S` | `none` | Execution sandbox backend (`none`, `local`, `docker`, `k8s`). |
| `--sandbox-id <id>` | — | Auto | Attach to an existing Docker container ID or Kubernetes pod name. |
| `--sandbox-setup <path>` | — | `None` | Path to a setup script executed inside the sandbox before the session starts. |
| `--shell-allow-list <list>` | — | Default safe list | Comma-separated list of allowed shell executables (or `recommended`). |
| `--no-Verify-ssl` | — | `False` | Disable TLS certificate verification for corporate proxy environments. |

#### Central Server & Multi-Client Connection Flags

| Flag | Short | Environment Variable | Description |
| :--- | :--- | :--- | :--- |
| `--server-url <url>` | — | `TALKOPS_SERVER_URL` | Connect the TUI or headless CLI as a remote client to a running `talkops serve` instance (e.g., `http://talkops.internal:2024`). |
| `--api-key <key>` | — | `TALKOPS_API_KEY` | Long-lived API key (`oc_live_...`, exchanged automatically for a short-lived JWT) or static server token. |
| `--user-id <id>` | — | `TALKOPS_USER_ID` | Explicit user identity (`X-TalkOps-User-Id`) for multi-tenant thread and memory isolation. |
| `--resume [thread_id]` | `-r` | — | Resume the most recent thread (when no ID is given) or attach to a specific local/remote `thread_id`. |
| `--initial-skill <name>` | — | — | Pre-load a specific `SKILL.md` runbook into the session context at startup. |

#### MCP & Utility Flags

| Flag | Short | Description |
| :--- | :--- | :--- |
| `--mcp-config <path>` | — | Load an additional `.mcp.json` configuration file for this session. |
| `--no-mcp` | — | Disable all external Model Context Protocol (MCP) servers for this run. |
| `--trust-project-mcp` | — | Automatically trust project-scoped `.mcp.json` stdio commands without prompting. |
| `--interactive` | `-i` | Force interactive TUI mode even if initial prompt arguments are supplied. |
| `--acp` | — | Run the Agent Client Protocol (ACP) stdio server for IDE integrations. |
| `--version` | `-v` | Print the installed TalkOps version and exit. |
| `--help` | `-h` | Show root CLI help and exit. |

---

## CLI Subcommands (Individual Mini-Sections)

### `talkops serve` (Aliases: `talkops webhooks`, `talkops webhook`)

Starts the LangGraph-backed Central Server and inbound webhook ingestion listener (`src/talkops/cli/commands/serve.py`), or lists all registered webhook provider adapters.

```bash
talkops serve [start|list] [FLAGS]
talkops webhooks [start|list] [FLAGS]
```

- **Sub-actions**:
  - `talkops serve` / `talkops serve start`: Launches the ASGI server hosting `/api/v1/*`, LangGraph `/threads` & `/runs`, OIDC `/.well-known/*`, and `/webhooks/{provider}`.
  - `talkops serve list` / `talkops webhooks list`: Prints all 9 registered webhook provider adapters (`sns`, `pagerduty`, `alertmanager`, `cloudevents`, `jenkins`, `github`, `datadog`, `servicenow`, `generic`) and their aliases.
- **Flags**:
  - `--host <addr>`: Bind address (default `0.0.0.0`, env `TALKOPS_WEBHOOK_HOST`).
  - `--port <int>`: Primary server port (default `2024`, env `TALKOPS_WEBHOOK_PORT`).
  - `--webhook-port <int>`: Optional separate port dedicated exclusively to `/webhooks/*` traffic.
  - `--webhook-host <addr>`: Bind address for the dedicated webhook listener when `--webhook-port` is used.
  - `--webhook-secret <secret>`: Default shared secret or bearer token for inbound webhooks (`TALKOPS_WEBHOOK_SECRET`).
  - `--postgres-uri <uri>`: PostgreSQL connection URI for multi-user checkpointing (`TalkOpsPostgresCheckpointer`) and store persistence (`TALKOPS_POSTGRES_URI`).
  - `--auth-mode {noop,token}`: Client authentication enforcement mode (`TALKOPS_AUTH_MODE`).
  - `--approval-mode {smart,auto,yolo,manual}`: Tool execution policy for unattended webhook/server runs (default `smart`).
  - `--workspaces-dir <dir>`: Base workspace directory for server execution (`TALKOPS_WORKSPACES_DIR`).
  - `--model <name>`: Override the default orchestrator model.

```bash
# Launch production central server with PostgreSQL and dedicated webhook port
talkops serve --host 0.0.0.0 --port 2024 --webhook-port 8080 \
  --postgres-uri postgresql://ops:secret@db:5432/talkops \
  --auth-mode token --approval-mode smart
```

---

### `talkops auth`

Manages Central Server JWT sessions, encrypted provider API keys in `~/.talkops/.state/config.db`, and AWS profile defaults (`src/talkops/cli/commands/auth.py`).

```bash
talkops auth <subcommand> [ARGS]
```

- **`talkops auth status`** *(default)*: Displays your active Control Plane / Central Server session, JWT expiration countdown, offline grace status, and configured LLM/cloud credentials.
- **`talkops auth login`**: Exchanges a long-lived API key (`oc_live_...`) at `POST /api/v1/auth/token` for a short-lived signed JWT (`eyJ...`) and caches it in `~/.talkops/.state/auth.json`.
  - Flags: `--server-url <url>`, `--api-key <key>`, `--user-id <id>`, `--control-plane-url <url>`.
- **`talkops auth logout`**: Clears cached JWTs and revokes the local Central Server session.
- **`talkops auth token [--refresh]`**: Prints the active bearer JWT for scripting (`curl -H "Authorization: Bearer $(talkops auth token)"`) or forces an immediate token refresh (`--refresh`).
- **`talkops auth list`**: Lists all LLM, Jev, and cloud credentials stored in `config.db` or environment variables with redacted previews.
- **`talkops auth set <provider> <key>`**: Encrypts and saves a provider API key (`anthropic`, `openai`, `google`, `typesafe`, `openrouter`, `deepseek`, `mistral`, `groq`, `together`, `fireworks`, `cohere`, `perplexity`, `xai`, `cerebras`, `sambanova`, `langsmith`).
- **`talkops auth remove <provider>`** *(alias: `delete`)*: Removes a stored credential from `config.db`.
- **`talkops auth aws --profile <name> [--region <region>]`**: Persists the default AWS CLI profile and region used by AWS tools and subagents.

---

### `talkops threads`

Lists and deletes persistent conversation threads stored either in the local SQLite database (`~/.talkops/sessions.db`) or on a remote Central Server (`src/talkops/cli/main.py`).

```bash
talkops threads list [-n <limit>] [--agent <name>] [--server-url <url>] [--api-key <key>] [--user-id <id>]
talkops threads delete <thread_id> [--server-url <url>] [--api-key <key>] [--user-id <id>]
```

```bash
# List the 20 most recent threads on a remote Central Server
talkops threads list -n 20 --server-url http://talkops.internal:2024 --user-id alice

# Delete a specific thread
talkops threads delete 8f3a9c12-4b2e-4d1a-9e5b-123456789abc
```

---

### `talkops mcp`

Lists, validates, and live-probes external Model Context Protocol (MCP) tool servers (`src/talkops/cli/commands/mcp.py`).

```bash
talkops mcp <list|status|probe> [FLAGS]
```

- **`talkops mcp list`** *(alias: `ls`)*: Lists all configured MCP servers merged from user, project (`.mcp.json`), and plugin scopes.
- **`talkops mcp status`**: Performs static validation of server configurations, checking that local `stdio` binaries exist on `PATH` and remote `sse`/`http` URLs are well-formed.
- **`talkops mcp probe [--server <name>] [--timeout <sec>]`**: Spawns or connects to each MCP server, completes the JSON-RPC initialization handshake, and lists every discovered tool schema.

---

### `talkops plugin` (Alias: `talkops plugins`)

Searches, installs, enables, disables, and manages multi-component DevOps plugins (`src/talkops/plugins/commands_cli.py`).

```bash
talkops plugin <subcommand> [ARGS]
```

- **`talkops plugin list`**: Lists installed plugins, versions, enabled state, and contributed skills/agents/MCP servers.
- **`talkops plugin search <query>`**: Searches registered plugin marketplaces by keyword or tag.
- **`talkops plugin install <spec>`**: Installs a plugin by marketplace name, GitHub `owner/repo`, Git URL, or local directory path.
- **`talkops plugin uninstall <name>`**: Removes an installed plugin.
- **`talkops plugin enable <name>`** / **`talkops plugin disable <name>`**: Toggles a plugin on or off without deleting its files.
- **`talkops plugin refresh`**: Refreshes cached marketplace manifests.
- **`talkops plugin marketplace <list|add|remove>`**: Manages custom Git/HTTP plugin marketplace registries.

---

### `talkops skills` (Alias: `talkops skill`)

Creates, inspects, trusts, and deletes reusable `SKILL.md` operational runbooks (`src/talkops/skills/commands.py`).

```bash
talkops skills <list|create|info|delete|trust> [ARGS] [--project]
```

- **`talkops skills list [--project]`**: Lists all discovered skills across `~/.talkops/skills/`, `.talkops/skills/`, and active plugins.
- **`talkops skills create <name> [--project]`**: Scaffolds a new skill directory with a template `SKILL.md` and `scripts/` folder.
- **`talkops skills info <name>`**: Displays the skill's YAML frontmatter, description, companion scripts, and SHA-256 trust status.
- **`talkops skills trust <name>`**: Computes and records a cryptographic trust signature for executable scripts bundled inside a project skill.
- **`talkops skills delete <name> [--project]`**: Deletes a skill directory.

---

### `talkops doctor`

Runs a comprehensive 9-point diagnostic check (`src/talkops/cli/commands/doctor.py`) verifying Python runtime, LLM provider keys, TypeSafe AI Jev connectivity (`TYPESAFE_API_KEY`), AWS/Azure/GCP/Kubernetes CLI availability, sandbox runtime (`docker`/`kubectl`), MCP server health, and SQLite/PostgreSQL state database permissions.

```bash
talkops doctor
```

---

### `talkops help`

Displays detailed CLI usage and subcommand syntax.

```bash
talkops help [subcommand]
```

---

## Interactive Slash Commands (Individual Mini-Sections)

Inside the interactive TUI (and in remote `--server-url` sessions), type `/` to invoke any of the built-in slash commands registered in `src/talkops/ui/command_registry.py`. Each command below is grouped by operational workflow with its exact syntax, aliases, and behavior.

### Approval & Safety Mode Commands

#### `/smart`
- **Syntax**: `/smart` (Keyboard shortcut: `Shift+Tab`)
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Activates **Jev Smart Mode**, enabling both `<70ms` 3-tier dynamic model and reasoning routing (`fast` with `effort="off"`, `standard` with `effort="medium"`, `powerful` with `effort="max"`) and `<100ms` System One tool safety classification (`is_mutating`, `blast_radius`, `intent_alignment`).

#### `/auto`
- **Syntax**: `/auto`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Switches the session to **Auto-Approve Mode**, executing routine tool calls automatically while blocking hardcoded catastrophic shell patterns. Restores your baseline model if switching out of `/smart`.

#### `/manual`
- **Syntax**: `/manual`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Switches the session to **Manual Approval Mode** (every mutating tool call or shell execution requires explicit `[y/n/e]` human confirmation) and automatically restores your pre-Smart baseline model.

#### `/permissions`
- **Syntax**: `/permissions`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive permission policy inspector showing active allow/ask/deny rules from `permissions.json` and session overrides.

---

### Model, Smart Pool & Reasoning Commands

#### `/model`
- **Syntax**: `/model [provider:model]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive model selector modal (or switches immediately when `[provider:model]` is passed, e.g., `/model anthropic:claude-sonnet-4-6`).

#### `/pool` (Alias: `/agent-pool`)
- **Syntax**: `/pool [fast,standard,powerful]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Configures or inspects the 3-tier **Dynamic Model Pool** (`Tier 0 Fast`, `Tier 1 Standard`, `Tier 2 Powerful`) used by Jev Smart Mode routing, and displays per-tier routing telemetry and cost savings.

#### `/effort`
- **Syntax**: `/effort [off|low|medium|high|xhigh|max]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Sets or inspects the thinking/reasoning token budget for reasoning-capable models (`claude-opus-4-6`, `claude-sonnet-4-6`, `o3`, `gpt-5.4`, `gemini-3.1-pro`). Note: In Smart Mode, Jev dynamically overrides effort per turn (`off` on Tier 0, `medium`/`low` on Tier 1, `max`/`xhigh` on Tier 2).

#### `/fast`
- **Syntax**: `/fast`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Toggles low-latency Fast Mode, pinning the session to your configured Tier 0 fast model with reasoning disabled (`effort="off"`) for rapid interactive lookups.

---

### Cloud, DevOps, Code Review & Goal Commands

#### `/cloud` (Alias: `/aws`)
- **Syntax**: `/cloud [profile] [region]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the multi-cloud context switcher or directly updates the active AWS profile/region, Azure subscription, GCP project, and Kubernetes context displayed in the TUI status bar.

#### `/review` (Alias: `/code-review`)
- **Syntax**: `/review [target]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Dispatches the `code-reviewer` specialist to perform a structured security, reliability, and IaC/code quality review on uncommitted `git diff` changes, a specific branch, or a file path.

#### `/goal`
- **Syntax**: `/goal [objective]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Launches an autonomous multi-turn goal-verification loop. The agent plans, executes tools, and evaluates its own output against your active `/rubric` until all acceptance criteria pass.

#### `/rubric` (Alias: `/criteria`)
- **Syntax**: `/rubric [criteria]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Defines or inspects the acceptance rubric used by `/goal` to determine when an operational objective is complete.

#### `/trace`
- **Syntax**: `/trace [count]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Displays execution trace spans, tool latencies, Jev classification timings, and LangSmith trace URLs for the most recent `[count]` turns.

#### `/doctor`
- **Syntax**: `/doctor`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Runs the 9-point environment health check directly inside the TUI and renders a diagnostic report card.

---

### Session, Thread & Context Management Commands

#### `/threads` (Aliases: `/resume`, `/continue`)
- **Syntax**: `/threads`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive thread browser to search, resume, rename, or delete local SQLite threads or remote Central Server threads (including webhook-created incident threads).

#### `/fork` (Alias: `/branch`)
- **Syntax**: `/fork [new-title]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Clones the current conversation checkpoint into a brand-new thread ID (`POST /api/v1/threads/{id}/fork` in remote mode) so you can explore an alternative debugging path without mutating the original thread.

#### `/compact` (Alias: `/offload`)
- **Syntax**: `/compact [focus instructions]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Summarizes older conversation turns into a dense operational handoff summary, frees up context window tokens, and prunes superseded checkpoints.

#### `/context`
- **Syntax**: `/context`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Renders a visual breakdown of your current context window usage across system prompts, `AGENTS.md`, tool schemas, MCP servers, and message history.

#### `/cost` (Alias: `/tokens`)
- **Syntax**: `/cost`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Displays cumulative input/output/reasoning token counts, prompt cache hit rates, Jev Smart Mode tier distribution, and estimated USD cost for the session.

#### `/btw` (Alias: `/aside`)
- **Syntax**: `/btw <question>`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Asks a quick epistemic side-question in an ephemeral overlay without polluting the main thread's message history or interrupting an ongoing plan.

#### `/clear` & `/force-clear`
- **Syntax**: `/clear` or `/force-clear`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Clears the visible terminal viewport and starts a fresh conversation thread (`/force-clear` resets immediately even if a background turn is winding down).

#### `/copy`
- **Syntax**: `/copy`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Copies the latest assistant response (or selected code block) to your system clipboard.

#### `/tasks`
- **Syntax**: `/tasks`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the background task manager showing running shell daemons, subagent runs, and scheduled loops.

#### `/loop`
- **Syntax**: `/loop <interval> <prompt>`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Schedules a recurring prompt inside the session (e.g., `/loop 5m Check rollout status of payment-api in prod-eks and alert if error rate > 1%`).

#### `/exit` (Aliases: `/quit`, `/q`)
- **Syntax**: `/exit` (Keyboard shortcut: `Ctrl+D`)
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Cleanly saves the session checkpoint and exits the TalkOps TUI.

---

### Subagents, Skills, Plugins, MCP & Memory Commands

#### `/agent` (Aliases: `/agents`, `/agent:<name>`)
- **Syntax**: `/agent` or `/agent:<name> [task]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the subagent catalog showing all discovered plugin subagents, custom `.talkops/agents/*.md` definitions, and `[async_subagents]`, or directly dispatches `<name>` (e.g., `/agent:eks-cluster-operator Check failing pods in kube-system`).

#### `/skills` (Aliases: `/tools`, `/skill:<name>`)
- **Syntax**: `/skills` or `/skill:<name> [args]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Lists all available `SKILL.md` operational runbooks and active tools, or directly loads and executes a specific skill (e.g., `/skill:eks-upgrade`).

#### `/skill-creator`
- **Syntax**: `/skill-creator [description]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Launches an interactive workflow that distills your recent troubleshooting commands or operational instructions into a reusable, validated `SKILL.md` file.

#### `/plugins`
- **Syntax**: `/plugins`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive plugin manager to browse, install, enable, or disable plugins from the marketplace.

#### `/mcp`
- **Syntax**: `/mcp`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Displays connected Model Context Protocol (MCP) servers, connection latency, and discovered external tools, with options to reconnect or probe servers.

#### `/memory` & `/remember`
- **Syntax**: `/memory` or `/remember <fact or preference>`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: `/memory` opens the persistent memory viewer (`AGENTS.md` and long-term store items); `/remember <fact>` immediately persists a new architectural rule or operational preference to long-term memory.

---

### Configuration, Authentication & UI Commands

#### `/config`
- **Syntax**: `/config [group|key] [value]`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive settings editor or directly reads/updates any canonical configuration key in `~/.talkops/.state/config.db` (e.g., `/config aws.region us-west-2`).

#### `/login` (Aliases: `/auth`, `/connect`) & `/logout`
- **Syntax**: `/login` or `/logout`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Opens the interactive credential & Central Server authentication modal to add provider API keys or exchange a Central Server token, or logs out of the active remote server session.

#### `/reload` & `/restart`
- **Syntax**: `/reload` (Local & Remote) / `/restart` (Local TUI)
- **What it does**: `/reload` hot-reloads `config.toml`, `AGENTS.md`, skills, plugins, and MCP servers without dropping your thread; `/restart` performs a full process re-exec of the local TUI.

#### `/install`, `/update`, & `/auto-update`
- **Syntax**: `/install`, `/update` (Local TUI), `/auto-update` (Local & Remote)
- **What it does**: Checks for and installs new TalkOps releases, or toggles background automatic updates.

#### `/theme`, `/notifications`, `/timestamps`, & `/scrollbar`
- **Syntax**: `/theme [name]`, `/notifications`, `/timestamps`, `/scrollbar`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Customizes TUI appearance—switching color themes (`dark`, `light`, `dracula`, `nord`, `monokai`), desktop completion notifications, message timestamps, and scrollbar visibility.

#### `/version`, `/bug` (Alias: `/feedback`), & `/help`
- **Syntax**: `/version`, `/bug`, `/help`
- **Supported Modes**: Local TUI & Remote Server Client
- **What it does**: Prints version/build metadata, submits diagnostics or bug feedback, or opens the interactive keyboard shortcut and command reference modal.
