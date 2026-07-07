<!-- mcp-name: io.github.jarmstrong158/context-keeper -->

# Context Keeper

Project memory for Claude. Records design decisions, pipeline flows, and constraints so Claude maintains context across conversations.

## The Problem

As conversations get long, Claude loses the "why" behind earlier decisions. New conversations start blank. This causes Claude to make changes that break established patterns — like rewriting a pipeline step it doesn't remember exists.

## The Solution

Context Keeper gives Claude 13 tools to record and retrieve structured project context:

| Tool | Purpose |
|------|---------|
| `record_decision` | Save a decision with structured rationale (problem, why_chosen, what_we_tried, tradeoffs) |
| `record_pipeline` | Save a multi-step workflow with ordering and `purpose` |
| `record_constraint` | Save a rule with scope, enforcement level, and `triggering_incident` |
| `get_context` | Retrieve relevant entries by query, tags, scope, or ID — relevance-ranked, pulls `related_to` links by default |
| `query_entries` | **Exact structured-field filtering** (status, origin, tags, scope, hardness, supersession, dates) — deterministic, no ranking; distinct from `get_context`'s relevance search |
| `get_project_summary` | Compact overview for conversation start |
| `update_entry` | Update any entry by ID |
| `deprecate_entry` | Retire an entry with reason |
| `prune_stale` | Find entries not verified recently |
| `get_compaction_report` | Check if last compaction lost any context |
| `verify_quality` | Scan entries for thin rationale, missing tags, isolated arcs (auto-called by PreCompact hook) |
| `export_markdown` | Regenerate `DECISIONS.md` from the decisions store — a derived, read-only projection |
| `reload_constraints` | Re-surface the constraints-only block on demand mid-session (rules refresh, not the full store) |

All data stored as human-editable JSON files in `.context/` inside your project directory. Zero dependencies by default, semantic retrieval optional.

## Capabilities at a glance

Context Keeper is a small, offline-first memory layer; several of its capabilities are easy to miss because they live inside existing tools rather than as separate features. The map below names them in memory-system terms:

| Capability | How Context Keeper does it |
|------------|----------------------------|
| **Procedural memory** | `record_pipeline` stores ordered, dependency-aware workflows (build/deploy/data flows) with `purpose` + `when_to_invoke` — reusable "how we do X", not just facts. |
| **Deduplication** | Every `record_*` runs a word-set Jaccard pass against the store and returns `similar_entries` when a new entry restates an existing one, so duplicates are caught at capture; `deprecate_entry(merge_into=...)` then folds the duplicate's unique content into the survivor and retires it in one non-destructive step. |
| **Contradiction detection** | Those same overlaps are classified `likely_restatement` vs `likely_contradiction` (negation/antonym polarity), and a reversal raises a `contradiction_note` telling the agent to resolve the conflict rather than leave two live rules disagreeing. |
| **Quality refinement** | `verify_quality` scans for thin rationale, missing tags, legacy-schema entries, and isolated (unlinked) arcs; the PreCompact hook runs it automatically so entries get enriched before context is compressed. |
| **Supersede / decay / forget** | `supersedes` demotes-but-keeps prior decisions (recallable history); `prune_stale` surfaces unverified entries for review; `deprecate_entry` removes an entry from retrieval entirely. |
| **Origin + trust / source attribution** | Every entry records `origin` (`user` / `agent` / `import`); retrieval gives user-stated entries a trust boost and it decides the default winner when entries conflict. |
| **Anticipated queries** | `retrieval_hints` stores alternate phrasings a future session might search for, so vocabulary-mismatch queries hit without embeddings. |
| **Hybrid retrieval** | Lexical (tag + word overlap) by default; an opt-in embedding-cosine blend (`semantic.enabled`) adds vector recall, with lexical fallback when the embedder is offline. |
| **Fact-metadata query** | `query_entries` filters entries by exact predicates over structured fields (status, origin, tags-any/all, scope, hardness, supersession, dates), AND-combined and deterministic — a precise lookup path distinct from `get_context`'s fuzzy relevance ranking. |
| **Cache-friendly injection** | The session-start memory block is deterministically ordered with a stable prefix and the only per-session-volatile line (quality-scan IDs) emitted last, so an unchanged store injects byte-identical text across sessions. |
| **Narrative + clustering** | `get_project_summary` clusters decisions by topic above a threshold and renders a compact narrative; the `DECISIONS.md` projection mirrors the store as human-readable prose. |
| **Data export / offline / privacy** | Plain JSON in `.context/` you can read, edit, grep, and commit; runs fully offline with zero required dependencies and no data leaving the machine. |

