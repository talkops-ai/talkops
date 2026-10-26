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

"""Goal acceptance-criteria review widget."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypedDict

from textual import on
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Markdown, Static

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

import uuid

from talkops.config.settings import get_glyphs
from talkops.rubrics.generator import extract_clarifying_questions
from talkops.ui.widgets._inline_prompt import (
    InlinePromptCompletion,
    InlinePromptOption,
    InlinePromptTextArea,
    apply_inline_prompt_border,
    newline_hint,
    stop_inline_prompt_blur,
)
from talkops.ui.widgets.ask_user import AskUserTextArea, _QuestionWidget
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.ui.widgets._ask_user_types import Question

logger = get_logger(__name__)

_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1. Approve plan & start execution (y)", "accept"),
    ("2. Edit plan / criteria (e)", "edit"),
    ("3. Refine plan with custom instructions (r)", "reject_with_message"),
    ("4. Cancel (n)", "cancel"),
)

_OPTIONS_WITH_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("1. Answer clarifying questions & refine plan (q)", "answer_questions"),
    ("2. Approve plan & start execution as-is (y)", "accept"),
    ("3. Edit plan / criteria (e)", "edit"),
    ("4. Refine plan with custom instructions (r)", "reject_with_message"),
    ("5. Cancel (n)", "cancel"),
)


class GoalReviewAccepted(TypedDict):
    """Widget result when the generated criteria are accepted unchanged."""

    type: Literal["accepted"]


class GoalReviewEdited(TypedDict, total=False):
    """Widget result when the user submits revised criteria or plan."""

    type: Literal["edited"]
    criteria: str
    plan: str


class GoalReviewRejected(TypedDict):
    """Widget result when the user rejects criteria with feedback."""

    type: Literal["rejected"]
    message: str


class GoalReviewCancelled(TypedDict):
    """Widget result when the user cancels the proposal."""

    type: Literal["cancelled"]


GoalReviewResult = (
    GoalReviewAccepted | GoalReviewEdited | GoalReviewRejected | GoalReviewCancelled
)


class GoalReviewTextArea(InlinePromptTextArea):
    """Text input that keeps goal-review edit keystrokes inside the editor."""

    class Submitted(InlinePromptTextArea.Submitted):
        """Posted when the user presses Enter to submit goal-review text."""

    class CancelEdit(Message):
        """Posted when Escape should leave goal criteria edit mode."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize GoalReviewTextArea."""
        super().__init__(**kwargs)
        self._escape_pending_time: float | None = None
        self._escape_timer: Any = None

    async def _on_key(self, event: events.Key) -> None:
        now = time.monotonic()

        # Terminal-emitted Option+Return (Alt+Enter) sends Escape followed rapidly by Enter.
        if event.key == "enter" and self._escape_pending_time is not None:
            gap = now - self._escape_pending_time
            if gap <= 0.08:
                logger.debug("Option+Return detected (gap=%.3fs); inserting newline", gap)
                self._escape_pending_time = None
                if self._escape_timer is not None:
                    try:
                        self._escape_timer.stop()
                    except Exception:
                        pass
                    self._escape_timer = None
                self.action_insert_newline()
                event.prevent_default()
                event.stop()
                return
            self._escape_pending_time = None

        if event.key == "escape":
            logger.debug("Escape key received in GoalReviewTextArea; buffering 50ms for Option+Return check")
            event.prevent_default()
            event.stop()
            self._escape_pending_time = now
            if self._escape_timer is not None:
                try:
                    self._escape_timer.stop()
                except Exception:
                    pass
            self._escape_timer = self.set_timer(0.05, self._flush_pending_escape)
            return

        await super()._on_key(event)

    def _flush_pending_escape(self) -> None:
        if self._escape_pending_time is not None:
            logger.debug("Flushing standalone Escape in GoalReviewTextArea; posting CancelEdit")
            self._escape_pending_time = None
            self._escape_timer = None
            self.post_message(self.CancelEdit())


