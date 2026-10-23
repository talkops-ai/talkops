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

"""External editor support for composing prompts in TalkOps."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

GUI_WAIT_FLAG: dict[str, str] = {
    "code": "--wait",
    "cursor": "--wait",
    "zed": "--wait",
    "atom": "--wait",
    "subl": "-w",
    "windsurf": "--wait",
}
"""Mapping of GUI editor base names to their blocking flag."""

VIM_EDITORS = {"vi", "vim", "nvim"}
"""Set of vim-family editor base names that receive the `-i NONE` flag."""

TERMINAL_EDITORS = {
    "vi",
    "vim",
    "nvim",
    "nano",
    "pico",
    "emacs",
    "helix",
    "hx",
    "micro",
    "kak",
    "joe",
    "ed",
}
"""Set of terminal-based editor names that require suspending the TUI."""


class ExternalEditorError(RuntimeError):
    """Raised when an external editor cannot be opened or read."""


def resolve_editor() -> list[str] | None:
    """Resolve editor command from environment.

    Checks $TALKOPS_EDITOR, then $VISUAL, then $EDITOR, then falls back to platform default.

    Returns:
        Tokenized command list, or `None` if env var was set but empty after tokenization.
    """
    editor = (
        os.environ.get("TALKOPS_EDITOR")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
    )
    if not editor:
        if sys.platform == "win32":
            return ["notepad"]
        return ["vi"]
    tokens = shlex.split(editor)
    return tokens or None


_LOCAL_URI_SCHEMES: tuple[str, ...] = (
    "file://localhost",
    "file://",
    "file:",
    "sandbox://",
    "sandbox:",
    "vscode://file",
    "cursor://file",
)


def _strip_local_uri_scheme(raw: str) -> tuple[str, int | None]:
    """Strip local/LLM file URI schemes (file://, sandbox:, etc.) and #L line fragments."""
    cleaned = raw.strip()
    lower = cleaned.lower()
    for prefix in _LOCAL_URI_SCHEMES:
        if lower.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            cleaned = urllib.parse.unquote(cleaned)
            if cleaned.startswith("//") and not cleaned.startswith("///"):
                cleaned = cleaned[1:]
            break

    anchor_line: int | None = None
    if "#" in cleaned:
        base, _, frag = cleaned.partition("#")
        m_line = re.match(r"^L?(\d+)(?:-L?\d+)?$", frag, re.IGNORECASE)
        if m_line:
            anchor_line = int(m_line.group(1))
            cleaned = base
        elif Path(os.path.expanduser(base)).exists():
            cleaned = base

    return cleaned, anchor_line


def extract_file_path(text: str) -> str | None:
    """Extract an existing file path from a text snippet, code block, or line.

    Scans text for candidate file paths (absolute, tilde-expanded, or relative),
    strips enclosing quotes or trailing sentence punctuation, and returns the
    first path that exists on disk as a file.
    """
    if not text:
        return None


    raw = text.strip()
    if (raw.startswith(('"', "'", "`")) and raw.endswith(('"', "'", "`"))) and len(raw) >= 2:
        raw = raw[1:-1].strip()

    raw, _ = _strip_local_uri_scheme(raw)

    cleaned = raw.rstrip(".,;:!?)'\">")
    m = re.match(r"^(.*?):(\d+)(?::(\d+))?$", cleaned)
    candidate = m.group(1) if m else cleaned
    try:
        p = Path(os.path.expanduser(candidate))
        if p.exists() and p.is_file():
            return cleaned
    except Exception:
        pass

    path_re = re.compile(
        r"(?:(?:/[a-zA-Z0-9_\-\.]+){2,}/?|~/(?:[a-zA-Z0-9_\-\.]+/)*[a-zA-Z0-9_\-\.]+|\./(?:[a-zA-Z0-9_\-\.]+/)*[a-zA-Z0-9_\-\.]+|(?:\b[a-zA-Z]:[\\/](?:[a-zA-Z0-9_\-\.]+[\\/])*[a-zA-Z0-9_\-\.]+))(?:\.[a-zA-Z0-9_]+)?"
    )
    for match in path_re.finditer(raw):
        cand = match.group(0).rstrip(".,;:!?)'\">")
        try:
            p = Path(os.path.expanduser(cand))
            if p.exists() and p.is_file():
                return cand
        except Exception:
            continue

    return None


def open_file_in_editor(
    path: str | Path,
    *,
    line: int | None = None,
    column: int | None = None,
    app: Any | None = None,
) -> bool:
    """Open an existing file in the user's preferred editor or OS default.

    Resolves the file path (stripping `file://`, expanding `~`, and trimming
    trailing sentence punctuation if needed). If $TALKOPS_EDITOR, $VISUAL,
    or $EDITOR is set, uses that editor command. For terminal-based editors
    (vim, nano, etc.), suspends the Textual app so the editor can control
    the terminal. For GUI editors (VS Code, Cursor, Zed, etc.), launches the
    editor asynchronously without blocking the TUI.

    If no editor environment variable is set, falls back to the OS default
    file opener (`open` on macOS, `xdg-open` on Linux, `start` on Windows).

    Args:
        path: Path or file:// URI to open.
        line: Optional line number to jump to.
        column: Optional column number.
        app: Optional Textual App instance (for suspending TUI or showing toasts).

    Returns:
        True if the editor was launched successfully, False otherwise.
    """
    raw, anchor_line = _strip_local_uri_scheme(str(path))
    if line is None and anchor_line is not None:
        line = anchor_line

    m = re.match(r"^(.*?):(\d+)(?::(\d+))?$", raw)
    if m:
        cand = m.group(1)
        if Path(os.path.expanduser(cand)).exists():
            raw = cand
            if line is None:
                line = int(m.group(2))
            if column is None and m.group(3):
                column = int(m.group(3))

    expanded = Path(os.path.expanduser(raw))
    if not expanded.exists():
        trimmed = raw.rstrip(".,;:!?)'\">")
        if Path(os.path.expanduser(trimmed)).exists():
            raw = trimmed
            expanded = Path(os.path.expanduser(trimmed))
        elif not trimmed.startswith("/") and Path("/" + trimmed).exists():
            raw = "/" + trimmed
            expanded = Path(raw)
        else:
            app_cwd = getattr(app, "_cwd", None) or getattr(app, "cwd", None)
            base_dirs = [Path(app_cwd)] if app_cwd else []
            base_dirs.append(Path.cwd())
            for base_dir in base_dirs:
                rel_candidate = base_dir / trimmed.lstrip("/")
                if rel_candidate.exists():
                    expanded = rel_candidate
                    break
                if trimmed.startswith(("/mnt/data/", "/workspace/")):
                    name_candidate = base_dir / Path(trimmed).name
                    if name_candidate.exists():
                        expanded = name_candidate
                        break

    target = expanded.resolve() if expanded.exists() else expanded
    if not target.exists():
        logger.warning("open_file_in_editor: file not found: %s", target)
        if app is not None and hasattr(app, "notify"):
            app.notify(f"File not found: {target.name}", severity="warning", timeout=5)
        return False

    editor_env = (
        os.environ.get("TALKOPS_EDITOR")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
    )

    cmd: list[str] | None = None
    if editor_env:
        cmd = shlex.split(editor_env)
    elif os.environ.get("VSCODE_PID"):
        if shutil.which("cursor"):
            cmd = ["cursor"]
        elif shutil.which("code"):
            cmd = ["code"]

    try:
        if cmd:
            exe = Path(cmd[0]).stem.lower()
            if exe in TERMINAL_EDITORS:
                args = list(cmd)
                if line is not None and exe in {"vi", "vim", "nvim", "nano", "pico"}:
                    args.append(f"+{line}")
                args.append(str(target))
                if app is not None and hasattr(app, "suspend"):
                    with app.suspend():
                        subprocess.run(args, check=False)
                else:
                    subprocess.run(args, check=False)
                return True
            else:
                args = [a for a in cmd if a not in ("--wait", "-w")]
                if (
                    exe in {"code", "cursor", "windsurf", "subl"}
                    and line is not None
                ):
                    loc = f"{target}:{line}"
                    if column is not None:
                        loc += f":{column}"
                    args.extend(["-g", loc])
                else:
                    args.append(str(target))
                subprocess.Popen(args, start_new_session=True)
                if app is not None and hasattr(app, "notify"):
                    app.notify(
                        f"Opening in editor: {target.name}",
                        severity="information",
                        timeout=3,
                    )
                return True

        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], start_new_session=True)
        elif sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(target)], start_new_session=True)
        else:
            if app is not None and hasattr(app, "suspend"):
                with app.suspend():
                    subprocess.run(["vi", str(target)], check=False)
            else:
                subprocess.run(["vi", str(target)], check=False)

        if app is not None and hasattr(app, "notify"):
            app.notify(
                f"Opening in editor: {target.name}",
                severity="information",
                timeout=3,
            )
        return True
    except Exception as exc:
        logger.warning("Failed to open file in editor %s: %s", target, exc, exc_info=True)
        if app is not None and hasattr(app, "notify"):
            app.notify(f"Could not open file: {target.name}", severity="error")
        return False


