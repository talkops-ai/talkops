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

"""TalkOps Browser Middleware — 4-tool Stagehand facade with bounded snapshot & persona management.

Exposes the 4-tool browser surface for LangChain Deep Agents:
  1. ``snapshot`` — Capture accessibility tree with ``[ref=...]`` IDs, with automatic 8KB
     truncation, disk offloading, and ephemeral history compaction so snapshots never
     bloat the context window.
  2. ``run`` — Execute batched ref actions (``click``, ``fill``, ``press``, ``goto``, etc.)
     or Playwright JavaScript ``page`` code in a single turn with URL/SSRF validation.
  3. ``screenshot`` — Capture viewport/full-page PNG/JPEG screenshots, persist to disk, and
     return multimodal image blocks.
  4. ``browser_handoff`` — Pause execution via LangGraph ``interrupt()`` when encountering
     login/SSO walls, 2FA/MFA prompts, CAPTCHAs, or Chrome remote-debugging setup, then
     resume in the exact same browser session with a fresh post-handoff page snapshot.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import InjectedToolCallId
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException, tool
from langchain_stagehand.actions import SnapshotAction
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from stagehand_middleware.facade import (
    StagehandFacadeExecutionError,
    StagehandFacadeInputError,
    facade_instructions,
    facade_tools,
)
from stagehand_middleware.facade._syntax import check_run_syntax

from talkops.browser.session import (
    DEFAULT_BROWSER_ARTIFACTS_DIR,
    BrowserConnectionConfig,
    BrowserConnectionError,
    BrowserSessionManager,
)
from talkops.middleware.registry import register_middleware
from talkops.schema.interrupts import AskUserRequest, Choice, Question
from talkops.security.url_validation import UrlValidationError, validate_url
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Default 8KB (8192-byte) snapshot truncation threshold before offloading full text to disk
DEFAULT_MAX_SNAPSHOT_BYTES: int = 8192

CURRENT_SNAPSHOT_TAG = "current_snapshot"
CURRENT_SNAPSHOT_EXPLANATION = (
    "This is a temporary snapshot of the current page's accessibility tree structure. "
    "Each interactive element has a bracketed [frame-node] identifier (e.g. [0-34]) "
    "that you can target via `run(actions=[{\"op\": \"click\", \"id\": \"0-34\"}])`."
)

CHROME_DEFAULT_PROFILE_SECTION = """## Chrome Default Profile & Connection Troubleshooting

This browser session connects to the user's running default Chrome browser (via `DevToolsActivePort` / `--autoConnect`) when available, or launches a persistent TalkOps Chrome profile (`~/.talkops/browser-profile`).
- When connected to the user's default Chrome profile, the browser already carries the user's existing cookies, active enterprise SSO sessions (Okta, AWS IAM Identity Center, Google Workspace, Entra ID, GitHub/GitLab, Grafana, Datadog, PagerDuty, etc.), and access to local `localhost` / `kubectl port-forward` endpoints.
- **Connection Failure Case A (Chrome not running or Remote Debugging disabled)**:
  If a tool call fails because Chrome cannot be reached via `DevToolsActivePort`, call `browser_handoff` (or inform the user) so they can open Chrome and enable remote debugging at `chrome://inspect/#remote-debugging`, then resume.
- **Connection Failure Case B (Authentication / SSO / 2FA required)**:
  If you encounter an unauthenticated login page, SAML/OIDC SSO redirect, hardware-key prompt, or 2FA/MFA challenge, do NOT guess or fabricate credentials. Call `browser_handoff` immediately so the user can authenticate directly in the Chrome window and confirm when ready."""

BROWSER_SAFETY_AND_HANDOFF_SECTION = """## Browser Safety, Production Blast-Radius Gates & Human Handoff (`browser_handoff`)

- **Never bypass or solve CAPTCHAs**: Never attempt to solve, click through, wait out, or bypass a CAPTCHA, "verify you are human" challenge, Cloudflare Turnstile, reCAPTCHA, hCaptcha, or anti-bot gate. Immediately call `browser_handoff` and let the user complete the challenge in the browser window.
- **Destructive & High-Blast-Radius Web Actions (Approval Required)**:
  - Before executing any destructive, state-wiping, or high-blast-radius production action in a cloud console, SRE/GitOps tool, or SaaS UI — such as clicking controls that `Delete`, `Remove`, `Terminate`, `Destroy`, `Drop`, `Revoke`, `Rotate Secret`, `Invalidate Token`, `Uninstall`, `Purge`, `Wipe`, `Decommission`, `Deactivate`, `Drain Node`, `Prune Resources`, `Force Sync`, `Force Push`, `Factory Reset`, `Cancel Subscription`, or `Close Account`, or mutating production IAM/firewall/DNS/alerting policies without explicit instruction — you MUST obtain explicit user confirmation.
  - Note: `run()` enforces an automatic Human-in-the-Loop (HITL) approval gate when targeting destructive UI labels or HTTP `DELETE` calls, pausing execution until the user approves or rejects.
