<!-- mcp-name: io.github.jarmstrong158/context-keeper -->

# Context Keeper

Project memory for Claude. Records design decisions, pipeline flows, and constraints so Claude maintains context across conversations.

## The Problem

As conversations get long, Claude loses the "why" behind earlier decisions. New conversations start blank. This causes Claude to make changes that break established patterns — like rewriting a pipeline step it doesn't remember exists.

## The Solution

Context Keeper gives Claude 10 tools to record and retrieve structured project context:

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

All data stored as human-editable JSON files in `.context/` inside your project directory. Zero external dependencies.

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
      }
    ]
  }
}
```

Replace `/path/to/context-keeper` with the actual install path. Set `CONTEXT_KEEPER_PROJECT` env var if your project isn't in the current working directory.

**Windows users:** Use forward slashes (`C:/Users/.../context-keeper/hooks/pre_compact.py`) or double-escaped backslashes in JSON. Single backslashes get mangled by the shell.

The hooks form a complete capture-and-retrieval loop:

- **SessionStart** — imports the server's own handlers and prints the project summary (plus any compaction-discrepancy report) straight to stdout, which Claude Code injects into context at turn one. This replaces the older approach of printing an instruction to *call* the tools — a request that reliably lost to a task-focused first turn since the tools are deferred. Stays silent when the project has no `.context/` yet, and emits ASCII-only output so it cannot crash on Windows cp1252 stdout
- **PostToolUse (Bash)** — fires after every Bash tool call; when the command contains `git commit`, it injects a reminder to record the matching decision/constraint/gotcha **in the same work cycle**. A commit is the single best capture trigger — it's the exact moment something became real enough to persist in version control. Born from field use: during incident-heavy sessions the agent batched capture "for later," and the user had to ask "update context keeper" three times in one night while a dozen commits shipped
- **PreCompact** — snapshots all active `.context/` entries, runs a quality scan (`verify_quality`), and prints a capture prompt + any flagged entries (thin reasoning, missing tags, isolated arcs) so Claude can enrich them before context is compressed
- **Stop** — compares post-compaction state against the snapshot, writes a diff report if anything changed (idempotent — skips if the snapshot hasn't changed since last comparison)

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
  "stale_threshold_days": 30
}
```

## Cross-Project Context

Query another project's context by passing `project_dir`:

```
Claude: [calls get_context with project_dir="/path/to/other-project"]
```

Or tag entries with other project names for cross-referencing.
