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

"""Native LangChain tools for OpenWiki retrieval and compilation lifecycle.

Follows the same first-class LangChain tool + middleware architecture as
``GoalToolsMiddleware`` (``src/talkops/middleware/goal_tools.py``):
- All 10 OpenWiki tools are native Python ``@tool`` functions bound directly
  by ``OpenWikiContextMiddleware`` (no external Node/MCP stdio process required).
- The 4 read-only retrieval tools (``openwiki_search``, ``openwiki_read``,
  ``openwiki_list_wikis``, ``openwiki_list_workspaces``) are always available
  and update ``openwiki_grounded=True`` in LangGraph state via ``Command``.
- The 6 compilation state-machine tools (``openwiki_begin``, ``openwiki_submit_plan``,
  ``openwiki_next_page``, ``openwiki_inspect_page_claims``, ``openwiki_submit_page``,
  ``openwiki_finish``) are gated by ``OpenWikiContextMiddleware`` and disclosed
  automatically when ``/openwiki init`` or ``/openwiki update`` is invoked.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from langchain_core.tools import BaseTool, tool
from pydantic import Field

from talkops.middleware.memory_guard import OPENWIKI_MEMORY_END, OPENWIKI_MEMORY_START

OPENWIKI_RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "openwiki_search",
        "openwiki_read",
        "openwiki_list_wikis",
        "openwiki_list_workspaces",
    }
)

OPENWIKI_LIFECYCLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "openwiki_begin",
        "openwiki_submit_plan",
        "openwiki_next_page",
        "openwiki_inspect_page_claims",
        "openwiki_submit_page",
        "openwiki_finish",
    }
)

OPENWIKI_ALL_TOOL_NAMES: frozenset[str] = (
    OPENWIKI_RETRIEVAL_TOOL_NAMES | OPENWIKI_LIFECYCLE_TOOL_NAMES
)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")

DEFAULT_OPENWIKI_IGNORE = """# TalkOps OpenWiki Ignore Rules
dist/
build/
out/
target/
vendor/
.terraform/
*.tfstate
*.tfstate.*
.venv/
venv/
node_modules/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.next/
coverage/
*.lock
*.min.js
*.map
"""

DEFAULT_OPENWIKI_INSTRUCTIONS = """# Repository Conventions & Verification Rules

## Purpose
This file (`openwiki/INSTRUCTIONS.md`) defines standing conventions, verification commands,
and architectural invariants read by both OpenWiki during wiki compilation and TalkOps
agents at the start of every coding session.

## Architectural Notes
- Keep `openwiki/quickstart.md` concise so agents can route directly to subsystem pages.
- Ground architectural claims in concrete repository-relative file paths and component/symbol names.
- Verify changes using the project's native build, test, or infrastructure validation tooling before completion.
"""


def _build_repo_instructions(repo_root: Path) -> str:
    """Generate repository-appropriate ``openwiki/INSTRUCTIONS.md`` based on detected project files."""
    verification_lines: list[str] = []
    if (repo_root / "crossplane" / "scripts" / "validate.sh").is_file():
        verification_lines.append(
            "- Validate Crossplane XRDs & compositions: `crossplane/scripts/validate.sh`"
        )
    if any(repo_root.glob("**/*.tf")):
        verification_lines.append(
            "- Validate Terraform configurations: `terraform init -backend=false && terraform validate`"
        )
    if any(repo_root.glob("**/*.tftest.hcl")):
        verification_lines.append("- Run Terraform module tests: `terraform test`")
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "pytest.ini").is_file():
        verification_lines.append("- Run Python tests & linter: `pytest` / `ruff check`")
    if (repo_root / "package.json").is_file():
        verification_lines.append("- Run Node/TypeScript checks: `npm test` / `npm run build`")
    if (repo_root / "go.mod").is_file():
        verification_lines.append("- Run Go tests: `go test ./...`")
    if (repo_root / "Cargo.toml").is_file():
        verification_lines.append("- Run Rust checks: `cargo test`")

    if not verification_lines:
        return DEFAULT_OPENWIKI_INSTRUCTIONS

    joined_verifications = "\n".join(verification_lines)
    return (
        "# Repository Conventions & Verification Rules\n\n"
        "## Purpose\n"
        "This file (`openwiki/INSTRUCTIONS.md`) defines standing conventions, verification commands,\n"
        "and architectural invariants read by both OpenWiki during wiki compilation and TalkOps\n"
        "agents at the start of every coding session.\n\n"
        "## Verification Commands\n"
        f"{joined_verifications}\n\n"
        "## Architectural Notes\n"
        "- Keep `openwiki/quickstart.md` concise so agents can route directly to subsystem pages.\n"
        "- Ground architectural claims in concrete repository-relative file paths and component/symbol names.\n"
    )


def bootstrap_openwiki_scaffold(
    repo_root: str | Path | None = None,
    *,
    create_ignore: bool = True,
    create_instructions: bool = True,
) -> dict[str, Any]:
    """Prepare the repository for OpenWiki compilation and heal CLAUDE.md symlinks."""
    from talkops.middleware.openwiki import ensure_claude_agents_symlink_safe

    root = Path(repo_root or Path.cwd()).expanduser().resolve()
    symlink_healed = ensure_claude_agents_symlink_safe(root)

    openwiki_dir = root / "openwiki"
    openwiki_dir.mkdir(parents=True, exist_ok=True)

    created_files: list[str] = []
    ignore_path = root / ".openwikiignore"
    if create_ignore and not ignore_path.exists():
        ignore_path.write_text(DEFAULT_OPENWIKI_IGNORE, encoding="utf-8")
        created_files.append(str(ignore_path.relative_to(root)))

    instructions_path = openwiki_dir / "INSTRUCTIONS.md"
    if create_instructions and not instructions_path.exists():
        instructions_path.write_text(_build_repo_instructions(root), encoding="utf-8")
        created_files.append(str(instructions_path.relative_to(root)))

    return {
        "repo_root": str(root),
        "openwiki_dir": str(openwiki_dir),
        "symlink_healed": symlink_healed,
        "created_files": created_files,
    }


def _extract_recorded_commit(meta: dict[str, Any]) -> str:
    """Extract the recorded Git commit hash from ``.last-update.json`` (supporting both ``lastCommit`` and ``gitHead``)."""
    status = str(meta.get("status") or "").strip().lower()
    if status in {"interrupted", "failed"} and not meta.get("lastCommit"):
        return ""
    return str(meta.get("lastCommit") or meta.get("gitHead") or "").strip()


def inspect_openwiki_status(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Inspect the repository's ``openwiki/`` knowledge base and return status metadata."""
    root = Path(repo_root or Path.cwd()).expanduser().resolve()
    openwiki_dir = root / "openwiki"
    current_head = _git_head(root)

    if not openwiki_dir.is_dir():
        return {
            "initialized": False,
            "repo_root": str(root),
            "openwiki_dir": str(openwiki_dir),
            "page_count": 0,
            "pages": [],
            "claim_files": 0,
            "last_commit": "",
            "updated_at": "",
            "current_head": current_head,
            "is_drifted": False,
            "changed_files": [],
            "affected_pages": [],
            "has_quickstart": False,
            "has_instructions": False,
            "has_openwikiignore": (root / ".openwikiignore").is_file(),
            "active_run": False,
        }

    md_pages = sorted(
        p.stem for p in openwiki_dir.glob("*.md") if p.name.lower() != "instructions.md"
    )
    claims_dir = openwiki_dir / ".claims"
    claim_files = len(list(claims_dir.glob("*.json"))) if claims_dir.is_dir() else 0

    last_update_path = openwiki_dir / ".last-update.json"
    last_commit = ""
    updated_at = ""
    if last_update_path.is_file():
        try:
            meta = json.loads(last_update_path.read_text(encoding="utf-8"))
            last_commit = _extract_recorded_commit(meta)[:12]
            updated_at = str(
                meta.get("updatedAt")
                or meta.get("completedAt")
                or meta.get("timestamp")
                or ""
            )
        except (OSError, ValueError, TypeError):
            pass

    delta = compute_openwiki_codebase_delta(root) if md_pages else {}
    changed_files = list(delta.get("changed_files", []))
    affected_pages = list(delta.get("affected_pages", []))
    is_drifted = bool(
        delta.get("head_drifted")
        or changed_files
        or affected_pages
        or (
            last_commit
            and current_head
            and not current_head.startswith(last_commit[:7])
            and not last_commit.startswith(current_head[:7])
        )
    )

    return {
        "initialized": bool(md_pages),
        "repo_root": str(root),
        "openwiki_dir": str(openwiki_dir),
        "page_count": len(md_pages),
        "pages": md_pages,
        "claim_files": claim_files,
        "last_commit": last_commit,
        "updated_at": updated_at,
        "current_head": current_head,
        "is_drifted": is_drifted,
        "changed_files": changed_files,
        "affected_pages": affected_pages,
        "has_quickstart": (openwiki_dir / "quickstart.md").is_file(),
        "has_instructions": (openwiki_dir / "INSTRUCTIONS.md").is_file(),
        "has_openwikiignore": (root / ".openwikiignore").is_file(),
        "active_run": (openwiki_dir / ".run.json").is_file(),
    }


