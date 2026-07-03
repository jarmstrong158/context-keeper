<!-- mcp-name: io.github.jarmstrong158/context-keeper -->

# Context Keeper

Project memory for Claude. Records design decisions, pipeline flows, and constraints so Claude maintains context across conversations.

## The Problem

As conversations get long, Claude loses the "why" behind earlier decisions. New conversations start blank. This causes Claude to make changes that break established patterns — like rewriting a pipeline step it doesn't remember exists.

## The Solution

Context Keeper gives Claude 11 tools to record and retrieve structured project context:

| Tool | Purpose |
|------|---------|
| `record_decision` | Save a decision with structured rationale (problem, why_chosen, what_we_tried, tradeoffs) |
| `record_pipeline` | Save a multi-step workflow with ordering and `purpose` |
| `record_constraint` | Save a rule with scope, enforcement level, and `triggering_incident` |
| `get_context` | Retrieve relevant entries by query, tags, scope, or ID — pulls `related_to` links by default |
| `get_project_summary` | Compact overview for conversation start |
| `update_entry` | Update any entry by ID |
| `deprecate_entry` | Retire an entry with reason |
| `prune_stale` | Find entries not verified recently |
| `get_compaction_report` | Check if last compaction lost any context |
| `verify_quality` | Scan entries for thin rationale, missing tags, isolated arcs (auto-called by PreCompact hook) |
| `export_markdown` | Regenerate `DECISIONS.md` from the decisions store — a derived, read-only projection |

All data stored as human-editable JSON files in `.context/` inside your project directory. Zero dependencies by default, semantic retrieval optional.

## v0.8: DECISIONS.md Projection (render-on-write)

Opt-in: mirror the decisions store into a human-readable `DECISIONS.md` at the project root. Enable in `.context/config.json`:

```json
{ "markdown_export": { "enabled": true, "path": "DECISIONS.md" } }
```

- **Render-on-write.** Every tool call that mutates a decision (`record_decision`, `update_entry`, `deprecate_entry`) regenerates the entire file from `decisions.json` after the JSON write and before the tool returns — so a subsequent `git commit` captures both in the same commit. Deliberately *not* a git/PostToolUse hook: rendering after the commit snapshot would reintroduce drift.
- **JSON stays canonical; markdown is derived and read-only.** The file is regenerated whole every time — never appended to, merged, or parsed back in. Hand edits are not preserved; a regenerated projection has no drift surface.
- **`export_markdown` tool** regenerates on demand (optionally to a custom `path`), so existing repos can backfill without enabling the flag.
- Pure stdlib string formatting; default behavior with the flag off is byte-for-byte unchanged.

Born from field use: Balatron's `DECISIONS.md` was kept in sync with the store by hand, one mirror-edit per commit. This automates that convention.

## v0.7: Anticipated Queries, Origin Trust, Timeline Filters

- **`retrieval_hints`** (all `record_*` tools): 2-4 alternate phrasings a future session might search for — synonyms, symptom descriptions, error messages. Indexed for both lexical and semantic retrieval, so vocabulary-mismatch queries ("value network diverging" vs. "value head saturating") can hit without embeddings. The zero-dependency complement to the semantic blend.
- **`origin` + trust weighting** (all `record_*` tools): entries record who authored them — `user` (explicitly stated), `agent` (inferred from the session), or `import` (backfilled). Retrieval scoring gives user-stated entries a trust boost over agent-inferred, which outrank imports. Pre-v0.7 entries score as `agent`, preserving their relative order.
- **`since` / `before` on `get_context`**: temporal filters against each entry's verified/created timestamp — "what did we decide this month" is now a query.

## v0.6: Capture-Time Guardrails

- **Scoped constraint injection.** New `scope_guard.py` hook (PostToolUse on `Edit|Write|NotebookEdit`): the moment the agent edits a file covered by a constraint's `scope`, that constraint is injected into context via `additionalContext`. Session-start injection briefs the model once at turn one; this enforces the rule at the exact moment it's about to matter. Each constraint fires at most once per session.
- **Similar-entry surfacing at record time.** `record_*` now compares the new entry against the store (word-set Jaccard, threshold configurable via `similar_threshold`) and returns `similar_entries` when existing entries overlap heavily — catching restatements and contradictions at capture instead of relying on MMR to mitigate duplicates at retrieval. Advisory only: the write always proceeds.

