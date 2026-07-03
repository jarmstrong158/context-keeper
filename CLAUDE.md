# Context Keeper MCP Server

Context Keeper maintains project memory across Claude conversations: architectural decisions, pipeline flows, and constraints that must not be forgotten or violated.

## Project Resolution

Context Keeper stores data in a `.context/` directory inside a project. The server resolves the project directory in this order:
1. `CONTEXT_KEEPER_PROJECT` env var (explicit opt-in — trusted)
2. cwd, but **only if** it already contains a `.context/` directory
3. Walk parent dirs from cwd, returning the first ancestor that already contains a `.context/` (git-style discovery — so launching from a subdirectory of your project still resolves correctly)
4. Otherwise: refuse. `record_*`, `update_entry`, `deprecate_entry`, and `prune_stale` all return an "unresolved project" error.

Steps 2 and 3 only resolve to directories that **already** contain `.context/`. The server never creates one implicitly, so you will never silently create a stray `.context/` in the wrong directory. The footgun from earlier versions — where Claude Code was launched from a parent directory and polluted it — is fixed at the code level.

**All 11 tools accept `project_dir`** for explicit cross-project targeting. When cwd doesn't resolve, pass `project_dir` to any tool — including `record_*`.

**Still good practice:**
- When recording to a non-obvious project, confirm with the user which project you're targeting before calling `record_*`.

## Capture Loop

Context Keeper has two halves:

1. **Retrieval** (session start): The SessionStart hook injects the compaction report and project summary directly into context (it runs the handlers itself and prints their output). Retrieval is automatic and unskippable — you do not need to call the tools to be oriented on what's already recorded. `get_compaction_report` / `get_project_summary` remain callable on demand.
2. **Capture** (during session + pre-compaction): Record decisions, constraints, and pipelines *as they happen* during the session. **A git commit is the capture trigger**: a commit that establishes a decision/constraint/gotcha is not finished until the matching record_*/update_entry lands in the same work cycle — never batch capture "for later" (in field use, "later" never came and the user had to ask three times in one night). The PostToolUse commit-reminder hook injects this prompt automatically after every `git commit`; the SessionStart hook injects a quality-scan nudge at turn one (PreCompact stdout is user-visible only, so it cannot prompt the model). Don't rely on either — record in-line whenever possible.

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

### On every record_* call (v0.7+):
- **`retrieval_hints`** — add 2-4 alternate phrasings a future session might search for: synonyms, the symptom you saw, the error message. Ask "what would I have typed to find this before I knew its name?" This is what rescues vocabulary-mismatch queries.
- **`origin`** — set `"user"` when the user explicitly stated the decision/rule in their own words; leave the default `"agent"` when you inferred it from the work; `"import"` for backfills. User-origin entries get a retrieval trust boost, so don't inflate: only claim `user` for things the user actually said.

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
- Set `scope` to a real file/directory path (e.g. `hooks/`, `server.py`) whenever the rule is localized — the scope_guard hook re-injects scoped constraints at the moment a covered file is edited, so a precise scope turns the rule into an active guardrail instead of a session-start memo
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

## Acting on similar_entries (v0.6+)
When a `record_*` call succeeds but the response includes `similar_entries`, do not ignore it. Those are existing active entries whose text heavily overlaps what you just recorded. Resolve immediately, while you still have context:
- **Restatement** — deprecate the entry you just created and `update_entry` the original instead (or vice versa if yours is richer).
- **Contradiction** — decide which is current; `deprecate_entry` the loser with `superseded_by` pointing at the winner.
- **Genuinely distinct** — link them: `update_entry` your new entry with `related_to` including the similar ids.

## Acting on no_confident_match (v0.10+)
When `get_context` returns `no_confident_match: true`, the top entry's lexical relevance to your query was below the floor — the returned entries are the closest neighbors but probably nothing was recorded on this exact topic. Do **not** present them as established project decisions. Treat it as "no memory on this" unless an entry genuinely fits on inspection. The entries are still included (not suppressed) so you can judge; `top_relevance` is the score.

## Superseding vs deprecating a decision (v0.10+)
Two different lifecycle actions:
- **`record_decision(..., supersedes=[old_id])`** — the new decision *replaces* an older one that was correct at the time. The old entry becomes `superseded`: demoted in ranking but still recallable, so "why did we change from X to Y?" still works. Use this for the normal evolution of a decision.
- **`deprecate_entry(id, reason)`** — the entry was wrong, obsolete, or removed. It is filtered out of retrieval entirely. Use this when the history should *not* surface.

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
