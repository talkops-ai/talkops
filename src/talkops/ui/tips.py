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

"""Contextual post-turn tips manager for TalkOps TUI.

Manages contextual operator tips across interactive sessions:
- Evaluates contextual eligibility predicates after a completed turn
  (context window utilization, tool count, turn duration, active goal,
  and commands already used by the operator).
- Suppresses footer tips when the assistant's response already recommended
  a slash command inline (`<slash_commands>` guidance).
- Uses a 50% probability roll (`rng.randrange(2 * len(eligible))`) and
  per-session deduplication/cooldown so tips feel helpful and non-intrusive.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Callable

_INLINE_SLASH_RECOMMENDATION_RE = re.compile(
    r"(?:`/[a-zA-Z][a-zA-Z0-9_-]*`"
    r"|\buse\s+/[a-zA-Z][a-zA-Z0-9_-]*"
    r"|\btry\s+/[a-zA-Z][a-zA-Z0-9_-]*"
    r"|\brun\s+/[a-zA-Z][a-zA-Z0-9_-]*)",
    re.IGNORECASE,
)


def has_inline_slash_recommendation(text: str | None) -> bool:
    """Return True if assistant response text already recommends a `/command`."""
    if not text:
        return False
    return bool(_INLINE_SLASH_RECOMMENDATION_RE.search(text))


@dataclass(frozen=True)
class TipContext:
    """Snapshot of session/turn state used to evaluate tip eligibility."""

    turn_count: int = 1
    context_fraction: float = 0.0
    elapsed_seconds: float = 0.0
    tool_count: int = 0
    goal_active: bool = False
    used_commands: frozenset[str] = field(default_factory=frozenset)
    assistant_recommended_slash_cmd: bool = False


@dataclass(frozen=True)
class Tip:
    """A single contextual tip candidate."""

    id: str
    text: str
    command: str | None = None
    priority: int = 1
    predicate: Callable[[TipContext], bool] | None = None

    def is_eligible(self, ctx: TipContext) -> bool:
        """Check whether this tip is eligible for the given context."""
        if self.command and self.command.lower() in ctx.used_commands:
            return False
        if self.predicate is not None:
            return bool(self.predicate(ctx))
        return True


DEFAULT_TIPS: tuple[Tip, ...] = (
    Tip(
        id="compact_high_context",
        command="/compact",
        priority=10,
        text="Context usage is growing — use /compact to summarize earlier turns and free up context space.",
        predicate=lambda ctx: ctx.context_fraction >= 0.55,
    ),
    Tip(
        id="goal_complex_turn",
        command="/goal",
        priority=8,
        text="Running a complex multi-step task? Use /goal to set acceptance criteria so TalkOps verifies completion automatically.",
        predicate=lambda ctx: (
            not ctx.goal_active and (ctx.tool_count >= 5 or ctx.elapsed_seconds >= 45.0)
        ),
    ),
    Tip(
        id="review_after_tools",
        command="/review",
        priority=6,
        text="Use /review to run a severity-classified security and IaC review on recent changes.",
        predicate=lambda ctx: ctx.tool_count >= 2,
    ),
    Tip(
        id="skill_creator_workflow",
        command="/skill-creator",
        priority=5,
        text="Completed a repeatable workflow? Use /skill-creator to package it into a reusable skill.",
        predicate=lambda ctx: ctx.turn_count >= 3 and ctx.tool_count >= 3,
    ),
    Tip(
        id="memory_learnings",
        command="/memory",
        priority=4,
        text="Use /memory to extract and save durable conventions from this session into AGENTS.md.",
        predicate=lambda ctx: ctx.turn_count >= 3 and "/remember" not in ctx.used_commands,
    ),
    Tip(
        id="btw_side_question",
        command="/btw",
        priority=2,
        text="Have a quick side question? Use /btw to ask without polluting the main conversation context.",
        predicate=lambda ctx: ctx.turn_count >= 2,
    ),
    Tip(
        id="loop_recurring",
        command="/loop",
        priority=2,
        text="Use /loop (e.g. /loop 5m check pod status) to run recurring health checks or instructions.",
        predicate=lambda ctx: ctx.turn_count >= 2,
    ),
    Tip(
        id="cloud_context",
        command="/cloud",
        priority=1,
        text="Use /cloud to switch AWS, Azure, GCP, or Kubernetes profiles and execution modes.",
    ),
    Tip(
        id="model_switch",
        command="/model",
        priority=1,
        text="Use /model to switch models or press Shift+Tab to cycle approval modes.",
    ),
)


class TipsManager:
    """Session-scoped manager for selecting non-intrusive post-turn tips."""

    def __init__(
        self,
        *,
        tips: tuple[Tip, ...] = DEFAULT_TIPS,
        rng: random.Random | None = None,
        min_turns_between_tips: int = 2,
        enabled: bool = True,
    ) -> None:
        """Initialize TipsManager."""
        self._tips = tuple(tips)
        self._rng = rng or random.Random()
        self._min_turns_between_tips = max(1, min_turns_between_tips)
        self.enabled = enabled
        self._shown_tip_ids: set[str] = set()
        self._used_commands: set[str] = set()
        self._last_tip_turn: int = -999
        self._turn_count: int = 0

    def record_command_used(self, raw_command: str) -> None:
        """Record that the user ran a slash command so we don't tip them about it."""
        if not raw_command:
            return
        cmd = raw_command.strip().split()[0].lower()
        if cmd.startswith("/"):
            self._used_commands.add(cmd)

    @property
    def used_commands(self) -> frozenset[str]:
        """Return the used commands property."""
        return frozenset(self._used_commands)

    @property
    def shown_tip_ids(self) -> frozenset[str]:
        """Return the shown tip ids property."""
        return frozenset(self._shown_tip_ids)

    def next_turn_tip(
        self,
        *,
        context_fraction: float = 0.0,
        elapsed_seconds: float = 0.0,
        tool_count: int = 0,
        goal_active: bool = False,
        last_assistant_text: str | None = None,
    ) -> str | None:
        """Advance turn counter and select at most one contextual tip."""
        self._turn_count += 1
        ctx = TipContext(
            turn_count=self._turn_count,
            context_fraction=max(0.0, float(context_fraction)),
            elapsed_seconds=max(0.0, float(elapsed_seconds)),
            tool_count=max(0, int(tool_count)),
            goal_active=bool(goal_active),
            used_commands=frozenset(self._used_commands),
            assistant_recommended_slash_cmd=has_inline_slash_recommendation(
                last_assistant_text
            ),
        )
        return self.select_tip(ctx)

    def select_tip(self, ctx: TipContext) -> str | None:
        """Select a tip for `ctx` using eligibility predicates and a 50% roll."""
        if not self.enabled or ctx.assistant_recommended_slash_cmd:
            return None

        urgent_compact = (
            ctx.context_fraction >= 0.70
            and "compact_high_context" not in self._shown_tip_ids
            and "/compact" not in ctx.used_commands
        )
        if (
            not urgent_compact
            and (ctx.turn_count - self._last_tip_turn) < self._min_turns_between_tips
        ):
            return None

        eligible = [
            tip
            for tip in self._tips
            if tip.id not in self._shown_tip_ids and tip.is_eligible(ctx)
        ]
        if not eligible:
            return None

        # 50% display probability roll across eligible tips
        roll = self._rng.randrange(2 * len(eligible))
        if roll >= len(eligible):
            return None

        # If a high-priority contextual trigger (priority >= 5) is eligible,
        # prefer the highest-priority eligible tip when the roll succeeds.
        highest_priority = max(t.priority for t in eligible)
        if highest_priority >= 5:
            high_priority_candidates = [
                t for t in eligible if t.priority == highest_priority
            ]
            chosen = high_priority_candidates[roll % len(high_priority_candidates)]
        else:
            chosen = eligible[roll]

        self._shown_tip_ids.add(chosen.id)
        self._last_tip_turn = ctx.turn_count
        return chosen.text


__all__ = [
    "DEFAULT_TIPS",
    "Tip",
    "TipContext",
    "TipsManager",
    "has_inline_slash_recommendation",
]