## v0.5: Data Integrity + Retrieval Fixes

- **Atomic writes.** Entry files are written to a temp file and swapped in with `os.replace`, so a crash mid-write can no longer leave a truncated JSON file behind.
- **Corrupt-store protection.** If an entry file exists but can't be parsed, `record_*`/`update_entry`/`deprecate_entry` now refuse to write (previously a corrupt file read as empty, and the next record silently replaced your entire history with one entry). Read-only tools still degrade gracefully.
- **`update_entry` enforces the schema.** Structured fields (`why_chosen`, `problem`, `reason`, `purpose`, ...) are min-length validated on update too, so entries can't be hollowed out after recording.
- **Better budget packing.** `get_context` skips entries that don't fit the token budget and keeps packing smaller ones, instead of stopping at the first oversized entry.
- **Fresh compaction reports.** The SessionStart hook now runs the snapshot comparison itself (SessionStart fires with source `compact` immediately after compaction — before any Stop), so the injected report is never one compaction stale. It also injects a one-line quality-scan nudge, which is the model-visible surface for `verify_quality` (PreCompact stdout is only shown to the user, not the model).
- **Semantic layer shipped in the package** (`semantic_index.py` was missing from the wheel/sdist), with batched embedding requests and one fewer HTTP round-trip per query.

## v0.4: Structured Rationale + Arc Linking

Earlier versions used a single freeform `rationale` field. In practice, agents wrote one-line summaries instead of full reasoning — defeating the point. v0.4 fixes this three ways:

1. **Schema-enforced depth.** `record_decision` requires `problem` (min 40 chars), `why_chosen` (min 60 chars), and accepts optional `what_we_tried` and `tradeoffs`. `record_pipeline` requires `purpose`. `record_constraint` enforces `reason` ≥ 40 chars and accepts optional `triggering_incident`. Thin entries are rejected server-side with field-specific guidance — the lazy path no longer produces a useful entry.
2. **Arc linking via `related_to`.** Every entry can reference IDs of related entries. `get_context` traverses these links by default (depth=1), so when you retrieve one decision the rest of its arc comes along. Connective tissue survives across sessions.
3. **Quality verification.** A new `verify_quality` tool scans for legacy entries, thin reasoning, missing tags, and isolated entries (tag overlap with no `related_to`). The `PreCompact` hook calls it automatically and surfaces flagged entries so they can be enriched before context is compressed.

Legacy entries (pre-v0.4) stay valid — they're never auto-rejected, just flagged by `verify_quality` for optional enrichment. The deprecated `rationale` parameter still works on `record_decision` for backward compatibility (it auto-maps to `why_chosen`), but `problem` is still required.

## Install

```bash
pip install context-keeper-mcp
```

### Claude Code

```bash
claude mcp add --scope user context-keeper -- python /path/to/context-keeper/server.py
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "context-keeper": {
      "command": "python",
      "args": ["/path/to/context-keeper/server.py"],
      "env": {
        "CONTEXT_KEEPER_PROJECT": "/path/to/your/project"
      }
    }
  }
}
```

### Other MCP clients (Cursor, Codex CLI, Gemini CLI, Windsurf, ...)

The server is a standard stdio MCP server, so any MCP-capable client can use it — the hooks are Claude Code extras, not requirements. Point your client's MCP config at `python /path/to/context-keeper/server.py` and set `CONTEXT_KEEPER_PROJECT`:

**Cursor** (`~/.cursor/mcp.json` or per-project `.cursor/mcp.json`) and **Windsurf** (`~/.codeium/windsurf/mcp_config.json`) use the same shape as Claude Desktop:

```json
{
  "mcpServers": {
    "context-keeper": {
      "command": "python",
      "args": ["/path/to/context-keeper/server.py"],
      "env": { "CONTEXT_KEEPER_PROJECT": "/path/to/your/project" }
    }
  }
}
```

