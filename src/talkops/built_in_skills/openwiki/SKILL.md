---
name: openwiki
description: "Compile, update, query, and maintain the repository OpenWiki knowledge base (openwiki/ directory, OKF v0.2 Markdown pages, and Grounded Claims). Trigger when the user runs /openwiki, asks to initialize or update the repository wiki, or asks architectural questions about the codebase."
license: Apache-2.0
compatibility: designed for talkops
metadata:
  workflow: "false"
  include_tools: "openwiki_begin openwiki_submit_plan openwiki_next_page openwiki_inspect_page_claims openwiki_submit_page openwiki_finish openwiki_search openwiki_read openwiki_list_wikis openwiki_list_workspaces"
---

# OpenWiki Repository Knowledge Base Skill

Use this skill to compile, incrementally update, or query a repository's durable Markdown knowledge base (`openwiki/`) using the native TalkOps OpenWiki LangChain tools (`openwiki_begin`, `openwiki_submit_plan`, `openwiki_next_page`, `openwiki_inspect_page_claims`, `openwiki_submit_page`, `openwiki_finish`, `openwiki_search`, `openwiki_read`).

> **CRITICAL EXECUTION RULE**:
> All 10 OpenWiki tools are native LangChain tools bound directly into your toolset by `OpenWikiContextMiddleware`.
> **NEVER** run `openwiki` or `talkops openwiki` inside the `execute` shell tool—always invoke the native `openwiki_*` tools directly!

---

## Mode 1: Querying the Wiki (`ask` / Architectural Discovery)

When answering architectural questions or orienting before a multi-file change:

1. Call `openwiki_search(query=...)` with focused subsystem or symbol keywords (BM25 + Porter stemmer + heading/title boosts + graph expansion).
2. Call `openwiki_read(slug=..., section=...)` to read only the relevant section of a page rather than loading every page into context.
3. Follow `[[slug]]` or `[[slug#Section]]` wikilinks when traversing cross-cutting workflows.

---

## Mode 2: Native Compilation & Incremental Update (`init` / `update`)

When invoked to initialize (`mode="init"`) or update (`mode="update"`) the repository's OpenWiki, execute the following 6-step loop strictly in order using the bound LangChain tools:

### Step 1 — Start Run (`openwiki_begin`)
Call `openwiki_begin(mode="init" | "update", brief=..., force=...)`:
- If `status` is `"up_to_date"`, report to the user that the wiki is already synchronized with the current Git commit and stop.
- Otherwise, read the returned `planningPacket` (`repoOverview`, `changedFiles`, `driftedPages`, `existingPages`, and `openwiki/INSTRUCTIONS.md`).

### Step 2 — Submit Plan (`openwiki_submit_plan`)
Explore the key source directories using `glob`, `grep`, and `read_file` if needed, then call `openwiki_submit_plan(pages=[...])`:
- Propose 3–8 focused subsystem pages (do not include `index` or `quickstart` in `pages`—`openwiki_finish` generates those automatically).
- Each page entry must have:
  - `slug`: kebab-case identifier (e.g. `"agent-factory"`, `"middleware-pipeline"`, `"cli-and-commands"`)
  - `title`: human-readable title
  - `description`: 1-sentence summary
  - `source_files`: list of primary relative file paths

### Step 3 — Pull Next Page Brief (`openwiki_next_page`)
Call `openwiki_next_page()` to retrieve the writing brief for the next pending page in `openwiki/.run.json`.
- When `done: true` is returned, proceed immediately to **Step 6 (`openwiki_finish`)**.

### Step 4 — Inspect Grounded Claims (`openwiki_inspect_page_claims`)
In `update` mode (or when revising an existing page), call `openwiki_inspect_page_claims(slug=...)` to inspect prior claims (`openwiki/.claims/<slug>.json`) and their SHA-256 drift status (`verified`, `fuzzy_relocated`, or `drifted`).

### Step 5 — Author & Submit Page (`openwiki_submit_page`)
Read the relevant source files for the current page, then call `openwiki_submit_page(slug=..., title=..., description=..., markdown=..., related=[...], claims=[...])`:
- Write clear Markdown using `##` section headings, `[[slug]]` wikilinks to related pages, and Mermaid diagrams (`flowchart TD`, `sequenceDiagram`) where helpful.
- **Always include a Key Source Files / Component Map table with full repository-relative file paths** (e.g. `crossplane/compositions/object-store/object-store.yaml`, `terraform-modules/s3/policy.tf`, `src/talkops/middleware/openwiki.py`) rather than bare folder or filenames so future coding turns can `read_file` exact paths directly after `openwiki_read` without calling `glob`.
- Pass structured `claims` anchoring the page to real repository files (`openwiki_submit_page` automatically computes and records the SHA-256 `contentHash`):
  - `{"type": "file_exists", "path": "src/..."}`
  - `{"type": "file_lines", "path": "src/...", "startLine": 1, "endLine": 45}`
  - `{"type": "symbol", "path": "src/...", "symbol": "MyClassOrFunction"}`
- Loop back to **Step 3 (`openwiki_next_page`)** until `done: true`.

### Step 6 — Finalize & Validate (`openwiki_finish`)
Call `openwiki_finish()` to:
1. Validate all `[[slug]]` wikilinks across `openwiki/*.md`.
2. Automatically generate `openwiki/index.md` and `openwiki/quickstart.md`.
3. Write `openwiki/.last-update.json` and update the `<!-- OPENWIKI:START --> ... <!-- OPENWIKI:END -->` pointer block in `AGENTS.md`.
4. Refresh the session's `openwiki_digest` in LangGraph state.

---

## Strict Safety Guardrails

- **Never** directly edit or delete `openwiki/.claims/*.json`, `openwiki/.run.json`, or `openwiki/.last-update.json` using `write_file`, `edit_file`, or `delete_file`. Those files are managed exclusively by `openwiki_submit_page` and `openwiki_finish`.
- **Never** modify content inside `<!-- OPENWIKI:START --> ... <!-- OPENWIKI:END -->` in `AGENTS.md` using `edit_file` — `openwiki_finish` manages that pointer block automatically.