# ---------------------------------------------------------------------------
# Pure-Python OpenWiki Engine Helpers
# ---------------------------------------------------------------------------


def _stem_token(word: str) -> str:
    """Lightweight suffix stemmer for BM25 keyword normalization."""
    w = word.lower()
    if len(w) <= 3:
        return w
    for suffix in ("ing", "tion", "ions", "ments", "ment", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def _tokenize(text: str) -> list[str]:
    return [_stem_token(m.group(0)) for m in _TOKEN_RE.finditer(text.lower())]


def _git_head(repo_root: Path) -> str:
    """Resolve current short Git commit hash."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return ""


_IGNORED_DELTA_PREFIXES: tuple[str, ...] = (
    "openwiki/",
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "dist/",
    "build/",
)


def _is_ignored_delta_path(rel_path: str) -> bool:
    norm = rel_path.strip().lstrip("./")
    if not norm or norm == "openwiki" or norm == ".openwikiignore":
        return True
    return any(norm.startswith(prefix) for prefix in _IGNORED_DELTA_PREFIXES)


def _git_changed_files(repo_root: Path, since_commit: str) -> list[str]:
    """Return source files changed between ``since_commit`` and ``HEAD``."""
    if not since_commit:
        return []
    try:
        res = subprocess.run(
            ["git", "diff", "--name-only", since_commit, "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            return [
                line.strip()
                for line in res.stdout.splitlines()
                if line.strip() and not _is_ignored_delta_path(line.strip())
            ]
    except Exception:
        pass
    return []


def _git_working_tree_changes(repo_root: Path) -> list[str]:
    """Return uncommitted (staged, unstaged, or untracked) source files in the working tree."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode != 0:
            return []
        changed: set[str] = set()
        for raw_line in res.stdout.splitlines():
            if len(raw_line) < 4:
                continue
            path_part = raw_line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1].strip()
            path_part = path_part.strip('"')
            if path_part and not _is_ignored_delta_path(path_part):
                changed.add(path_part)
        return sorted(changed)
    except Exception:
        return []


def _git_recent_commits(repo_root: Path, since_commit: str, max_count: int = 5) -> list[str]:
    """Return concise one-line commit summaries between ``since_commit`` and ``HEAD``."""
    if not since_commit:
        return []
    try:
        res = subprocess.run(
            ["git", "log", "--oneline", f"-n{max_count}", f"{since_commit}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def _scan_repo_overview(repo_root: Path, max_entries: int = 80) -> list[str]:
    """Return a concise list of top-level and second-level source directories/files."""
    ignored = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
        "openwiki",
    }
    entries: list[str] = []
    try:
        for item in sorted(repo_root.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if item.name in ignored or item.name.startswith(".DS_Store"):
                continue
            rel = item.relative_to(repo_root)
            if item.is_dir():
                entries.append(f"{rel}/")
                for sub in sorted(item.iterdir(), key=lambda p: (not p.is_dir(), p.name))[:20]:
                    if sub.name in ignored:
                        continue
                    sub_rel = sub.relative_to(repo_root)
                    entries.append(f"  {sub_rel}{'/' if sub.is_dir() else ''}")
                    if len(entries) >= max_entries:
                        break
            else:
                entries.append(str(rel))
            if len(entries) >= max_entries:
                break
    except OSError:
        pass
    return entries


def parse_wiki_page(md_path: Path) -> dict[str, Any]:
    """Parse an OpenWiki Markdown file into YAML frontmatter, body, sections, and wikilinks."""
    raw = md_path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] = {}
    body = raw
    if raw.startswith("---\n"):
        end_idx = raw.find("\n---", 4)
        if end_idx != -1:
            fm_text = raw[4:end_idx]
            body = raw[end_idx + 4 :].lstrip("\n")
            try:
                parsed_fm = yaml.safe_load(fm_text)
                if isinstance(parsed_fm, dict):
                    frontmatter = parsed_fm
            except yaml.YAMLError:
                frontmatter = {}

    slug = str(frontmatter.get("slug") or md_path.stem)
    title = str(frontmatter.get("title") or slug.replace("-", " ").title())
    description = str(frontmatter.get("description") or "")
    updated_at = str(frontmatter.get("updated_at") or "")
    related_raw = frontmatter.get("related")
    related: list[str] = (
        [str(x) for x in related_raw if x] if isinstance(related_raw, list) else []
    )

    wikilinks = sorted({m.group(1).strip() for m in _WIKILINK_RE.finditer(body)})
    all_related = sorted(set(related) | set(wikilinks) - {slug})

    # Parse sections
    sections: list[dict[str, str]] = []
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        sections.append({"heading": title, "level": "1", "content": body.strip()})
    else:
        for i, match in enumerate(matches):
            level = str(len(match.group(1)))
            heading = match.group(2).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            sections.append(
                {
                    "heading": heading,
                    "level": level,
                    "content": body[start:end].strip(),
                }
            )

    return {
        "slug": slug,
        "title": title,
        "description": description,
        "updated_at": updated_at,
        "related": all_related,
        "frontmatter": frontmatter,
        "body": body,
        "raw": raw,
        "sections": sections,
        "path": str(md_path),
    }


def format_okf_markdown(
    *,
    slug: str,
    title: str,
    description: str,
    markdown_body: str,
    related: list[str] | None = None,
    page_type: str | None = None,
) -> str:
    """Format a wiki page with canonical Open Knowledge Format (OKF v0.2) frontmatter."""
    body = markdown_body.strip()
    # Strip existing frontmatter if the model included one in markdown_body
    if body.startswith("---\n"):
        end_idx = body.find("\n---", 4)
        if end_idx != -1:
            body = body[end_idx + 4 :].lstrip("\n")

    discovered_links = sorted({m.group(1).strip() for m in _WIKILINK_RE.finditer(body)})
    merged_related = sorted((set(related or []) | set(discovered_links)) - {slug})
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    effective_type = page_type or (
        "index" if slug == "index" else ("overview" if slug == "quickstart" else "architecture")
    )
    fm_dict: dict[str, Any] = {}
    if slug == "index":
        fm_dict["okf_version"] = "0.2"
    fm_dict.update(
        {
            "title": title,
            "slug": slug,
            "type": effective_type,
            "description": description,
            "updated_at": now_iso,
            "related": merged_related,
        }
    )
    fm_yaml = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm_yaml}\n---\n\n{body}\n"


def _compute_lines_sha256(lines: list[str]) -> str:
    normalized = "\n".join(line.rstrip("\r\n") for line in lines).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_EVIDENCE_RESOURCE_RE = re.compile(r"^repo://([^#]+?)(?:#L(\d+)(?:-L?(\d+))?)?$")


