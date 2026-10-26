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

"""Middleware to collapse list-based system message content blocks into a single string."""

import re
from collections.abc import Sequence
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_COMPACT_SUBAGENT_DISPATCH_SECTION = """### Dispatching Subagents with `task`

Use `task` inside `js_eval` to dispatch configured built-in or plugin subagents (up to 32 concurrent calls with `Promise.all`):

```javascript
const results = await Promise.all(targets.map((t) =>
  task({
    description: `Complete autonomous brief for ${t}`, // pass file paths/ARNs as locators, not pasted contents
    subagentType: "<subagent-name>",                   // required: built-in or plugin subagent name
    label: `Audit ${t}`,                               // optional short UI progress label
    responseSchema: { type: "object", ... },           // optional JSON Schema (declarative subagents only; resolves to typed JS value)
  })
));
return results; // return via final expression, not console.log (stdout is truncated)
```

- Each dispatch is stateless: include the goal, scope, and expected output format in `description`.
- Reuse top-level variables persisted in the REPL across `js_eval` calls instead of re-typing prior results."""

_DISPATCH_SUBAGENTS_RE = re.compile(
    r"### Dispatching Subagents with `task`\n\n.*?(?=\n### API Reference — `tools` namespace|\n## |\Z)",
    re.DOTALL,
)

_PTC_API_REFERENCE_RE = re.compile(
    r"### API Reference — `tools` namespace\n\n.*?(?=\n## |\n### |\Z)",
    re.DOTALL,
)

_TS_CODE_BLOCK_RE = re.compile(r"```typescript\n(.*?)```", re.DOTALL)
_JSDOC_COMMENT_RE = re.compile(r"/\*\*(?:\n.*?\n\s*\*/|[^\n]*\*/)\s*", re.DOTALL)
_TOOL_SIG_RE = re.compile(
    r"(tools\.[A-Za-z0-9_]+)\(input:\s*\{(.*?)\}\):\s*Promise<.*?>",
    re.DOTALL,
)


def _compact_ptc_api_section(section: str) -> str:
    """Compact the verbose `### API Reference — tools namespace` block into 1-line TS signatures."""
    if "Example shape — substitute real tool names:" not in section and "/**" not in section:
        return section

    ts_blocks = _TS_CODE_BLOCK_RE.findall(section)
    if not ts_blocks:
        return section

    raw_sigs = ts_blocks[-1]
    cleaned = _JSDOC_COMMENT_RE.sub("", raw_sigs)
    one_line_sigs: list[str] = []
    for match in _TOOL_SIG_RE.finditer(cleaned):
        fn_name = match.group(1)
        fields_raw = " ".join(line.strip() for line in match.group(2).splitlines() if line.strip())
        one_line_sigs.append(f"{fn_name}(input: {{ {fields_raw} }}): Promise<unknown>")

    if not one_line_sigs:
        sig_block = cleaned.strip()
    else:
        sig_block = "\n".join(one_line_sigs)

    return (
        "### API Reference — `tools` namespace\n\n"
        "Exposed agent tools are available at `tools.<name>({ ... })` (returning a Promise resolving to a typed JS value; "
        "no `JSON.parse` needed). Chain dependent calls or fan out independent calls with `await Promise.all([...])` "
        "inside a single `js_eval` script:\n\n"
        f"```typescript\n{sig_block}\n```\n"
    )


def compact_interpreter_prompt(text: str) -> str:
    """Compact verbose QuickJS interpreter subagent and PTC API documentation in system prompts."""
    if "### Dispatching Subagents with `task`" in text and "#### The primitive" in text:
        text = _DISPATCH_SUBAGENTS_RE.sub(_COMPACT_SUBAGENT_DISPATCH_SECTION + "\n\n", text)
    if "### API Reference — `tools` namespace" in text:
        text = _PTC_API_REFERENCE_RE.sub(lambda m: _compact_ptc_api_section(m.group(0)), text)
    return text


def unify_system_message(system_message: SystemMessage | None) -> SystemMessage | None:
    """Execute unify system message."""
    if system_message is None:
        return None
    content = getattr(system_message, "content", None)
    if isinstance(content, str):
        compacted = compact_interpreter_prompt(content)
        if compacted != content:
            return SystemMessage(content=compacted)
        return system_message

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if txt:
                    parts.append(txt)
            elif isinstance(block, str) and block:
                parts.append(block)
        unified_text = compact_interpreter_prompt("".join(parts).strip())
        return SystemMessage(content=unified_text)
    return system_message


def _get_model_provider_family(model: Any) -> str:
    """Classify the target model into a content-block compatibility family."""
    if model is None:
        return "standard"
    provider = ""
    try:
        ls_params = getattr(model, "_get_ls_params", None)
        if callable(ls_params):
            params = ls_params()
            if isinstance(params, dict) and isinstance(params.get("ls_provider"), str):
                provider = params["ls_provider"].lower()
    except Exception:
        pass
    if not provider:
        provider = str(getattr(model, "_model_provider", "") or "").lower()
    if not provider:
        mod = getattr(type(model), "__module__", "").lower()
        if "anthropic" in mod:
            provider = "anthropic"
        elif "google_genai" in mod or "google_vertexai" in mod:
            provider = "google_genai"
        elif "openai" in mod:
            provider = "openai"
        elif "ollama" in mod:
            provider = "ollama"

    if provider in {"anthropic", "anthropic_bedrock", "google_anthropic_vertex"}:
        return "anthropic"
    if provider in {"google_genai", "google_vertexai", "google"}:
        return "google"
    if provider in {"openai", "openai_codex", "azure_openai"}:
        if getattr(model, "use_responses_api", False) or isinstance(
            getattr(model, "reasoning", None), dict
        ):
            return "openai_responses"
        return "standard"
    return "standard"