def _prepare_command(cmd: list[str], filepath: str) -> list[str]:
    """Build the full command list with appropriate flags.

    Adds --wait/-w for GUI editors and `-i NONE` for vim-family editors.

    Returns:
        The complete command list with flags and filepath appended.
    """
    cmd = list(cmd)  # copy
    exe = Path(cmd[0]).stem.lower()

    # Auto-inject wait flag for GUI editors
    if exe in GUI_WAIT_FLAG:
        flag = GUI_WAIT_FLAG[exe]
        if flag not in cmd:
            cmd.insert(1, flag)

    # Vim workaround: avoid viminfo errors in temp environments
    if exe in VIM_EDITORS and "-i" not in cmd:
        cmd.extend(["-i", "NONE"])

    cmd.append(filepath)
    return cmd


def open_in_editor(
    current_text: str,
    *,
    allow_empty: bool = False,
    raise_on_error: bool = False,
) -> str | None:
    """Open current_text in an external editor.

    Creates a temp .md file, launches the editor, and reads back the result.

    Args:
        current_text: The text to pre-populate in the editor.
        allow_empty: Return an empty or whitespace-only edited result instead of
            treating it as cancellation.
        raise_on_error: Re-raise editor launch and file errors instead of treating
            them as cancellation.

    Returns:
        The edited text with normalized line endings, or `None` if the editor
            exited with a non-zero status, returned blank text while `allow_empty`
            is false, or failed while `raise_on_error` is false.

    Raises:
        ExternalEditorError: If opening or reading the editor file fails while
            `raise_on_error` is true.
    """
    cmd = resolve_editor()
    if cmd is None:
        if raise_on_error:
            msg = "Editor command resolved to no arguments"
            raise ExternalEditorError(msg)
        return None

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".md",
            prefix="talkops-edit-",
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(current_text)

        full_cmd = _prepare_command(cmd, tmp_path)

        result = subprocess.run(  # noqa: S603
            full_cmd,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Editor exited with code %d: %s", result.returncode, full_cmd
            )
            if raise_on_error:
                msg = f"Editor exited with non-zero exit code {result.returncode}"
                raise ExternalEditorError(msg)
            return None

        edited = Path(tmp_path).read_text(encoding="utf-8")

        # Normalize line endings
        edited = edited.replace("\r\n", "\n").replace("\r", "\n")

        # Strip POSIX final newline so cursor lands on content
        edited = edited.removesuffix("\n")

        if not allow_empty and not edited.strip():
            return None

    except FileNotFoundError as exc:
        if raise_on_error:
            msg = "External editor executable or temporary file was not found"
            raise ExternalEditorError(msg) from exc
        return None
    except Exception as exc:
        logger.warning("Editor failed", exc_info=True)
        if raise_on_error:
            msg = "External editor failed"
            raise ExternalEditorError(msg) from exc
        return None
    else:
        return edited
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


__all__ = [
    "ExternalEditorError",
    "extract_file_path",
    "open_file_in_editor",
    "open_in_editor",
    "resolve_editor",
]
