# Context Keeper MCP Server

Context Keeper maintains project memory across Claude conversations: architectural decisions, pipeline flows, and constraints that must not be forgotten or violated.

## CRITICAL: Always Confirm the Project Before Writing

Context Keeper stores data in a `.context/` directory inside a project. The server resolves the project directory from the `CONTEXT_KEEPER_PROJECT` env var, or falls back to the current working directory. That fallback is dangerous — Claude Code's cwd often isn't the project you think you're working on.

**Before calling any `record_*` tool, you MUST:**
1. Tell the user what project you're about to record to (e.g., "I'm about to record this decision to `C:/Users/jarms/repos/skillmatch-mcp/.context/`")
2. Ask the user to confirm the target project
3. If the user specifies a different project, pass `project_dir` explicitly (if supported) or warn the user to set `CONTEXT_KEEPER_PROJECT` before proceeding

**Before calling `get_project_summary` or `get_context` at session start:**
1. Ask the user which project they're working on
2. Call the tool with the appropriate `project_dir` if possible, or verify the cwd matches their intent

Never silently write to whatever directory happens to be the cwd. Stale or misplaced context entries are worse than no entries.

## When to Record

### Record a Decision when:
- You and the user choose between multiple approaches
- A technical trade-off is made (e.g., "JSON over SQLite because human-editable")
- A library, pattern, or architecture is selected
- The user says "let's go with X" after discussing options

Call `record_decision` with summary, rationale, and alternatives considered. Always include constraints_created if the decision limits future choices.

### Record a Pipeline when:
- A multi-step workflow is established (build, deploy, data processing)
- Steps have ordering dependencies (A must happen before B)
- The user describes "the flow" or "the process"

Call `record_pipeline` with ordered steps. Include constraints like "never skip step 2" or "step 3 requires output from step 1."

### Record a Constraint when:
- The user says "never do X" or "always do Y"
- A gotcha or footgun is discovered ("running from source breaks the scheduler")
- A project convention is established ("all API responses use camelCase")
- An external requirement exists ("must support Python 3.12+")

Call `record_constraint` with the rule, reason, scope, and hardness. Use hardness=absolute for true invariants, advisory for preferences.

## When to Retrieve

### At conversation start:
1. Call `get_compaction_report` first. If the report shows discrepancies (missing or modified entries), surface them to the user before doing anything else. Missing entries may need to be re-recorded.
2. Then call `get_project_summary` to orient yourself on the project's decisions, pipelines, and constraints. This prevents you from suggesting changes that violate established patterns.

### Before making architectural changes:
Call `get_context` with tags or a query describing what you're about to change. Check for conflicting decisions or constraints before proposing changes.

### When the user asks "why did we...":
Call `get_context` with relevant tags to find the decision with its rationale.

### Before modifying a pipeline:
Call `get_context` with the pipeline name or tags to see the current flow and its constraints.

## When NOT to Record
- Trivial implementation details (variable names, formatting choices)
- Temporary workarounds that will be removed
- Information already in the code comments or README
- One-off debugging steps

## Staleness Management
Periodically (every few sessions or when the user asks), call `prune_stale` to find entries that haven't been verified recently. Present stale entries to the user and ask: "Is this still accurate?" Then either:
- Call `update_entry` to refresh verified_at (confirming it's still valid)
- Call `deprecate_entry` if it's no longer relevant

## Tags Convention
Use lowercase, hyphen-separated tags. Common categories:
- Component names: auth, api, database, ui, deployment
- Cross-cutting: architecture, security, performance, testing
- Project names: skillmatch, conductor (for cross-project references)