def _get_message_origin_family(message: AIMessage) -> str | None:
    """Infer the provider family that produced an AIMessage."""
    resp_meta = getattr(message, "response_metadata", None) or {}
    if isinstance(resp_meta, dict):
        orig_prov = str(resp_meta.get("model_provider") or "").lower()
        if orig_prov in {"anthropic", "anthropic_bedrock", "google_anthropic_vertex"}:
            return "anthropic"
        if orig_prov in {"google_genai", "google_vertexai", "google"}:
            return "google"
        if orig_prov in {"openai", "openai_codex", "azure_openai"}:
            return "openai_responses"
        if orig_prov:
            return "standard"

    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "redacted_thinking" or (btype == "thinking" and "signature" in block):
                return "anthropic"
            if btype == "thinking" or "extras" in block:
                return "google"
            if btype == "reasoning" and ("id" in block or "summary" in block):
                return "openai_responses"
    return None


def _sanitize_ai_message_for_family(message: AIMessage, target_family: str) -> AIMessage:
    """Normalize AIMessage content blocks when replaying history to a different provider family."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return message

    origin_family = _get_message_origin_family(message)
    if (
        target_family in {"anthropic", "google", "openai_responses"}
        and origin_family == target_family
    ):
        # Verify blocks are structurally valid for the target family
        if target_family == "anthropic":
            has_unsigned_thinking = any(
                isinstance(b, dict) and b.get("type") == "thinking" and not b.get("signature")
                for b in content
            )
            if not has_unsigned_thinking:
                return message
        elif target_family == "google":
            has_foreign_signature = any(
                isinstance(b, dict) and b.get("type") == "thinking" and "signature" in b
                for b in content
            )
            if not has_foreign_signature:
                return message
        else:
            return message

    # Cross-provider switch or standard/Ollama target: extract text and drop
    # provider-bound reasoning/thinking/tool metadata blocks from AIMessage.content
    # (tool calls remain intact on AIMessage.tool_calls).
    text_parts: list[str] = []
    needs_sanitize = False
    for block in content:
        if isinstance(block, str):
            if block:
                text_parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                if isinstance(txt, str) and txt:
                    text_parts.append(txt)
                if set(block.keys()) - {"type", "text"}:
                    needs_sanitize = True
            elif btype == "non_standard":
                val = block.get("value")
                if val is not None:
                    text_parts.append(str(val))
                needs_sanitize = True
            else:
                # thinking, redacted_thinking, reasoning, tool_use, function_call,
                # tool_call, tool_call_chunk, server_tool_use, etc.
                needs_sanitize = True
        else:
            needs_sanitize = True

    if not needs_sanitize and target_family != "standard":
        return message

    # Collapse to a clean string (universally accepted by Ollama, OpenAI, Anthropic,
    # Google GenAI, Bedrock, Mistral, Groq, DeepSeek, Cohere, etc.)
    joined_text = "\n".join(text_parts) if len(text_parts) > 1 else (text_parts[0] if text_parts else "")
    return message.model_copy(update={"content": joined_text})


def sanitize_messages_for_model(
    messages: Sequence[BaseMessage] | None,
    model: Any = None,
) -> list[BaseMessage] | None:
    """Sanitize message list for cross-provider compatibility.

    Returns a new list only if at least one message was modified; otherwise returns None.
    """
    if not messages:
        return None

    target_family = _get_model_provider_family(model)
    updated: list[BaseMessage] = []
    changed = False

    for msg in messages:
        if isinstance(msg, SystemMessage):
            unified = unify_system_message(msg)
            if unified is not None and unified is not msg:
                updated.append(unified)
                changed = True
            else:
                updated.append(msg)
        elif isinstance(msg, AIMessage) and isinstance(getattr(msg, "content", None), list):
            sanitized = _sanitize_ai_message_for_family(msg, target_family)
            if sanitized is not msg:
                updated.append(sanitized)
                changed = True
            else:
                updated.append(msg)
        else:
            updated.append(msg)

    return updated if changed else None


@register_middleware(name="unified_system_message")
class UnifiedSystemMessageMiddleware(AgentMiddleware[Any, Any]):
    """Middleware that collapses SystemMessage content blocks and sanitizes cross-provider AIMessage history."""

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        overrides: dict[str, Any] = {}
        unified_msg = unify_system_message(request.system_message)
        if unified_msg is not None and unified_msg is not request.system_message:
            overrides["system_message"] = unified_msg
        sanitized_msgs = sanitize_messages_for_model(request.messages, request.model)
        if sanitized_msgs is not None:
            overrides["messages"] = sanitized_msgs
        req_tools = getattr(request, "tools", None)
        if req_tools:
            seen_tool_names: set[str] = set()
            deduped_tools: list[Any] = []
            has_dup_tools = False
            for t in req_tools:
                t_name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                if t_name:
                    name_str = str(t_name)
                    if name_str in seen_tool_names:
                        has_dup_tools = True
                        continue
                    seen_tool_names.add(name_str)
                deduped_tools.append(t)
            if has_dup_tools:
                overrides["tools"] = deduped_tools
        if overrides:
            return request.override(**overrides)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap synchronous model invocation."""
        return handler(self._prepare_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap asynchronous model invocation."""
        return await handler(self._prepare_request(request))
