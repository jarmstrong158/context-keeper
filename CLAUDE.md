# Context Keeper MCP Server

Context Keeper maintains project memory across Claude conversations: architectural decisions, pipeline flows, and constraints that must not be forgotten or violated.

## Project Resolution

Context Keeper stores data in a `.context/` directory inside a project. The server resolves the project directory in this order:
1. `CONTEXT_KEEPER_PROJECT` env var (explicit opt-in — trusted)
2. cwd, but **only if** it already contains a `.context/` directory
3. Otherwise: refuse. `record_*`, `update_entry`, `deprecate_entry`, and `prune_stale` all return an "unresolved project" error.

This means you will never silently create a stray `.context/` in the wrong directory. The footgun from earlier versions — where Claude Code was launched from a parent directory and polluted it — is fixed at the code level.

**Still good practice:**
- When recording to a non-obvious project, confirm with the user which project you're targeting before calling `record_*`.
- For cross-project work, `get_context`, `get_project_summary`, `update_entry`, `deprecate_entry`, and `prune_stale` all accept an explicit `project_dir` parameter. Prefer that over relying on cwd.

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
