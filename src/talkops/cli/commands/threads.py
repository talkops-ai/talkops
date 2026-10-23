# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Threads management CLI command."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

from talkops.output import OutputFormat, write_json
from talkops.state.session import (
    format_relative_timestamp,
    format_timestamp,
    list_threads,
)


def run_threads_command(
    output_format: OutputFormat = "text",
    *,
    server_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
) -> int:
    """Execute threads command."""
    if server_url:
        from talkops.state.remote_session import set_remote_client_context

        set_remote_client_context(server_url, api_key=api_key, user_id=user_id)

    threads = asyncio.run(list_threads(include_message_count=True, user_id=user_id))

    if output_format == "json":
        write_json("threads list", threads)
        return 0

    console = Console()
    console.print("\n[bold yellow]Saved Session Threads[/bold yellow]\n")
    if not threads:
        console.print("[dim]No saved sessions found in SQLite database.[/dim]\n")
        return 0

    table = Table(border_style="cyan", show_lines=False)
    table.add_column("Thread ID", style="bold green", no_wrap=True)
    table.add_column("Cloud / Env", style="bold magenta", no_wrap=True)
    table.add_column("Msgs", justify="right", style="cyan", no_wrap=True)
    table.add_column("Initial Prompt", style="white")
    table.add_column("Updated", style="dim", no_wrap=True)

    from talkops.state.conversation_title import get_thread_display_label

    for t in threads:
        tid = t.get("thread_id", "")
        short_id = tid[:8] if len(tid) > 8 else tid
        cloud = t.get("cloud_badge") or t.get("cloud_provider") or "-"
        msg_count = str(t.get("message_count", 0))
        prompt = get_thread_display_label(t, fallback="(No initial prompt)")
        if len(prompt) > 50:
            prompt = prompt[:47] + "..."
        updated = format_relative_timestamp(t.get("updated_at")) or format_timestamp(t.get("updated_at"))

        table.add_row(short_id, cloud, msg_count, prompt, updated)

    console.print(table)
    console.print("\n[dim]To resume a thread, run:[/dim] talkops -r <thread_id>\n")
    return 0