class GoalReviewMenu(Container):
    """Inline review widget for generated goal implementation plan and acceptance criteria."""

    can_focus = True
    can_focus_children = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("q", "answer_questions", "Answer questions", show=False),
        Binding("tab", "next_question", "Next question", show=False),
        Binding("1", "key_1", "Option 1", show=False),
        Binding("y", "accept", "Accept", show=False),
        Binding("2", "key_2", "Option 2", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("3", "key_3", "Option 3", show=False),
        Binding("r", "reject_with_message", "Reject with message", show=False),
        Binding("4", "key_4", "Option 4", show=False),
        Binding("5", "key_5", "Option 5", show=False),
        Binding("n", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    GoalReviewMenu {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        background: $surface;
        border: solid $success;
    }
    GoalReviewMenu .goal-review-title {
        text-style: bold;
        color: $success;
        margin-bottom: 0;
    }
    GoalReviewMenu .goal-review-content {
        height: auto;
        max-height: 24;
        padding: 0 1;
        overflow-y: auto;
    }
    GoalReviewMenu .goal-review-body {
        height: auto;
    }
    GoalReviewMenu .goal-review-markdown {
        margin: 0 0 1 0;
        padding: 0;
    }
    GoalReviewMenu .goal-review-questions-section {
        height: auto;
        margin: 0 1 1 1;
        padding: 0 1;
        background: $background;
        border: solid $primary;
    }
    GoalReviewMenu .goal-review-questions-header {
        text-style: bold;
        color: $primary;
        margin: 0 0 1 0;
    }
    GoalReviewMenu .goal-review-questions {
        height: auto;
        padding: 0;
    }
    GoalReviewMenu .ask-user-question {
        height: auto;
        margin-bottom: 1;
    }
    GoalReviewMenu .ask-user-question-active {
        border-left: thick $primary;
        padding-left: 1;
    }
    GoalReviewMenu .ask-user-question-inactive {
        opacity: 0.75;
        padding-left: 2;
    }
    GoalReviewMenu .ask-user-question-text {
        margin: 0 0 0 1;
        padding: 0;
        color: $foreground;
    }
    GoalReviewMenu .ask-user-choice {
        height: auto;
        padding: 0 1;
        color: $foreground;
    }
    GoalReviewMenu .ask-user-choice-selected {
        color: $primary;
        text-style: bold;
    }
    GoalReviewMenu .ask-user-text-input,
    GoalReviewMenu .ask-user-other-input {
        margin: 1 0 0 2;
        height: auto;
        min-height: 3;
        max-height: 8;
        background: $surface;
        border: solid $primary;
        padding: 0 1;
        color: $foreground;
    }
    GoalReviewMenu .ask-user-text-input:focus,
    GoalReviewMenu .ask-user-other-input:focus {
        border: heavy $accent;
    }
    GoalReviewMenu .goal-review-options-container {
        height: auto;
        background: $surface;
        padding: 0 1;
        margin-top: 0;
    }
    GoalReviewMenu .goal-review-option {
        height: 1;
        padding: 0 1;
    }
    GoalReviewMenu .goal-review-option-selected {
        background: $primary;
        text-style: bold;
    }
    GoalReviewMenu .goal-review-input-label {
        margin: 1 1 0 1;
        padding: 0 1;
        text-style: bold;
        color: $primary;
    }
    GoalReviewMenu .goal-review-edit-input {
        margin: 0 1 1 1;
        height: auto;
        min-height: 4;
        max-height: 12;
        background: $background;
        border: solid $primary;
        padding: 0 1;
        color: $foreground;
    }
    GoalReviewMenu .goal-review-edit-input:focus {
        border: heavy $accent;
        background: $background;
    }
    GoalReviewMenu .goal-review-help {
        color: $text-muted;
        text-style: italic;
        margin-top: 0;
        margin-bottom: 0;
    }
    """

    class Decided(Message):
        """Message sent when the user accepts, edits, or cancels."""

        def __init__(
            self,
            result: GoalReviewResult,
            widget: GoalReviewMenu | None = None,
        ) -> None:
            """Initialize Decided."""
            super().__init__()
            self.result = result
            self.widget = widget

    def __init__(
        self,
        objective: str,
        criteria: str,
        *,
        plan: str | None = None,
        todos: list[str] | None = None,
        questions: list[dict[str, Any]] | None = None,
        amendment: bool = False,
        id: str | None = None,
    ) -> None:
        """Initialize GoalReviewMenu."""
        menu_id = id if (id and id != "goal-review-menu") else f"goal-review-menu-{uuid.uuid4().hex[:8]}"
        super().__init__(
            id=menu_id,
            classes="inline-prompt goal-review-menu",
        )
        self._objective = objective
        self._criteria = criteria
        raw_plan = plan.strip() if isinstance(plan, str) and plan.strip() else None
        cleaned_plan, extracted_questions = extract_clarifying_questions(raw_plan, questions)
        self._plan = cleaned_plan or raw_plan
        self._todos = [str(t).strip() for t in todos if str(t).strip()] if todos else None
        self._questions: list[Any] = list(extracted_questions)
        self._amendment = amendment
        self._options: tuple[tuple[str, str], ...] = (
            _OPTIONS_WITH_QUESTIONS if self._questions else _OPTIONS
        )
        self._selected = 0
        self._option_widgets: list[InlinePromptOption] = []
        self._question_widgets: list[_QuestionWidget] = []
        self._answers: list[str] = [""] * len(self._questions)
        self._confirmed: list[bool] = [False] * len(self._questions)
        self._current_question: int = 0
        self._help_widget: Static | None = None
        self._input_label: Static = Static("", classes="goal-review-input-label")
        self._input_label.display = False
        self._edit_input: GoalReviewTextArea = GoalReviewTextArea(classes="goal-review-edit-input")
        self._edit_input.text = self._criteria
        self._edit_input.display = False
        self._input_mode: Literal["edit", "reject", "questions"] | None = None
        self._last_input_mode_exit_time: float = 0.0
        self._completion: InlinePromptCompletion[GoalReviewResult] = (
            InlinePromptCompletion()
        )

    def set_future(self, future: asyncio.Future[GoalReviewResult]) -> None:
        """Set the future to resolve when the user decides."""
        self._completion.set_future(future)

    def _build_review_markdown(self) -> str:
        """Build the unified Markdown document showing Plan, Todos, and Verification Criteria."""
        sections: list[str] = []
        if self._amendment:
            sections.append(f"**Proposed objective**\n\n{self._objective}")

        if self._plan:
            sections.append(f"**Implementation Plan**\n\n{self._plan}")

        if self._todos:
            todo_lines = "\n".join(f"{idx + 1}. `[pending]` {step}" for idx, step in enumerate(self._todos))
            sections.append(f"**Tactical Execution Checklist (`write_todos`)**\n\n{todo_lines}")

        sections.append(f"**Proposed criteria (Jev Verification Rubric)**\n\n{self._criteria}")
        return "\n\n---\n\n".join(sections)

    def compose(self) -> ComposeResult:
        """Compose the review widget."""
        glyphs = get_glyphs()
        title = (
            "Review implementation plan amendment"
            if self._amendment
            else "Review implementation plan & rubric"
        )
        yield Static(
            Content.from_markup("$cursor $title", cursor=glyphs.cursor, title=title),
            classes="inline-prompt-title goal-review-title",
        )
        with (
            VerticalScroll(classes="goal-review-content"),
            Vertical(classes="goal-review-body"),
        ):
            source = self._build_review_markdown()
            yield Markdown(source, classes="goal-review-markdown")

        if self._questions:
            count = len(self._questions)
            q_label = (
                "Clarifying Question — choose an option below (or select Other to type):"
                if count == 1
                else f"Clarifying Questions ({count}) — choose options below (or select Other to type):"
            )
            with Vertical(classes="goal-review-questions-section"):
                yield Static(
                    f"{glyphs.cursor} {q_label}",
                    classes="goal-review-questions-header",
                )
                with Vertical(classes="ask-user-questions goal-review-questions"):
                    for i, q in enumerate(self._questions):
                        qw = _QuestionWidget(q, index=i)
                        self._question_widgets.append(qw)
                        yield qw

        with Container(classes="goal-review-options-container"):
            for i, (label, _) in enumerate(self._options):
                widget = InlinePromptOption(
                    label,
                    i,
                    selected=i == self._selected,
                    selected_class="goal-review-option-selected",
                    classes="goal-review-option",
                )
                self._option_widgets.append(widget)
                yield widget
        yield self._input_label
        yield self._edit_input
        self._help_widget = Static(
            "",
            classes="inline-prompt-help goal-review-help",
        )
        yield self._help_widget

    async def on_mount(self) -> None:
        """Focus the menu and render options after mount."""
        apply_inline_prompt_border(self)
        if self._question_widgets:
            self._highlight_question(0)
        self._update_options()
        self.focus()

    def focus_active(self) -> None:
        """Focus the active control."""
        if not self.is_mounted:
            return
        if self._input_mode == "questions" and self._question_widgets:
            self._set_active_question(self._current_question)
            return
        if self._input_mode is not None and self._edit_input is not None:
            self._edit_input.focus()
            return
        self.focus()

    def action_move_up(self) -> None:
        """Move selection up."""
        if self._input_mode is not None:
            return
        self._selected = (self._selected - 1) % len(self._options)
        self._update_options()

    def action_move_down(self) -> None:
        """Move selection down."""
        if self._input_mode is not None:
            return
        self._selected = (self._selected + 1) % len(self._options)
        self._update_options()

    def action_select(self) -> None:
        """Select the highlighted option."""
        if self._input_mode is not None:
            return
        gap = time.monotonic() - self._last_input_mode_exit_time
        if gap < 0.150:
            logger.debug("action_select suppressed due to exit cool-off (gap=%.3fs)", gap)
            return
        action_name = self._options[self._selected][1]
        logger.debug("GoalReviewMenu select triggered: option %d -> %s", self._selected, action_name)
        getattr(self, f"action_{action_name}")()

    def _trigger_option_by_number(self, number: int) -> None:
        if self._input_mode is not None:
            return
        idx = number - 1
        if 0 <= idx < len(self._options):
            self._selected = idx
            self._update_options()
            action_name = self._options[idx][1]
            getattr(self, f"action_{action_name}")()

    def action_key_1(self) -> None:
        """Execute the key 1 action."""
        self._trigger_option_by_number(1)

    def action_key_2(self) -> None:
        """Execute the key 2 action."""
        self._trigger_option_by_number(2)

    def action_key_3(self) -> None:
        """Execute the key 3 action."""
        self._trigger_option_by_number(3)

    def action_key_4(self) -> None:
        """Execute the key 4 action."""
        self._trigger_option_by_number(4)

    def action_key_5(self) -> None:
        """Execute the key 5 action."""
        self._trigger_option_by_number(5)

    def action_answer_questions(self) -> None:
        """Focus the interactive clarifying questions widget to select or type answers."""
        if self._completion.resolved or self._input_mode is not None:
            return
        if not self._question_widgets:
            self.action_reject_with_message()
            return
        logger.debug("GoalReviewMenu entering interactive clarifying questions mode")
        self._input_mode = "questions"
        self._confirmed = [False] * len(self._question_widgets)
        self._update_options()
        if self.is_mounted:
            self._set_active_question(0)

    def action_next_question(self) -> None:
        """Advance to the next clarifying question when in questions mode."""
        if self._input_mode != "questions" or not self._question_widgets:
            return
        next_idx = (self._current_question + 1) % len(self._question_widgets)
        self._set_active_question(next_idx)

    def _set_active_question(self, index: int) -> None:
        if not self._question_widgets:
            return
        index = max(0, min(index, len(self._question_widgets) - 1))
        self._highlight_question(index)
        qw = self._question_widgets[index]
        qw.focus_input()
        try:
            qw.scroll_visible()
        except Exception:
            pass

    def _highlight_question(self, index: int) -> None:
        self._current_question = index
        for i, qw in enumerate(self._question_widgets):
            if i == index:
                qw.add_class("ask-user-question-active")
                qw.remove_class("ask-user-question-inactive")
            else:
                qw.remove_class("ask-user-question-active")
                qw.add_class("ask-user-question-inactive")

    def on_ask_user_text_area_submitted(self, event: AskUserTextArea.Submitted) -> None:
        """Confirm the clarifying question whose free-form/Other text area was submitted."""
        event.stop()
        for qw in self._question_widgets:
            if (qw._text_input and qw._text_input is event.text_area) or (
                qw._other_input and qw._other_input is event.text_area
            ):
                answer = qw.get_answer()
                if answer.strip() or not qw._required:
                    self._input_mode = "questions"
                    self.confirm_and_advance(qw._index)
                return

    def confirm_and_advance(self, index: int) -> None:
        """Record the answer for question `index` and advance or submit Q&A refinement."""
        if not self._question_widgets or index >= len(self._question_widgets):
            return
        self._input_mode = "questions"
        self._answers[index] = self._question_widgets[index].get_answer()
        self._confirmed[index] = True

        for i in range(index + 1, len(self._question_widgets)):
            if not self._confirmed[i]:
                self._set_active_question(i)
                self._update_options()
                return

        for i, qw in enumerate(self._question_widgets):
            self._answers[i] = qw.get_answer()

        if all(
            a.strip() or not self._question_widgets[i]._required
            for i, a in enumerate(self._answers)
        ):
            self._submit_question_answers()
            return

        for i, a in enumerate(self._answers):
            if not a.strip() and self._question_widgets[i]._required:
                self._confirmed[i] = False
                self._set_active_question(i)
                self._update_options()
                return

    def _format_question_answers(self) -> str:
        """Format answered clarifying questions into structured feedback for plan refinement."""
        lines: list[str] = ["User answers to clarifying questions:"]
        for i, q in enumerate(self._questions):
            q_text = str(q.get("question") or "").strip()
            ans = (self._answers[i] if i < len(self._answers) else "").strip()
            if q_text and ans:
                lines.append(f"- Q{i + 1}: {q_text}\n  Answer: {ans}")
        return "\n".join(lines)

    def _submit_question_answers(self) -> None:
        formatted = self._format_question_answers()
        self._submit({"type": "rejected", "message": formatted})

    def action_accept(self) -> None:
        """Accept the proposed criteria unchanged."""
        if self._input_mode is not None:
            return
        gap = time.monotonic() - self._last_input_mode_exit_time
        if gap < 0.150:
            logger.debug("action_accept suppressed due to exit cool-off (gap=%.3fs)", gap)
            return
        logger.debug("GoalReviewMenu accepted proposed criteria")
        self._submit({"type": "accepted"})

    def action_edit(self) -> None:
        """Open the inline editor for revised criteria."""
        if self._completion.resolved or self._input_mode is not None:
            return
        logger.debug("GoalReviewMenu opening inline editor for criteria")
        self._input_mode = "edit"
        self._input_label.update(
            "✎ Edit acceptance criteria below (Enter to save, Esc to go back):"
        )
        self._input_label.display = True
        if self._edit_input is not None:
            self._edit_input.text = self._criteria
            self._edit_input.display = True
            if self.is_mounted:
                self._edit_input.focus()
                self.call_after_refresh(self._edit_input.scroll_visible)
        self._update_options()

    def action_reject_with_message(self) -> None:
        """Open the inline feedback input for regenerating criteria."""
        if self._completion.resolved or self._input_mode is not None:
            return
        logger.debug("GoalReviewMenu opening feedback editor for rejection")
        self._input_mode = "reject"
        self._input_label.update(
            "✎ Type instructions or answers to refine the plan (Enter to submit, Esc to go back):"
        )
        self._input_label.display = True
        if self._edit_input is not None:
            self._edit_input.text = ""
            self._edit_input.display = True
            if self.is_mounted:
                self._edit_input.focus()
                self.call_after_refresh(self._edit_input.scroll_visible)
        self._update_options()

    def action_cancel(self) -> None:
        """Cancel editing or cancel the whole proposal."""
        if self._completion.resolved:
            return
        if self._input_mode is not None:
            self._last_input_mode_exit_time = time.monotonic()
            logger.debug("GoalReviewMenu exiting input_mode %s (cool-off set)", self._input_mode)
            self._input_mode = None
            self._input_label.display = False
            if self._edit_input is not None:
                self._edit_input.display = False
            self._update_options()
            if self.is_mounted:
                self.focus()
            return
        logger.debug("GoalReviewMenu cancelling proposal")
        self._submit({"type": "cancelled"})

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Sync active question highlight when a question widget or its input receives focus."""
        node: Any = event.widget
        while node is not None and not isinstance(node, _QuestionWidget):
            node = node.parent
        if node is not None:
            self._input_mode = "questions"
            if node._index != self._current_question:
                self._highlight_question(node._index)
            self._update_options()

    def on_goal_review_text_area_submitted(
        self,
        event: GoalReviewTextArea.Submitted,
    ) -> None:
        """Submit edited criteria when Enter is pressed in the editor."""
        if event.text_area is not self._edit_input:
            return
        event.stop()
        if self._input_mode == "edit":
            self._submit_edit()
            return
        if self._input_mode == "reject":
            self._submit_rejection()

    def on_goal_review_text_area_cancel_edit(
        self,
        event: GoalReviewTextArea.CancelEdit,
    ) -> None:
        """Return from edit mode when Escape is pressed in the editor."""
        event.stop()
        self.action_cancel()

    def on_blur(self, event: events.Blur) -> None:
        """Prevent blur from dismissing the review prompt."""
        stop_inline_prompt_blur(event)

    def _submit_edit(self) -> None:
        """Submit the current editor text as revised criteria."""
        if self._edit_input is None:
            return
        criteria = self._edit_input.submitted_value.strip()
        if not criteria:
            self._hint_empty_submission("criteria")
            return
        self._submit({"type": "edited", "criteria": criteria})

    def _submit_rejection(self) -> None:
        """Submit the current editor text as regeneration feedback."""
        if self._edit_input is None:
            return
        message = self._edit_input.submitted_value.strip()
        if not message:
            self._hint_empty_submission("feedback")
            return
        self._submit({"type": "rejected", "message": message})

    def _hint_empty_submission(self, what: str) -> None:
        """Explain why an empty editor submission did nothing."""
        if self._help_widget is None:
            return
        glyphs = get_glyphs()
        self._help_widget.update(
            f"Enter some {what}, or press Esc to go back {glyphs.bullet} "
            f"{newline_hint()}"
        )

    def _submit(self, result: GoalReviewResult) -> None:
        """Resolve the result future and post the decision message."""
        if self._completion.resolved:
            return
        self.display = False
        if self._completion.resolve(result):
            self.post_message(self.Decided(result, self))

    def _update_options(self) -> None:
        """Render option labels and help text."""
        for i, widget in enumerate(self._option_widgets):
            widget.set_state(
                cursor=i == self._selected,
                highlighted=i == self._selected and self._input_mode is None,
            )

        if self._help_widget is None:
            return
        glyphs = get_glyphs()
        if self._input_mode == "questions":
            parts = [
                f"{glyphs.arrow_up}/{glyphs.arrow_down} select choice",
                "Enter confirm answer",
            ]
            if len(self._question_widgets) > 1:
                parts.append("Tab next question")
            parts.append("Esc back to menu")
            self._help_widget.update(f" {glyphs.bullet} ".join(parts))
            return
        if self._input_mode == "edit":
            self._help_widget.update(
                f"Enter save edits {glyphs.bullet} "
                f"{newline_hint()} {glyphs.bullet} Esc back"
            )
            return
        if self._input_mode == "reject":
            self._help_widget.update(
                f"Enter regenerate plan {glyphs.bullet} "
                f"{newline_hint()} {glyphs.bullet} Esc back"
            )
            return
        quick_keys = "q/y/e/r/n" if self._questions else "y/e/r/n"
        self._help_widget.update(
            f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate {glyphs.bullet} "
            f"Enter select {glyphs.bullet} {quick_keys} quick keys {glyphs.bullet} "
            "Esc cancel"
        )


class GoalReviewScreen(ModalScreen[str]):
    """Legacy Modal for reviewing a proposed goal and rubric.

    Dismiss values:
      - ``"accept"``  — accept the proposed rubric
      - ``"reject"``  — reject and regenerate
      - ``""``        — cancelled (Escape)
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=True),
        Binding("enter", "accept", "Accept", show=True),
    ]

    DEFAULT_CSS = """
    GoalReviewScreen {
        align: center middle;
    }

    GoalReviewScreen > #goal-review-container {
        width: 80;
        max-height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    GoalReviewScreen > #goal-review-container > #goal-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-objective {
        color: $text-muted;
        margin-bottom: 1;
        padding: 0 1;
    }

    GoalReviewScreen > #goal-review-container > #rubric-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    GoalReviewScreen > #goal-review-container > #rubric-content {
        color: $text;
        margin-bottom: 1;
        padding: 0 1;
        max-height: 20;
        overflow-y: auto;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > Button {
        margin: 0 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-accept {
        background: $success;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-reject {
        background: $error;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-cancel {
        background: $surface-darken-2;
    }
    """

    def __init__(
        self,
        objective: str,
        rubric: str,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize GoalReviewScreen."""
        super().__init__(name=name, id=id, classes=classes)
        self._objective = objective
        self._rubric = rubric

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        with Vertical(id="goal-review-container"):
            yield Label("Goal Review", id="goal-title")
            yield Static(self._objective, id="goal-objective")
            yield Label("Acceptance Criteria:", id="rubric-title")
            yield Static(self._rubric, id="rubric-content")
            with Horizontal(id="goal-button-bar"):
                yield Button("✓ Accept", id="btn-accept", variant="success")
                yield Button("✗ Reject", id="btn-reject", variant="error")
                yield Button("Cancel", id="btn-cancel", variant="default")

    @on(Button.Pressed, "#btn-accept")
    def _on_accept(self, event: Button.Pressed) -> None:
        self.dismiss("accept")

    @on(Button.Pressed, "#btn-reject")
    def _on_reject(self, event: Button.Pressed) -> None:
        self.dismiss("reject")

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss("")

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss("")

    def action_accept(self) -> None:
        """Execute the accept action."""
        self.dismiss("accept")


__all__ = [
    "GoalReviewAccepted",
    "GoalReviewCancelled",
    "GoalReviewEdited",
    "GoalReviewMenu",
    "GoalReviewRejected",
    "GoalReviewResult",
    "GoalReviewScreen",
    "GoalReviewTextArea",
]
