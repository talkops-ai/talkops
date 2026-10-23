# Plugins and marketplaces

> Install plugins from marketplaces or package subagents, skills, MCP servers, and hooks for TalkOps

Plugins extend TalkOps (`talkops`) with reusable [subagents](./subagents.md), [skills](./memory-and-skills.md#skills), [MCP servers](./mcp-tools.md), [custom commands](./custom-commands.md), and [hooks](./hooks.md). Marketplaces provide catalogs for discovering and installing plugins across projects or teams. TalkOps connects out of the box to the official **TalkOps DevOps Plugin Marketplace** ([`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins)) and supports private enterprise marketplaces, as described in [Create a plugin](#create-a-plugin) and [Create a marketplace](#create-a-marketplace).

> [!WARNING]
> Install plugins and marketplaces only from sources you trust. An enabled plugin can add instructions and run MCP servers, hook commands, or custom subagents with your user permissions.

## Plugin types

TalkOps categorizes plugins into three types based on their package contents:

| Type | What it adds | How it binds | Examples |
| - | - | - | - |
| **Agent plugins** | Dedicated specialist subagent with domain skills and isolated MCP servers | Binds to the dedicated subagent with `SubagentMemoryStore` isolation | `aws-sre-agent`, `aws-finops-agent`, `aws-iac-engineer` |
| **Vertical plugins** | Domain skill packs and lifecycle hooks (no separate subagent) | Binds directly to the root orchestrator | `aws-networking`, `aws-containers`, `aws-cost-optimization` |
| **Partner plugins** | Official third-party vendor integrations bundling tools and skills | Binds to the root agent or designated subagent | HashiCorp Terraform skill collection |

TalkOps detects plugin types automatically: if a plugin package contains an `agents/` directory, it registers as an **agent plugin**; otherwise, its skills and hooks attach directly to the main orchestrator.

### TalkOps DevOps Plugin Marketplace

The [`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins) marketplace provides specialist cloud subagents:

* **`aws-finops-agent`**: Audits cloud spend, identifies unattached EBS volumes and idle NAT Gateways, analyzes Compute Savings Plans, and generates cost-optimization diffs.
* **`aws-sre-agent`**: Investigates CloudWatch alarms, correlates X-Ray traces, filters log streams, and diagnoses incident root causes.
* **`aws-iac-engineer`**: Architects, validates, and refactors AWS CDK, CloudFormation, OpenTofu, and Terraform infrastructure.
* **`aws-cloud-security-engineer`**: Reviews IAM policies for least privilege, triages Security Hub and GuardDuty findings, and validates KMS encryption.
* **`aws-database-engineer`**: Tunes RDS/Aurora parameter groups, analyzes slow queries, and configures DynamoDB scaling policies.
* **`aws-platform-engineer`**: Manages EKS clusters, VPC network topologies, AWS Load Balancer Controller, and Karpenter autoscaling.

---

## Manage plugins interactively

To browse marketplaces and manage plugins in an `talkops` session:

1. Run `/plugins` to open the plugin manager modal.
2. Open the **Marketplaces** tab to browse or add marketplace sources. Supported sources include:
   * A GitHub repository in `owner/repo` format (for example, `talkops-ai/devops-plugins`), optionally followed by `@branch-or-tag`.
   * An HTTPS Git repository URL, optionally followed by `#branch-or-tag`.
   * An HTTPS URL that serves a `marketplace.json` catalog file.
   * A local marketplace directory or JSON file.
3. Select a plugin from the catalog to inspect its description, bundled subagents, skills, and MCP tools, then click **Install**.
4. Run `/reload` to activate newly installed plugin subagents, skills, MCP servers, and hooks without restarting the session.

The plugin manager also lets you enable, disable, and uninstall installed plugins. Disabling a plugin keeps it installed on disk under `~/.talkops/plugins/` but excludes its subagents, skills, MCP servers, and hooks after you run `/reload` or start a new session.

<p align="center">
  <img src="./images/talkops-plugins-modal.png" alt="TalkOps TUI plugin manager showing installed plugins and the TalkOps marketplace" width="850" />
</p>

> [!IMPORTANT]
> **Image Placeholder (`docs/talkops-docs/images/talkops-plugins-modal.png`):**
> Capture a screenshot of the interactive `/plugins` modal inside `talkops`, showing:
> 1. The **Installed** and **Marketplaces** tabs (`talkops-ai/devops-plugins`).
> 2. A selected plugin entry (such as `aws-sre-agent` or `aws-finops-agent`) displaying its bundled subagents, skills, and MCP servers.
> 3. The Enable / Disable / Install action controls.

---

## Manage plugins from the command line

Use `talkops plugin` for scripts and terminal-based administration:

```bash
# Add and inspect a marketplace
talkops plugin marketplace add talkops-ai/devops-plugins
talkops plugin marketplace list
talkops plugin marketplace refresh

# Install specialist subagents and vertical skill packs (global user scope by default)
talkops plugin install aws-sre-agent
talkops plugin install aws-finops-agent --scope project
talkops plugin install aws-networking --scope local

# List and search plugins for the current workspace
talkops plugin list
talkops plugin search finops

# Change plugin state (globally or per workspace)
talkops plugin disable aws-finops-agent --scope project
talkops plugin enable aws-finops-agent --scope project

# Uninstall a plugin from a specific scope or across all scopes
talkops plugin uninstall aws-finops-agent --scope project
talkops plugin uninstall aws-sre-agent
```

After installing or toggling a plugin from the shell, run `/reload` in an active interactive session or start a new session.

---

## Scopes and workspace isolation

TalkOps supports three installation scopes for marketplace plugins plus repository-local plugin directories, ensuring that plugins (and their bundled skills, subagents, MCP servers, and commands) installed for one repository do not leak into other workspaces.

### Installation scopes

| Scope | Flag / UI Option | Settings File | Visibility & Git Tracking |
| - | - | - | - |
| **User (Global)** | `--scope user` ("Install for you") | `~/.talkops/settings.json` | Active across all workspaces for your user account unless disabled in a specific workspace. |
| **Project (Shared)** | `--scope project` ("Install for all collaborators on this repository") | `<repo>/.talkops/settings.json` | Active **only** inside `<repo>`. Commit `.talkops/settings.json` so collaborators auto-materialize the plugin on session start. |
| **Local (Repo-Private)** | `--scope local` ("Install for you, in this repo only") | `<repo>/.talkops/settings.local.json` | Active **only** inside `<repo>`. Automatically added to `<repo>/.git/info/exclude` so it stays out of version control. |
| **Workspace Directory / Manifest** | Placed in repo tree | `<repo>/.talkops/plugins/<name>`, `<repo>/.agents/plugins/<name>`, `<repo>/plugins/<name>`, or `<repo>/{.talkops,.agents}/plugins.json` | Discovered **only** when running inside `<repo>`. Supports `plugin.json`, `.talkops-plugin/plugin.json`, `.agents-plugin/plugin.json`, and `.claude-plugin/plugin.json`. |

### How workspace isolation and precedence work

1. **Shared Versioned Cache with Scoped Registries**:
   * Marketplace plugins are downloaded once into the versioned cache at `~/.talkops/plugins/cache/<marketplace>/<plugin>/<version>` and tracked per scope in `~/.talkops/.state/installed_plugins.json`.
   * Even though the physical cache is shared to avoid duplicate downloads, a plugin installed with `--scope project` or `--scope local` in **Repository A** is **only** enabled in Repository A's settings file. When you run TalkOps in **Repository B**, Repository A's project/local plugins—and all of their skills, subagents, MCP servers, and slash commands—remain completely invisible.
2. **Scope Precedence (`local > project > user`)**:
   * Enablement state is merged in order from lowest to highest specificity: `user` (`~/.talkops/settings.json`) $\rightarrow$ `project` (`<repo>/.talkops/settings.json`) $\rightarrow$ `local` (`<repo>/.talkops/settings.local.json`).
   * You can disable a globally installed (`user`) plugin inside a specific repository without uninstalling it globally:
     ```bash
     talkops plugin disable aws-finops-agent --scope project
     ```
3. **Declarative Workspace `plugins.json` (`entries` and `exclude`)**:
   * A repository can also declare local plugin directories or suppress global plugins via `.talkops/plugins.json` or `.agents/plugins.json`:
     ```json
     {
       "entries": ["../custom-plugins/internal-sre"],
       "exclude": ["aws-finops-agent"]
     }
     ```

### Uninstalling globally vs. in a workspace

* **Workspace-Scoped Uninstall (`--scope project` or `--scope local`)**:
  Removes the plugin's install record for that workspace and clears its `enabledPlugins` entry from `<repo>/.talkops/settings.json` (or `settings.local.json`). If another workspace or your `user` scope still references the cached plugin version, the cached files in `~/.talkops/plugins/cache/` are preserved.
* **Global / All-Scopes Uninstall (`--scope user` or omitting `--scope`)**:
  Removes the user-level (or all scoped) install records, clears `enabledPlugins` entries, and—once no remaining scope references the plugin—deletes both the cached package directory (`~/.talkops/plugins/cache/...`) and the plugin's persistent data directory (`~/.talkops/plugins/data/<plugin-id>`).
* **Repository-Tracked Directory Plugins**:
  Plugins placed directly inside `<repo>/.talkops/plugins/`, `<repo>/.agents/plugins/`, or `<repo>/plugins/` are source files owned by the repository rather than the managed cache. To deactivate them in a workspace without deleting repository files, run `talkops plugin disable <name>@project --scope project` (or add the name to `"exclude"` in `.talkops/plugins.json`).

---

## Create a plugin

An TalkOps plugin is a directory containing a `plugin.json` manifest and any combination of `agents/`, `skills/`, `.mcp.json`, `commands/`, or `hooks/`.

<details>
<summary><b>Agent plugin vs. vertical plugin directory layout</b></summary>

**Agent plugin structure** (bundles a dedicated subagent with isolated memory and MCP servers):

```text
aws-finops-agent/
├── plugin.json                # Plugin metadata, dependencies, author
├── agents/
│   └── aws-finops-agent.md    # Subagent definition (YAML frontmatter + prompt)
├── skills/
│   ├── aws-cost-optimization/
│   │   └── SKILL.md
│   └── aws-billing-audit/
│       └── SKILL.md
└── .mcp.json                  # Isolated MCP servers scoped to this subagent
```

**Vertical plugin structure** (bundles skills and hooks directly on the main agent):

```text
aws-networking/
├── plugin.json
└── skills/
    ├── vpc-design/
    │   └── SKILL.md
    └── transit-gateway-routing/
        └── SKILL.md
```

</details>

### Define the plugin manifest (`plugin.json`)

Place `plugin.json` at the plugin root to declare metadata, dependencies, subagents, skills, and MCP servers:

```json
{
  "name": "aws-finops-agent",
  "version": "1.0.0",
  "description": "Autonomous cloud financial management and cost optimization agent",
  "author": "TalkOps AI",
  "homepage": "https://github.com/talkops-ai/devops-plugins",
  "dependencies": [],
  "subagents": ["aws-finops-agent"],
  "skills": ["aws-cost-optimization", "aws-billing-audit"],
  "mcp_servers": {
    "aws-cost": {
      "command": "uvx",
      "args": ["aws-cost-mcp-server"]
    }
  }
}
```

### Add subagents, skills, and MCP servers

* **Subagents**: Place Markdown definitions with YAML frontmatter under `agents/`. See [Subagents](./subagents.md#defining-a-custom-subagent) for the frontmatter schema.
* **Skills**: Organize each skill as a directory under `skills/` containing `SKILL.md`. See [Memory and skills](./memory-and-skills.md#skills).
* **MCP servers**: Define servers in `.mcp.json` or under `mcp_servers` in `plugin.json`. For agent plugins, these MCP servers launch on demand when the subagent runs and stay scoped to that subagent. See [MCP tools](./mcp-tools.md).

---

## Create a marketplace

A marketplace is a repository or directory containing a `marketplace.json` catalog that lists available plugins and their source locations:

```json
{
  "name": "talkops-devops-plugins",
  "plugins": [
    {
      "name": "aws-sre-agent",
      "source": "./plugins/aws-sre-agent",
      "description": "CloudWatch log analysis, X-Ray trace correlation, and incident diagnosis"
    },
    {
      "name": "aws-finops-agent",
      "source": "./plugins/aws-finops-agent",
      "description": "Cloud spend audits, idle resource discovery, and Savings Plans analysis"
    }
  ]
}
```

Test a local marketplace by adding its directory and installing a plugin:

```bash
talkops plugin marketplace add ./my-marketplace
talkops plugin install aws-sre-agent
```

---

## See also

* [Subagents](./subagents.md)
* [Memory and skills](./memory-and-skills.md)
* [Custom commands](./custom-commands.md)
* [MCP tools](./mcp-tools.md)
* [Hooks](./hooks.md)
* [Command reference](./cli-reference.md)