**OpenAI Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.context-keeper]
command = "python"
args = ["/path/to/context-keeper/server.py"]
env = { "CONTEXT_KEEPER_PROJECT" = "/path/to/your/project" }
```

**Gemini CLI** (`~/.gemini/settings.json`) uses the same `mcpServers` JSON shape as Cursor above.

Without the Claude Code hooks you lose automatic session-start injection and edit-time constraint guards — call `get_project_summary` at conversation start and `record_*` as you work instead (the tool descriptions prompt for this).

Set `CONTEXT_KEEPER_PROJECT` to the root of your project. If omitted, the server resolves the project directory in this order:

1. **`CONTEXT_KEEPER_PROJECT`** env var (explicit opt-in — trusted)
2. **cwd** if it already contains a `.context/` directory
3. **Walk parent dirs** from cwd looking for an existing `.context/` (git-style discovery — finds your project when the server is launched from any subdirectory of it)
4. Otherwise: refuse, and `record_*` returns an "unresolved project" error

Steps 2 and 3 only resolve to directories that **already** contain `.context/`. The server never creates one implicitly, so you can never accidentally pollute a parent directory by launching from the wrong place. Pass `project_dir` explicitly to any tool to force-create a new project.

## How It Works

### Recording Context

When you make a design decision:
```
You: Let's use JSON files instead of SQLite for storage.
Claude: [calls record_decision with summary, problem, why_chosen, alternatives,
         and optionally what_we_tried + tradeoffs + related_to links]
```

When you establish a workflow:
```
You: The deploy pipeline is: run tests, build, push to registry, deploy.
Claude: [calls record_pipeline with ordered steps]
```

When you set a rule:
```
You: Never run Conductor from source. Always use the exe.
Claude: [calls record_constraint with rule, reason, and hardness=absolute]
```

### Retrieving Context

At conversation start, the SessionStart hook injects the project summary (and any compaction-discrepancy report) directly into context — no tool call required, so retrieval can't be skipped on a task-focused first turn. `get_project_summary` remains callable on demand. Before making changes, Claude calls `get_context` with relevant tags to check for conflicts.

### Relevance Scoring

Without embeddings or external services, Context Keeper scores entries using:
- **Tag match** — overlap between query and entry tags
- **Text match** — query words found in summary/rationale/rule text
- **Recency** — recently verified entries score higher
- **Status** — active entries prioritized over superseded

Results are capped by a configurable token budget (default: 4000 tokens).

## Claude Code Hook Setup

Context Keeper includes hooks that inject project memory at session start, remind Claude to capture after every git commit, snapshot your context before Claude Code compaction, and detect if anything was lost afterward.

Add to your Claude Code hooks config (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/pre_compact.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/post_compact.py"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/session_start.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/commit_capture_reminder.py"
          }
        ]
      },
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/scope_guard.py"
          }
        ]
      }
    ]
  }
}
```

Replace `/path/to/context-keeper` with the actual install path. Set `CONTEXT_KEEPER_PROJECT` env var if your project isn't in the current working directory.

**Windows users:** Use forward slashes (`C:/Users/.../context-keeper/hooks/pre_compact.py`) or double-escaped backslashes in JSON. Single backslashes get mangled by the shell.

The hooks form a complete capture-and-retrieval loop:

