# Contributing to TalkOps

Thank you for your interest in contributing to TalkOps! Bug fixes, new webhook adapters, skills, subagents, plugins, MCP integrations, and documentation improvements are all welcome.

By participating, you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md). To report a security vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

---

## Development Setup

### Prerequisites

- Python 3.12+ (CI tests on 3.12 and 3.13)
- [uv](https://docs.astral.sh/uv/)
- Git

### 1. Clone and install

```bash
git clone https://github.com/talkops-ai/talkops.git
cd talkops

# Create .venv and install all extras plus dev dependencies (same as CI)
uv sync --all-extras --dev
```

### 2. Run TalkOps from source

```bash
uv run talkops doctor     # check your local environment
uv run talkops            # launch the TUI
```

### 3. Run the checks CI runs

```bash
# Unit tests (fast, deterministic, no API keys needed)
uv run pytest tests/ -m unit

# Installer shell syntax
bash -n scripts/install.sh

# Build the sdist and wheel
uv build
```

Optional, but recommended before opening a PR:

```bash
uvx ruff check src tests          # lint (ruff config lives in pyproject.toml)
uvx ruff format --check src tests # formatting
uv run pyright src/               # type checking, if pyright is installed
```

### Test markers

| Marker | What it covers | Needs API keys |
| :--- | :--- | :--- |
| `unit` | Fast deterministic tests (run in CI) | No |
| `integration` | Real LLM APIs and external services | Yes |
| `eval` | Agent behavior evaluations with LangSmith datasets | Yes |

New features and bug fixes should come with `unit` tests under `tests/unit_tests/`.

---

## Where Things Live

| Area | Location | Docs |
| :--- | :--- | :--- |
| CLI entry point and subcommands | `src/talkops/cli/` | [CLI Reference](docs/talkops-docs/cli-reference.md) |
| TUI and slash commands | `src/talkops/ui/` (`command_registry.py`) | [CLI Reference](docs/talkops-docs/cli-reference.md#in-chat-slash-commands) |
| Approval modes and safety | `src/talkops/security/`, `src/talkops/middleware/` | [Approval Modes](docs/talkops-docs/approval-mode.md) |
| Model providers | `src/talkops/model/` | [Model Providers](docs/talkops-docs/model-providers.md) |
| Central Server and addons | `src/talkops/server/`, `src/talkops/addons/` | [Central Server](docs/talkops-docs/central-server.md), [Server Addons](docs/talkops-docs/server-addons.md) |
| Webhook adapters and write-back sinks | `src/talkops/webhooks/` | [Webhooks](docs/talkops-docs/webhooks.md) |
| Built-in skills | `src/talkops/built_in_skills/` | [Memory & Skills](docs/talkops-docs/memory-and-skills.md) |
| Plugins and marketplaces | `src/talkops/plugins/` | [Plugins](docs/talkops-docs/plugins.md) |
| Remote sandboxes | `src/talkops/backend/` | [Remote Sandboxes](docs/talkops-docs/remote-sandboxes.md) |

---

## Common Contributions

### Adding a webhook adapter

1. Add an adapter in `src/talkops/webhooks/providers/` that subclasses the base adapter in `base.py`. Give it a unique `name` and any `aliases`.
2. Implement payload normalization and signature/token verification. Use the constant-time helpers in `src/talkops/webhooks/security.py`.
3. If the provider supports writing results back (ticket notes, PR comments), add a sink in `src/talkops/webhooks/sinks.py`.
4. Add unit tests with sample payloads, including a test for rejected signatures.
5. Update the provider table in [webhooks.md](docs/talkops-docs/webhooks.md) and in the README.

### Adding a built-in skill

1. Create `src/talkops/built_in_skills/<skill-name>/SKILL.md` with YAML frontmatter (`name`, `description`).
2. Put supporting files under `references/`, `scripts/`, or `assets/` as needed.
3. If the skill must ship in the wheel, make sure the CI wheel check in `.github/workflows/ci.yml` still passes.

Most skills don't need to be built in. Users can add them without code changes in `~/.talkops/skills/` or `.talkops/skills/`, or ship them in a plugin.

### Adding subagents, commands, hooks, or MCP servers

The preferred way to ship these is a **plugin**, so they can be installed from a marketplace without changing core:

```
my-plugin/
├── .talkops-plugin/
│   └── plugin.json      # Plugin manifest
├── agents/              # Subagent definitions (*.md with frontmatter)
├── skills/              # SKILL.md runbooks
├── commands/            # Custom slash commands
└── .mcp.json            # Bundled MCP servers
```

See [Plugins](docs/talkops-docs/plugins.md), [Subagents](docs/talkops-docs/subagents.md), [Custom Commands](docs/talkops-docs/custom-commands.md), [Hooks](docs/talkops-docs/hooks.md), and [MCP Tools](docs/talkops-docs/mcp-tools.md).

### Adding a model provider

Register the provider's API-key and base-URL environment variables in `src/talkops/model/config.py`, add the LangChain integration dependency to `pyproject.toml`, and document it in [model-providers.md](docs/talkops-docs/model-providers.md).

---

## Documentation Guidelines

| Location | Purpose |
| :--- | :--- |
| `README.md` | Project landing page. Keep it high-level and outcome-focused. |
| `docs/talkops-docs/` | Detailed feature guides, one file per topic. |

- **Lead with outcomes**: say what the user can do before how it works.
- **Avoid implementation jargon**: write "TalkOps checks the command for safety", not "the `JevSecurityClassifier` middleware…".
- **Verify every command**: CLI examples must match `talkops <subcommand> --help`.
- **Images**: put screenshots and GIFs in `docs/assets/`.

---

## Pull Request Guidelines

1. **Branch from `main`**: `git checkout -b feat/my-change`
2. **Use Conventional Commits**: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
3. **Keep PRs focused**: one logical change per PR.
4. **Make sure checks pass**: unit tests, `bash -n scripts/install.sh`, and `uv build`.
5. **Update docs**: any user-facing change should update the relevant file in `docs/talkops-docs/` and the README if needed.
6. **Open the PR against `main`**: describe what changed, why, and how you tested it.

---

## License

By contributing to TalkOps, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