### Evaluation & benchmarks (open methodology)

The retrieval and honesty properties are measured, not asserted — the harness is in [`evals/`](evals/) and reproducible with no network required:

- **Token reduction** — session-start injection vs. dumping the full store: **97.3% / 94.1% / 85.5% / 73.3%** across four real stores ([`evals/token_reduction.py`](evals/token_reduction.py)). The meaningful property is that injected cost stays roughly flat as the store grows.
- **Retrieval quality** — on a held-out 3-store set, the opt-in semantic blend lifts **hit@5 from 80% → 93% and MRR 0.63 → 0.88** ([`evals/retrieval_eval.py`](evals/retrieval_eval.py)).
- **Abstention** — measures whether `get_context` says "nothing relevant" instead of confabulating on no-answer queries; the 0.20 relevance floor is the highest with zero false-abstention on the eval set ([`evals/abstention.py`](evals/abstention.py)).

Every dataset, metric, and caveat is checked into the repo — see [`evals/README.md`](evals/README.md).

## v0.14: Dedup Merge (`deprecate_entry(merge_into=...)`)

Capture-time detection already caught near-duplicates (`similar_entries` with a `likely_restatement` relation), but *resolving* one was a manual two-step: deprecate the duplicate, then `update_entry` the original to fold in anything it was missing. v0.14 collapses that into one atomic, non-destructive operation.

- **Opt-in param on the existing tool, not a new tool.** `deprecate_entry(id=<dupe>, reason=..., merge_into=<survivor>)` folds the duplicate's unique content into the survivor, then deprecates the duplicate with `superseded_by=<survivor>`. When `merge_into` is absent, `deprecate_entry` behaves exactly as before — byte-for-byte.
- **Additive and non-destructive.** The survivor can only *gain* content: list fields (`tags`, `retrieval_hints`, `related_to`, `constraints`/`constraints_created`) are unioned, and empty text fields are backfilled from the duplicate — a non-empty field on the survivor is **never** overwritten. The duplicate isn't hard-deleted; it stays on disk as a deprecated entry pointing at the survivor, so the merge is fully auditable and reversible.
- **Same-type, single-write, validated first.** Merge requires both entries to be the same type (so their schemas line up), resolves and validates the target before any write, and mutates both entries in one file write so the two updates can't clobber each other. A bad `merge_into` (missing target, cross-type, or self) errors cleanly and deprecates nothing.
- **Roots held.** Explicit and agent-invoked (like every other lifecycle tool), zero new dependencies, no LLM call, deterministic. It streamlines the restatement workflow the capture loop already prescribes rather than adding a background process.

```jsonc
// dec-002 restates dec-001 — merge and retire it in one call
{ "id": "dec-002", "reason": "Restatement of dec-001", "merge_into": "dec-001" }
// -> dec-001 gains dec-002's unique tags/hints/related_to + any text it lacked;
//    dec-002 becomes deprecated with superseded_by = dec-001
```

## v0.13: Structured Field Query (`query_entries`)

`get_context` answers *"what's relevant to what I'm working on?"* — it ranks by relevance, blends optional semantics, and flags low-relevance results with an abstention signal. That's the right tool for fuzzy recall, but the wrong one when you already know the exact field values you want. `query_entries` fills that gap: **deterministic filtering over the structured fields that already exist on every entry**, no ranking and no abstention.

