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

"""Non-interactive (headless CLI) runner for TalkOps.

Connects to the TalkOps server session, streams agent output to stdout,
supports --quiet for clean Unix piping, --no-stream for buffered loggers,
--json for CI machine-readable envelopes, and enforces safety caps (--max-turns, --timeout).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from talkops.approval_mode import ApprovalMode
from talkops.cli.server_manager import server_session
from talkops.config.settings import sync_aws_env_aliases
from talkops.output import write_json
from talkops.server import ServerConfig
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def _is_turn_update(chunk: Any) -> bool:
    """Return True if a LangGraph `updates` chunk represents an agent/model turn.

    LangGraph emits `updates` for every middleware hook (`*.before_model`,
    `*.after_model`, `*Middleware*`, etc.) as well as the main `model`/`agent`
    node. Only actual model/agent node completions (or direct `messages` dicts
    in unit tests) count as agentic turns for `--max-turns`.
    """
    if not isinstance(chunk, dict) or not chunk:
        return False
    if "messages" in chunk:
        return True
    return any(k in chunk for k in ("model", "agent"))


def _extract_update_messages(chunk: Any) -> list[Any]:
    """Extract messages list from a direct or node-keyed LangGraph `updates` chunk."""
    if not isinstance(chunk, dict):
        return []
    direct = chunk.get("messages")
    if isinstance(direct, list):
        return direct
    extracted: list[Any] = []
    for node_key, node_val in chunk.items():
        if node_key == "__interrupt__":
            continue
        if isinstance(node_val, dict):
            msgs = node_val.get("messages")
            if isinstance(msgs, list):
                extracted.extend(msgs)
    return extracted


async def _run_headless(
    prompt: str,
    *,
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    assistant_id: str = "talkops",
    approval_mode: ApprovalMode | str = ApprovalMode.AUTO,
    shell_allow_list: list[str] | None = None,
    read_only: bool = False,
    aws_profile: str | None = None,
    aws_region: str | None = None,
    quiet: bool = False,
    no_stream: bool = False,
    json_output: bool = False,
    max_turns: int | None = None,
    timeout: float | None = None,
    rubric: str | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    initial_skill: str | None = None,
    startup_cmd: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool = False,
    thread_id: str | None = None,
    cwd: str | None = None,
    server_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
    trajectory_file: str | None = None,
    no_subagents: bool = False,
    tnr: bool = False,
    agent: str | None = None,
    sandbox_type: str | None = None,
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
) -> int:
    """Execute a single headless run against the server session."""
    # 1. Startup command
    if startup_cmd:
        try:
            if not quiet and not json_output:
                print(f"[Running startup command: {startup_cmd}]", file=sys.stderr)
            proc = subprocess.run(startup_cmd, shell=True, capture_output=True, text=True)
            if proc.stdout and not quiet and not json_output:
                print(proc.stdout.strip(), file=sys.stderr)
            if proc.returncode != 0 and not quiet and not json_output:
                print(f"[Startup command exited with code: {proc.returncode}]", file=sys.stderr)
        except Exception as exc:
            if not quiet and not json_output:
                print(f"[Startup command failed: {exc}]", file=sys.stderr)

    # 2. AWS Profile and Region environment synchronization
    from talkops.config.cloud_profiles import (
        get_active_aws_profile,
        get_active_aws_region,
    )

    if not aws_profile:
        aws_profile = get_active_aws_profile()

    if not aws_region:
        aws_region = get_active_aws_region()

    if aws_profile:
        os.environ["AWS_PROFILE"] = aws_profile
    if aws_region:
        os.environ["AWS_REGION"] = aws_region
    if aws_profile or aws_region:
        sync_aws_env_aliases()

    # 3. Model spec resolution
    resolved_model = model
    if not resolved_model:
        try:
            from talkops.model.factory import _get_default_model_spec

            resolved_model = _get_default_model_spec()
        except Exception:
            resolved_model = "anthropic:claude-3-5-sonnet-latest"

    # 4. Server Configuration
    from talkops.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    config_kwargs: dict[str, Any] = {
        "project_context": project_context,
        "model": resolved_model,
        "assistant_id": assistant_id,
        "interactive": False,
        "approval_mode": str(approval_mode),
        "shell_allow_list": tuple(shell_allow_list) if shell_allow_list else None,
        "read_only": read_only,
        "aws_profile": aws_profile,
        "aws_region": aws_region,
        "mcp_config_path": mcp_config_path,
        "no_mcp": no_mcp,
        "trust_project_mcp": trust_project_mcp,
        "server_url": server_url,
        "api_key": api_key,
        "user_id": user_id,
    }
    if no_subagents:
        config_kwargs["no_subagents"] = True
    if tnr:
        config_kwargs["tnr"] = True
    if sandbox_type and sandbox_type not in ("none", "local"):
        config_kwargs["sandbox_type"] = sandbox_type
    if sandbox_id:
        config_kwargs["sandbox_id"] = sandbox_id
    if sandbox_snapshot_name:
        config_kwargs["sandbox_snapshot_name"] = sandbox_snapshot_name
    if sandbox_setup:
        config_kwargs["sandbox_setup"] = sandbox_setup
    config = ServerConfig.from_cli_args(**config_kwargs)

    if thread_id:
        try:
            uuid.UUID(thread_id)
            effective_thread = thread_id
        except ValueError:
            # Deterministically convert custom thread identifier to a valid UUIDv5
            effective_thread = str(uuid.uuid5(uuid.NAMESPACE_DNS, thread_id))
    else:
        from talkops.state.session import generate_thread_id

        effective_thread = generate_thread_id()

    # Route session logs to thread-specific log file
    from talkops.utils.logger import bind_logging_to_thread

    bind_logging_to_thread(effective_thread)

    final_prompt = prompt
    if initial_skill:
        final_prompt = f"Use the skill '{initial_skill}' for this task:\n{prompt}"

    start_time = time.monotonic()
    turn_count = 0
    full_response: list[str] = []
    rubric_status: str | None = None

    try:
        from talkops.benchmarks.trajectory import TrajectoryRecorder

        recorder: Any = TrajectoryRecorder(
            effective_thread,
            trajectory_file=trajectory_file,
            start_monotonic=start_time,
        )
    except ImportError:

        class _NoOpRecorder:
            def record_event(self, *args: Any, **kwargs: Any) -> None:
                pass

            def record_tool_activity(self, *args: Any, **kwargs: Any) -> None:
                pass

            def record_custom_chunk(self, *args: Any, **kwargs: Any) -> None:
                pass

            def finalize(self, **kwargs: Any) -> dict[str, Any]:
                return {}

        recorder = _NoOpRecorder()
    recorder.record_event(
        "run_start",
        {
            "model": resolved_model,
            "approval_mode": str(approval_mode),
            "read_only": read_only,
            "no_subagents": no_subagents,
            "tnr": tnr,
            "max_turns": max_turns,
            "prompt": final_prompt,
        },
        turn=0,
    )

    logger.info(
        "Starting headless run | thread_id=%s model=%s prompt=%r",
        effective_thread,
        resolved_model or "default",
        final_prompt[:80] + ("..." if len(final_prompt) > 80 else ""),
    )

    target_agent_name = agent.strip() if isinstance(agent, str) and agent.strip() else None

    with server_session(config) as agent:
        if not quiet and not json_output:
            print(f"TalkOps running headless: {final_prompt}\n", file=sys.stderr)
            if rubric:
                print(f"Active Acceptance Criteria:\n{rubric}\n", file=sys.stderr)

        from talkops.security.approval_mode import (
            approval_mode_key,
            awrite_approval_mode,
        )
        try:
            await awrite_approval_mode(agent, effective_thread, mode=approval_mode)
        except Exception:
            logger.debug("Failed to pre-write approval mode to store", exc_info=True)

        context: dict[str, Any] = {
            "model": resolved_model,
            "approval_mode": str(approval_mode),
            "approval_mode_key": approval_mode_key(effective_thread),
            "thread_id": effective_thread,
            "read_only": read_only,
            "yolo": str(approval_mode).lower() in {"yolo", "y"},
            "smart": str(approval_mode).lower() in {"smart", "jev"},
            "no_subagents": no_subagents,
            "tnr": tnr,
        }
        if model_params:
            context["model_params"] = model_params
        if profile_override:
            context["profile_overrides"] = profile_override
        if rubric_model:
            context["rubric_model"] = rubric_model
        if rubric_max_iterations is not None:
            context["rubric_max_iterations"] = rubric_max_iterations
        if shell_allow_list:
            context["shell_allow_list"] = shell_allow_list

        run_config: dict[str, Any] = {
            "configurable": {"thread_id": effective_thread},
        }
        if recursion_limit is not None:
            run_config["recursion_limit"] = recursion_limit

        input_data: dict[str, Any] = {"messages": [{"role": "user", "content": final_prompt}]}
        if target_agent_name:
            from talkops.commands.power.agents import resolve_plugin_agent
            from talkops.subagents.loader import list_subagents

            proj_root = project_context.project_root or project_context.user_cwd
            discovered_specs = list_subagents(project_root=proj_root, include_plugins=True)
            matched_spec = resolve_plugin_agent(target_agent_name, discovered_specs)
            resolved_agent_name = str(matched_spec.get("name") or target_agent_name) if matched_spec else target_agent_name
            resolved_plugin_id = str(matched_spec.get("plugin_id") or "") if matched_spec else ""
            input_data["agent_session_request"] = {
                "kind": "start",
                "request_id": str(uuid.uuid4()),
                "agent_name": resolved_agent_name,
                "plugin_id": resolved_plugin_id or None,
                "prompt": final_prompt,
            }
        if rubric:
            input_data["rubric"] = rubric

        if effective_thread:
            from talkops.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID

            ACTIVE_SESSION_THREAD_ID.set(effective_thread)

        try:
            async for ns, mode, chunk in agent.astream(
                input_data,
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
                config=run_config,
                context=context,
            ):
                if mode == "messages":
                    msg = chunk[0] if isinstance(chunk, tuple) else chunk
                    if isinstance(msg, str):
                        content = msg
                    elif isinstance(msg, dict):
                        content = msg.get("content", "")
                    else:
                        content = getattr(msg, "content", "")

                    text_piece = ""
                    if isinstance(content, str):
                        text_piece = content
                    elif isinstance(content, list):
                        parts: list[str] = []
                        for part in content:
                            if isinstance(part, str):
                                parts.append(part)
                            elif isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        text_piece = "".join(parts)

                    if text_piece:
                        full_response.append(text_piece)
                        if not no_stream and not json_output:
                            sys.stdout.write(text_piece)
                            sys.stdout.flush()

                elif mode == "updates":
                    if _is_turn_update(chunk):
                        turn_count += 1
                        logger.debug("Turn %d completed | thread_id=%s", turn_count, effective_thread)
                        if max_turns is not None and turn_count > max_turns:
                            logger.warning("Reached max-turns limit (%d) | thread_id=%s", max_turns, effective_thread)
                            recorder.finalize(
                                status="max_turns_exceeded",
                                turns=turn_count,
                                duration_seconds=round(time.monotonic() - start_time, 2),
                            )
                            if not quiet and not json_output:
                                print(f"\n[Reached max-turns limit ({max_turns})]", file=sys.stderr)
                            return 124

                    for m in _extract_update_messages(chunk):
                        is_dict_msg = isinstance(m, dict)
                        tool_calls = (
                            m.get("tool_calls") or (m.get("additional_kwargs") or {}).get("tool_calls")
                            if is_dict_msg
                            else getattr(m, "tool_calls", None)
                        )
                        if isinstance(tool_calls, list):
                            for tc in tool_calls:
                                if isinstance(tc, dict):
                                    tc_name = str(tc.get("name") or (tc.get("function") or {}).get("name") or "")
                                    tc_args = tc.get("args")
                                    if tc_args is None and isinstance(tc.get("function"), dict):
                                        raw_fn_args = tc["function"].get("arguments")
                                        if isinstance(raw_fn_args, str):
                                            try:
                                                import json as _json

                                                tc_args = _json.loads(raw_fn_args)
                                            except Exception:
                                                tc_args = None
                                        elif isinstance(raw_fn_args, dict):
                                            tc_args = raw_fn_args
                                    tc_id = tc.get("id")
                                    if tc_name:
                                        recorder.record_tool_activity(
                                            tc_name,
                                            tc_args if isinstance(tc_args, dict) else None,
                                            turn=turn_count,
                                            tool_call_id=str(tc_id) if tc_id else None,
                                            is_result=False,
                                        )
                        name = m.get("name", "") if is_dict_msg else getattr(m, "name", "")
                        msg_type = (m.get("type") or m.get("role") or "") if is_dict_msg else getattr(m, "type", "")
                        tc_id_res = m.get("tool_call_id") if is_dict_msg else getattr(m, "tool_call_id", None)
                        if name and isinstance(name, str) and (not is_dict_msg or msg_type in {"tool", "function", ""} or tc_id_res):
                            logger.info(
                                "Turn %d: Tool executed: %s | thread_id=%s",
                                turn_count,
                                name,
                                effective_thread,
                            )
                            raw_out = m.get("content") if is_dict_msg else getattr(m, "content", None)
                            out_str = str(raw_out) if isinstance(raw_out, (str, list, dict)) else None
                            recorder.record_tool_activity(
                                name,
                                turn=turn_count,
                                tool_call_id=str(tc_id_res) if tc_id_res else None,
                                output_preview=out_str,
                                is_result=True,
                            )
                            if not quiet and not json_output:
                                print(f"\n[Tool: {name}]", file=sys.stderr)

                elif mode == "custom":
                    if isinstance(chunk, dict):
                        recorder.record_custom_chunk(chunk, turn=turn_count)
                        event_type = chunk.get("type") or chunk.get("event")
                        if event_type == "subagent":
                            phase = chunk.get("phase")
                            sub_type = chunk.get("subagent_type") or chunk.get("id") or "subagent"
                            label = chunk.get("label") or chunk.get("description") or ""
                            logger.info(
                                "Subagent %s (%s): %s | thread_id=%s",
                                sub_type,
                                phase,
                                label,
                                effective_thread,
                            )
                            if not quiet and not json_output:
                                msg = f": {label}" if label else ""
                                if phase == "start":
                                    print(f"\n[Subagent started: {sub_type}{msg}]", file=sys.stderr)
                                elif phase == "complete":
                                    dur = chunk.get("duration_ms")
                                    dur_str = f" in {dur}ms" if dur else ""
                                    print(f"\n[Subagent completed: {sub_type}{dur_str}]", file=sys.stderr)
                                elif phase == "error":
                                    err = chunk.get("error") or "Unknown error"
                                    print(f"\n[Subagent error: {sub_type} - {err}]", file=sys.stderr)
                        elif event_type == "rubric_evaluation_start":
                            it = chunk.get("iteration", 0)
                            logger.info("Acceptance criteria grading started (attempt %d) | thread_id=%s", it + 1, effective_thread)
                            if not quiet and not json_output:
                                print(f"\n[Grading acceptance criteria (attempt {it + 1})...]", file=sys.stderr)
                        elif event_type == "rubric_evaluation_end":
                            verdict = chunk.get("verdict") or chunk.get("result") or "unknown"
                            rubric_status = verdict
                            logger.info("Acceptance criteria check completed: verdict=%s | thread_id=%s", verdict, effective_thread)
                            if not quiet and not json_output:
                                if verdict == "satisfied":
                                    print("\n[Acceptance criteria satisfied!]", file=sys.stderr)
                                else:
                                    print(f"\n[Acceptance criteria check: {verdict}]", file=sys.stderr)
                                    expl = chunk.get("explanation") or chunk.get("feedback")
                                    if expl:
                                        print(f"[Deficiencies]\n{expl}", file=sys.stderr)

            # Flush buffered text if --no-stream was requested
            if no_stream and not json_output and full_response:
                sys.stdout.write("".join(full_response))
                sys.stdout.flush()

            duration = round(time.monotonic() - start_time, 2)
            status = "success"
            if rubric and rubric_status != "satisfied":
                status = "criteria_unmet"

            telemetry_summary = recorder.finalize(
                status=status,
                turns=turn_count,
                duration_seconds=duration,
            )

            logger.info(
                "Headless run completed in %.2fs (%d turns) | thread_id=%s status=%s",
                duration,
                turn_count,
                effective_thread,
                rubric_status or "success",
            )

            if not quiet and not json_output:
                sys.stdout.write("\n")
                sys.stdout.flush()

            # Output standardized JSON envelope if --json was requested
            if json_output:
                write_json(
                    "run",
                    {
                        "status": status,
                        "thread_id": effective_thread,
                        "turns": turn_count,
                        "duration_seconds": duration,
                        "rubric_status": rubric_status,
                        "content": "".join(full_response),
                        **telemetry_summary,
                    },
                )

            if rubric and rubric_status != "satisfied":
                logger.warning("Headless run completed but criteria were not satisfied: %s", rubric_status)
                return 2

            return 0

        except (KeyboardInterrupt, asyncio.CancelledError):
            recorder.finalize(
                status="interrupted",
                turns=turn_count,
                duration_seconds=round(time.monotonic() - start_time, 2),
            )
            raise
        except Exception as e:
            logger.error("Headless run failed: %s", e, exc_info=True)
            recorder.finalize(
                status="error",
                turns=turn_count,
                duration_seconds=round(time.monotonic() - start_time, 2),
            )
            if not quiet and not json_output:
                print(f"\nHeadless run error: {e}", file=sys.stderr)
            return 1


def run_non_interactive(
    prompt: str,
    *,
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    assistant_id: str = "talkops",
    approval_mode: ApprovalMode | str = ApprovalMode.AUTO,
    shell_allow_list: list[str] | None = None,
    read_only: bool = False,
    aws_profile: str | None = None,
    aws_region: str | None = None,
    quiet: bool = False,
    no_stream: bool = False,
    json_output: bool = False,
    max_turns: int | None = None,
    timeout: float | None = None,
    rubric: str | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    initial_skill: str | None = None,
    startup_cmd: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool = False,
    thread_id: str | None = None,
    cwd: str | None = None,
    server_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
    trajectory_file: str | None = None,
    no_subagents: bool = False,
    tnr: bool = False,
    agent: str | None = None,
    sandbox_type: str | None = None,
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
) -> int:
    """Synchronous entrypoint for running TalkOps non-interactively with timeout and signals."""
    headless_kwargs: dict[str, Any] = {
        "model": model,
        "model_params": model_params,
        "profile_override": profile_override,
        "assistant_id": assistant_id,
        "approval_mode": approval_mode,
        "shell_allow_list": shell_allow_list,
        "read_only": read_only,
        "aws_profile": aws_profile,
        "aws_region": aws_region,
        "quiet": quiet,
        "no_stream": no_stream,
        "json_output": json_output,
        "max_turns": max_turns,
        "timeout": timeout,
        "rubric": rubric,
        "rubric_model": rubric_model,
        "rubric_max_iterations": rubric_max_iterations,
        "recursion_limit": recursion_limit,
        "initial_skill": initial_skill,
        "startup_cmd": startup_cmd,
        "mcp_config_path": mcp_config_path,
        "no_mcp": no_mcp,
        "trust_project_mcp": trust_project_mcp,
        "thread_id": thread_id,
        "cwd": cwd,
        "server_url": server_url,
        "api_key": api_key,
        "user_id": user_id,
    }
    if trajectory_file is not None:
        headless_kwargs["trajectory_file"] = trajectory_file
    if no_subagents:
        headless_kwargs["no_subagents"] = True
    if tnr:
        headless_kwargs["tnr"] = True
    if agent:
        headless_kwargs["agent"] = agent
    if sandbox_type and sandbox_type not in ("none", "local"):
        headless_kwargs["sandbox_type"] = sandbox_type
    if sandbox_id:
        headless_kwargs["sandbox_id"] = sandbox_id
    if sandbox_snapshot_name:
        headless_kwargs["sandbox_snapshot_name"] = sandbox_snapshot_name
    if sandbox_setup:
        headless_kwargs["sandbox_setup"] = sandbox_setup
    coro = _run_headless(prompt, **headless_kwargs)
    if timeout is not None:
        coro = asyncio.wait_for(coro, timeout=timeout)

    try:
        return asyncio.run(coro)
    except TimeoutError:
        if not quiet and not json_output:
            print(f"\nError: agent timed out after {timeout}s.", file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        if not quiet and not json_output:
            print("\n[Interrupted]", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Non-interactive execution failed: %s", exc)
        if not quiet and not json_output:
            print(f"\nError: {exc}", file=sys.stderr)
        return 1
