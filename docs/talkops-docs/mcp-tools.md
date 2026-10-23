# MCP tools

> Load additional tools from Model Context Protocol (MCP) servers

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) lets you extend TalkOps (`talkops`) with tools from external servers—Kubernetes cluster inspectors, Terraform registries, cloud SDKs, databases, and internal developer platforms—without modifying the agent itself. TalkOps connects to configured MCP servers at startup, discovers their tools, and exposes them alongside its built-in tools.

Add MCP servers by placing an `.mcp.json` configuration file in your project for project-level scope, or in `~/.talkops/.mcp.json` to make them available across all projects on your machine.

## Quickstart

### 1. Create the `.mcp.json` config file

Create `~/.talkops/.mcp.json` for user-wide servers, or `.mcp.json` (or `.talkops/.mcp.json`) in your project root for repository-specific servers:

```bash
# User-level (available in every project)
mkdir -p ~/.talkops
touch ~/.talkops/.mcp.json

# Or project-level (available in the current repository)
touch .mcp.json
```

### 2. Add MCP servers

Define your servers under the `mcpServers` key:

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "npx",
      "args": ["-y", "@kubernetes/mcp-server"],
      "env": {
        "KUBECONFIG": "/Users/user/.kube/config"
      }
    },
    "aws": {
      "command": "npx",
      "args": ["-y", "@aws/mcp-server"],
      "env": {
        "AWS_PROFILE": "production",
        "AWS_REGION": "us-west-2"
      }
    }
  }
}
```

### 3. Launch TalkOps and inspect loaded tools

```bash
talkops
```

On startup, TalkOps connects to each configured server, discovers its tools, and registers them using the namespaced format `mcp__<server>__<tool>` (for example, `mcp__kubernetes__get_pods`).

Run `/mcp` inside an interactive session to inspect connected servers, tool schemas, and connection health, or test tools manually before agent execution.

<p align="center">
  <img src="./images/talkops-mcp-viewer.png" alt="TalkOps TUI /mcp server and tool inspector modal" width="850" />
</p>

> [!IMPORTANT]
> **Image Placeholder (`docs/talkops-docs/images/talkops-mcp-viewer.png`):**
> Capture a screenshot of the interactive `/mcp` modal in `talkops`, showing:
> 1. Connected MCP servers and their green connection status indicators.
> 2. The searchable list of registered `mcp__<server>__<tool>` tools.
> 3. The JSON parameter schema and description pane for a selected tool.

---

## Auto-discovery and flags

TalkOps searches for MCP configuration files in the following precedence order:

| Priority | Location | Scope |
| - | - | - |
| **1 (highest)** | `--mcp-config PATH` | Explicit CLI override for the current session |
| **2** | `<project>/.mcp.json` or `<project>/.talkops/.mcp.json` | Project-level (Claude Code compatible) |
| **3 (lowest)** | `~/.talkops/.mcp.json` | User-level (applies across all projects) |

### CLI flags and subcommands

| Flag / Command | Behavior |
| - | - |
| `--mcp-config PATH` | Load a custom MCP JSON config file as the highest-precedence source |
| `--no-mcp` | Disable all MCP server connections and tool loading for the session |
| `--trust-project-mcp` | Trust project-level MCP servers without interactive confirmation for the current run |
| `talkops mcp list` | List configured MCP servers and their connection status from the shell |
| `talkops mcp tools` | List all tools exposed by configured MCP servers from the shell |

> [!NOTE]
> `--mcp-config` and `--no-mcp` are mutually exclusive. If you already have a `.mcp.json` at your project root for Claude Code or Cursor, TalkOps discovers it automatically.

---

## Configuration format

Each key under `mcpServers` is a server name.

### `stdio` servers

`stdio` servers run as child processes over stdin/stdout:

| Field | Type | Required | Description |
| - | - | - | - |
| `command` | `string` | Yes | Executable to launch (`npx`, `uvx`, `python`, `docker`) |
| `args` | `string[]` | No | Command-line arguments passed to `command` |
| `env` | `object` | No | Environment variables injected into the server subprocess |

### Subagent and plugin MCP servers

Agent plugins and custom subagents can bundle their own dedicated MCP servers (via an `.mcp.json` inside the plugin package or `mcp_config` in the subagent's YAML frontmatter). Subagent MCP servers launch on demand when the subagent is invoked, scope their tools exclusively to that subagent, and shut down cleanly when the subagent finishes. See [Plugins and marketplaces](./plugins.md) and [Subagents](./subagents.md).

---

## Trust and security

### Global vs. project trust

* **User-level servers** (`~/.talkops/.mcp.json`) are always trusted because they live in your home directory.
* **Project-level servers** (`.mcp.json`, `.talkops/.mcp.json`) require confirmation on first use because they can be committed to Git repositories by external contributors. TalkOps saves your trust decisions in `~/.talkops/.state/mcp_trust.json`.

For unattended CI/CD runs that use project-scoped MCP servers, pass `--trust-project-mcp`:

```bash
talkops -p "Check cluster pod status" --trust-project-mcp
```

### Headless MCP security guard

When running non-interactively (`-p` / `-n` or piped stdin), `HeadlessMCPGuardMiddleware` classifies every MCP tool into one of four security tiers to prevent destructive unattended actions:

| Tier | Behavior in headless mode | Examples |
| - | - | - |
| **`READ_ONLY`** | Executes automatically | `mcp__kubernetes__get_pods`, `mcp__aws__describe_instances` |
| **`MUTATING_SAFE`** | Allowed in Auto (`-y`) and Smart (`--smart`) modes | `mcp__kubernetes__apply_manifest`, `mcp__aws__tag_resource` |
| **`MUTATING_DESTRUCTIVE`** | Blocked unless explicitly allowlisted | `mcp__kubernetes__delete_namespace`, `mcp__aws__terminate_instances` |
| **`PRIVILEGED`** | Blocked in unattended mode | Modifying cluster RBAC or root IAM policies |

---

## Troubleshooting

<details>
<summary><b>Server fails to start (stdio)</b></summary>

* Verify that the `command` binary (`npx`, `uvx`, `python`, `docker`) is installed and on your `PATH` by running `talkops doctor`.
* Check that all required environment variables (such as `KUBECONFIG` or `AWS_PROFILE`) exist in the `env` block or your shell.

</details>

<details>
<summary><b>Tools not appearing in the session</b></summary>

* Run `/mcp` in the TUI (or `talkops mcp list` and `talkops mcp tools` from the shell) to check whether the server connected or reported a startup error.
* If you edited `.mcp.json` while a session was already open, run `/reload` to refresh MCP connections.

</details>

<details>
<summary><b>Project MCP server skipped in non-interactive mode</b></summary>

* Non-interactive runs (`-p` / `-n`) cannot display an interactive trust prompt. Either open `talkops` interactively once in the repository to record trust in `~/.talkops/.state/mcp_trust.json`, or pass `--trust-project-mcp` on the command line.

</details>

---

## See also

* [Configuration](./Configuration.md)
* [Approval modes](./approval-mode.md)
* [Plugins and marketplaces](./plugins.md)
* [Subagents](./subagents.md)
* [Command reference](./cli-reference.md)