- **Exact predicates, AND-combined:** `types`, `status` (active/superseded/deprecated), `origin` (user/agent/import), `tags_any`, `tags_all`, `scope` (exact, case-sensitive), `hardness` (absolute/advisory), `supersedes` / `superseded_by`, and the same `since` / `before` temporal filters as `get_context`. Every predicate is a hard match over an existing field — a query either matches or it doesn't.
- **No relevance, no confabulation.** Results come back in stable natural-ID order with no score and no `min_relevance` floor — an empty result set is a real, honest answer, not an abstention message. The abstention machinery is for fuzzy text queries; a structured predicate doesn't need it.
- **Same store, same budget.** It reuses the exact store-reading and entry-serialization paths `get_context` uses, and packs the matched set into the same token budget (default 4000, `token_budget` per call), so a broad query can't dump the store — `matched_entries` vs `entries_returned` and a `budget_truncated` flag tell you if the cap clipped anything.
- **Additive and self-contained.** Zero new dependencies, stdlib only, no embeddings and no LLM call — pure in-memory filtering over JSON already on disk. `get_context`, the semantic blend, the scoring, and every hook are untouched; default behavior of every existing tool is byte-for-byte unchanged.

One deliberate difference from `get_context`: **`query_entries` applies no default status filter**, so `superseded` and `deprecated` entries *are* returned unless you pass `status`. `get_context` always hides deprecated entries; the structured tool lets you ask for them on purpose.

**Examples:**

```jsonc
// Absolute constraints scoped to the hooks/ directory
{ "types": ["constraints"], "hardness": "absolute", "scope": "hooks/" }

// User-stated decision that superseded dec-005
{ "origin": "user", "supersedes": "dec-005" }

// Active pipelines tagged "release"
{ "types": ["pipelines"], "status": "active", "tags_any": ["release"] }

// Everything a user asserted this month, across all types
{ "origin": "user", "since": "2026-07-01" }
```

## v0.12: Contradiction Detection + Cache-Stable Injection

- **Restatement vs contradiction, at capture time.** The similar-entry pass already caught heavy overlaps; now it classifies each one. Two dependency-free signals — negation asymmetry ("X is required" vs "X is *not* required") and antonym polarity ("always" here / "never" there, "enable" / "disable") — label a match `likely_restatement` or `likely_contradiction`. A restatement nudges you to merge; a contradiction raises a `contradiction_note` telling the agent to resolve which rule is current (`deprecate_entry` with `superseded_by`) instead of silently leaving two live rules that disagree. Advisory only, and only evaluated on pairs Jaccard already flagged as overlapping — the write always proceeds. Zero new dependencies, no LLM call, no added tokens at record time.
- **Cache-stable session-start injection.** The injected memory block is ordered so its large stable portion — constraints, decisions, pipelines, and the fixed capture guidance — forms a prefix that repeats byte-for-byte across sessions when the store hasn't changed, while the one volatile line (the quality scan's flagged IDs) is emitted last. This keeps the memory block inside the model's cacheable prompt prefix rather than busting the cache each session. It also *reduces* tokens rather than adding them.

## v0.11: Mid-Session Constraint Re-Injection (opt-in)

The SessionStart hook injects your constraints once, at turn one. As a long
session fills with tool output, those rules scroll out of the model's working
attention and effectively decay — the model can violate a constraint it was
briefed on an hour ago simply because it is buried. v0.11 re-surfaces the
constraints **during** a long session, not just at the start.

Two ways in, both **constraints-only** — they re-inject the exact
Absolute/Advisory block SessionStart shows, and nothing else from the store
(no decisions, no pipelines). It's a lightweight rules refresh, not a second
full dump.

- **`reload_constraints` tool** — returns the current constraints block on
  demand. Always available; call it whenever you want the rules back in
  context.
- **`constraint_reinject.py` hook (PostToolUse)** — **opt-in, default off.**
  When enabled, it counts tool calls per session and re-injects the
  constraints block every *N* calls (`every_n_tools`, default 25) via
  `additionalContext`.

**What triggers it, honestly.** The automatic path is the **PostToolUse**
hook — that surface *is* injected into the model, and its firing rate tracks
tool-output volume, which is the thing actually burying the rules. It is **not
a timer**: an MCP server has no wall-clock inside the context window, so
re-injection is driven by counting tool calls, not elapsed seconds. It is
**not PreCompact** either — PreCompact stdout is shown only to the user, never
injected into the model (the compaction boundary is already re-covered by the
SessionStart hook, which re-fires with source `compact`).

