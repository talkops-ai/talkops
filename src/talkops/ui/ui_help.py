"""Rich-rendered help screens for TalkOps CLI commands and subcommands."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from talkops._version import __version__
from talkops.ui.theme import DC_MUTED, DC_TEAL

console = Console()

PRIMARY = DC_TEAL
MUTED = DC_MUTED
DOCS_URL = "https://talkops.ai/docs"
_HELP_OPTION_LINE = "  -h, --help    Show this help message and exit"


def show_help() -> None:
    """Show top-level help information."""
    console.print()
    console.print(f"[bold {PRIMARY}]talkops[/bold {PRIMARY}] v{__version__}")
    console.print()
    console.print(
        f"Docs: [link={DOCS_URL}]{DOCS_URL}[/link]",
        style=MUTED,
    )
    console.print()
    console.print("[bold]Usage:[/bold]", style=PRIMARY)
    console.print("  talkops [OPTIONS]                               Start interactive session")
    console.print("  talkops -p|--prompt|-n PROMPT [OPTIONS]         Run non-interactive headless prompt")
    console.print("  talkops doctor [--json]                         Run system & cloud health checks")
    console.print("  talkops auth [--json]                           Show AWS STS authentication status")
    console.print("  talkops threads <list|delete> [--json]          Manage conversation threads")
    console.print("  talkops mcp [list|status|probe] [--json]        List and probe MCP servers")
    console.print("  talkops serve [start|list] [OPTIONS]            Start central webhook server")
    console.print("  talkops plugin <list|search|install|uninstall|enable|disable|refresh|marketplace>")
    console.print("  talkops skills <list|create|info|delete|trust>  Manage agent skills")
    console.print("  talkops help [SUBCOMMAND]                       Show help information")
    console.print()

    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  -p, --prompt, -n, --non-interactive MSG  Run non-interactively (headless) and exit")
    console.print("  -m, --message, --initial-prompt TEXT     Initial prompt to auto-submit on start")
    console.print("  -s, --skill NAME                         Invoke a skill when session starts")
    console.print("  --startup-cmd CMD                        Shell command executed at startup")
    console.print("  -r, --resume, --resume-thread ID         Resume an existing session thread ID")
    console.print("  -M, --model MODEL                        Model specification to use")
    console.print("  --model-params JSON                      Extra kwargs to pass to model as JSON")
    console.print("  --profile-override JSON                  Override model profile fields as JSON")
    console.print("  --default-model [MODEL]                  Set or show the default model")
    console.print("  --clear-default-model                    Clear the configured default model")
    console.print("  --approval-mode {manual,smart,auto,yolo} Tool approval mode")
    console.print("  -y, --auto-approve                       Enable classifier-backed Auto mode")
    console.print("  --smart                                  Enable Jev-powered Smart approval mode")
    console.print("  --yolo                                   Enable unrestricted YOLO mode (headless/serve)")
    console.print("  --read-only, --dry-run                   Enforce read-only cloud safety mode")
    console.print("  --aws-profile PROFILE                    AWS profile override for this session")
    console.print("  --aws-region REGION                      AWS region override for this session")
    console.print("  -S, --shell-allow-list CMDS              Comma-separated cmds, 'recommended', or 'all'")
    console.print("  --mcp-config PATH                        Load MCP tools from config JSON file")
    console.print("  --no-mcp                                 Disable all MCP tool loading")
    console.print("  --trust-project-mcp                      Trust project MCP configs without prompt")
    console.print("  -q, --quiet                              Clean output for piping (requires headless)")
    console.print("  --no-stream                              Buffer full response instead of streaming")
    console.print("  --json, --output-format {text,json}      Emit machine-readable JSON output")
    console.print("  --max-turns N                            Max agent turns before stopping (headless)")
    console.print("  --timeout SECONDS                        Hard wall-clock timeout in seconds (headless)")
    console.print("  --recursion-limit N                      Override graph step budget")
    console.print("  --stdin                                  Read input from stdin explicitly")
    console.print("  --cwd PATH                               Working directory path for this execution")
    console.print("  --goal TEXT                              Initial goal objective (interactive TUI)")
    console.print("  --rubric TEXT|@PATH                      Acceptance criteria to grade against (headless)")
    console.print("  --rubric-model MODEL                     Model specification for the rubric grader")
    console.print("  --rubric-max-iterations N                Maximum evaluation iterations for rubric grader")
    console.print("  -V, --verbose                            Enable verbose DEBUG logging")
    console.print("  --log-level LEVEL                        Set logging level (debug, info, warning, error, critical)")
    console.print("  --log-dir PATH                           Directory for log files")
    console.print("  --log-file PATH                          Log file path or filename")
    console.print("  -v, --version                            Show talkops version")
    console.print("  -h, --help                               Show this help message and exit")
    console.print()

    console.print("[bold]Non-Interactive Mode Examples:[/bold]", style=PRIMARY)
    console.print("  talkops -p 'Plan terraform deployment'           # Headless task execution", style=MUTED)
    console.print("  talkops -p 'List files' -S recommended           # Safe shell commands", style=MUTED)
    console.print("  talkops -p 'Search logs' -S ls,cat,grep -q       # Quiet piping mode", style=MUTED)
    console.print("  cat prompt.txt | talkops --stdin -q              # Read from stdin", style=MUTED)
    console.print()


def show_agents_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops agents <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls    List all available agents")
    console.print("  reset       Reset an agent's prompt to default")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_list_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops agents list [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("List all configured and built-in agents.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_reset_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops agents reset --agent NAME [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Reset an agent's prompt to default or copy from another agent.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME     Name of agent to reset (required)")
    console.print("  --target NAME    Copy prompt from another agent")
    console.print("  --dry-run        Show changes without applying")
    console.print("  --json           Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls                 List all discovered skills")
    console.print("  create NAME [OPTIONS]    Create a new skill with template SKILL.md")
    console.print("  info NAME [OPTIONS]      Show details and content for a specific skill")
    console.print("  delete NAME [OPTIONS]    Delete a custom skill directory")
    console.print("  trust <list|revoke|clear> Manage trusted skill directories")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_list_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills list [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("List skills discovered from user, project, plugin, and built-in locations.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME  Agent profile identifier (default: talkops)")
    console.print("  --project     Show only project-level skills")
    console.print("  --json        Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_create_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills create NAME [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Create a new skill directory with a template SKILL.md file conforming to Agent Skills spec.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME  Agent profile identifier (default: talkops)")
    console.print("  --project     Create skill in project directory instead of user directory")
    console.print("  --json        Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_info_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills info NAME [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Show description, metadata, supporting files, and raw SKILL.md instructions for a skill.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME  Agent profile identifier (default: talkops)")
    console.print("  --project     Search only in project skills")
    console.print("  --json        Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_delete_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills delete NAME [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Delete a custom skill directory and all its supporting files.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME     Agent profile identifier (default: talkops)")
    console.print("  --project        Search only in project skills")
    console.print("  -f, --force      Skip confirmation prompt")
    console.print("  --dry-run        Show what would be removed without making changes")
    console.print("  --json           Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_skills_trust_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops skills trust <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Manage directories trusted to be read outside standard skill roots.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls         List all trusted directories with timestamps")
    console.print("  revoke <dir>     Revoke trust for a directory")
    console.print("  clear            Remove all trusted directory entries")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json           Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_plugins_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops plugin <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls        List installed and available plugins")
    console.print("  install ID      Install a plugin from marketplace or source")
    console.print("  uninstall ID    Uninstall a plugin")
    console.print("  enable ID       Enable an installed plugin")
    console.print("  disable ID      Disable an installed plugin")
    console.print("  marketplace     Manage plugin marketplace sources")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json          Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_config_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops config [get <key>|set <key> <value>|path] [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Inspect or update TalkOps configuration values.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  get <key|section>  Show effective value and source for an option or section")
    console.print("  set <key> <value>  Update a configuration setting in config.toml")
    console.print("  path               Print configuration file locations")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  -v, --verbose, --all Show full descriptions and sources")
    console.print("  --json               Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_auth_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops auth <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Manage stored LLM provider credentials.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls           List providers and authentication status")
    console.print("  set <provider>     Store API key for a provider (read from stdin or --from-env)")
    console.print("  remove <provider>  Remove stored credentials (aliases: rm, delete)")
    console.print("  status <provider>  Show credential resolution source for a provider")
    console.print("  path               Print path to auth.json credential file")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --from-env VAR     Copy key from an existing environment variable")
    console.print("  --json             Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_threads_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops threads <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Manage conversation threads and checkpoints.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list, ls          List recent conversation threads")
    console.print("  delete <thread_id> Delete a thread and its history")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json            Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_threads_list_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops threads list [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --agent NAME      Filter by agent name")
    console.print("  -n, --limit N     Max threads to display (default: 20)")
    console.print("  --sort {created,updated} Sort order")
    console.print("  --branch NAME     Filter by git branch")
    console.print("  --cwd [PATH]      Filter by working directory")
    console.print("  -v, --verbose     Show detailed columns (branch, prompt)")
    console.print("  -r, --relative    Show relative timestamps (e.g., 5m ago)")
    console.print("  --json            Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_threads_delete_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops threads delete <thread_id> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --dry-run   Show what would be deleted without making changes")
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_doctor_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops doctor [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Print install health, dependencies, and environment diagnostics.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_tools_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops tools <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Manage external tools available to the agent.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  list        List available built-in and MCP tools")
    console.print("  install     Install or verify managed tools (e.g. ripgrep)")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_tools_list_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops tools list [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("List all tools exposed to the agent model.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_tools_install_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops tools install [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Install or verify external dependencies (ripgrep).", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_mcp_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops mcp <command> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Manage Model Context Protocol (MCP) servers and tools.", style=MUTED)
    console.print()
    console.print("[bold]Commands:[/bold]", style=PRIMARY)
    console.print("  config      Show MCP configuration discovery paths and servers")
    console.print("  login NAME  Run OAuth login flow for a remote MCP server")
    console.print("  list        List configured MCP servers and tool count")
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_mcp_config_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops mcp config [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Show MCP configuration file resolution paths and loaded servers.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --json      Emit machine-readable JSON output")
    console.print(_HELP_OPTION_LINE)
    console.print()


def show_mcp_login_help() -> None:
    console.print()
    console.print("[bold]Usage:[/bold] talkops mcp login <server> [OPTIONS]", style=PRIMARY)
    console.print()
    console.print("Initiate OAuth authentication with a remote MCP server.", style=MUTED)
    console.print()
    console.print("[bold]Options:[/bold]", style=PRIMARY)
    console.print("  --mcp-config PATH Explicit path to MCP config file")
    console.print(_HELP_OPTION_LINE)
    console.print()
