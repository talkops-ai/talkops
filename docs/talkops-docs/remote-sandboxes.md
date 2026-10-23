# Use remote sandboxes

> Run TalkOps tool execution in Modal, Daytona, LangSmith, AgentCore, Runloop, Vercel, or Docker sandboxes

TalkOps (`talkops`) uses the **sandbox as tool** pattern: the `talkops` orchestration process (LLM loop, Jev router, memory, and web search) runs on your local machine, while filesystem and shell tool calls (`execute`, `read_file`, `write_file`, `edit_file`, `glob`, `grep_search`) execute inside an isolated remote container.

Running in a sandbox isolates mutating infrastructure commands from your workstation, keeps local credentials and SSH keys out of the execution environment, and provides clean, reproducible containers for CI/CD validation.

## Get started with a sandbox

### 1. Set provider credentials

Configure credentials for your chosen sandbox provider in your shell environment (or run local Docker with no cloud key):

| Provider | `--sandbox` value | Required credentials / setup |
| - | - | - |
| **Modal** | `modal` | Run `modal setup` |
| **Daytona** | `daytona` | `export DAYTONA_API_KEY="your-key"` |
| **LangSmith** | `langsmith` | `export LANGSMITH_API_KEY="lsv2_..."` |
| **AgentCore** | `agentcore` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` (or `AWS_PROFILE`) |
| **Runloop** | `runloop` | `export RUNLOOP_API_KEY="your-key"` |
| **Vercel** | `vercel` | `VERCEL_TOKEN`, `VERCEL_PROJECT_ID`, `VERCEL_TEAM_ID` (or Vercel OIDC) |
| **Docker** | `docker` | Local Docker daemon running |

### 2. Launch TalkOps with `--sandbox`

Pass `--sandbox <provider>` at launch:

```bash
# Launch an interactive session backed by a Modal container
talkops --sandbox modal

# Launch with Daytona or local Docker
talkops --sandbox daytona
talkops --sandbox docker
```

---

## Sandbox flags and examples

| Flag | Description |
| - | - |
| `--sandbox [TYPE]` | Sandbox provider to use: `modal`, `daytona`, `langsmith`, `agentcore`, `runloop`, `vercel`, or `docker` (default: `none`). Pass `--sandbox` with no value to use the configured default |
| `--sandbox-id ID` | Attach to an existing running sandbox instance instead of creating a new one (skips creation and cleanup) |
| `--sandbox-snapshot-name NAME` | Launch from or create a pre-baked container snapshot or blueprint |
| `--sandbox-setup PATH` | Path to a setup shell script executed inside the sandbox immediately after startup |

Each provider uses a default working directory inside the container where setup scripts and `execute` commands run:

| Provider | Default working directory |
| - | - |
| Modal | `/workspace` |
| Docker | `/workspace` |
| Daytona | `/home/daytona` |
| Runloop | `/home/user` |
| LangSmith | `/root` |
| AgentCore | `/tmp` |
| Vercel | `/vercel/sandbox` |

Examples:

```bash
# Create a new Modal sandbox
talkops --sandbox modal

# Attach to an existing running sandbox (skips creation and cleanup)
talkops --sandbox runloop --sandbox-id sb-prod-cluster-98234

# Launch from a snapshot and run a setup script
talkops --sandbox modal \
  --sandbox-snapshot-name terraform-k8s-base \
  --sandbox-setup ./scripts/sandbox-init.sh
```

> [!NOTE]
> Because `--sandbox` accepts an optional value, keep the bare `--sandbox` form **last** on the command line, or pass an explicit provider name so a following positional prompt is not consumed as the provider value.

---

## Workspace synchronization and tool routing

When a sandbox session starts, TalkOps synchronizes your project workspace automatically:

1. **Upload**: Copies the active repository tree into the container's working directory while skipping heavy or sensitive local directories (`.git/`, `node_modules/`, `.venv/`, `.terraform/`, `__pycache__/`, `.talkops/.state/`).
2. **Intelligent tool routing**:
   * `execute`, `read_file`, `write_file`, `edit_file`, `glob`, and `grep_search` run inside the **remote sandbox**.
   * `web_search` and `fetch_url` run on the **local host** to avoid remote proxy overhead.
3. **Sync back and cleanup**: Modified files and generated artifacts sync back to your local workspace on completion, and ephemeral containers tear down automatically when the session exits (unless `--sandbox-id` was used).

---

## Setup scripts

Use `--sandbox-setup` to install DevOps toolchains (`opentofu`, `terraform`, `kubectl`, `helm`, `awscli`) or seed environment state when the container boots:

```bash
talkops --sandbox modal --sandbox-setup ./scripts/ci-setup.sh
```

```bash
#!/usr/bin/env bash
# ./scripts/ci-setup.sh
set -euo pipefail

apt-get update && apt-get install -y opentofu kubectl helm awscli

tofu version
kubectl version --client=true
helm version
```

---

## Use sandboxes in CI/CD

Combine `--sandbox` with non-interactive mode (`-n` / `-p`) and `--rubric` for zero-trust pull request verification:

```bash
talkops -n "Run OpenTofu plan and check for security group violations" \
  --sandbox runloop \
  --sandbox-setup ./scripts/ci-setup.sh \
  --rubric "tofu plan succeeds with zero syntax errors and no 0.0.0.0/0 ingress on port 22" \
  --quiet \
  -y
```

---

## See also

* [Command reference](./cli-reference.md#model--sandbox-flags)
* [Approval modes](./approval-mode.md)
* [Goals and rubrics](./goal-and-rubrics.md)
* [Configuration](./Configuration.md)