**Default behavior is unchanged.** With no config (or `enabled: false`), the
hook is inert and SessionStart works exactly as before. Enable it in
`.context/config.json`:

```json
{ "constraint_reinjection": { "enabled": true, "every_n_tools": 25 } }
```

## v0.10: Abstention + Supersession-as-Ranking

Two ideas adapted from studying [Curion](https://github.com/geanatz/curion), kept dependency-free:

- **`get_context` can now say "I don't have anything relevant."** Previously it always returned its top-scored entries — but the composite score banks ~55 points from recency/status/origin regardless of relevance, so a query with *no* relevant memory silently got a confident-looking result. Measured confabulation was **100%** on no-answer queries (`evals/abstention.py`). Now the response carries `top_relevance` and, when the top entry's tag/text relevance falls below `min_relevance` (config, default 0.20), `no_confident_match: true` with guidance telling the agent not to present the entries as established fact. It **annotates, never suppresses** — weak matches are still returned, so the vocabulary-mismatch recall that `retrieval_hints` and the semantic blend preserve survives. 0.20 is the highest floor with zero false-abstention on the eval set.
- **Supersession as a ranking signal, not just a filter.** `record_decision` accepts `supersedes: [ids]`: the prior decisions become `superseded` — **demoted in ranking but still recallable** ("why did we change from X?"), distinct from `deprecate_entry` which removes an entry from retrieval entirely. Superseded entries are skipped by `prune_stale`/`verify_quality` (they're intentional history, not stale work) and marked `**SUPERSEDED** by dec-NNN` in the `DECISIONS.md` projection.

Deliberately *not* adopted from Curion: its LLM-controller architecture (an API call on every store and recall). context-keeper stays zero-dependency and offline by default.

## v0.9: Topic Clustering, More Embedding Backends + a Bug the Measurement Caught

- **Critical fix: empty session-start injection for large stores.** The summary truncation loop evaluated the *original* text in its condition, so any store whose summary exceeded the token budget (~30+ entries) silently popped every line and injected an **empty** summary at session start. Found while measuring token reduction: a 78-entry store was injecting ~0 tokens of memory. Now truncates correctly to budget.
- **Topic clustering.** Above 8 decisions, `get_project_summary` groups decisions by their most-frequent shared tag instead of one flat list — a 59-decision store reads as a dozen topics.
- **OpenAI-compatible embeddings.** `semantic.api: "openai"` points the semantic blend at any `/v1/embeddings` endpoint — LM Studio, llama.cpp server, or OpenAI itself (`api_key_env` names the env var holding the key). Ollama stays the default; same fail-safe lexical fallback. nomic task prefixes now apply only to nomic models.
- **Trust-aware conflict guidance.** `similar_entries` matches now carry each entry's `origin`, and the guidance states the precedence: user-stated overrides agent-inferred overrides imported.
- **Token-reduction measurement** (`evals/token_reduction.py`), run against four real stores:

| store | active entries | full store (tokens) | injected at session start | reduction |
|---|---|---|---|---|
| balatron | 78 | ~75,277 | ~2,057 | 97.3% |
| clark | 55 | ~35,445 | ~2,102 | 94.1% |
| context-keeper | 13 | ~5,692 | ~828 | 85.5% |
| conductor | 9 | ~1,538 | ~411 | 73.3% |

  Baseline = dumping every active entry into context; injected = the `get_project_summary` output the SessionStart hook prints. Honest caveat: the summary is budget-capped (default 2000 tokens), so for large stores part of the reduction is by construction — the meaningful property is that injected cost stays flat as stores grow.
- **Six more MCP clients documented** (OpenCode, Copilot CLI, Antigravity, OpenClaw, Hermes, pi/oh-my-pi) — see Other MCP clients below.

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

**GitHub Copilot CLI** (`~/.copilot/mcp-config.json`) and **oh-my-pi** (`mcpServers` config) use the `mcpServers` shape with `"type": "stdio"`:

```json
{
  "mcpServers": {
    "context-keeper": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/context-keeper/server.py"],
      "env": { "CONTEXT_KEEPER_PROJECT": "/path/to/your/project" }
    }
  }
}
```

**OpenCode** (`opencode.json`):

```json
{
  "mcp": {
    "context-keeper": {
      "type": "local",
      "command": ["python", "/path/to/context-keeper/server.py"],
      "environment": { "CONTEXT_KEEPER_PROJECT": "/path/to/your/project" }
    }
  }
}
```

**Antigravity** (`~/.gemini/config/mcp_config.json` or workspace `.agents/mcp_config.json`) and **OpenClaw** (`openclaw.json`) use the `mcpServers` shape with `command`/`args`, same as Copilot above.

**Hermes** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  context-keeper:
    command: "python"
    args: ["/path/to/context-keeper/server.py"]
    env:
      CONTEXT_KEEPER_PROJECT: "/path/to/your/project"
```

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
      },
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/context-keeper/hooks/constraint_reinject.py"
          }
        ]
      }
    ]
  }
}
```

The `constraint_reinject.py` entry is only active when
`constraint_reinjection.enabled` is set in `.context/config.json` (default
off) — wiring it up is harmless until you opt in. Its matcher is `""` (every
tool call) so the per-session counter advances on all activity.

Replace `/path/to/context-keeper` with the actual install path. Set `CONTEXT_KEEPER_PROJECT` env var if your project isn't in the current working directory.

**Windows users:** Use forward slashes (`C:/Users/.../context-keeper/hooks/pre_compact.py`) or double-escaped backslashes in JSON. Single backslashes get mangled by the shell.

The hooks form a complete capture-and-retrieval loop:

- **SessionStart** — imports the server's own handlers and prints the project summary (plus any compaction-discrepancy report and a one-line quality-scan nudge) straight to stdout, which Claude Code injects into context at turn one. It also runs the post-compaction snapshot comparison itself before reading the report — SessionStart fires with source `compact` immediately after compaction, before any Stop hook, so this keeps the injected report fresh. This replaces the older approach of printing an instruction to *call* the tools — a request that reliably lost to a task-focused first turn since the tools are deferred. Stays silent when the project has no `.context/` yet, and emits ASCII-only output so it cannot crash on Windows cp1252 stdout
- **PostToolUse (Bash)** — fires after every Bash tool call; when the command contains `git commit`, it injects a reminder to record the matching decision/constraint/gotcha **in the same work cycle**. A commit is the single best capture trigger — it's the exact moment something became real enough to persist in version control. Born from field use: during incident-heavy sessions the agent batched capture "for later," and the user had to ask "update context keeper" three times in one night while a dozen commits shipped
- **PostToolUse (Edit|Write)** — `scope_guard.py`: when the agent edits a file covered by a constraint's `scope` (e.g. a constraint scoped to `hooks/` and an edit to `hooks/session_start.py`), that constraint is injected right then via `additionalContext`. Session start briefs the rules; this enforces them at the moment of edit. Once per constraint per session
- **PostToolUse (any tool)** — `constraint_reinject.py`: **opt-in, default off.** When `constraint_reinjection.enabled` is set, it counts tool calls per session and re-injects the constraints-only block every `every_n_tools` calls via `additionalContext`, so rules injected at session start don't decay as tool output buries them. PostToolUse is chosen deliberately: it's a model-visible surface (unlike PreCompact) and its firing rate tracks tool-output volume. Not a timer — an MCP server has no wall-clock in the context window
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
    reinject_state.json      # Per-session tool counter for constraint re-injection (auto-generated)
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
  "min_relevance": 0.20,
  "markdown_export": {
    "enabled": false,
    "path": "DECISIONS.md"
  },
  "constraint_reinjection": {
    "enabled": false,
    "every_n_tools": 25
  },
  "semantic": {
    "enabled": false,
    "weight": 150,
    "model": "nomic-embed-text",
    "url": "http://localhost:11434",
    "api": "ollama",
    "api_key_env": ""
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

Any OpenAI-compatible endpoint works too: set `"api": "openai"` and point `url`
at an LM Studio / llama.cpp server (`http://localhost:1234`) or OpenAI itself,
with `"api_key_env"` naming the environment variable that holds the key.

## Cross-Project Context

Query another project's context by passing `project_dir`:

```
Claude: [calls get_context with project_dir="/path/to/other-project"]
```

Or tag entries with other project names for cross-referencing.
