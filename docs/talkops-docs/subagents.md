# Subagents

Subagents in TalkOps (`src/talkops/subagents/loader.py` and `src/talkops/middleware/subagents.py`) allow the orchestrator to delegate specialized DevOps, cloud, SRE, security, and coding workflows to isolated worker agents.

Each subagent runs with its own system prompt, isolated memory branch (`SubagentMemoryStore`), scoped tool permissions (`permission_tier`), domain skills, and optional dedicated MCP servers (`mcp_config` / `mcp_files`). Verbose cloud telemetry, `kubectl describe` dumps, and multi-file searches remain confined to the subagent's execution branch—only the final structured synthesis returns to your main conversation thread.

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/talkops-subagents-panel.png`**
> **What to capture:** Screenshot of the live **Subagent Panel** in the `talkops` TUI showing parallel spawned subagents (e.g., `aws-sre-agent`, `k8s-cluster-operator`, `finops-auditor`), active tool calls, Jev Smart Mode tier badges (`Tier 0 Fast [effort=off]` vs. `Tier 1 Standard`), elapsed duration, and token usage.

---

## Dynamic discovery and precedence

TalkOps resolves subagents dynamically from four sources in order of precedence (`src/talkops/subagents/loader.py`):

```text
1. Installed Agent Plugins    ~/.talkops/plugins/<plugin>/agents/{name}.md
2. Project Subagents          .talkops/agents/{name}.md  (or .talkops/agents/{name}/AGENTS.md, .agents/)
3. User Subagents             ~/.talkops/agents/{name}.md (or ~/.agents/)
4. Async Remote Subagents     Declared in config.toml under [async_subagents]
```

1. **Dynamic Plugin Subagents**: Agent plugins installed via `talkops plugin install` (or `/plugins`) bundle domain-specific subagent Markdown files alongside scoped `skills/` and isolated `.mcp.json` servers.
2. **Project Subagents**: Checked into version control under `.talkops/agents/` or `.agents/` so your entire team shares repo-specific operational specialists.
3. **User Subagents**: Personal subagent definitions stored in `~/.talkops/agents/`.
4. **Async Remote Subagents**: Long-running remote LangGraph subagents configured under `[async_subagents]` in `config.toml`.

---

## How Smart Mode optimizes subagent cost, latency & safety

When **Jev Smart Mode** (`--smart` or `/smart`) is active, subagents benefit from two dedicated TypeSafe AI Jev optimizations:

### 1. Independent subagent model routing (`SUBAGENT_ROUTE_CRITERIA`)

Unless a subagent hardcodes a `model:` in its YAML frontmatter, `JevDynamicModelRouterMiddleware` (`src/talkops/middleware/jev_model_router.py`) sets `routing_state["agent_role"] = "subagent"` and routes every subagent turn in `<70ms` across your 3-tier model pool:
- **Tier 0 (`fast` + `effort="off"`)**: Subagent turns performing routine log grep, single-file inspection, or status output parsing run on lightweight models (`haiku`, `mini`, `flash-lite`) with reasoning disabled—cutting subagent fan-out token costs by **80–90%**.
- **Tier 1 (`standard` + `effort="medium"`)**: Subagent turns performing standard single-domain analysis (e.g., inspecting an EKS deployment or drafting a Terraform diff) run on balanced models.
- **Tier 2 (`powerful` + `effort="max"`)**: Subagent turns performing deep root-cause correlation across multi-service failure modes or FinOps/security audits automatically route to frontier reasoning models.

### 2. Subagent dispatch safety classification (`<100ms`)

When the orchestrator invokes the `task` tool to dispatch a subagent, `JevSecurityClassifier` inspects the target subagent's `capabilities`, `permission_tier` (`read-only`, `controlled`, `full`), and task prompt in `<100ms` to verify `intent_alignment` and `blast_radius` before execution begins.

---

## Defining a custom subagent

Subagent files use YAML frontmatter followed by a Markdown body that serves as the subagent's system prompt:

```markdown
---
name: eks-cluster-operator
description: Specialist in Kubernetes cluster health diagnostics, node group capacity, and add-on upgrades
skills:
  - aws-eks-autopilot
  - kubernetes-diagnostics
tools:
  - execute
  - read_file
  - edit_file
permission_tier: controlled
capabilities:
  - kubernetes
  - aws-eks
---

You are an EKS operations subagent. Analyze cluster health, node group capacity, and add-on versions before recommending or applying changes.

## Your Process
1. Inspect live cluster state with `kubectl get` and `aws eks describe-cluster`.
2. Summarize findings in a structured table before proposing manifest or node group changes.
```

### Frontmatter reference (`src/talkops/subagents/loader.py`)

| Field | Required | Description |
| :--- | :--- | :--- |
| `name` | Yes *(or filename)* | Unique identifier for the subagent (invoked via `/agent:<name>` or `talkops -a <name>`). |
| `description` | Yes | Summary of capabilities used by the orchestrator when deciding which specialist to dispatch. |
| `model` | No | Optional fixed `provider:model` override. Omit this field to let **Jev Smart Mode** dynamically route each turn across `fast`, `standard`, and `powerful`. |
| `skills` | No | List (or comma-separated string) of `SKILL.md` runbooks to pre-load for this subagent. |
| `tools` | No | Explicit tool allowlist (`execute`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`). |
| `permission_tier` | No | Execution boundary: `read-only`, `controlled`, or `full`. |
| `capabilities` | No | Domain capability tags passed to Jev `routing_state` and `JevSecurityClassifier`. |
| `mcp_config` | No | Inline dictionary of MCP servers scoped exclusively to this subagent. |
| `mcp_files` | No | List of `.mcp.json` file paths loaded exclusively for this subagent. |

---

## Invoking and managing subagents

### From the CLI (`-a` / `--agent`)

Launch TalkOps directly into a named subagent profile:

```bash
talkops -a eks-cluster-operator --smart -p "Audit node group capacity in prod-eks"
```

### From the interactive TUI (`/agent` & `/agent:<name>`)

```text
/agent                                                   # Open the interactive subagent selector & inspector
/agent:eks-cluster-operator Check failing kube-system pods # Dispatch a specific subagent directly
```

### Webhook RBAC subagent scoping (`IdentityMapper`)

When `talkops serve` processes an inbound webhook (`/webhooks/{provider}`), `IdentityMapper` (`src/talkops/webhooks/dispatcher.py`) maps the webhook source and severity to an RBAC role and injects `allowed_subagents` into the LangGraph configurable context, ensuring automated webhook investigations only dispatch authorized specialists.

---

## Next steps

- **[Plugins](plugins.md)** — Install pre-packaged DevOps agent bundles (`talkops plugin install`)
- **[Memory & Skills](memory-and-skills.md)** — Author reusable `SKILL.md` runbooks attached to your subagents
- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — Learn how `SUBAGENT_ROUTE_CRITERIA` and System One safety govern subagents