- **Context & Token Efficiency**:
  - Prefer `snapshot` + `run(actions=[...])` for navigation and DOM controls; use `screenshot` selectively when inspecting time-series `<canvas>`/`<svg>` charts, topology graphs, flamegraphs, heatmaps, or visual UI rendering issues.
  - Batch sequential interactions (e.g., `click` -> `fill` -> `press`) into a single `run(actions=[...])` call whenever element IDs are already known from the current snapshot.
  - Snapshots exceeding 8KB are automatically truncated in-context (preserving the top 65% header/nav/filters and bottom 35% SPA drawers/modals/log tails) and saved in full to disk; use `grep` or `read_file` on the saved `snapshot_full_*.txt` path whenever you need table rows or panels from the middle of a dense dashboard."""

BROWSER_SUBAGENT_SYSTEM_PROMPT = """# TalkOps Browser — Autonomous DevOps, SRE & Web Automation Subagent

You are TalkOps Browser, an autonomous **DevOps, CloudOps & SRE Web Automation Subagent** running inside an isolated browser delegation context.
You specialize first and foremost in operating **DevOps SaaS platforms, SRE & observability dashboards, cloud provider consoles, GitOps/CI-CD portals, incident-management UIs, internal developer portals, and `localhost` / port-forwarded services**, while remaining **universally capable of driving any commercial, open-source, or custom web application** via zero-shot UI discovery.

Your job is to control Chrome via `snapshot`, `run`, `screenshot`, and `browser_handoff` — paired with your workspace file tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`) and `ask_user` — to execute the delegated web workflow end-to-end and return an accurate, actionable report to the parent orchestrator. Host shell execution (`execute`) is intentionally not bound in this subagent; use `## Local Context` (active cloud account/project, region, cluster, and workspace files) as reference context when verifying target environments in web UIs.

## Autonomous Web & Dashboard Operations Architecture

### 1. Core Browser Loop (`Observe -> Act -> Verify`)
1. **Navigate & Reuse Live Enterprise Sessions**:
   - Navigate with `run(code="await page.goto('https://...', { waitUntil: 'domcontentloaded' });")` and call `snapshot()` (keeping `includeIframes=True`) to inspect the accessibility tree and bracketed `[frame-node]` IDs (e.g., `[0-34]`).
   - Reuse existing authenticated SSO sessions seamlessly. If you hit a login wall, SAML/OIDC redirect, MFA/2FA prompt, or CAPTCHA, immediately call `browser_handoff` so the user can complete it in Chrome and resume.
2. **Zero-Shot Semantic Discovery & State Transitions**:
   - Never guess brittle CSS/XPath selectors on unfamiliar UIs. Discover buttons, comboboxes, filter chips, tabs, menus, and dialogs from `snapshot()` roles and labels, then act via `run(actions=[{"op": "click", "id": "0-34"}, {"op": "fill", "id": "0-42", "value": "..."}])`.
   - Re-capture `snapshot()` after any action that mutates DOM state (navigation, switching tabs, opening drawers/modals, expanding dropdowns, applying filters).
   - Modern SPA slide-over drawers, modal portals, dropdown menus, and live log tails mount near the end of `<body>` (preserved in the tail of `<current_snapshot>` or searchable via `grep`/`read_file` on `snapshot_full_*.txt`). If clicking an action opens a new tab or OAuth popup, inspect `context.pages` inside `run(code="...")`.

### 2. DevOps, CloudOps & SRE UI Playbooks (Primary Focus)
1. **Observability, APM & SRE Dashboards** *(Grafana, Datadog, Kibana/OpenSearch, Prometheus, Alertmanager, Jaeger/Tempo, New Relic, Dynatrace, Splunk, Sentry)*:
   - **Verify Time Range, Timezone & Template Variables First**: Before reading panel metrics, logs, or traces—or reporting "no data / no anomalies"—always inspect and align the active **time-range picker** (e.g., `Last 15m` vs incident window, UTC vs local timezone) and top **template variables / facet filters** (`cluster`, `namespace`, `env`, `service`, `region`, `datasource`).
   - **Canvas/SVG Charts, Topologies & Flamegraphs**: Time-series graphs, heatmaps, service-mesh maps (Kiali, Argo CD, Datadog), and distributed trace timelines often render inside `<canvas>` or `<svg>` without exposing data points in `snapshot()`. Combine DOM data views (stat panels, legends, **Inspect -> Data** / table toggles, expanding error trace spans) with `screenshot()` whenever visual spikes, latency regressions, or degraded red topology nodes must be verified.
   - **Query Builders & Code Editors** *(PromQL, LogQL, TraceQL, KQL/Lucene, SQL)*: Switch from visual "Builder" mode to "Code" mode when entering exact queries. For Monaco, CodeMirror, Ace, or `contenteditable` editors where declarative `fill` does not bind state, use `run(code="...")` with Playwright focus/keyboard input, trigger execution (`Run query` / `Shift+Enter`), and wait inside a bounded `run(code="...")` for panel loading spinners (`aria-busy`, skeleton loaders) to finish before calling `snapshot()`.
2. **Cloud Provider & Infrastructure Consoles** *(AWS, GCP, Azure, Cloudflare, Terraform Cloud/Spacelift, Vault, Rancher/Headlamp/Kubernetes Dashboard)*:
   - **Verify Account / Project / Subscription & Region Header First**: Cloud consoles frequently open in a cached region (e.g., `us-east-1`) or wrong project/account. Cross-check the top header's **Account/Project ID, IAM Role, and Region selector** against the user's request and `<Local Context>` before inspecting resources or changing configuration.
   - **Multi-Frame Blades, Filtering & Pagination**: Keep `includeIframes=True` for iframe-isolated consoles (AWS Console, Azure Portal blades, embedded dashboards), use built-in search/tag filter bars rather than manual scrolling, and check table pagination counts so resources on page 2+ are not missed.
