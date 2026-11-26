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

"""Streamlined CLI ``argparse`` formatter and parser for TalkOps.

Eliminates standard ``argparse`` help redundancies across ``talkops --help``
and all nested subcommands:
1. Replaces verbose multi-line flag/choice dumps in ``usage:`` with concise
   ``usage: talkops [OPTIONS] [COMMAND [ARGS]...]`` syntax when a parser has
   subcommands or more than 4 optional flags.
2. Suppresses the redundant unformatted ``{cmd1,cmd2,alias1,...}`` header line
   printed above the formatted subcommand table.
3. Prints option metavars once after comma-separated flag aliases
   (e.g. ``-p, --prompt, -n, --non-interactive PROMPT``).
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Iterable
from typing import Any


class TalkOpsHelpFormatter(argparse.HelpFormatter):
    """Clean, non-redundant help formatter for TalkOps CLI parsers."""

    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 36,
        width: int | None = None,
    ) -> None:
        """Initialize TalkOpsHelpFormatter."""
        resolved_width = width
        if resolved_width is None:
            resolved_width = min(shutil.get_terminal_size((100, 24)).columns, 110)
        super().__init__(
            prog,
            indent_increment=indent_increment,
            max_help_position=max_help_position,
            width=resolved_width,
        )

    def _format_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None,
    ) -> str:
        if usage is not None:
            return super()._format_usage(usage, actions, groups, prefix)

        action_list = list(actions)
        optionals = [
            a for a in action_list if a.option_strings and a.help is not argparse.SUPPRESS
        ]
        positionals = [
            a for a in action_list if not a.option_strings and a.help is not argparse.SUPPRESS
        ]
        has_subparsers = any(isinstance(a, argparse._SubParsersAction) for a in positionals)

        if not has_subparsers and len(optionals) <= 4:
            return super()._format_usage(usage, action_list, groups, prefix)

        parts: list[str] = [self._prog]
        if optionals:
            parts.append("[OPTIONS]")

        for pos in positionals:
            if isinstance(pos, argparse._SubParsersAction):
                raw_mv = pos.metavar if isinstance(pos.metavar, str) else ""
                clean_mv = raw_mv.strip().strip("[]").strip()
                cmd_label = clean_mv.upper() if clean_mv else "COMMAND"
                if getattr(pos, "required", False):
                    parts.append(f"{cmd_label} [ARGS]...")
                else:
                    parts.append(f"[{cmd_label} [ARGS]...]")
            else:
                raw_mv = pos.metavar if isinstance(pos.metavar, str) else pos.dest
                label = str(raw_mv).strip().strip("[]").upper() or "ARG"
                if pos.nargs == "?":
                    parts.append(f"[{label}]")
                elif pos.nargs == "*":
                    parts.append(f"[{label}...]")
                elif pos.nargs == "+":
                    parts.append(f"{label} [{label}...]")
                else:
                    parts.append(label)

        resolved_prefix = "usage: " if prefix is None else prefix
        return f"{resolved_prefix}{' '.join(parts)}\n\n"

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            parts: list[str] = []
            for sub in action._get_subactions():
                parts.append(argparse.HelpFormatter._format_action(self, sub))
            return "".join(parts)
        return argparse.HelpFormatter._format_action(self, action)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            default = self._get_default_metavar_for_positional(action)
            (metavar,) = self._metavar_formatter(action, default)(1)
            return metavar
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return f"{', '.join(action.option_strings)} {args_string}"


class TalkOpsArgumentParser(argparse.ArgumentParser):
    """``ArgumentParser`` that propagates ``TalkOpsHelpFormatter`` to all subcommands."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize TalkOpsArgumentParser."""
        kwargs.setdefault("formatter_class", TalkOpsHelpFormatter)
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs: Any) -> argparse._SubParsersAction[Any]:
        """Execute `add_subparsers`."""
        kwargs.setdefault("parser_class", TalkOpsArgumentParser)
        kwargs.setdefault("title", "commands")
        action = super().add_subparsers(**kwargs)
        if len(self._action_groups) >= 2:
            cmd_group = self._action_groups[-1]
            if cmd_group.title == kwargs.get("title") and cmd_group is not self._optionals:
                self._action_groups.remove(cmd_group)
                insert_idx = (
                    1
                    if self._action_groups and self._action_groups[0] is self._positionals
                    else 0
                )
                self._action_groups.insert(insert_idx, cmd_group)
        return action


__all__ = ["TalkOpsArgumentParser", "TalkOpsHelpFormatter"]