def verify_and_enrich_claim(repo_root: Path, claim: dict[str, Any]) -> dict[str, Any]:
    """Verify a Grounded Claim against the repository working tree and compute SHA-256 hashes.

    Supports both TalkOps claim schemas and upstream OpenWiki ``evidence`` URI schemas:
    - ``file_exists``: ``{"type": "file_exists", "path": "src/..."}``
    - ``file_lines``: ``{"type": "file_lines", "path": "src/...", "startLine": 10, "endLine": 25}``
    - ``symbol``: ``{"type": "symbol", "path": "src/...", "symbol": "MyClass"}``
    - ``evidence``: ``{"evidence": [{"resource": "repo://src/...#L10-L25", "version": "repo-lines-v1:sha256:..."}]}``
    """
    claim_type = str(claim.get("type") or "").strip()
    rel_path = str(claim.get("path") or claim.get("file") or "").strip()
    start_line_hint: int | None = None
    end_line_hint: int | None = None
    recorded_hash_hint: str = str(claim.get("contentHash") or "")

    if not rel_path and isinstance(claim.get("evidence"), list) and claim["evidence"]:
        first_ev = claim["evidence"][0]
        if isinstance(first_ev, dict):
            res_uri = str(first_ev.get("resource") or "").strip()
            m_ev = _EVIDENCE_RESOURCE_RE.match(res_uri)
            if m_ev:
                rel_path = m_ev.group(1).strip()
                if m_ev.group(2):
                    claim_type = claim_type or "file_lines"
                    start_line_hint = int(m_ev.group(2))
                    end_line_hint = int(m_ev.group(3) or m_ev.group(2))
                else:
                    claim_type = claim_type or "file_exists"
            ver_str = str(first_ev.get("version") or "").strip()
            if "sha256:" in ver_str and not recorded_hash_hint:
                recorded_hash_hint = ver_str.split("sha256:", 1)[1].strip()

    if not claim_type:
        claim_type = "file_exists"

    enriched = dict(claim)
    enriched["type"] = claim_type
    enriched["path"] = rel_path

    if not rel_path:
        enriched["status"] = "invalid"
        enriched["reason"] = "Missing 'path' in claim."
        return enriched

    target = (repo_root / rel_path).resolve()
    if not target.is_file():
        enriched["status"] = "missing_file"
        enriched["reason"] = f"File '{rel_path}' does not exist."
        return enriched

    if claim_type == "file_exists":
        enriched["status"] = "verified"
        return enriched

    try:
        file_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        enriched["status"] = "error"
        enriched["reason"] = str(exc)
        return enriched

    if claim_type == "file_lines":
        start_line = max(
            1,
            int(claim.get("startLine") or claim.get("start_line") or start_line_hint or 1),
        )
        end_line = max(
            start_line,
            int(
                claim.get("endLine")
                or claim.get("end_line")
                or end_line_hint
                or min(len(file_lines), start_line + 15)
            ),
        )
        end_line = min(len(file_lines), end_line)
        slice_lines = file_lines[start_line - 1 : end_line]
        current_hash = _compute_lines_sha256(slice_lines)
        recorded_hash = recorded_hash_hint

        if not recorded_hash or recorded_hash == current_hash:
            enriched["startLine"] = start_line
            enriched["endLine"] = end_line
            enriched["contentHash"] = current_hash
            enriched["status"] = "verified"
            return enriched

        # Fuzzy window search (+/- 60 lines) when a prior contentHash exists
        window_span = end_line - start_line + 1
        search_min = max(1, start_line - 60)
        search_max = min(len(file_lines) - window_span + 1, start_line + 60)
        for cand_start in range(search_min, max(search_min, search_max) + 1):
            cand_slice = file_lines[cand_start - 1 : cand_start - 1 + window_span]
            if _compute_lines_sha256(cand_slice) == recorded_hash:
                enriched["startLine"] = cand_start
                enriched["endLine"] = cand_start + window_span - 1
                enriched["contentHash"] = recorded_hash
                enriched["status"] = "fuzzy_relocated"
                return enriched

        enriched["status"] = "drifted"
        enriched["currentHash"] = current_hash
        return enriched

    if claim_type == "symbol":
        symbol = str(claim.get("symbol") or claim.get("name") or "").strip()
        if not symbol:
            enriched["status"] = "invalid"
            enriched["reason"] = "Missing 'symbol' name."
            return enriched
        sym_re = re.compile(
            rf"(?:\b(?:class|def|async\s+def|function|interface|type|const|let|var|func|fn|struct|enum|resource|module|variable|output|data|kind)\b[\s:\"\']*)?{re.escape(symbol)}\b"
        )
        for idx, line in enumerate(file_lines, start=1):
            if sym_re.search(line):
                enriched["symbol"] = symbol
                enriched["line"] = idx
                enriched["contentHash"] = _compute_lines_sha256([line])
                enriched["status"] = "verified"
                return enriched
        enriched["status"] = "drifted"
        enriched["reason"] = f"Symbol '{symbol}' not found in '{rel_path}'."
        return enriched

    enriched["status"] = "verified"
    return enriched


def compute_openwiki_codebase_delta(repo_root: str | Path) -> dict[str, Any]:
    """Compute live codebase delta between compiled OpenWiki state and the Git working tree.

    Evaluates:
    1. Committed file changes and commit one-liners between ``lastCommit``/``gitHead`` and ``HEAD``.
    2. Uncommitted working-tree/index/untracked source file changes (`git status --porcelain`).
    3. Grounded Claim SHA-256 verification and file-level overlap across ``openwiki/.claims/*.json``.
    """
    root = Path(repo_root).expanduser().resolve()
    openwiki_dir = root / "openwiki"
    claims_dir = openwiki_dir / ".claims"
    last_update_path = openwiki_dir / ".last-update.json"

    current_head = _git_head(root)
    recorded_head = ""
    if last_update_path.is_file():
        try:
            meta = json.loads(last_update_path.read_text(encoding="utf-8"))
            recorded_head = _extract_recorded_commit(meta)[:12]
        except Exception:
            recorded_head = ""

    head_drifted = bool(
        recorded_head
        and current_head
        and not current_head.startswith(recorded_head[:7])
        and not recorded_head.startswith(current_head[:7])
    )
    committed_changed = _git_changed_files(root, recorded_head) if head_drifted else []
    working_tree_changed = _git_working_tree_changes(root)
    changed_files = sorted(set(committed_changed) | set(working_tree_changed))
    changed_files_set = set(changed_files)
    recent_commits = _git_recent_commits(root, recorded_head, max_count=5) if head_drifted else []

    drifted_pages: dict[str, list[dict[str, Any]]] = {}
    pages_with_modified_files: dict[str, list[str]] = {}

    if openwiki_dir.is_dir():
        for md_path in sorted(openwiki_dir.glob("*.md")):
            if md_path.name.lower() in {"instructions.md", "index.md", "quickstart.md"}:
                continue
            slug = md_path.stem
            page_files: set[str] = set()
            claim_file = claims_dir / f"{slug}.json"
            if claim_file.is_file():
                try:
                    raw_claims = json.loads(claim_file.read_text(encoding="utf-8")).get("claims", [])
                    for c in raw_claims:
                        checked = verify_and_enrich_claim(root, c)
                        rel_p = str(checked.get("path") or "").strip().lstrip("./")
                        if rel_p:
                            page_files.add(rel_p)
                        if checked.get("status") not in {"verified", "fuzzy_relocated"}:
                            drifted_pages.setdefault(slug, []).append(checked)
                            if rel_p:
                                changed_files_set.add(rel_p)
                        elif checked.get("status") == "fuzzy_relocated" and rel_p:
                            changed_files_set.add(rel_p)
                except Exception:
                    pass

            if changed_files_set:
                try:
                    parsed = parse_wiki_page(md_path)
                    for match in re.finditer(r"`([^`\n]+)`", parsed["body"]):
                        token = match.group(1).strip().removeprefix("./")
                        if token and token in changed_files_set:
                            page_files.add(token)
                except Exception:
                    pass

            overlapping = sorted(page_files & changed_files_set)
            if overlapping:
                pages_with_modified_files[slug] = overlapping

    changed_files = sorted(changed_files_set)
    affected_pages = sorted(set(drifted_pages.keys()) | set(pages_with_modified_files.keys()))
    return {
        "recorded_head": recorded_head,
        "current_head": current_head,
        "head_drifted": head_drifted,
        "committed_changed_files": committed_changed,
        "working_tree_changed_files": working_tree_changed,
        "changed_files": changed_files,
        "recent_commits": recent_commits,
        "drifted_pages": drifted_pages,
        "pages_with_modified_files": pages_with_modified_files,
        "affected_pages": affected_pages,
    }