3. **GitOps, CI/CD, Source Control & Incident Portals** *(Argo CD, Flux, GitHub/GitLab Actions & Settings, Jenkins, Buildkite, PagerDuty, Opsgenie, Rootly, Statuspage)*:
   - Inspect sync/health states (`Synced`/`OutOfSync`, `Healthy`/`Degraded`/`Progressing`), failed pipeline stages/steps, webhook delivery payloads/HTTP status codes, and active alert escalations.
   - **Streaming Logs & In-Progress Rollouts**: Never poll `snapshot()` in a tight multi-turn loop while builds, deployments, or queries are running. Poll or wait inside a single bounded `run(code="...")` call (`await page.waitForFunction(...)` or short stability checks) and capture one `snapshot()` once settled, expanding collapsed failed step logs as needed.
4. **Localhost, Port-Forwarded Services & Web App Verification** *(`localhost`, `127.0.0.1` — e.g., `kubectl port-forward` UIs or frontend/API smoke tests)*:
   - Pair UI inspection (`snapshot()` / `screenshot()`) with runtime diagnostics inside `run(code="...")` (checking HTTP 4xx/5xx responses, failed XHR/fetch requests, CORS errors, or browser console errors) to pinpoint root causes alongside visual symptoms.

### 3. General SaaS & Any Other Web UI
- When operating non-DevOps web applications (Jira/Linear, Confluence/Notion, Slack/Discord admin, billing/admin portals, vendor status/docs sites, or any custom web UI), apply the same zero-shot `Observe -> Act -> Verify` discipline cleanly without forcing DevOps assumptions.

### 4. Workspace Integration & Operational Reporting
- Use `read_file`, `ls`, `glob`, and `grep` (parallelizing independent reads) to load local manifests, Helm/Terraform/YAML/JSON configs, webhook payloads, or offloaded `snapshot_full_*.txt` dumps; use `write_file` and `edit_file` to save exported configurations, scraped tables, or raw logs (>50 lines).
- Keep intermediate navigation churn inside your own turns and return a crisp, structured summary containing: exact URLs visited, verified context (account/project/region/cluster/time window when applicable), concrete metrics/states/errors observed, UI actions executed, and any saved screenshot or workspace file paths.