- **SessionStart** — imports the server's own handlers and prints the project summary (plus any compaction-discrepancy report and a one-line quality-scan nudge) straight to stdout, which Claude Code injects into context at turn one. It also runs the post-compaction snapshot comparison itself before reading the report — SessionStart fires with source `compact` immediately after compaction, before any Stop hook, so this keeps the injected report fresh. This replaces the older approach of printing an instruction to *call* the tools — a request that reliably lost to a task-focused first turn since the tools are deferred. Stays silent when the project has no `.context/` yet, and emits ASCII-only output so it cannot crash on Windows cp1252 stdout
- **PostToolUse (Bash)** — fires after every Bash tool call; when the command contains `git commit`, it injects a reminder to record the matching decision/constraint/gotcha **in the same work cycle**. A commit is the single best capture trigger — it's the exact moment something became real enough to persist in version control. Born from field use: during incident-heavy sessions the agent batched capture "for later," and the user had to ask "update context keeper" three times in one night while a dozen commits shipped
- **PostToolUse (Edit|Write)** — `scope_guard.py`: when the agent edits a file covered by a constraint's `scope` (e.g. a constraint scoped to `hooks/` and an edit to `hooks/session_start.py`), that constraint is injected right then via `additionalContext`. Session start briefs the rules; this enforces them at the moment of edit. Once per constraint per session
- **PreCompact** — snapshots all active `.context/` entries and runs a quality scan (`verify_quality`), printing flagged entries (thin reasoning, missing tags, isolated arcs) to the transcript. Note: PreCompact stdout is user-visible only — Claude Code does not inject it into the model's context, which is why the model-visible quality nudge lives in the SessionStart hook instead
- **Stop** — safety-net run of the same snapshot comparison SessionStart performs, in case the session ends without a new session starting (idempotent — skips if the snapshot hasn't changed since last comparison)

This closes the capture loop: SessionStart injects retrieval at turn one, the commit reminder anchors capture to the moment changes land, PreCompact is the pre-compression safety net, and Stop handles integrity checking. Retrieval is unavoidable; capture is now *prompted at the right moment* rather than left to the agent's discretion mid-task.

## Data Storage

```
your-project/
  .context/
    decisions.json           # Design decisions with rationale
    pipelines.json           # Multi-step workflows
    constraints.json         # Rules and invariants
    config.json              # Token budget, stale threshold
    compaction_snapshot.json  # Pre-compaction snapshot (auto-generated)
    compaction_report.json   # Post-compaction diff report (auto-generated)
    hook.log                 # Hook activity log
```

All files are human-readable JSON. You can edit them directly. IDs are sequential and readable: `dec-001`, `pipe-001`, `con-001`.

## Configuration

Create `.context/config.json` to customize:

```json
{
  "project_name": "my-project",
  "token_budget": 4000,
  "max_entry_tokens": 1000,
  "stale_threshold_days": 30,
  "markdown_export": {
    "enabled": false,
    "path": "DECISIONS.md"
  },
  "semantic": {
    "enabled": false,
    "weight": 150,
    "model": "nomic-embed-text",
    "url": "http://localhost:11434"
  },
  "mmr": {
    "enabled": false,
    "lambda": 0.7
  }
}
```

`mmr` (opt-in, default off) reorders the ranked results for **Maximal Marginal
Relevance**: a candidate is penalized by its lexical similarity to entries already
chosen, so near-duplicate restatements of one topic don't crowd the token budget
and a second relevant topic gets a seat. `lambda` trades relevance (1.0 = pure
relevance order) against diversity. Entries linked by `related_to` are exempt —
those arcs are meant to surface together. On today's store sizes the effect is
small (redundancy@5 is already low); it earns its keep as a store grows and
accumulates superseded/restated entries.

## Semantic Retrieval (opt-in)

By default, `get_context` ranks entries with pure lexical matching (tag + word
overlap) — zero dependencies, works offline. The weakness is **vocabulary
mismatch**: a query about a "value network diverging" won't find the decision
about a "value head saturating", because they share no keywords.

Setting `semantic.enabled: true` blends an embedding-cosine signal into the
ranking, using a local [Ollama](https://ollama.com) server
(`ollama pull nomic-embed-text`). On a held-out eval across three real project
stores this lifted **hit@5 from 80% to 93% and MRR from 0.63 to 0.88** (the
retrieval harness lives in [`evals/`](evals/)). Entry embeddings are cached per
store in `.context/embeddings.json`, keyed by a hash of the entry text, so an
edited entry is re-embedded automatically.

It is strictly additive and fail-safe: if Ollama is unreachable or the model is
missing, retrieval silently falls back to lexical ranking. The default stays
`enabled: false`, so zero-dependency remains the out-of-the-box behavior.

## Cross-Project Context

Query another project's context by passing `project_dir`:

```
Claude: [calls get_context with project_dir="/path/to/other-project"]
```

Or tag entries with other project names for cross-referencing.
