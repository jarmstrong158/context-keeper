# Context Keeper MCP Server

Context Keeper maintains project memory across Claude conversations: architectural decisions, pipeline flows, and constraints that must not be forgotten or violated.

## Project Resolution

Context Keeper stores data in a `.context/` directory inside a project. The server resolves the project directory in this order:
1. `CONTEXT_KEEPER_PROJECT` env var (explicit opt-in — trusted)
2. cwd, but **only if** it already contains a `.context/` directory
3. Walk parent dirs from cwd, returning the first ancestor that already contains a `.context/` (git-style discovery — so launching from a subdirectory of your project still resolves correctly)
4. Otherwise: refuse. `record_*`, `update_entry`, `deprecate_entry`, and `prune_stale` all return an "unresolved project" error.

Steps 2 and 3 only resolve to directories that **already** contain `.context/`. The server never creates one implicitly, so you will never silently create a stray `.context/` in the wrong directory. The footgun from earlier versions — where Claude Code was launched from a parent directory and polluted it — is fixed at the code level.

**All 9 tools accept `project_dir`** for explicit cross-project targeting. When cwd doesn't resolve, pass `project_dir` to any tool — including `record_*`.

**Still good practice:**
- When recording to a non-obvious project, confirm with the user which project you're targeting before calling `record_*`.

## Capture Loop

Context Keeper has two halves:

1. **Retrieval** (session start): The SessionStart hook injects the compaction report and project summary directly into context (it runs the handlers itself and prints their output). Retrieval is automatic and unskippable — you do not need to call the tools to be oriented on what's already recorded. `get_compaction_report` / `get_project_summary` remain callable on demand.
2. **Capture** (during session + pre-compaction): Record decisions, constraints, and pipelines *as they happen* during the session. The PreCompact hook fires a reminder before compaction, prompting you to review the session and record anything important before context is compressed. This is a safety net — don't rely on it. Record in-line whenever possible.

Both halves must work for the system to be useful. Retrieval without capture means the same entries get stale. Capture without retrieval means you don't know what's already recorded.

## When to Record

### Record a Decision when:
- You and the user choose between multiple approaches
- A technical trade-off is made (e.g., "JSON over SQLite because human-editable")
- A library, pattern, or architecture is selected
- The user says "let's go with X" after discussing options

Call `record_decision` with the v0.4 structured fields:
- `summary` — short label (1-2 sentences)
- `problem` (required, ≥40 chars) — what forced this decision, what was at stake
- `why_chosen` (required, ≥60 chars) — actual reasoning, evidence, principle behind the choice
- `what_we_tried` (optional but strongly encouraged) — prior attempts and why they failed (the "we tried X 3 times before Y" arc — the most valuable field for future sessions)
- `tradeoffs` (optional) — what was given up by choosing this
- `alternatives` — options considered with reasons rejected
- `constraints_created` — new constraints this decision introduces
- `related_to` — IDs of related entries (link to constraints created, prior decisions, etc.)
- `tags` — for retrieval

**Write rationales as mini-narratives, not summaries.** The schema enforces 60-char minimums on `why_chosen`, but the realistic floor for a useful entry is 2-4 sentences per structured field. Future-you cannot recover the "why" you didn't write.

### Record a Pipeline when:
- A multi-step workflow is established (build, deploy, data processing)
- Steps have ordering dependencies (A must happen before B)
- The user describes "the flow" or "the process"

Call `record_pipeline` with:
- `name`, ordered `steps`, optional `constraints`
- `purpose` (required, ≥40 chars) — why this pipeline exists, what it accomplishes that ad-hoc steps couldn't
- `when_to_invoke` (optional but encouraged) — triggers/conditions that should make a future session reach for this pipeline (the reusable knowledge)
- `related_to` for arc linking, `tags` for retrieval

### Record a Constraint when:
- The user says "never do X" or "always do Y"
- A gotcha or footgun is discovered ("running from source breaks the scheduler")
- A project convention is established ("all API responses use camelCase")
- An external requirement exists ("must support Python 3.12+")

Call `record_constraint` with:
- `rule`, `scope`, `hardness` (absolute for true invariants, advisory for preferences)
- `reason` (required, ≥40 chars) — what goes wrong if it's violated, concretely
- `triggering_incident` (optional but encouraged) — the specific bug/gotcha/incident that led to this rule (concrete > abstract for future sessions)
- `related_to` (link to the decision that created this constraint, etc.), `tags`

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

## Quality Verification (v0.4+)
Call `verify_quality` periodically — and especially before compaction — to scan for:
- **legacy** entries (pre-v0.4 schema, missing structured fields) — candidates for re-recording
- **thin_reason** entries (rationale text below threshold) — enrich via `update_entry`
- **no_tags** entries (won't surface in tag queries)
- **isolated** entries (share tags with siblings but have no `related_to` links — a missed arc link)

The PreCompact hook calls `verify_quality` automatically and prints flagged entries. When you see them, enrich what you can while the session context is still warm — once compaction fires, the connective tissue may be unrecoverable.

## Tags Convention
Use lowercase, hyphen-separated tags. Common categories:
- Component names: auth, api, database, ui, deployment
- Cross-cutting: architecture, security, performance, testing
- Project names: skillmatch, conductor (for cross-project references)