## Subagent Execution & Anti-Recursion Discipline
- You are already the dedicated `browser` subagent for this task. Execute the workflow directly with your browser and workspace file tools — do NOT attempt to re-delegate your assignment.
- Maximize parallel calls for independent workspace file lookups (`read_file`, `grep`, `glob`, `ls`), and offload large raw logs or exported JSON (>50 lines) to workspace files rather than dumping raw blobs into your final reply.
- Ground every claim with exact URLs, verified UI labels/metrics, cloud resource IDs/ARNs, or `file_path:line_number` references, preserving structured tables and actionable remediation details in your final report."""

_SNAPSHOT_ACTIONS_ADAPTER = TypeAdapter(list[SnapshotAction])

_JS_GOTO_URL_RE = re.compile(
    r"""\bpage\.goto\(\s*(['"`])(https?://[^'"`\s]+)\1""",
    re.IGNORECASE,
)


def _normalize_action_dict(action: dict[str, Any]) -> dict[str, Any]:
    """Normalize shorthand ``{"type": "click", "ref": 2}`` into Stagehand's ``{"op": "click", "id": "2"}``."""
    out = dict(action)
    if "op" not in out and "type" in out:
        out["op"] = out.pop("type")
    if "id" not in out and "ref" in out:
        out["id"] = str(out.pop("ref"))
    elif "id" in out and not isinstance(out["id"], str):
        out["id"] = str(out["id"])
    return out


class _SnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    includeIframes: bool = Field(
        default=True,
        description="Whether to include iframe accessibility trees in the snapshot.",
    )


class _RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(
        default=None,
        description=(
            "Async JavaScript body run against an existing Playwright `page` "
            "(also has `context` and `browser`). Navigate with `await page.goto('https://...')`. "
            "Pass either `code` or `actions`, not both."
        ),
    )
    actions: list[SnapshotAction] | None = Field(
        default=None,
        description=(
            "Declarative batch of actions targeting bracketed `id` strings from `snapshot()`. "
            "Each action uses `op` ('click', 'hover', 'fill', 'type', 'press', 'select') and `id`."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_shorthand_actions(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("actions"), list):
            data = dict(data)
            raw_actions = data["actions"]
            # If the model passed a single goto action in `actions`, transparently convert it to `code`
            if (
                data.get("code") is None
                and len(raw_actions) == 1
                and isinstance(raw_actions[0], dict)
                and (raw_actions[0].get("op") == "goto" or raw_actions[0].get("type") == "goto")
                and isinstance(raw_actions[0].get("url"), str)
            ):
                target_url = raw_actions[0]["url"].strip()
                data.pop("actions", None)
                data["code"] = (
                    f"await page.goto({json.dumps(target_url)}, {{ waitUntil: 'domcontentloaded' }});"
                )
                return data
            data["actions"] = [
                _normalize_action_dict(item) if isinstance(item, dict) else item
                for item in raw_actions
            ]
        return data


class _ScreenshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fullPage: bool | None = Field(
        default=None,
        description="Capture the full scrollable page instead of the current viewport.",
    )
    type: Literal["png", "jpeg"] = Field(
        default="png",
        description="Image format ('png' or 'jpeg').",
    )
    quality: float | None = Field(
        default=None,
        description="JPEG quality (0..100). Only used when type='jpeg'.",
    )


class _HandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        description=(
            "Clear explanation shown to the user describing what manual action is needed "
            "in the browser window (e.g., 'Please complete SSO login and 2FA on Okta', "
            "'Please solve the Cloudflare Turnstile challenge', or "
            "'Please enable remote debugging at chrome://inspect/#remote-debugging')."
        ),
    )
    url: str | None = Field(
        default=None,
        description="Optional URL the user should open or interact with before resuming.",
    )


def _is_localhost_url(url: str) -> bool:
    """Return True if *url* points to localhost or loopback (127.0.0.1 / ::1)."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().strip("[]")
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    except Exception:
        return False


def validate_browser_navigation_urls(
    *,
    code: str | None = None,
    actions: Sequence[dict[str, Any]] | None = None,
    allow_localhost: bool = True,
    validate_ssrf: bool = True,
) -> None:
    """Validate URLs in `run(actions=...)` or `run(code=...)` before browser navigation.

    Blocks non-http(s) schemes (`file://`, `javascript:`, `chrome://`) and private/metadata
    IP ranges (e.g. `169.254.169.254`), while optionally permitting `localhost`/`127.0.0.1`
    for local web-app testing.
    """
    urls_to_check: list[str] = []
    if actions:
        for action in actions:
            if isinstance(action, dict) and action.get("type") == "goto":
                raw_url = action.get("url")
                if isinstance(raw_url, str) and raw_url.strip():
                    urls_to_check.append(raw_url.strip())

    if code:
        for match in _JS_GOTO_URL_RE.finditer(code):
            urls_to_check.append(match.group(2))

    for url in urls_to_check:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise UrlValidationError(
                f"Browser navigation scheme not allowed: {parsed.scheme!r} (must be http or https)"
            )
        if allow_localhost and _is_localhost_url(url):
            continue
        if validate_ssrf:
            validate_url(url)


_SNAPSHOT_ELEMENT_RE = re.compile(
    r"\[(?:ref=)?([0-9]+(?:-[0-9]+)?)\]\s*([^\r\n]+)"
)
_DESTRUCTIVE_LABEL_RE = re.compile(
    r"\b("
    r"delete|remove|terminate|destroy|drop|revoke|uninstall|purge|wipe|erase|"
    r"decommission|deactivate|drain\s+node|prune\s+resources|force\s+sync|"
    r"invalidate\s+token|rotate\s+secret|disable\s+account|close\s+account|"
    r"cancel\s+subscription|factory\s+reset|force\s+push"
    r")\b",
    re.IGNORECASE,
)
_BENIGN_LABEL_RE = re.compile(
    r"\b("
    r"remove\s+filter|clear\s+filter|clear\s+search|clear\s+selection|reset\s+zoom|"
    r"close\s+modal|close\s+dialog|close\s+menu|close\s+banner|close\s+notification|"
    r"close\s+drawer|dismiss"
    r")\b",
    re.IGNORECASE,
)
_DESTRUCTIVE_JS_CLICK_RE = re.compile(
    r"(?:\.click|\.press|getByRole|getByText|locator)\s*\([^)]*"
    r"\b(delete|terminate|destroy|drop|revoke|uninstall|purge|wipe|drain\s+node|"
    r"prune\s+resources|force\s+sync|invalidate\s+token|rotate\s+secret|"
    r"close\s+account|cancel\s+subscription|factory\s+reset|force\s+push)\b",
    re.IGNORECASE,
)
_DESTRUCTIVE_JS_HTTP_DELETE_RE = re.compile(
    r"\bmethod\s*:\s*['\"`]DELETE['\"`]",
    re.IGNORECASE,
)


def parse_snapshot_element_labels(snapshot_text: str) -> dict[str, str]:
    """Parse Stagehand accessibility snapshot text into a mapping of ``{element_id: role_and_label}``."""
    elements: dict[str, str] = {}
    for match in _SNAPSHOT_ELEMENT_RE.finditer(snapshot_text):
        el_id = match.group(1).strip()
        label = match.group(2).strip()
        if el_id and label:
            elements[el_id] = label
    return elements


def detect_destructive_browser_actions(
    *,
    code: str | None = None,
    actions: Sequence[dict[str, Any]] | None = None,
    snapshot_elements: dict[str, str] | None = None,
) -> list[str]:
    """Detect destructive browser interactions in ``run(actions=...)`` or ``run(code=...)``.

    Returns a list of human-readable descriptions for any destructive UI actions
    (e.g., clicking a button labeled 'Delete Application' or issuing an HTTP DELETE),
    or an empty list if all actions are safe/non-destructive.
    """
    reasons: list[str] = []
    element_map = snapshot_elements or {}

    if actions:
        for raw_action in actions:
            if not isinstance(raw_action, dict):
                continue
            norm = _normalize_action_dict(raw_action)
            op = str(norm.get("op") or "").lower()
            target_id = str(norm.get("id") or "").strip()
            if op not in {"click", "press"} or not target_id:
                continue
            if op == "press":
                key = str(norm.get("key") or "").lower()
                if key not in {"enter", "return", "space", " "}:
                    continue

            label = element_map.get(target_id, "")
            if not label and target_id.isdigit():
                label = element_map.get(f"0-{target_id}", "")
            if label and _DESTRUCTIVE_LABEL_RE.search(label) and not _BENIGN_LABEL_RE.search(label):
                reasons.append(f"{op.capitalize()} [{target_id}] ({label})")

    if code:
        click_match = _DESTRUCTIVE_JS_CLICK_RE.search(code)
        if click_match:
            snippet = click_match.group(0).strip()
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            reasons.append(f"JavaScript destructive UI interaction: `{snippet}`")
        if _DESTRUCTIVE_JS_HTTP_DELETE_RE.search(code):
            reasons.append("JavaScript HTTP `DELETE` request in `run(code=...)`")

    return reasons


def format_and_offload_snapshot(
    snapshot_text: str,
    *,
    artifacts_dir: Path,
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> tuple[str, Path, Path | None]:
    """Save snapshot to disk and apply 8KB Head + Tail truncation & XML wrapping.

    Preserves both top-level navigation and bottom portal/footer regions on dense SaaS UIs:
    - If ``len(encoded) > max_bytes`` (default 8KB = 8192 bytes):
      1. Saves full text to ``snapshots/snapshot_full_<ts>.txt``.
      2. Preserves the first ~65% (Head: top navigation & controls) and last ~35%
         (Tail: streaming build/deployment logs, table footers, and React/Vue modal portals
         appended at the end of ``<body>``) within ``max_bytes``.
      3. Appends ``[Snapshot truncated because it exceeded 8KB. Only showing first 8KB. Full snapshot saved to <path>]``.
    - Saves the in-context snapshot to ``snapshots/snapshot_<ts>.txt``.
    - Wraps with ``<current_snapshot_explanation>`` and ``<current_snapshot>``.

    Returns:
        ``(formatted_output, snapshot_path, full_snapshot_path_or_none)``
    """
    snapshots_dir = artifacts_dir.expanduser().resolve() / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)

    encoded = snapshot_text.encode("utf-8")
    full_snapshot_path: Path | None = None

    if len(encoded) > max_bytes:
        full_snapshot_path = snapshots_dir / f"snapshot_full_{ts_ms}.txt"
        full_snapshot_path.write_text(snapshot_text, encoding="utf-8")

        head_bytes = int(max_bytes * 0.65)
        tail_bytes = max(0, max_bytes - head_bytes)
        head_text = encoded[:head_bytes].decode("utf-8", errors="ignore")
        tail_text = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes > 0 else ""
        omitted_bytes = max(0, len(encoded) - head_bytes - tail_bytes)
        kb_label = max(1, max_bytes // 1024)
        body_text = (
            f"{head_text}\n"
            f"... [{omitted_bytes} bytes omitted from middle; use `grep` or `read_file` on {full_snapshot_path}] ...\n"
            f"{tail_text}\n\n"
            f"[Snapshot truncated because it exceeded {kb_label}KB. "
            f"Only showing first {kb_label}KB. Full snapshot saved to {full_snapshot_path}]"
        )
    else:
        body_text = snapshot_text

    snapshot_path = snapshots_dir / f"snapshot_{ts_ms}.txt"
    snapshot_path.write_text(body_text, encoding="utf-8")

    formatted = (
        f"<current_snapshot_explanation>\n{CURRENT_SNAPSHOT_EXPLANATION}\n</current_snapshot_explanation>\n"
        f"<{CURRENT_SNAPSHOT_TAG} path=\"{snapshot_path}\""
        + (f" full_path=\"{full_snapshot_path}\"" if full_snapshot_path else "")
        + f">\n{body_text}\n</{CURRENT_SNAPSHOT_TAG}>"
    )
    return formatted, snapshot_path, full_snapshot_path


def prune_stale_snapshots_in_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Replace older ``<current_snapshot>`` ToolMessages so only the newest snapshot stays in context.

    Implements ephemeral snapshot compaction across
    LangChain message lists: if multiple ``ToolMessage`` objects contain ``<current_snapshot``,
    all but the last one are compacted to a single-line pointer referencing the saved file on disk.
    """
    snapshot_indices: list[int] = []
    for idx, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
            if f"<{CURRENT_SNAPSHOT_TAG}" in msg.content:
                snapshot_indices.append(idx)

    if len(snapshot_indices) <= 1:
        return list(messages)

    stale_set = set(snapshot_indices[:-1])
    updated: list[BaseMessage] = []
    path_re = re.compile(rf'<{CURRENT_SNAPSHOT_TAG}\s+path="([^"]+)"')

    for idx, msg in enumerate(messages):
        if idx not in stale_set or not isinstance(msg, ToolMessage) or not isinstance(msg.content, str):
            updated.append(msg)
            continue

        match = path_re.search(msg.content)
        saved_ref = f" (saved at {match.group(1)})" if match else ""
        compacted_content = (
            f"[Previous page snapshot superseded by a newer snapshot{saved_ref}. "
            "Use the latest <current_snapshot> or read the saved file if needed.]"
        )
        updated.append(
            msg.model_copy(
                update={
                    "content": compacted_content,
                }
            )
        )

    return updated


@register_middleware(name="browser")
class TalkOpsBrowserMiddleware(AgentMiddleware[Any, Any]):
    """LangChain AgentMiddleware managing the 4 Stagehand/TalkOps browser tools and session lifecycle."""

    def __init__(
        self,
        *,
        session_manager: BrowserSessionManager | None = None,
        config: BrowserConnectionConfig | None = None,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        allow_localhost: bool = True,
        validate_ssrf: bool = True,
        require_destructive_approval: bool = True,
        close_on_agent_finish: bool = False,
    ) -> None:
        """Initialize TalkOpsBrowserMiddleware.

        Args:
            session_manager: Optional explicit ``BrowserSessionManager``. Defaults to singleton.
            config: Optional ``BrowserConnectionConfig`` if creating/updating the session manager.
            max_snapshot_bytes: Byte threshold before snapshots are truncated and offloaded (default 8KB).
            allow_localhost: Whether to permit ``localhost`` / ``127.0.0.1`` URLs in ``run``.
            validate_ssrf: Whether to run SSRF DNS/IP validation on non-localhost URLs.
            require_destructive_approval: Whether to prompt the user via HITL ``interrupt()`` before
                executing destructive browser clicks or HTTP ``DELETE`` calls.
            close_on_agent_finish: Whether to detach/close the browser at the end of each agent run.
        """
        super().__init__()
        self._session_manager = session_manager or BrowserSessionManager.get_instance(config=config)
        self.max_snapshot_bytes = max_snapshot_bytes
        self.allow_localhost = allow_localhost
        self.validate_ssrf = validate_ssrf
        self.require_destructive_approval = require_destructive_approval
        self.close_on_agent_finish = close_on_agent_finish
        self._latest_snapshot_elements: dict[str, str] = {}

        self.tools: list[BaseTool] = [
            self._build_run_tool(),
            self._build_snapshot_tool(),
            self._build_screenshot_tool(),
            self._build_handoff_tool(),
        ]

    @property
    def session_manager(self) -> BrowserSessionManager:
        """Return the underlying ``BrowserSessionManager``."""
        return self._session_manager

    @property
    def system_prompt_section(self) -> str:
        """Return the combined Stagehand + TalkOps browser system prompt instructions."""
        stagehand_prompt = facade_instructions("playwright").strip()
        if stagehand_prompt.startswith("Browser tool surface:"):
            stagehand_prompt = (
                stagehand_prompt.split("\n", 1)[1].strip() if "\n" in stagehand_prompt else ""
            )
        stagehand_prompt = stagehand_prompt.replace(
            "You control one persistent browser through exactly three tools:",
            "You control one persistent browser through four tools:",
        )
        if "- browser_handoff:" not in stagehand_prompt:
            stagehand_prompt = re.sub(
                r"(- screenshot:[^\n]+)",
                (
                    r"\1\n"
                    r"- browser_handoff: pause execution via HITL interrupt for manual login/SSO/2FA, "
                    r"CAPTCHA completion, or Chrome remote-debugging setup, then automatically resume "
                    r"with a fresh snapshot."
                ),
                stagehand_prompt,
                count=1,
            )
        tool_surface_section = (
            f"## Browser Tool Surface (Stagehand Playwright Facade)\n\n{stagehand_prompt}".strip()
        )
        return (
            f"{tool_surface_section}\n\n"
            f"{CHROME_DEFAULT_PROFILE_SECTION}\n\n"
            f"{BROWSER_SAFETY_AND_HANDOFF_SECTION}"
        )

    def _validate_run_input(
        self,
        *,
        code: str | None = None,
        actions: list[SnapshotAction] | None = None,
    ) -> None:
        has_code = code is not None
        has_actions = actions is not None
        if has_code == has_actions:
            raise ToolException("Pass either `code` or `actions`, not both.")
        if code is not None:
            if not isinstance(code, str) or not code.strip():
                raise ToolException("`code` must be a non-empty string.")
            try:
                check_run_syntax(code)
            except SyntaxError as exc:
                raise ToolException(f"Invalid JavaScript syntax in `code`: {exc}") from exc
            return
        if not isinstance(actions, list) or len(actions) == 0:
            raise ToolException("`actions` must be a non-empty array.")
        normalized = [
            _normalize_action_dict(dict(item)) if isinstance(item, dict) else item
            for item in actions
        ]
        try:
            _SNAPSHOT_ACTIONS_ADAPTER.validate_python(normalized)
        except Exception as exc:
            raise ToolException(f"Invalid `actions` input: {exc}") from exc

    def _build_snapshot_tool(self) -> BaseTool:
        facade_desc = {spec["name"]: spec["description"] for spec in facade_tools()}.get(
            "snapshot",
            "Capture the active page's Stagehand accessibility tree and hydrate its displayed IDs.",
        )

        async def _snapshot(*, includeIframes: bool = True) -> str:
            try:
                facade = await self._session_manager.get_facade()
                raw_snapshot = await facade.snapshot(include_iframes=includeIframes)
            except BrowserConnectionError as exc:
                return (
                    f"ERROR: {exc}\n\n"
                    "Action required: Call `browser_handoff` to ask the user to open Chrome and "
                    "enable remote debugging at `chrome://inspect/#remote-debugging`."
                )
            except (StagehandFacadeInputError, StagehandFacadeExecutionError) as exc:
                raise ToolException(str(exc)) from None
            except ToolException:
                raise
            except Exception as exc:
                raise ToolException(f"{type(exc).__name__}: {exc}") from None
            self._latest_snapshot_elements = parse_snapshot_element_labels(raw_snapshot)
            artifacts_dir = self._session_manager.config.artifacts_dir or DEFAULT_BROWSER_ARTIFACTS_DIR
            formatted, _, _ = format_and_offload_snapshot(
                raw_snapshot,
                artifacts_dir=artifacts_dir,
                max_bytes=self.max_snapshot_bytes,
            )
            return formatted

        snapshot_tool = StructuredTool.from_function(
            coroutine=_snapshot,
            name="snapshot",
            description=(
                f"{facade_desc} Capture a new snapshot after navigation or stale IDs before "
                "taking actions. Snapshots over 8KB are automatically truncated in-context and "
                "saved in full to disk."
            ),
            args_schema=_SnapshotInput,
        )
        snapshot_tool.handle_tool_error = True
        snapshot_tool.handle_validation_error = True
        return snapshot_tool

    def _build_run_tool(self) -> BaseTool:
        facade_desc = {spec["name"]: spec["description"] for spec in facade_tools()}.get(
            "run",
            "Browse and automate websites in the persistent browser.",
        )

        async def _run(
            *,
            code: str | None = None,
            actions: list[SnapshotAction] | None = None,
        ) -> str:
            self._validate_run_input(code=code, actions=actions)
            try:
                validate_browser_navigation_urls(
                    code=code,
                    actions=actions,
                    allow_localhost=self.allow_localhost,
                    validate_ssrf=self.validate_ssrf,
                )
            except UrlValidationError as exc:
                raise ToolException(str(exc)) from exc

            if self.require_destructive_approval:
                destructive_reasons = detect_destructive_browser_actions(
                    code=code,
                    actions=actions,
                    snapshot_elements=self._latest_snapshot_elements,
                )
                if destructive_reasons:
                    bullet_list = "\n".join(f"• {desc}" for desc in destructive_reasons)
                    ask_request = AskUserRequest(
                        type="ask_user",
                        questions=[
                            Question(
                                question=(
                                    "The Browser Agent is about to execute a potentially destructive "
                                    f"action in the web application:\n\n{bullet_list}\n\n"
                                    "Do you approve executing this browser action?"
                                ),
                                header="Approve Destructive Browser Action",
                                type="multiple_choice",
                                choices=[
                                    Choice(
                                        value="approve",
                                        description="Approve and execute this browser action",
                                    ),
                                    Choice(
                                        value="reject",
                                        description="Reject and block this browser action",
                                    ),
                                ],
                            )
                        ],
                        tool_call_id="browser_run_destructive_gate",
                    )
                    response = interrupt(ask_request)
                    decision = "approve"
                    if isinstance(response, dict):
                        if response.get("status") in {"cancelled", "error"}:
                            decision = "reject"
                        else:
                            answers = response.get("answers", [])
                            if isinstance(answers, list) and answers:
                                decision = str(answers[0]).strip().lower()
                    elif isinstance(response, (list, tuple)) and response:
                        decision = str(response[0]).strip().lower()
                    elif isinstance(response, str):
                        decision = response.strip().lower()

                    if any(word in decision for word in ("reject", "cancel", "abort", "no", "deny")):
                        return (
                            "Action blocked: User rejected destructive browser action ("
                            + "; ".join(destructive_reasons)
                            + "). Do NOT retry this destructive action unless explicitly instructed by the user."
                        )

            try:
                facade = await self._session_manager.get_facade()
                result = await facade.run(code=code, actions=actions)
                return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            except BrowserConnectionError as exc:
                return (
                    f"ERROR: {exc}\n\n"
                    "Action required: Call `browser_handoff` to ask the user to open Chrome and "
                    "enable remote debugging at `chrome://inspect/#remote-debugging`."
                )
            except (StagehandFacadeInputError, StagehandFacadeExecutionError) as exc:
                raise ToolException(str(exc)) from None
            except ToolException:
                raise
            except Exception as exc:
                raise ToolException(f"{type(exc).__name__}: {exc}") from None

        run_tool = StructuredTool.from_function(
            coroutine=_run,
            name="run",
            description=(
                f"{facade_desc} Reuse the initialized browser; do not launch another browser "
                "or browser process. Capture a new snapshot after navigation or stale IDs."
            ),
            args_schema=_RunInput,
        )
        run_tool.handle_tool_error = True
        run_tool.handle_validation_error = True
        return run_tool

    def _build_screenshot_tool(self) -> BaseTool:
        facade_desc = {spec["name"]: spec["description"] for spec in facade_tools()}.get(
            "screenshot",
            "Capture a screenshot of the active page.",
        )

        async def _screenshot(
            *,
            fullPage: bool | None = None,
            type: Literal["png", "jpeg"] = "png",
            quality: float | None = None,
        ) -> list[dict[str, str]]:
            try:
                facade = await self._session_manager.get_facade()
                image = await facade.screenshot(
                    full_page=fullPage,
                    type=type,
                    quality=quality,
                )
            except (StagehandFacadeInputError, StagehandFacadeExecutionError) as exc:
                raise ToolException(str(exc)) from None
            except ToolException:
                raise
            except Exception as exc:
                raise ToolException(f"{type(exc).__name__}: {exc}") from None
            artifacts_dir = (
                self._session_manager.config.artifacts_dir or DEFAULT_BROWSER_ARTIFACTS_DIR
            ).expanduser().resolve()
            screenshots_dir = artifacts_dir / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            ext = "jpg" if type == "jpeg" else "png"
            ts_ms = int(time.time() * 1000)
            saved_path = screenshots_dir / f"screenshot_{ts_ms}.{ext}"
            try:
                saved_path.write_bytes(base64.b64decode(image["data"]))
            except Exception as exc:
                logger.debug("Failed to persist screenshot to %s: %s", saved_path, exc)

            return [
                {
                    "type": "image",
                    "base64": image["data"],
                    "mime_type": image["mimeType"],
                },
                {
                    "type": "text",
                    "text": f"Screenshot saved to {saved_path}",
                },
            ]

        screenshot_tool = StructuredTool.from_function(
            coroutine=_screenshot,
            name="screenshot",
            description=(
                f"{facade_desc} Persists screenshots to `.talkops/artifacts/browser/screenshots/` "
                "and returns the image for visual inspection."
            ),
            args_schema=_ScreenshotInput,
        )
        screenshot_tool.handle_tool_error = True
        screenshot_tool.handle_validation_error = True
        return screenshot_tool

    def _build_handoff_tool(self) -> BaseTool:
        @tool(
            "browser_handoff",
            description=(
                "Pause browser automation and hand control of the Chrome window to the user. "
                "Use this tool immediately when you encounter: (1) a login/SSO/SAML page or "
                "2FA/MFA prompt requiring user credentials, (2) a CAPTCHA, Cloudflare Turnstile, "
                "or 'verify you are human' challenge, or (3) a Chrome remote-debugging connection "
                "prompt (`chrome://inspect/#remote-debugging`). Once the user confirms completion, "
                "this tool automatically captures a fresh page snapshot and resumes."
            ),
        )
        async def _browser_handoff(
            reason: str,
            tool_call_id: Annotated[str, InjectedToolCallId],
            url: str | None = None,
        ) -> Command[Any]:
            target_hint = f"\nURL: {url}" if url else ""
            question_text = (
                f"Browser Handoff Requested: {reason.strip()}{target_hint}\n\n"
                "Please complete the required action in the Chrome browser window, "
                "then select an option below to resume."
            )
            questions: list[Question] = [
                Question(
                    question=question_text,
                    header="Browser Handoff",
                    type="multiple_choice",
                    choices=[
                        Choice(
                            value="completed",
                            description="I have completed the action in the browser — resume automation",
                        ),
                        Choice(
                            value="abort",
                            description="Abort this browser task",
                        ),
                    ],
                )
            ]
            ask_request = AskUserRequest(
                type="ask_user",
                questions=questions,
                tool_call_id=tool_call_id,
            )
            response = interrupt(ask_request)

            user_decision = "completed"
            if isinstance(response, dict):
                if response.get("status") in {"cancelled", "error"}:
                    user_decision = "abort"
                else:
                    answers = response.get("answers", [])
                    if isinstance(answers, list) and answers:
                        user_decision = str(answers[0]).strip().lower()
            elif isinstance(response, (list, tuple)) and response:
                user_decision = str(response[0]).strip().lower()

            if "abort" in user_decision or "cancel" in user_decision:
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                "User aborted the browser handoff. Stop browser actions and report back.",
                                tool_call_id=tool_call_id,
                            )
                        ]
                    }
                )

            # Automatically take a fresh snapshot after the user completes the handoff
            post_snapshot_msg = ""
            try:
                facade = await self._session_manager.get_facade()
                raw_snapshot = await facade.snapshot(include_iframes=True)
                self._latest_snapshot_elements = parse_snapshot_element_labels(raw_snapshot)
                artifacts_dir = (
                    self._session_manager.config.artifacts_dir or DEFAULT_BROWSER_ARTIFACTS_DIR
                )
                formatted_snap, _, _ = format_and_offload_snapshot(
                    raw_snapshot,
                    artifacts_dir=artifacts_dir,
                    max_bytes=self.max_snapshot_bytes,
                )
                post_snapshot_msg = f"\n\nUpdated page snapshot after handoff:\n{formatted_snap}"
            except Exception as exc:
                post_snapshot_msg = f"\n\n(Could not capture automatic post-handoff snapshot: {exc})"

            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            f"User confirmed browser handoff completed ({user_decision}).{post_snapshot_msg}",
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            )

        return _browser_handoff

    def _prepare_model_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        """Inject browser instructions into the system prompt and prune stale snapshots."""
        existing_prompt = request.system_prompt or ""
        section = self.system_prompt_section
        if section not in existing_prompt:
            new_prompt = f"{existing_prompt}\n\n{section}".strip()
        else:
            new_prompt = existing_prompt

        pruned_messages = prune_stale_snapshots_in_messages(request.messages)
        if pruned_messages != list(request.messages):
            return request.override(
                system_message=SystemMessage(content=new_prompt),
                messages=pruned_messages,
            )
        return request.override(system_message=SystemMessage(content=new_prompt))

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Synchronously inject browser guidance and compact stale snapshots before model call."""
        return handler(self._prepare_model_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Asynchronously inject browser guidance and compact stale snapshots before model call."""
        return await handler(self._prepare_model_request(request))

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Optionally detach/close the browser session when the agent run finishes."""
        del state, runtime
        if self.close_on_agent_finish:
            await self._session_manager.close()
        return None


__all__ = [
    "BROWSER_SAFETY_AND_HANDOFF_SECTION",
    "BROWSER_SUBAGENT_SYSTEM_PROMPT",
    "CHROME_DEFAULT_PROFILE_SECTION",
    "CURRENT_SNAPSHOT_EXPLANATION",
    "CURRENT_SNAPSHOT_TAG",
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "TalkOpsBrowserMiddleware",
    "detect_destructive_browser_actions",
    "format_and_offload_snapshot",
    "parse_snapshot_element_labels",
    "prune_stale_snapshots_in_messages",
    "validate_browser_navigation_urls",
]
