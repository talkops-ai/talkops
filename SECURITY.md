# Security Policy

TalkOps runs shell commands, edits files, calls cloud and Kubernetes APIs, and accepts inbound webhooks for you. We treat security as a first-class concern, and we appreciate responsible disclosure from the community.

---

## Supported Versions

TalkOps is in active pre-release development. Security fixes go into the latest release line only.

| Version | Supported |
| :--- | :--- |
| `0.3.x` (latest beta) | ✅ |
| `< 0.3.0` | ❌ |

Always upgrade to the newest release before reporting an issue: `uv tool upgrade --prerelease=allow talkops-ai` or `pip install --pre -U talkops-ai`.

---

## Reporting a Vulnerability

**Please do not open public GitHub issues, discussions, or pull requests for security vulnerabilities.**

Report vulnerabilities privately through **GitHub Private Vulnerability Reporting**:

1. Go to the repository's **[Security → Advisories](https://github.com/talkops-ai/talkops/security/advisories/new)** page.
2. Click **Report a vulnerability**.
3. Include as much of the following as you can:
   - A description of the vulnerability and the affected component (CLI/TUI, `talkops serve`, webhooks, plugins, MCP, skills, sandboxes, auth).
   - Steps to reproduce or a proof of concept.
   - The TalkOps version (`talkops --version`), OS, Python version, and approval mode (`manual`, `smart`, `auto`, `yolo`).
   - The potential impact (for example: approval-gate bypass, credential exposure, prompt-injection-driven command execution, webhook signature bypass, cross-user thread access).

### What to expect

- We will acknowledge your report and keep you updated while we investigate and fix it.
- Once a fix is released, we will publish a GitHub Security Advisory and credit you, unless you prefer to remain anonymous.
- Please give us reasonable time to remediate before any public disclosure.

---

## Scope

**In scope:**

- Bypassing approval gates (Manual, Smart, or Auto mode) or `--read-only` enforcement.
- Leaking credentials stored by `talkops auth` or exposed through logs or tool output.
- Authentication or tenant-isolation flaws in `talkops serve` (JWT/API-key exchange, per-user thread scoping).
- Webhook signature or token verification bypasses, and replay or deduplication weaknesses.
- Server-side request forgery (SSRF) via URL-fetching tools.
- Code execution through untrusted project MCP servers, skills, or plugins without the expected trust prompt.

**Out of scope:**

- Actions you explicitly approved, or actions run in `--yolo` mode (which disables approval gates by design).
- Vulnerabilities in third-party LLM providers, MCP servers, plugins, or sandbox providers that TalkOps does not maintain. Please report those upstream.
- Prompt-injection outcomes that were blocked or surfaced for human approval as designed.

---

## How TalkOps Protects Your Environment

### Approval modes

Every tool call (shell command, file write, MCP tool, or subagent dispatch) passes through an approval gate. Use `Shift+Tab` in the TUI or `--approval-mode` on the CLI to choose a mode:

- **Manual**: you approve every action.
- **Smart** (`--smart`): the TypeSafe AI Jev classifier scores each action for mutation likelihood, blast radius, and intent alignment. Smart Mode **fails closed**: it always asks for human approval when an action targets a production environment, when classifier confidence is low, or when `TYPESAFE_API_KEY` is not configured.
- **Auto** (`-y`): an LLM-based classifier auto-approves safe read-only actions.
- **YOLO** (`--yolo`): no gating. Only available for headless runs (`-p`) or `talkops serve`; use it only inside disposable sandboxes.

Add `--read-only` to block all mutating cloud, Kubernetes, and Terraform operations, regardless of approval mode.

See [Approval Modes & Jev Smart Mode](docs/talkops-docs/approval-mode.md).

### Shell command analysis

Shell commands are parsed into a syntax tree before execution. Destructive or infrastructure-mutating verbs (for example `rm -rf`, `kubectl delete`, `terraform destroy`) are flagged for approval. Use `-S/--shell-allow-list` to restrict which commands may run at all.

### Prompt-injection and Unicode defenses

Prompts, file contents, and tool inputs are scanned for BiDi override characters ("Trojan Source"), zero-width characters, and other invisible formatting controls that can disguise what a command really does.

### SSRF protection

URL-fetching tools only allow `http`/`https` and resolve DNS before connecting. They block private, loopback, link-local, reserved, and other non-global addresses, including cloud metadata endpoints.

### Credential handling

- `talkops auth` stores provider API keys in the OS keychain, with a `0600`-permission file fallback on headless hosts.
- System-critical environment variables (`PATH`, `LD_PRELOAD`, etc.) cannot be overridden from `.env` files.

### Central Server and webhooks

- `talkops serve` requires token authentication. Long-lived API keys are exchanged for short-lived signed JWTs, and each thread is scoped to the signed-in user.
- Inbound webhooks are verified with provider HMAC signatures (PagerDuty, GitHub) or shared tokens, compared in constant time. Duplicate deliveries are deduplicated.

See [Central Server](docs/talkops-docs/central-server.md) and [Webhooks](docs/talkops-docs/webhooks.md).

### Project trust

Project-level MCP servers and skills can be committed to a repository by anyone, so TalkOps asks for confirmation before using them. Manage trust with `talkops skills trust` and `--trust-project-mcp`.

### Remote sandboxes

For maximum isolation, run commands inside a [remote sandbox](docs/talkops-docs/remote-sandboxes.md) (`--sandbox`) instead of on your workstation.

---

## Operator Best Practices

- Treat webhook payloads, logs, tickets, and third-party repository files as **untrusted input**. They enter the agent's context and may contain prompt-injection attempts.
- Always configure webhook secrets on `talkops serve`, and expose it only behind TLS.
- Give TalkOps the narrowest IAM, RBAC, and cloud permissions it needs. Use read-only profiles (for example `--aws-profile prod-ro` with `--read-only`) for investigations.
- Test custom skills, plugins, hooks, and MCP servers in a non-production environment or sandbox before enabling unattended execution.