def upsert_agents_md_openwiki_pointer(repo_root: Path, pages: list[dict[str, Any]]) -> Path:
    """Insert or update the ``<!-- OPENWIKI:START --> ... <!-- OPENWIKI:END -->`` block in AGENTS.md."""
    from talkops.middleware.openwiki import ensure_claude_agents_symlink_safe

    ensure_claude_agents_symlink_safe(repo_root)
    agents_md = repo_root / "AGENTS.md"
    existing = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""

    _ = pages  # Page routing table is maintained in openwiki/quickstart.md to avoid duplicating it in AGENTS.md
    pointer_block = (
        f"{OPENWIKI_MEMORY_START}\n"
        "## OpenWiki\n\n"
        "This repository has a generated `openwiki/` evidence index. "
        "It is optional just-in-time context, not required startup reading.\n\n"
        "- Use `openwiki_search` for just-in-time context and `openwiki_read(slug, section=...)` for relevant sections; "
        "see [`openwiki/quickstart.md`](openwiki/quickstart.md) for the subsystem routing table.\n"
        "- Treat source code and tests as authoritative, and stop querying OpenWiki once the question is grounded.\n"
        f"{OPENWIKI_MEMORY_END}"
    )

    start_idx = existing.find(OPENWIKI_MEMORY_START)
    end_idx = existing.find(OPENWIKI_MEMORY_END)
    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        end_idx += len(OPENWIKI_MEMORY_END)
        prefix = existing[:start_idx].rstrip()
        suffix = existing[end_idx:].strip()
        parts = [p for p in (prefix, pointer_block, suffix) if p]
        updated = "\n\n".join(parts).rstrip() + "\n"
    else:
        base = existing.rstrip()
        updated = f"{base}\n\n{pointer_block}\n" if base else f"{pointer_block}\n"

    agents_md.write_text(updated, encoding="utf-8")
    return agents_md


