<!-- mcp-name: io.github.jarmstrong158/context-keeper -->

# Context Keeper

Project memory for Claude. Records design decisions, pipeline flows, and constraints so Claude maintains context across conversations.

## The Problem

As conversations get long, Claude loses the "why" behind earlier decisions. New conversations start blank. This causes Claude to make changes that break established patterns — like rewriting a pipeline step it doesn't remember exists.

## The Solution

Context Keeper gives Claude 9 tools to record and retrieve structured project context:

| Tool | Purpose |
|------|---------|
| `record_decision` | Save a decision with rationale and alternatives |
| `record_pipeline` | Save a multi-step workflow with ordering |
| `record_constraint` | Save a rule with scope and enforcement level |
| `get_context` | Retrieve relevant entries by query, tags, scope, or ID |
| `get_project_summary` | Compact overview for conversation start |
| `update_entry` | Update any entry by ID |
| `deprecate_entry` | Retire an entry with reason |
| `prune_stale` | Find entries not verified recently |
| `get_compaction_report` | Check if last compaction lost any context |

All data stored as human-editable JSON files in `.context/` inside your project directory. Zero external dependencies.

## Install

```bash
pip install context-keeper
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

Set `CONTEXT_KEEPER_PROJECT` to the root of your project. If omitted, it uses the current working directory.

## How It Works

### Recording Context

When you make a design decision:
```
You: Let's use JSON files instead of SQLite for storage.
Claude: [calls record_decision with summary, rationale, and alternatives]
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

At conversation start, Claude calls `get_project_summary` to see all active decisions, pipelines, and constraints. Before making changes, it calls `get_context` with relevant tags to check for conflicts.

### Relevance Scoring

Without embeddings or external services, Context Keeper scores entries using:
- **Tag match** — overlap between query and entry tags
- **Text match** — query words found in summary/rationale/rule text
- **Recency** — recently verified entries score higher
- **Status** — active entries prioritized over superseded

Results are capped by a configurable token budget (default: 4000 tokens).

## Claude Code Hook Setup

Context Keeper includes hooks that snapshot your context before Claude Code compaction and detect if anything was lost afterward.

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
            "command": "python -c \"print('[Context Keeper] At session start: call get_compaction_report first, then get_project_summary. Record new decisions, pipelines, and constraints as they happen.')\""
          }
        ]
      }
    ]
  }
}
```

Replace `/path/to/context-keeper` with the actual install path. Set `CONTEXT_KEEPER_PROJECT` env var if your project isn't in the current working directory.

**Windows users:** Use forward slashes (`C:/Users/.../context-keeper/hooks/pre_compact.py`) or double-escaped backslashes in JSON. Single backslashes get mangled by the shell.

The hooks do three things:
- **PreCompact** — snapshots all active `.context/` entries before Claude Code compaction
- **Stop** — compares post-compaction state against the snapshot, writes a diff report if anything changed
- **SessionStart** — reminds Claude to call `get_compaction_report` and `get_project_summary` at the start of every new session

At session start, Claude calls `get_compaction_report` to check if the last compaction lost any context entries. If discrepancies are found, they're surfaced before any work begins.

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
