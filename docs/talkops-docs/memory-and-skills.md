# Memory and skills

> Persistent memory, AGENTS.md files, and reusable skills for TalkOps, including creation, discovery, and invocation

There are two primary ways to customize how TalkOps (`talkops`) works in your environment:

* **[Memory](#memory)**: `AGENTS.md` files and persistent memory entries that carry across sessions. Use memory for coding style, infrastructure naming conventions, and project architecture.
* **[Skills](#skills)**: Reusable, on-demand `SKILL.md` directories that the agent discovers at startup and reads only when relevant. Use skills for task-specific workflows, cloud provider best practices, and reference checklists.

Use `/remember` inside any session to prompt the agent to extract conventions from the current conversation and save them to memory or skills.

> [!TIP]
> Want to define parameterized `/command` workflows with inline shell execution? See [Custom commands](./custom-commands.md).

---

## Memory

### Automatic convention learning

As you collaborate with TalkOps, you can teach it infrastructure and coding conventions that persist across future sessions:

```text
> We use AWS KMS customer-managed keys for all S3 buckets and enforce bucket-owner-enforced object ownership
```

Or explicitly save a convention with `/remember`:

```text
/remember Our S3 buckets always require KMS CMK encryption and bucket-owner-enforced ownership
```

TalkOps stores the convention and applies it automatically when you ask it to create or review resources in later sessions.

### Where memories are stored

| Scope | Path | When it loads |
| - | - | - |
| **Project** (higher priority) | `.talkops/memory/` | Whenever running inside that project |
| **User** (fallback) | `~/.talkops/memory/` | Every session across all directories |

When both scopes contain a memory with the same key, the project-level entry takes precedence. To keep memory from consuming excessive context-window capacity, TalkOps caps injected memory at **200 lines** (or **25 KB**) per session.

### Manage memory (`/memory`)

Use the `/memory` command inside an interactive session to inspect or edit stored entries:

```text
/memory                    # List all active memories
/memory save <key>         # Save a new memory entry
/memory delete <key>       # Remove a stored memory entry
```

### `AGENTS.md` files

[`AGENTS.md`](https://agents.md/) files provide persistent instructions that are always loaded into the system prompt at session start:

* **Project**: `.talkops/AGENTS.md` or `AGENTS.md` in the repository root—loaded whenever TalkOps runs inside that project (commit this file to Git to share conventions with your team).
* **Global**: `~/.talkops/{agent}/AGENTS.md`—loaded on every session for that agent across all repositories.

<details>
<summary><b>When to use global vs. project AGENTS.md</b></summary>

Use a **global `AGENTS.md`** (`~/.talkops/{agent}/AGENTS.md`) for:

* Personal communication style and formatting preferences
* Universal DevOps habits (for example, preferring `tofu` over `terraform` or always running dry-run diffs first)
* Tool usage patterns that apply across every repository

Use a **project `AGENTS.md`** (`.talkops/AGENTS.md` or `AGENTS.md`) for:

* Repository architecture, directory layout, and environment naming (`dev`, `staging`, `prod`)
* Terraform state backend conventions, required resource tags, and module structure
* Kubernetes cluster context rules, Helm chart conventions, and CI/CD test commands

</details>

---

## Skills

Skills package domain expertise—such as workflows, best practices, helper scripts, and reference docs—into reusable directories that follow the [Agent Skills specification](https://agentskills.io/).

To preserve context-window capacity, TalkOps uses **progressive disclosure**: at startup, it reads only the `name` and `description` from each `SKILL.md` frontmatter. When a task matches a skill's description, the agent loads the full `SKILL.md` instructions on demand. Discovery runs again whenever you execute `/reload`.

### Add a skill

#### 1. Create a skill directory

Scaffold a new skill from the shell or create a directory containing `SKILL.md`:

```bash
talkops skills create my-eks-runbook
```

Each skill directory follows this structure:

```text
my-eks-runbook/
├── SKILL.md          # Required — instructions with YAML frontmatter
├── scripts/          # Optional — helper scripts executed by the agent
├── references/       # Optional — domain documentation and cheat sheets
└── assets/           # Optional — templates, examples, and schemas
```

#### 2. Edit `SKILL.md`

Add YAML frontmatter (`name` and `description`) followed by Markdown instructions:

```markdown
---
name: kubernetes
description: "Author, audit, and troubleshoot Kubernetes manifests following Pod Security Standards"
---

# Kubernetes Engineering Skill

When authoring or debugging manifests:
1. Always define explicit CPU and memory requests and limits.
2. Configure readinessProbe and livenessProbe on all long-running workloads.
3. Enforce non-root securityContext (runAsNonRoot: true).
4. Include standard labels: app.kubernetes.io/name, instance, version.
```

### Skill discovery and precedence

TalkOps discovers skills across seven locations. When two skills share the same name, the higher-priority source overrides the lower one:

| Priority | Source | Location |
| - | - | - |
| **1 (highest)** | Project Agent Skills | `.agents/skills/` |
| **2** | Project TalkOps Skills | `.talkops/skills/` |
| **3** | User Agent Skills | `~/.agents/skills/` |
| **4** | User TalkOps Skills | `~/.talkops/skills/` |
| **5** | Marketplace & Workspace Plugin Skills | Enabled plugins for the active workspace (`~/.talkops/plugins/cache/`, `<repo>/.talkops/plugins/`, `<repo>/.agents/plugins/`) |
| **6** | Subagent Skills | Bundled subagent packages and agent-plugin skills (isolated to their subagent) |
| **7 (lowest)** | Global Built-in Skills | Bundled with TalkOps (`cloud-core`, `docker`, `kubernetes`, `remember`) |

> [!TIP]
> Use `.agents/skills/` for portable skills shared across multiple AI coding assistants, and `.talkops/skills/` for skills tailored to TalkOps workflows. Skills bundled inside a plugin installed with `--scope project` or `--scope local` (or placed under `<repo>/.talkops/plugins/` or `<repo>/.agents/plugins/`) are strictly isolated to that workspace and will not appear in other repositories. See [Plugins and marketplaces](./plugins.md#scopes-and-workspace-isolation).

> [!NOTE]
> To protect your machine from untrusted files in cloned repositories, TalkOps tracks project skill trust in `~/.talkops/.state/skill_trust.json` and prompts for confirmation the first time a project skill is discovered.

### Invoke or pre-load a skill

Skills activate automatically when a prompt matches their description, or you can invoke them explicitly:

```text
/skills                    # Browse all loaded skills in the TUI
/skill kubernetes          # Explicitly invoke a skill mid-session
```

To pre-load a skill at launch from the shell:

```bash
talkops -s kubernetes
talkops -s kubernetes -p "Audit all deployments in k8s/prod/" -q
```

<p align="center">
  <img src="./images/talkops-skills-browser.png" alt="TalkOps TUI skills browser showing loaded built-in, plugin, and project skills" width="850" />
</p>

> [!IMPORTANT]
> **Image Placeholder (`docs/talkops-docs/images/talkops-skills-browser.png`):**
> Capture a screenshot of the `/skills` viewer inside an interactive `talkops` session, showing the discovered skills list (`cloud-core`, `kubernetes`, `docker`, plus any installed plugin or project skills), their scope labels, and descriptions.

### List and search skills from the shell

```bash
# List all discovered skills
talkops skills list

# Inspect a specific skill
talkops skills info kubernetes

# Search skills by keyword
talkops skills find terraform
```

---

## See also

* [Custom commands](./custom-commands.md)
* [Subagents](./subagents.md)
* [Plugins and marketplaces](./plugins.md)
* [Configuration](./Configuration.md)
* [Command reference](./cli-reference.md)