def search_openwiki_pages(
    openwiki_dir: Path,
    query: str,
    *,
    max_results: int = 8,
) -> dict[str, Any]:
    """Execute BM25 + title/heading boost + 1-hop wikilink graph expansion over ``openwiki/*.md``."""
    if not openwiki_dir.is_dir():
        return {"query": query, "results": [], "total_pages": 0}

    md_files = sorted(
        p for p in openwiki_dir.glob("*.md") if p.name.lower() != "instructions.md"
    )
    if not md_files:
        return {"query": query, "results": [], "total_pages": 0}

    pages = [parse_wiki_page(p) for p in md_files]
    q_tokens = _tokenize(query)
    if not q_tokens:
        return {
            "query": query,
            "total_pages": len(pages),
            "results": [
                {
                    "slug": p["slug"],
                    "title": p["title"],
                    "description": p["description"],
                    "score": 1.0,
                    "sections": [s["heading"] for s in p["sections"][:6]],
                    "related": p["related"],
                }
                for p in pages[:max_results]
            ],
        }

    # Build document token statistics for BM25
    doc_tokens: dict[str, list[str]] = {}
    df: dict[str, int] = {}
    for p in pages:
        toks = _tokenize(f"{p['slug']} {p['title']} {p['description']} {p['body']}")
        doc_tokens[p["slug"]] = toks
        for ut in set(toks):
            df[ut] = df.get(ut, 0) + 1

    n_docs = max(1, len(pages))
    avgdl = sum(len(t) for t in doc_tokens.values()) / n_docs or 1.0
    k1 = 1.5
    b = 0.75

    raw_scores: dict[str, float] = {}
    matched_sections: dict[str, list[dict[str, str]]] = {}

    for p in pages:
        slug = p["slug"]
        toks = doc_tokens[slug]
        dl = len(toks) or 1
        tf_map: dict[str, int] = {}
        for t in toks:
            tf_map[t] = tf_map.get(t, 0) + 1

        score = 0.0
        slug_title_toks = set(_tokenize(f"{p['slug']} {p['title']}"))
        desc_toks = set(_tokenize(p["description"]))

        for qt in q_tokens:
            tf = tf_map.get(qt, 0)
            if tf > 0:
                idf = math.log(1.0 + (n_docs - df.get(qt, 0) + 0.5) / (df.get(qt, 0) + 0.5))
                score += idf * ((tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (dl / avgdl))))
            if qt in slug_title_toks:
                score += 3.5
            if qt in desc_toks:
                score += 1.5

        # Score individual sections
        sec_hits: list[tuple[float, dict[str, str]]] = []
        for sec in p["sections"]:
            h_toks = set(_tokenize(sec["heading"]))
            c_toks = _tokenize(sec["content"])
            c_set = set(c_toks)
            sec_score = 0.0
            for qt in q_tokens:
                if qt in h_toks:
                    sec_score += 2.2
                if qt in c_set:
                    sec_score += 0.8
            if sec_score > 0:
                score += sec_score * 0.5
                snippet = sec["content"][:320].replace("\n", " ").strip()
                sec_hits.append(
                    (
                        sec_score,
                        {"heading": sec["heading"], "snippet": snippet},
                    )
                )
        sec_hits.sort(key=lambda item: item[0], reverse=True)
        matched_sections[slug] = [item[1] for item in sec_hits[:3]]
        raw_scores[slug] = score

    # 1-hop graph expansion across [[slug]] wikilinks
    expanded_scores = dict(raw_scores)
    page_by_slug = {p["slug"]: p for p in pages}
    for slug, base_score in raw_scores.items():
        if base_score <= 0:
            continue
        for neighbor in page_by_slug[slug]["related"]:
            if neighbor in expanded_scores:
                expanded_scores[neighbor] += base_score * 0.25

    ranked = sorted(
        (p for p in pages if expanded_scores.get(p["slug"], 0.0) > 0),
        key=lambda p: expanded_scores[p["slug"]],
        reverse=True,
    )
    if not ranked:
        ranked = pages[:max_results]

    results = []
    for p in ranked[:max_results]:
        slug = p["slug"]
        results.append(
            {
                "slug": slug,
                "title": p["title"],
                "description": p["description"],
                "score": round(expanded_scores.get(slug, 0.0), 3),
                "matched_sections": matched_sections.get(slug)
                or [
                    {
                        "heading": s["heading"],
                        "snippet": s["content"][:240].replace("\n", " ").strip(),
                    }
                    for s in p["sections"][:2]
                ],
                "all_sections": [s["heading"] for s in p["sections"]],
                "related": p["related"],
            }
        )

    return {
        "query": query,
        "total_pages": len(pages),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Factory: Build the 10 LangChain StructuredTools Bound to repo_root
# ---------------------------------------------------------------------------


def build_openwiki_tools(repo_root: str | Path) -> list[BaseTool]:
    """Create the 10 native LangChain OpenWiki tools bound to ``repo_root``."""
    root = Path(repo_root).expanduser().resolve()
    openwiki_dir = root / "openwiki"
    claims_dir = openwiki_dir / ".claims"
    run_file = openwiki_dir / ".run.json"
    last_update_file = openwiki_dir / ".last-update.json"

    @tool
    def openwiki_search(
        query: Annotated[
            str,
            Field(description="Keywords, symbol names, or architectural concepts to search for."),
        ],
        max_results: Annotated[
            int,
            Field(default=8, description="Maximum number of ranked wiki pages to return."),
        ] = 8,
    ) -> str:
        """Search the repository's OpenWiki knowledge base using BM25 + graph expansion.

        Use this tool first when exploring repository architecture, locating subsystem
        entry points, or checking module conventions before editing files.
        """
        payload = search_openwiki_pages(openwiki_dir, query, max_results=max_results)
        return json.dumps(payload, indent=2)

    @tool
    def openwiki_read(
        slug: Annotated[
            str,
            Field(description="Page slug to read (e.g. 'index', 'quickstart', 'middleware')."),
        ],
        section: Annotated[
            str | None,
            Field(
                default=None,
                description="Optional section heading to read instead of the entire page.",
            ),
        ] = None,
    ) -> str:
        """Read a compiled OpenWiki page or a specific section by slug.

        Pass ``section`` to load only the relevant section and conserve context tokens.
        """
        clean_slug = slug.strip().removesuffix(".md")
        md_path = openwiki_dir / f"{clean_slug}.md"
        if not md_path.is_file():
            available = sorted(
                p.stem for p in openwiki_dir.glob("*.md") if p.name.lower() != "instructions.md"
            )
            err_payload = {
                "error": f"Wiki page '{clean_slug}' not found in {openwiki_dir}.",
                "available_slugs": available,
            }
            return json.dumps(err_payload, indent=2)

        parsed = parse_wiki_page(md_path)
        selected_content = parsed["body"]
        matched_heading = None
        if section:
            sec_lower = section.strip().lower()
            for sec in parsed["sections"]:
                if sec_lower in sec["heading"].lower():
                    selected_content = sec["content"]
                    matched_heading = sec["heading"]
                    break

        claim_path = claims_dir / f"{clean_slug}.json"
        claim_count = 0
        resolved_source_set: set[str] = set()
        relative_source_set: set[str] = set()
        drifted_claims: list[dict[str, Any]] = []
        if claim_path.is_file():
            try:
                claim_data = json.loads(claim_path.read_text(encoding="utf-8"))
                claims_list = claim_data.get("claims", [])
                claim_count = len(claims_list)
                for c in claims_list:
                    checked = verify_and_enrich_claim(root, c)
                    rel_p = str(checked.get("path", "") or "").strip()
                    if rel_p:
                        relative_source_set.add(rel_p.lstrip("./"))
                        abs_p = (root / rel_p).resolve()
                        if abs_p.is_file():
                            resolved_source_set.add(str(abs_p))
                    if checked.get("status") not in {"verified", "fuzzy_relocated"}:
                        drifted_claims.append(checked)
            except Exception:
                claim_count = 0

        for match in re.finditer(r"`([^`\n]+)`", parsed["body"]):
            token = match.group(1).strip().removeprefix("./")
            if not token or " " in token or token.startswith("-"):
                continue
            cand = (root / token).resolve()
            if cand.is_file():
                resolved_source_set.add(str(cand))
                try:
                    relative_source_set.add(str(cand.relative_to(root)))
                except ValueError:
                    pass

        delta = compute_openwiki_codebase_delta(root)
        changed_files_set = set(delta.get("changed_files", []))
        modified_source_files = sorted(relative_source_set & changed_files_set)

        result_payload: dict[str, Any] = {
            "slug": parsed["slug"],
            "title": parsed["title"],
            "description": parsed["description"],
            "repo_root": str(root),
            "updated_at": parsed["updated_at"],
            "related": parsed["related"],
            "sections": [s["heading"] for s in parsed["sections"]],
            "selected_section": matched_heading,
            "claim_count": claim_count,
            "verified_source_files": sorted(resolved_source_set),
            "modified_source_files": modified_source_files,
            "drifted_claims": drifted_claims,
            "content": selected_content,
        }
        if modified_source_files or drifted_claims:
            result_payload["freshness_warning"] = (
                "One or more source files or Grounded Claims referenced by this wiki page "
                "have been modified since the page was compiled. Read the live source files "
                "directly with `read_file` as the authoritative source of truth."
            )
        return json.dumps(result_payload, indent=2)

    @tool
    def openwiki_list_wikis() -> str:
        """List all compiled pages in the repository's OpenWiki knowledge base."""
        if not openwiki_dir.is_dir():
            return json.dumps(
                {"initialized": False, "pages": [], "repo_root": str(root)},
                indent=2,
            )

        pages_summary = []
        for md_path in sorted(openwiki_dir.glob("*.md")):
            if md_path.name.lower() == "instructions.md":
                continue
            p = parse_wiki_page(md_path)
            pages_summary.append(
                {
                    "slug": p["slug"],
                    "title": p["title"],
                    "description": p["description"],
                    "updated_at": p["updated_at"],
                    "related": p["related"],
                    "sections": [s["heading"] for s in p["sections"]],
                }
            )

        last_commit = ""
        if last_update_file.is_file():
            try:
                last_commit = _extract_recorded_commit(
                    json.loads(last_update_file.read_text(encoding="utf-8"))
                )
            except Exception:
                last_commit = ""

        payload = {
            "initialized": bool(pages_summary),
            "repo_root": str(root),
            "last_commit": last_commit,
            "current_head": _git_head(root),
            "page_count": len(pages_summary),
            "pages": pages_summary,
        }
        return json.dumps(payload, indent=2)

    @tool
    def openwiki_list_workspaces() -> str:
        """List federated OpenWiki workspaces or the active single-repository workspace."""
        ws_file = root / ".openwiki-workspace.json"
        if ws_file.is_file():
            try:
                data = json.loads(ws_file.read_text(encoding="utf-8"))
                return json.dumps({"workspace_file": str(ws_file), "config": data}, indent=2)
            except Exception as exc:
                return json.dumps({"error": f"Failed to parse {ws_file}: {exc}"}, indent=2)
        has_compiled_wiki = bool(
            (openwiki_dir / "quickstart.md").is_file()
            or (openwiki_dir / "index.md").is_file()
        )
        return json.dumps(
            {
                "mode": "single-repository",
                "repo_root": str(root),
                "openwiki_dir": str(openwiki_dir),
                "initialized": has_compiled_wiki,
            },
            indent=2,
        )

    @tool
    def openwiki_begin(
        mode: Annotated[
            Literal["init", "update"],
            Field(description="'init' for full wiki compilation or 'update' for incremental sync."),
        ] = "init",
        brief: Annotated[
            str,
            Field(default="", description="Optional focus instructions for wiki compilation."),
        ] = "",
        force: Annotated[
            bool,
            Field(default=False, description="Force full recompilation even if up to date."),
        ] = False,
        repoPath: Annotated[  # noqa: N803
            str | None,
            Field(default=None, description="Optional repository root path override."),
        ] = None,
    ) -> str:
        """Step 1 of the OpenWiki compilation state machine: start an init or update run.

        Creates ``openwiki/.run.json``, evaluates Git commit diff and Grounded Claims
        drift, and returns the Planning Packet for ``openwiki_submit_plan``.
        """
        del repoPath  # Bound to root for workspace safety
        bootstrap_openwiki_scaffold(root)
        openwiki_dir.mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)

        current_head = _git_head(root)
        last_commit = ""
        if last_update_file.is_file():
            try:
                last_commit = _extract_recorded_commit(
                    json.loads(last_update_file.read_text(encoding="utf-8"))
                )
            except Exception:
                last_commit = ""

        committed_changed = _git_changed_files(root, last_commit) if last_commit else []
        working_tree_changed = _git_working_tree_changes(root)
        changed_files = sorted(set(committed_changed) | set(working_tree_changed))

        # Evaluate existing claims across all pages
        drifted_pages: dict[str, list[dict[str, Any]]] = {}
        existing_pages: list[dict[str, Any]] = []
        for md_path in sorted(openwiki_dir.glob("*.md")):
            if md_path.name.lower() in {"instructions.md", "index.md", "quickstart.md"}:
                continue
            parsed = parse_wiki_page(md_path)
            slug = parsed["slug"]
            existing_pages.append(
                {
                    "slug": slug,
                    "title": parsed["title"],
                    "description": parsed["description"],
                }
            )
            claim_file = claims_dir / f"{slug}.json"
            if claim_file.is_file():
                try:
                    raw_claims = json.loads(claim_file.read_text(encoding="utf-8")).get(
                        "claims", []
                    )
                    for c in raw_claims:
                        checked = verify_and_enrich_claim(root, c)
                        if checked.get("status") not in {"verified", "fuzzy_relocated"}:
                            drifted_pages.setdefault(slug, []).append(checked)
                            rel_p = str(checked.get("path") or "").strip().lstrip("./")
                            if rel_p and rel_p not in changed_files:
                                changed_files.append(rel_p)
                except Exception:
                    pass

        if (
            mode == "update"
            and not force
            and existing_pages
            and last_commit
            and current_head
            and current_head.startswith(last_commit[:7])
            and not changed_files
            and not drifted_pages
        ):
            return json.dumps(
                {
                    "status": "up_to_date",
                    "lastCommit": last_commit,
                    "currentHead": current_head,
                    "message": "OpenWiki is already synchronized with current Git HEAD and all Grounded Claims are verified.",
                },
                indent=2,
            )

        instructions_path = openwiki_dir / "INSTRUCTIONS.md"
        instructions_text = (
            instructions_path.read_text(encoding="utf-8") if instructions_path.is_file() else ""
        )

        run_state = {
            "phase": "planning",
            "mode": mode,
            "brief": brief,
            "force": force,
            "startedAt": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "targetCommit": current_head,
            "previousCommit": last_commit,
            "changedFiles": changed_files,
            "driftedPages": drifted_pages,
            "existingPages": existing_pages,
            "plannedPages": [],
            "completedPages": [],
        }
        run_file.write_text(json.dumps(run_state, indent=2), encoding="utf-8")

        return json.dumps(
            {
                "status": "ready_for_plan",
                "mode": mode,
                "repoRoot": str(root),
                "targetCommit": current_head,
                "previousCommit": last_commit,
                "planningPacket": {
                    "brief": brief,
                    "instructions": instructions_text,
                    "repoOverview": _scan_repo_overview(root),
                    "changedFiles": changed_files,
                    "driftedPages": drifted_pages,
                    "existingPages": existing_pages,
                },
                "nextStep": (
                    "Explore key directories with glob/read_file if needed, then call "
                    "`openwiki_submit_plan(pages=[...])` with 3-8 subsystem pages."
                ),
            },
            indent=2,
        )

    @tool
    def openwiki_submit_plan(
        pages: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "List of planned wiki pages. Each item must contain: "
                    "{'slug': 'kebab-case-slug', 'title': 'Page Title', "
                    "'description': '1-sentence summary', 'source_files': ['src/...']}."
                )
            ),
        ],
    ) -> str:
        """Step 2 of the OpenWiki compilation state machine: submit the page compilation plan."""
        if not run_file.is_file():
            return json.dumps(
                {"error": "No active OpenWiki run (.run.json). Call `openwiki_begin` first."},
                indent=2,
            )

        run_state = json.loads(run_file.read_text(encoding="utf-8"))
        normalized_pages: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()
        for idx, item in enumerate(pages):
            raw_slug = str(item.get("slug") or f"page-{idx + 1}").strip().lower()
            clean_slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug).strip("-")
            if not clean_slug or clean_slug in {"index", "quickstart"} or clean_slug in seen_slugs:
                continue
            seen_slugs.add(clean_slug)
            normalized_pages.append(
                {
                    "slug": clean_slug,
                    "title": str(item.get("title") or clean_slug.replace("-", " ").title()),
                    "description": str(item.get("description") or ""),
                    "source_files": [str(f) for f in (item.get("source_files") or item.get("files") or [])],
                    "sections": [str(s) for s in (item.get("sections") or [])],
                }
            )

        if not normalized_pages:
            return json.dumps(
                {
                    "error": "Plan must contain at least one subsystem page (excluding 'index' and 'quickstart', which are synthesized automatically by openwiki_finish)."
                },
                indent=2,
            )

        run_state["phase"] = "writing"
        run_state["plannedPages"] = normalized_pages
        run_state["completedPages"] = []
        run_file.write_text(json.dumps(run_state, indent=2), encoding="utf-8")

        return json.dumps(
            {
                "status": "plan_accepted",
                "plannedPageCount": len(normalized_pages),
                "plannedSlugs": [p["slug"] for p in normalized_pages],
                "nextStep": "Call `openwiki_next_page()` to fetch the brief for the first page.",
            },
            indent=2,
        )

    @tool
    def openwiki_next_page() -> str:
        """Step 3 of the OpenWiki compilation state machine: retrieve the next page brief to write."""
        if not run_file.is_file():
            return json.dumps(
                {"error": "No active OpenWiki run (.run.json). Call `openwiki_begin` first."},
                indent=2,
            )

        run_state = json.loads(run_file.read_text(encoding="utf-8"))
        planned: list[dict[str, Any]] = run_state.get("plannedPages", [])
        completed: set[str] = set(run_state.get("completedPages", []))

        for page_spec in planned:
            slug = page_spec["slug"]
            if slug not in completed:
                existing_md_path = openwiki_dir / f"{slug}.md"
                existing_markdown = (
                    existing_md_path.read_text(encoding="utf-8")
                    if existing_md_path.is_file()
                    else ""
                )
                return json.dumps(
                    {
                        "done": False,
                        "remaining": len(planned) - len(completed),
                        "pageBrief": page_spec,
                        "allPlannedSlugs": [p["slug"] for p in planned],
                        "existingMarkdown": existing_markdown[:4000] if existing_markdown else None,
                        "nextStep": (
                            f"Inspect the source files for '{slug}' (and call "
                            f"`openwiki_inspect_page_claims(slug='{slug}')` if updating), then "
                            f"submit the page via `openwiki_submit_page(slug='{slug}', ...)`."
                        ),
                    },
                    indent=2,
                )

        return json.dumps(
            {
                "done": True,
                "completedCount": len(completed),
                "completedSlugs": sorted(completed),
                "nextStep": "All planned pages have been written! Call `openwiki_finish()` now.",
            },
            indent=2,
        )

    @tool
    def openwiki_inspect_page_claims(
        slug: Annotated[
            str,
            Field(description="Slug of the wiki page whose Grounded Claims should be verified."),
        ],
    ) -> str:
        """Step 4 of the OpenWiki compilation state machine: inspect and verify Grounded Claims for a page."""
        clean_slug = slug.strip().removesuffix(".md")
        claim_file = claims_dir / f"{clean_slug}.json"
        if not claim_file.is_file():
            return json.dumps(
                {
                    "slug": clean_slug,
                    "hasExistingClaims": False,
                    "claims": [],
                },
                indent=2,
            )

        try:
            raw_data = json.loads(claim_file.read_text(encoding="utf-8"))
            claims_list = raw_data.get("claims", [])
        except Exception as exc:
            return json.dumps({"slug": clean_slug, "error": str(exc)}, indent=2)

        evaluated = [verify_and_enrich_claim(root, c) for c in claims_list]
        summary = {
            "verified": sum(1 for c in evaluated if c.get("status") == "verified"),
            "fuzzy_relocated": sum(1 for c in evaluated if c.get("status") == "fuzzy_relocated"),
            "drifted": sum(1 for c in evaluated if c.get("status") == "drifted"),
            "missing_file": sum(1 for c in evaluated if c.get("status") == "missing_file"),
        }
        return json.dumps(
            {
                "slug": clean_slug,
                "hasExistingClaims": True,
                "summary": summary,
                "claims": evaluated,
            },
            indent=2,
        )

    @tool
    def openwiki_submit_page(
        slug: Annotated[
            str,
            Field(description="Kebab-case slug for the page (e.g. 'agent-factory', 'middleware')."),
        ],
        title: Annotated[
            str,
            Field(description="Human-readable page title."),
        ],
        description: Annotated[
            str,
            Field(description="Concise 1-sentence summary of what this page covers."),
        ],
        markdown: Annotated[
            str,
            Field(
                description=(
                    "Full Markdown body for the page using ## headings, [[slug]] wikilinks, "
                    "and Mermaid diagrams where appropriate."
                )
            ),
        ],
        related: Annotated[
            list[str] | None,
            Field(default=None, description="List of related page slugs."),
        ] = None,
        claims: Annotated[
            list[dict[str, Any]] | None,
            Field(
                default=None,
                description=(
                    "Grounded Claims anchoring this page to source code, e.g. "
                    "[{'type': 'file_exists', 'path': 'src/...'}, "
                    "{'type': 'file_lines', 'path': 'src/...', 'startLine': 1, 'endLine': 40}, "
                    "{'type': 'symbol', 'path': 'src/...', 'symbol': 'ClassName'}]."
                ),
            ),
        ] = None,
    ) -> str:
        """Step 5 of the OpenWiki compilation state machine: write an OKF v0.2 page and its SHA-256 Grounded Claims."""
        clean_slug = re.sub(r"[^a-z0-9-]+", "-", slug.strip().lower()).strip("-")
        if not clean_slug:
            return json.dumps({"error": "Invalid empty slug."}, indent=2)

        openwiki_dir.mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)

        formatted_md = format_okf_markdown(
            slug=clean_slug,
            title=title.strip(),
            description=description.strip(),
            markdown_body=markdown,
            related=related,
        )
        md_path = openwiki_dir / f"{clean_slug}.md"
        md_path.write_text(formatted_md, encoding="utf-8")

        verified_claims = [verify_and_enrich_claim(root, c) for c in (claims or [])]
        claim_payload = {
            "slug": clean_slug,
            "updatedAt": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commit": _git_head(root),
            "claims": verified_claims,
        }
        claim_path = claims_dir / f"{clean_slug}.json"
        claim_path.write_text(json.dumps(claim_payload, indent=2), encoding="utf-8")

        remaining = 0
        if run_file.is_file():
            try:
                run_state = json.loads(run_file.read_text(encoding="utf-8"))
                completed = list(run_state.get("completedPages", []))
                if clean_slug not in completed:
                    completed.append(clean_slug)
                run_state["completedPages"] = completed
                planned_slugs = [p["slug"] for p in run_state.get("plannedPages", [])]
                remaining = len([s for s in planned_slugs if s not in completed])
                run_file.write_text(json.dumps(run_state, indent=2), encoding="utf-8")
            except Exception:
                pass

        return json.dumps(
            {
                "status": "page_saved",
                "slug": clean_slug,
                "path": str(md_path),
                "claimsSaved": len(verified_claims),
                "verifiedClaims": sum(
                    1 for c in verified_claims if c.get("status") == "verified"
                ),
                "remainingPlannedPages": remaining,
                "nextStep": (
                    "Call `openwiki_next_page()` for the next page."
                    if remaining > 0
                    else "All planned pages written! Call `openwiki_finish()` to finalize."
                ),
            },
            indent=2,
        )

    @tool
    def openwiki_finish(
        quickstart_markdown: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Optional custom Markdown body for openwiki/quickstart.md. "
                    "If omitted, a routing table is synthesized automatically from compiled pages."
                ),
            ),
        ] = "",
    ) -> str:
        """Step 6 of the OpenWiki compilation state machine: validate links, generate index/quickstart, and finalize."""
        openwiki_dir.mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)

        subsystem_pages: list[dict[str, Any]] = []
        for md_path in sorted(openwiki_dir.glob("*.md")):
            if md_path.name.lower() in {"instructions.md", "index.md", "quickstart.md"}:
                continue
            subsystem_pages.append(parse_wiki_page(md_path))

        known_slugs = {p["slug"] for p in subsystem_pages} | {"index", "quickstart"}
        broken_links: list[dict[str, str]] = []
        for p in subsystem_pages:
            for target_slug in p["related"]:
                if target_slug not in known_slugs:
                    broken_links.append({"from": p["slug"], "missing_target": target_slug})

        # 1. Generate openwiki/index.md
        index_lines = [
            "# OpenWiki Knowledge Base Index",
            "",
            "Welcome to the repository's durable architectural knowledge base.",
            "",
            "## Entry Points",
            "- [[quickstart]] — **Task-Routing Table & Developer Quickstart**",
            "- `openwiki/INSTRUCTIONS.md` — **Standing Repository Conventions & Verification Commands**",
            "",
            "## Subsystem Pages",
        ]
        for p in subsystem_pages:
            index_lines.append(f"- [[{p['slug']}]] — **{p['title']}**: {p['description']}")

        index_md = format_okf_markdown(
            slug="index",
            title="Repository Knowledge Base Index",
            description="Master index of all compiled OpenWiki architectural pages.",
            markdown_body="\n".join(index_lines),
            related=["quickstart", *[p["slug"] for p in subsystem_pages]],
        )
        (openwiki_dir / "index.md").write_text(index_md, encoding="utf-8")

        # 2. Generate openwiki/quickstart.md
        if quickstart_markdown.strip():
            qs_body = quickstart_markdown.strip()
        else:
            qs_lines = [
                "# Architecture Routing Table & Quickstart",
                "",
                "Use this routing table to jump directly to the relevant subsystem page via "
                "`openwiki_read(slug, section=...)` or `openwiki_search(query)`.",
                "",
                "| Subsystem | Wiki Page | Key Sections | Summary |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for p in subsystem_pages:
                sec_list = ", ".join(f"`{s['heading']}`" for s in p["sections"][:4])
                qs_lines.append(
                    f"| **{p['title']}** | [[{p['slug']}]] | {sec_list} | {p['description']} |"
                )
            qs_body = "\n".join(qs_lines)

        quickstart_md = format_okf_markdown(
            slug="quickstart",
            title="Architecture Routing Table & Quickstart",
            description="Task-routing map linking repository subsystems to OpenWiki pages.",
            markdown_body=qs_body,
            related=["index", *[p["slug"] for p in subsystem_pages]],
        )
        (openwiki_dir / "quickstart.md").write_text(quickstart_md, encoding="utf-8")

        # 3. Record openwiki/.last-update.json and clean up .run.json
        current_head = _git_head(root)
        now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        total_claims = 0
        for cf in claims_dir.glob("*.json"):
            try:
                total_claims += len(
                    json.loads(cf.read_text(encoding="utf-8")).get("claims", [])
                )
            except Exception:
                pass

        last_update_payload = {
            "lastCommit": current_head,
            "gitHead": current_head,
            "status": "completed",
            "updatedAt": now_iso,
            "pageCount": len(subsystem_pages) + 2,
            "pages": ["index", "quickstart", *[p["slug"] for p in subsystem_pages]],
            "totalClaims": total_claims,
            "brokenLinks": broken_links,
        }
        last_update_file.write_text(
            json.dumps(last_update_payload, indent=2),
            encoding="utf-8",
        )
        if run_file.is_file():
            run_file.unlink(missing_ok=True)

        # 4. Update <!-- OPENWIKI:START --> pointer block in AGENTS.md
        agents_md_path = upsert_agents_md_openwiki_pointer(root, subsystem_pages)

        summary_payload = {
            "status": "completed",
            "repoRoot": str(root),
            "lastCommit": current_head,
            "updatedAt": now_iso,
            "compiledPages": last_update_payload["pages"],
            "totalClaims": total_claims,
            "brokenLinks": broken_links,
            "agentsMdUpdated": str(agents_md_path),
        }
        return json.dumps(summary_payload, indent=2)

    return [
        openwiki_search,
        openwiki_read,
        openwiki_list_wikis,
        openwiki_list_workspaces,
        openwiki_begin,
        openwiki_submit_plan,
        openwiki_next_page,
        openwiki_inspect_page_claims,
        openwiki_submit_page,
        openwiki_finish,
    ]


def compile_openwiki_deterministic(
    repo_root: str | Path,
    *,
    mode: Literal["init", "update"] = "init",
    brief: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Deterministically compile or update ``openwiki/`` using the native Python state machine.

    Used by ``talkops openwiki init`` / ``talkops openwiki update`` in headless/CLI
    environments (where no interactive TTY or LLM loop is running) and as a fallback
    baseline compiler.
    """
    root = Path(repo_root).expanduser().resolve()
    tools_by_name = {t.name: t for t in build_openwiki_tools(root)}

    begin_raw = tools_by_name["openwiki_begin"].invoke(
        {"mode": mode, "brief": brief, "force": force}
    )
    begin_data = json.loads(begin_raw)
    if begin_data.get("status") == "up_to_date":
        return begin_data

    # Discover subsystem directories under src/ (or top-level packages)
    src_root = root / "src"
    pkg_dirs: list[Path] = []
    if src_root.is_dir():
        for child in sorted(src_root.iterdir()):
            if child.is_dir() and not child.name.startswith((".", "_")) and not child.name.endswith(".egg-info"):
                sub_dirs = [
                    d
                    for d in sorted(child.iterdir())
                    if d.is_dir() and not d.name.startswith((".", "_"))
                ]
                pkg_dirs.extend(sub_dirs or [child])
    else:
        pkg_dirs = [
            d
            for d in sorted(root.iterdir())
            if d.is_dir()
            and d.name not in {".git", ".venv", "venv", "node_modules", "openwiki", "dist", "build", "__pycache__"}
            and not d.name.startswith(".")
        ]

    # Prioritize core architectural subsystems first, then largest directories by source/config file count
    priority_names = {
        "agent": 0,
        "middleware": 1,
        "tools": 2,
        "commands": 3,
        "cli": 4,
        "state": 5,
        "skills": 6,
        "memory": 7,
        "mcp": 8,
        "ui": 9,
    }
    supported_exts = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".tf",
        ".hcl",
        ".yaml",
        ".yml",
        ".sh",
        ".rb",
        ".md",
    }

    def _collect_dir_source_files(directory: Path) -> list[Path]:
        files = [
            f
            for f in sorted(directory.rglob("*"))
            if f.is_file()
            and f.suffix.lower() in supported_exts
            and not any(part.startswith(".") for part in f.relative_to(directory).parts)
        ]
        # Prefer code/IaC files over README/markdown when both exist
        code_files = [f for f in files if f.suffix.lower() != ".md"]
        md_files = [f for f in files if f.suffix.lower() == ".md"]
        return (code_files + md_files)[:15]

    pkg_dirs.sort(
        key=lambda d: (
            priority_names.get(d.name.lower(), 50),
            -len(_collect_dir_source_files(d)),
            d.name,
        )
    )

    planned_pages: list[dict[str, Any]] = []
    page_payloads: list[dict[str, Any]] = []

    symbol_decl_re = re.compile(
        r"^(?:"
        r"(?:export\s+)?(?:class|def|async\s+def|function|interface|type|func|fn|struct|enum)\s+([A-Za-z][A-Za-z0-9_]*)"
        r"|(?:resource|module|variable|output|data)\s+(?:\"[^\"]+\"\s+)?\"([A-Za-z0-9_-]+)\""
        r"|kind:\s*([A-Za-z][A-Za-z0-9_-]*)"
        r")"
    )

    for pkg_dir in pkg_dirs[:10]:
        rel_dir = pkg_dir.relative_to(root)
        slug = re.sub(r"[^a-z0-9-]+", "-", pkg_dir.name.lower()).strip("-")
        if not slug or slug in {"index", "quickstart"}:
            continue
        discovered_files = _collect_dir_source_files(pkg_dir)
        source_files = [str(f.relative_to(root)) for f in discovered_files[:12]]
        if not source_files:
            continue

        title = f"{pkg_dir.name.replace('_', ' ').title()} Subsystem"
        desc = f"Architecture, key modules, and components in `{rel_dir}/`."
        planned_pages.append(
            {
                "slug": slug,
                "title": title,
                "description": desc,
                "source_files": source_files,
            }
        )

        # Build Markdown body and Grounded Claims from actual source files
        md_lines = [
            f"# {title}",
            "",
            f"Overview of the `{rel_dir}/` subsystem.",
            "",
            "## Key Source Files & Modules",
        ]
        claims: list[dict[str, Any]] = []
        symbols_found: list[tuple[str, str, int]] = []

        for rel_file in source_files[:10]:
            abs_file = root / rel_file
            claims.append({"type": "file_exists", "path": rel_file})
            try:
                lines = abs_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if lines:
                claims.append(
                    {
                        "type": "file_lines",
                        "path": rel_file,
                        "startLine": 1,
                        "endLine": min(len(lines), 25),
                    }
                )
            md_lines.append(f"- `{rel_file}` ({len(lines)} lines)")
            for line_no, line in enumerate(lines, start=1):
                m = symbol_decl_re.match(line.strip())
                if m and len(symbols_found) < 12:
                    sym_name = next((g for g in m.groups() if g), "")
                    if sym_name:
                        symbols_found.append((sym_name, rel_file, line_no))
                        claims.append(
                            {"type": "symbol", "path": rel_file, "symbol": sym_name}
                        )

        if symbols_found:
            md_lines.extend(["", "## Core Components & Symbols"])
            for sym_name, rel_file, line_no in symbols_found:
                md_lines.append(f"- `{sym_name}` — `{rel_file}:{line_no}`")

        page_payloads.append(
            {
                "slug": slug,
                "title": title,
                "description": desc,
                "markdown": "\n".join(md_lines),
                "claims": claims,
            }
        )

    if not planned_pages:
        # Fallback single architecture overview page for minimal repositories
        top_files = [
            str(p.relative_to(root))
            for p in sorted(root.iterdir())
            if p.is_file() and not p.name.startswith(".")
        ][:8]
        planned_pages.append(
            {
                "slug": "architecture-overview",
                "title": "Architecture Overview",
                "description": "High-level repository structure and entry points.",
                "source_files": top_files,
            }
        )
        page_payloads.append(
            {
                "slug": "architecture-overview",
                "title": "Architecture Overview",
                "description": "High-level repository structure and entry points.",
                "markdown": (
                    "# Architecture Overview\n\n## Repository Files\n"
                    + "\n".join(f"- `{f}`" for f in top_files)
                ),
                "claims": [{"type": "file_exists", "path": f} for f in top_files],
            }
        )

    tools_by_name["openwiki_submit_plan"].invoke({"pages": planned_pages})
    all_slugs = [p["slug"] for p in page_payloads]
    for idx, page in enumerate(page_payloads):
        neighbors = [s for s in all_slugs if s != page["slug"]][:3]
        tools_by_name["openwiki_submit_page"].invoke(
            {
                "slug": page["slug"],
                "title": page["title"],
                "description": page["description"],
                "markdown": page["markdown"],
                "related": neighbors,
                "claims": page["claims"],
            }
        )

    finish_raw = tools_by_name["openwiki_finish"].invoke({"quickstart_markdown": ""})
    return json.loads(str(finish_raw))


__all__ = [
    "DEFAULT_OPENWIKI_IGNORE",
    "DEFAULT_OPENWIKI_INSTRUCTIONS",
    "OPENWIKI_ALL_TOOL_NAMES",
    "OPENWIKI_LIFECYCLE_TOOL_NAMES",
    "OPENWIKI_RETRIEVAL_TOOL_NAMES",
    "bootstrap_openwiki_scaffold",
    "build_openwiki_tools",
    "compile_openwiki_deterministic",
    "compute_openwiki_codebase_delta",
    "format_okf_markdown",
    "inspect_openwiki_status",
    "parse_wiki_page",
    "search_openwiki_pages",
    "upsert_agents_md_openwiki_pointer",
    "verify_and_enrich_claim",
]
