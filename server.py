#!/usr/bin/env python3
"""Context Keeper MCP Server — Project memory for Claude.

Records and retrieves design decisions, pipeline flows, and constraints
so Claude maintains context across conversations. Zero external dependencies.
"""

import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone

# Optional local modules. Imported defensively so a partial checkout, or a
# vendored copy of server.py on its own, still starts — every call site treats
# a missing module as "no information available" rather than an error.
try:
    import code_drift
except ImportError:  # pragma: no cover - defensive
    code_drift = None
try:
    import usage
except ImportError:  # pragma: no cover - defensive
    usage = None
try:
    import work_focus
except ImportError:  # pragma: no cover - defensive
    work_focus = None

# Two-way mirror (local <-> remote). Optional and fail-soft: with no
# CONTEXT_KEEPER_REMOTE_URL set, every mirror call is a no-op. Imported at
# top level; mirror.py imports server lazily (inside functions) so there is
# no import-time circular dependency.
try:
    import mirror as _mirror
except Exception:  # never let a mirror import problem break the server
    _mirror = None

# Single source of truth for the version *inside the Python package*. The
# same literal also lives in pyproject.toml, server.json (twice) and
# mcpb/manifest.json, which the packaging formats require verbatim -- so
# tests/test_server.py::TestVersionConsistency asserts all five agree. Bump
# them together or that test fails the build.
__version__ = "0.16.0"

# Store location and raw reads live in store_paths so the hooks can have them
# without paying for this module's imports (~73ms, mostly mirror -> urllib).
# Re-exported here because every existing caller and test refers to them
# through server. One implementation, two import costs.
from store_paths import (  # noqa: E402
    CONTEXT_DIR_NAME,
    _read_json_file_checked,
    _resolve_project_dir,
    _xylem_active_project_file,
    _xylem_session_project,
)


def read_json_file(path):
    """Soft read for retrieval paths: missing or corrupt both yield [].

    Write paths must use _load_entries_for_write instead — silently
    treating a corrupt store as empty turns the next append into a
    full-history wipe.

    Deliberately a wrapper here rather than an import of store_paths'
    identical function: every read server makes must pass through THIS
    module's _read_json_file_checked, so patching that one symbol still
    intercepts all of them. TestStaleIndexWrites simulates a concurrent
    edit by counting reads through it, and an import would have routed
    half of them past the patch point.
    """
    entries, _err = _read_json_file_checked(path)
    return entries

# Where the scoped-constraint rules projection writes. A subdirectory of
# .claude/rules/ rather than the directory itself, so regeneration can retire
# its own stale files without ever considering a hand-written rule.
RULES_DIR_DEFAULT = os.path.join(".claude", "rules", "context-keeper")

PROJECT_DIR = _resolve_project_dir()
CONTEXT_DIR = os.path.join(PROJECT_DIR, CONTEXT_DIR_NAME) if PROJECT_DIR else None
DECISIONS_PATH = os.path.join(CONTEXT_DIR, "decisions.json") if CONTEXT_DIR else None
PIPELINES_PATH = os.path.join(CONTEXT_DIR, "pipelines.json") if CONTEXT_DIR else None
CONSTRAINTS_PATH = os.path.join(CONTEXT_DIR, "constraints.json") if CONTEXT_DIR else None
CONFIG_PATH = os.path.join(CONTEXT_DIR, "config.json") if CONTEXT_DIR else None

UNRESOLVED_PROJECT_ERROR = {
    "error": (
        "No project resolved. Set the CONTEXT_KEEPER_PROJECT environment "
        "variable to the project root, or run from a directory that already "
        "contains a .context/ folder. Refusing to create one implicitly."
    )
}

DEFAULT_CONFIG = {
    "token_budget": 4000,
    "max_entry_tokens": 1000,
    "stale_threshold_days": 30,
    "project_name": "",
    # Abstention floor: get_context flags no_confident_match when the top
    # entry's relevance is below this (0-1). 0.20 is the highest value with
    # zero false-abstention on the eval set (positive queries never fall
    # below it). Results are still returned — this only annotates.
    #
    # The relevance it compares against is tag/text overlap, plus — when the
    # opt-in semantic blend is on — the CALIBRATED cosine (see
    # _semantic_relevance). The calibration is not optional decoration: raw
    # nomic-embed cosines never drop below ~0.51 even for a completely
    # unrelated question, so comparing a raw cosine to this floor would put
    # every query above it and disable abstention entirely. Tune the
    # calibration for a different embedding model with
    # semantic.relevance_floor / semantic.relevance_ceiling.
    "min_relevance": 0.20,
    # Opt-in derived projection: regenerate a human-readable DECISIONS.md
    # from decisions.json on every decision write. The markdown is
    # read-only output — never parsed back in, never merged.
    "markdown_export": {"enabled": False, "path": "DECISIONS.md"},
    # Opt-in derived projection: mirror scoped constraints into Claude
    # Code's own .claude/rules/ format, one file per scope, each carrying
    # `paths:` frontmatter. The harness then loads the rule when Claude
    # READS a covered file -- earlier than the scope_guard hook can fire,
    # and with no hook wired at all. Same contract as markdown_export:
    # derived, regenerated whole, never parsed back in.
    "rules_export": {"enabled": False, "path": RULES_DIR_DEFAULT},
    # Opt-in periodic constraint re-injection. SessionStart injects the
    # constraints once at turn one; as a long session fills with tool
    # output those rules get buried. When enabled, the PostToolUse
    # constraint_reinject.py hook re-surfaces the constraints-only block
    # every `every_n_tools` tool calls via additionalContext. Default off
    # so nothing changes unless a project asks for it. This is the ONLY
    # honest mid-session surface: PreCompact stdout is not injected into
    # the model, and no wall-clock timer exists — the hook counts tool
    # calls (the thing actually burying context), not seconds.
    "constraint_reinjection": {"enabled": False, "every_n_tools": 25},
    # Opt-in escalation for the scope_guard hook when wired under PreToolUse:
    # an edit to a file covered by an ABSOLUTE constraint returns
    # permissionDecision "ask" so the user confirms, instead of only
    # annotating the tool result. Default off -- it interrupts, and it
    # escalates on scope, not on an actual violation (nothing reads the diff).
    "scope_guard": {"confirm_absolute": False},
}

USAGE_GUIDANCE = (
    "Context Keeper maintains project memory across conversations. "
    "Call get_project_summary at conversation start to orient yourself. "
    "Call get_context before making architectural changes. "
    "Record decisions when choosing between approaches. "
    "Record pipelines when multi-step workflows are established. "
    "Record constraints when 'never do X' or 'always do Y' patterns emerge. "
    "Do NOT record trivial details (variable names, formatting, one-off debugging). "
    "Periodically call prune_stale and verify or deprecate flagged entries."
)

# ============================================================
# Tool definitions
# ============================================================

TOOLS = [
    {
        "name": "record_entry",
        "description": (
            "Unified write tool: record a decision, constraint, or pipeline. "
            "Required fields by kind (validated server-side): decision needs "
            "summary/problem/why_chosen; constraint needs rule/reason; pipeline "
            "needs name/purpose/steps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["decision", "constraint", "pipeline"],
                    "description": "Which kind of entry to record.",
                },
                # decision fields
                "summary": {"type": "string", "description": "decision: what was decided (1-2 sentences)."},
                "problem": {"type": "string", "description": "decision: what forced it. Min 40 chars."},
                "why_chosen": {"type": "string", "description": "decision: the reasoning. Min 60 chars."},
                "what_we_tried": {"type": "string", "description": "decision: prior attempts. Encouraged."},
                "tradeoffs": {"type": "string", "description": "decision: what was given up. Encouraged."},
                "alternatives": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "option": {"type": "string"}, "reason_rejected": {"type": "string"}}},
                    "description": "decision: options considered and why rejected.",
                },
                "constraints_created": {"type": "array", "items": {"type": "string"},
                                        "description": "decision: new constraints this introduces."},
                "supersedes": {"type": "array", "items": {"type": "string"},
                               "description": "decision: ids of prior decisions this replaces."},
                # constraint fields
                "rule": {"type": "string", "description": "constraint: the rule, imperative."},
                "reason": {"type": "string", "description": "constraint: why it exists. Min 40 chars."},
                "triggering_incident": {"type": "string", "description": "constraint: the incident behind it."},
                "enforced_by": {"type": "string", "description": "constraint: test/command that checks it (named, never run)."},
                "scope": {"type": "string", "description": "constraint: 'global' or a file/module path.",
                          "default": "global"},
                "hardness": {"type": "string", "enum": ["absolute", "advisory"],
                             "description": "constraint: absolute vs advisory.", "default": "absolute"},
                # pipeline fields
                "name": {"type": "string", "description": "pipeline: name."},
                "purpose": {"type": "string", "description": "pipeline: why it exists. Min 40 chars."},
                "when_to_invoke": {"type": "string", "description": "pipeline: triggers. Encouraged."},
                "steps": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "order": {"type": "integer"}, "action": {"type": "string"},
                        "output": {"type": "string"}}, "required": ["order", "action"]},
                    "description": "pipeline: ordered steps.",
                },
                "constraints": {"type": "array", "items": {"type": "string"},
                                "description": "pipeline: rules that apply."},
                # shared fields
                "related_to": {"type": "array", "items": {"type": "string"},
                               "description": "ids of related entries; get_context traverses these."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "tags for retrieval."},
                "retrieval_hints": {"type": "array", "items": {"type": "string"},
                                    "description": "2-4 alternate phrasings for retrieval."},
                "origin": {"type": "string", "enum": ["user", "agent", "import"],
                           "description": "who authored this (default agent)."},
                "project_dir": {"type": "string",
                                "description": "Absolute path to the target project. Creates .context/ if needed."},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "get_context",
        "description": (
            "Retrieve relevant project context ranked by relevance within a token budget. "
            "Pass an id to fetch a single entry at full fidelity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Fetch a single entry by ID (e.g. 'dec-001')"},
                "query": {"type": "string", "description": "Free-text description of what you're working on"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to entries with any of these tags",
                },
                "scope": {"type": "string", "description": "File or module path to focus on"},
                "token_budget": {
                    "type": "integer",
                    "description": "Max tokens to return (default: from config)",
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["decisions", "pipelines", "constraints"]},
                    "description": "Limit to specific entry types. Default: all.",
                },
                "since": {
                    "type": "string",
                    "description": "Entries verified/created on or after this ISO date.",
                },
                "before": {
                    "type": "string",
                    "description": "Entries verified/created strictly before this ISO date.",
                },
                "include_related": {
                    "type": "boolean",
                    "description": "Also pull entries linked via related_to (depth 1). Default: true.",
                    "default": True,
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project to read context from",
                },
            },
        },
    },
    {
        "name": "query_entries",
        "description": (
            "Structured field filter — exact AND-combined predicates, NOT relevance "
            "search. Use when you know the field values (e.g. absolute constraints "
            "scoped to 'hooks/'). Stable ID order, no ranking; empty is a real "
            "answer. Includes superseded/deprecated unless you filter status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["decisions", "pipelines", "constraints"]},
                    "description": "Restrict to these entry types. Default: all.",
                },
                "kind": {
                    "type": ["string", "array"],
                    "description": "Singular alias for `types`. `types` wins if both given.",
                },
                "text": {
                    "type": "string",
                    "description": "Free text over text fields; every term must appear (AND, substring).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap entries returned (matched_entries reports the total).",
                },
                "status": {
                    "type": ["string", "array"],
                    "description": "active/superseded/deprecated (string or list).",
                },
                "origin": {
                    "type": ["string", "array"],
                    "description": "user/agent/import (string or list). Missing counts as 'agent'.",
                },
                "tags_any": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Entries with AT LEAST ONE of these tags.",
                },
                "tags_all": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Entries with ALL of these tags.",
                },
                "scope": {
                    "type": "string",
                    "description": "Exact, case-sensitive match on a constraint's scope (e.g. 'hooks/').",
                },
                "hardness": {
                    "type": ["string", "array"],
                    "description": "Constraint hardness absolute/advisory (string or list).",
                },
                "supersedes": {
                    "type": "string",
                    "description": "The entry that superseded this id.",
                },
                "superseded_by": {
                    "type": "string",
                    "description": "Entries replaced by this id (superseded_by == id).",
                },
                "since": {"type": "string", "description": "Verified/created on or after this ISO date."},
                "before": {"type": "string", "description": "Verified/created strictly before this ISO date."},
                "token_budget": {"type": "integer", "description": "Max tokens returned (default from config)."},
                "project_dir": {"type": "string", "description": "Absolute path to another project to query."},
            },
        },
    },
    {
        "name": "get_project_summary",
        "description": (
            "The single orienting call for conversation start: counts by kind and "
            "status, active constraints (compact), recent decisions, the full id "
            "list, and the human-readable summary — all in one response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_budget": {
                    "type": "integer",
                    "description": "Max tokens for the summary (default: 2000)",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project to summarize",
                },
            },
        },
    },
    {
        "name": "update_entry",
        "description": (
            "Update an existing decision, pipeline, or constraint by ID. "
            "Refreshes verified_at timestamp automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Entry ID (e.g. 'dec-001', 'pipe-003', 'con-012')"},
                "updates": {
                    "type": "object",
                    "description": "Fields to update. Any field except id and created_at.",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project whose entry should be updated",
                },
            },
            "required": ["id", "updates"],
        },
    },
    {
        "name": "deprecate_entry",
        "description": "Mark an entry as deprecated. For decisions, optionally link to the superseding decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Entry ID to deprecate"},
                "reason": {"type": "string", "description": "Why this is being deprecated"},
                "superseded_by": {"type": "string", "description": "ID of the replacing decision (decisions only)"},
                "merge_into": {
                    "type": "string",
                    "description": (
                        "Dedup merge: fold this entry's unique content into this same-type "
                        "target, then deprecate this one with superseded_by=target. "
                        "Non-destructive — the target is only added to."
                    ),
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project whose entry should be deprecated",
                },
            },
            "required": ["id", "reason"],
        },
    },
    {
        "name": "prune_stale",
        "description": (
            "Find entries not verified in N days. Returns them for review — does not delete. "
            "Call periodically to keep context fresh."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Entries not verified in this many days are flagged (default: from config)",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project to prune",
                },
            },
        },
    },
    {
        "name": "get_compaction_report",
        "description": (
            "Check if the last compaction lost or modified any context entries. "
            "Surface discrepancies to the user before proceeding."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project to check",
                },
            },
        },
    },
    {
        "name": "verify_quality",
        "description": (
            "Scan entries for quality issues (legacy schema, thin reasoning, missing "
            "tags, isolated entries) and return them for enrichment via update_entry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_reason_chars": {
                    "type": "integer",
                    "description": "Below this length, flag the entry as thin (default: 80)",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to another project to verify",
                },
            },
        },
    },
    {
        "name": "export_markdown",
        "description": (
            "Regenerate DECISIONS.md from the decisions store — a derived, "
            "read-only projection; overwrites the file whole."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Output path relative to project root. Default: config markdown_export.path.",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to the target project.",
                },
            },
        },
    },
    {
        "name": "reload_constraints",
        "description": (
            "Re-surface the project's constraints only (not the full store) when a long "
            "session has buried the ones injected at start. Same block as session start."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to the target project.",
                },
            },
        },
    },
    {
        "name": "export_snapshot",
        "description": (
            "Write the whole store to a compressed, committable snapshot at "
            ".context-keeper/memory.json.gz (next to the project) and add a "
            ".gitattributes merge=ours guard. For sharing project memory with a team "
            "via git. Committing it is opt-in. Non-destructive to the working store."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path to the target project."},
            },
        },
    },
    {
        "name": "import_snapshot",
        "description": (
            "Import the committed .context-keeper/memory.json.gz snapshot into the "
            "working store. Non-destructive: a store that already has entries is left "
            "untouched. Runs automatically on first use when the store is empty."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Absolute path to the target project."},
            },
        },
    },
    {
        "name": "mirror",
        "description": (
            "Sync entries with the remote store. op='pull' merges remote entries "
            "into local (newest wins); op='backfill' pushes all local entries to "
            "remote (upsert). No-op if remote unconfigured."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["pull", "backfill"],
                       "description": "pull = remote→local merge; backfill = local→remote push."},
                "project_dir": {"type": "string"},
            },
            "required": ["op"],
        },
    },
]

# ============================================================
# File helpers
# ============================================================


def ensure_context_dir(path=None):
    os.makedirs(path or CONTEXT_DIR, exist_ok=True)


def _load_entries_for_write(path):
    """Load entries for a read-modify-write cycle. Returns (entries, error_dict).

    If the file exists but is corrupt, returns an error instead of []
    so the caller refuses to write over existing history.
    """
    entries, err = _read_json_file_checked(path)
    if err:
        return None, {
            "error": (
                f"Refusing to write: {os.path.basename(path)} exists but could not "
                f"be read ({err}). Writing now would silently discard every entry "
                f"already in the file. Fix or restore the file, then retry."
            )
        }
    # Heal any pre-updated_at entries in passing: this is a read-modify-write
    # cycle, so the backfilled timestamps persist on the coming write. The
    # mirror keys its two-way merge on updated_at, so every entry needs one.
    _backfill_updated_at(entries)
    return entries, None


def _backfill_updated_at(entries):
    """Give every entry an updated_at, backfilling from created_at (else now).

    Mutates the list in place; returns True if anything changed. Older stores
    only stamped updated_at on update/deprecate, so entries that were only ever
    recorded lack it — without a value the mirror's newest-wins comparison has
    nothing to compare, so we backfill their creation time as the baseline.
    """
    changed = False
    for e in entries:
        if isinstance(e, dict) and not e.get("updated_at"):
            e["updated_at"] = e.get("created_at") or now_iso()
            changed = True
    return changed


def write_json_file(path, data):
    """Atomically write entries: write to a temp file, then os.replace.

    A crash mid-write leaves the old file intact instead of a truncated
    JSON document that the next read would treat as an empty store.
    """
    ensure_context_dir(os.path.dirname(path))
    # Unique temp name per write. A shared "<path>.tmp" is atomic only within
    # one process; the SessionStart hook's pull_remote -> _merge_rows runs in a
    # DIFFERENT process from the live MCP server and both write the same store,
    # so a shared suffix lets two concurrent writers clobber each other's temp
    # file before os.replace. pid + uuid makes each writer's temp private.
    tmp = path + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Don't leave a stray temp file behind on a failed write.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_config(base_dir=None):
    cfg_path = os.path.join(base_dir, "config.json") if base_dir else CONFIG_PATH
    if cfg_path is None or not os.path.exists(cfg_path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def _leading_int(eid, prefix):
    """Parse the numeric part of an id, tolerating an Option-B suffix.

    'dec-012' -> 12, 'dec-012-a7f3' -> 12, non-matching -> None. Reads the
    run of digits right after the prefix dash and stops at the first
    non-digit, so a suffixed id still contributes its sequence number.
    """
    if not eid.startswith(prefix + "-"):
        return None
    rest = eid[len(prefix) + 1:]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def next_id(entries, prefix):
    """Mint the next id for a store.

    ID-collision safety (chosen approach = Option B, "short random suffix
    on new ids", per the mirror design):

      Two stores that mint sequential ids independently collide: the local
      desktop store and the remote Worker both hand out 'dec-013' for
      different decisions, and import_entries -- which skips existing
      (project, id) rather than overwriting -- would then silently drop
      whichever arrived second. The fix is to make every NEW id unique
      across stores by appending a short random hex suffix: 'dec-013-a7f3'.

      Two properties this preserves:
        - Old ids stay stable and readable. dec-001..dec-062 /
          con-001..con-018 are never rewritten; the number still leads, so
          they remain sortable and greppable, and DECISIONS.md is unchanged.
        - The suffix is only added when a SECOND store actually exists --
          i.e. when mirroring is configured (CONTEXT_KEEPER_REMOTE_URL set).
          With mirroring off there is exactly one writer, no collision is
          possible, and ids stay bare 'dec-013' (so single-store use and the
          existing test-suite expectations are untouched). A local suffixed
          id ('dec-013-a7f3') can never equal a remote-minted bare id
          ('dec-013') or another store's differently-suffixed id, so
          independent creation on both sides no longer collides.
    """
    max_num = 0
    for e in entries:
        num = _leading_int(e.get("id", ""), prefix)
        if num is not None:
            max_num = max(max_num, num)
    base = f"{prefix}-{max_num + 1:03d}"
    # Add the collision-avoidance suffix only when a second store exists.
    if _mirror is not None and _mirror.mirror_enabled():
        base += "-" + secrets.token_hex(2)
    return base


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(raw):
    """Parse an ISO date/timestamp into an aware UTC datetime, or None.

    Accepts date-only strings ('2026-06-01') and full timestamps; naive
    values are assumed UTC so they compare safely against stored
    (timezone-aware) entry timestamps.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _entry_timestamp(entry):
    """The timestamp used for recency and temporal filtering."""
    return _parse_iso_utc(
        entry.get("verified_at") or entry.get("updated_at") or entry.get("created_at"))


# ============================================================
# Scoring & token estimation
# ============================================================


def estimate_tokens(text):
    return max(1, len(text) // 4)


def _text_words(entry):
    """Extract searchable words from an entry's text fields.

    Pulls from both v0.4 structured fields (problem, why_chosen,
    what_we_tried, tradeoffs, purpose, when_to_invoke,
    triggering_incident) and the legacy `rationale` field so old
    entries remain searchable.
    """
    parts = []
    text_keys = (
        "summary", "rationale", "rule", "reason", "name",
        "problem", "why_chosen", "what_we_tried", "tradeoffs",
        "purpose", "when_to_invoke", "triggering_incident",
    )
    for key in text_keys:
        val = entry.get(key, "")
        if val:
            parts.append(val.lower())
    for tag in entry.get("tags", []):
        parts.append(tag.lower())
    # Anticipated-query hints: alternate phrasings supplied at record time,
    # indexed so vocabulary-mismatch queries hit without embeddings.
    for hint in entry.get("retrieval_hints", []) or []:
        parts.append(str(hint).lower())
    # Include step actions for pipelines
    for step in entry.get("steps", []):
        action = step.get("action", "")
        if action:
            parts.append(action.lower())
    return set(" ".join(parts).split())


def score_entry(entry, query_tags=None, query_text=None, scope=None, now_dt=None):
    score = 0.0
    entry_tags = set(t.lower() for t in entry.get("tags", []))
    entry_words = _text_words(entry)

    # Tag matching (0-40)
    if query_tags:
        q_tags = set(t.lower() for t in query_tags)
        overlap = len(entry_tags & q_tags)
        score += 40 * (overlap / max(len(q_tags), 1))

    # Free-text word matching against tags + text fields (0-40)
    if query_text:
        q_words = set(query_text.lower().split())
        overlap = len(q_words & entry_words)
        score += 40 * (overlap / max(len(q_words), 1))

    # If neither tags nor text query, give base score so all entries are considered
    if not query_tags and not query_text:
        score += 20

    # Scope matching (0-20)
    entry_scope = entry.get("scope", "global")
    if scope:
        if entry_scope != "global" and scope.lower() in entry_scope.lower():
            score += 20
        elif entry_scope == "global":
            score += 10
        else:
            score += 5
    elif entry_scope == "global":
        score += 10
    else:
        score += 5

    # Recency (0-20)
    verified = entry.get("verified_at") or entry.get("updated_at") or entry.get("created_at")
    if verified and now_dt:
        try:
            v_dt = datetime.fromisoformat(verified.replace("Z", "+00:00"))
            days_ago = (now_dt - v_dt).days
            # Clamp to [0, 1]: a future timestamp (clock skew, manual edit)
            # must not push recency past its 20-point cap.
            recency = max(0.0, min(1.0, 1 - (days_ago / 90)))
            score += 20 * recency
        except Exception:
            score += 10  # can't parse, give middle score

    # Status (0-20)
    status = entry.get("status", "active")
    if status == "active":
        score += 20
    elif status == "superseded":
        score += 5

    # Origin trust (0-10): user-stated entries outrank agent-inferred,
    # which outrank imported/backfilled. Entries without an origin
    # (pre-v0.7) score as agent — a uniform shift that preserves their
    # relative order.
    origin = entry.get("origin", "agent")
    score += {"user": 10, "agent": 5, "import": 2}.get(origin, 5)

    return score


# Common English + query-filler words that carry no retrieval signal. Small
# and dependency-free. Used ONLY for the abstention relevance signal below,
# never for the main ranking (which stays backward-compatible).
_STOPWORDS = frozenset("""
a an the this that these those of in on at to for from by with about into over
and or but if is are was were be been being do does did doing done have has had
how what why when where which who whom whose it its we our us you your they them
their i my me can could should would will shall may might must not no nor as so
than then there here out up down off again once only just also very more most
""".split())


# Cosine calibration for the abstention signal. These map a RAW embedding
# cosine onto the same 0-1 scale the lexical signal already lives on, and
# they are model-specific — do not treat them as universal.
#
# Why any mapping at all: a cosine from nomic-embed-text is not a
# probability and shares no scale with lexical overlap. Measured on this
# repo's eval set (evals/, 31 answerable + 16 no-answer queries across three
# real stores), nomic's cosines are compressed into a high, narrow band:
#
#     answerable queries   top cosine  min 0.636  median 0.719  max 0.815
#     NO-ANSWER queries    top cosine  min 0.515  median 0.603  max 0.718
#
# The bands overlap heavily, and the no-answer FLOOR is 0.515 — far above
# the 0.20 abstention floor. So the obvious implementation,
# max(lexical, cosine), puts every query on earth above the floor and
# silently disables abstention completely: measured TNR falls 19% -> 0%.
# ("what is the capital of France" scores ~0.49 against this store.)
#
# _SEM_REL_LO is therefore set just above the highest cosine any no-answer
# query reached (0.718), so the semantic term contributes exactly nothing
# until a match is stronger than anything a no-answer query produced, and
# lexical alone decides. Above that it ramps linearly to _SEM_REL_HI (near
# the observed answerable maximum), where it saturates at 1.0.
#
# Overridable per project via config semantic.relevance_floor /
# .relevance_ceiling, because a different embedding model has a different
# cosine distribution and these numbers would be wrong for it.
_SEM_REL_LO = 0.72
_SEM_REL_HI = 0.85


def _semantic_relevance(cosine, lo=_SEM_REL_LO, hi=_SEM_REL_HI):
    """Map a raw embedding cosine onto the lexical signal's 0-1 scale.

    Deliberately returns 0.0 for anything at or below `lo` rather than a
    small positive number: below the calibration floor the cosine carries
    no evidence that this entry answers the query, and letting it leak a
    fraction of a point is what would erode the abstention floor.
    """
    if cosine is None or hi <= lo:
        return 0.0
    if cosine <= lo:
        return 0.0
    return min(1.0, (cosine - lo) / (hi - lo))


def _relevance_signal(entry, query_tags, query_text, cosine=None,
                      sem_lo=_SEM_REL_LO, sem_hi=_SEM_REL_HI):
    """0-1 estimate of how well this entry matches the QUERY specifically —
    tag/text overlap, plus the calibrated semantic cosine when one is given.

    This is the signal the abstention floor keys on. The composite
    score_entry value is inflated by non-relevance terms (an active,
    recent, global entry banks ~55 points with zero query overlap), so a
    totally irrelevant entry can masquerade as a confident hit. This
    isolates the part that actually reflects "does this answer the query."

    `cosine` is the entry's embedding cosine when the opt-in semantic blend
    is active, and None otherwise. It matters because get_context already
    blends cosine into the RANKING: without it here, an entry retrieved
    purely on semantic similarity — the vocabulary-mismatch rescue the
    blend exists for — was ranked first and then flagged
    `no_confident_match` for having no lexical overlap, which is precisely
    backwards. The cosine is run through _semantic_relevance first; see the
    calibration note there for why the raw value must never be used
    directly.

    Returns None when no query is given — a bare summary/tag-less request
    is never an abstention case. Stopwords are dropped so filler like
    "what/the/we/about" doesn't manufacture overlap on no-answer queries.
    """
    sigs = []
    if query_tags:
        q = set(t.lower() for t in query_tags)
        if q:
            et = set(t.lower() for t in entry.get("tags", []))
            sigs.append(len(q & et) / len(q))
    if query_text:
        qw = {w for w in query_text.lower().split() if w and w not in _STOPWORDS}
        if qw:
            ew = _text_words(entry)
            sigs.append(len(qw & ew) / len(qw))
    if not sigs:
        # No lexical basis at all (no query text and no tags) -- a bare
        # request, not an abstention case, even if a cosine exists.
        return None
    if cosine is not None:
        sigs.append(_semantic_relevance(cosine, sem_lo, sem_hi))
    return max(sigs)


def _jaccard(a, b):
    """Word-set Jaccard similarity of two entries' text (zero-dependency)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _mmr_reorder(scored, lam=0.7):
    """Greedily reorder (score, type, entry) tuples for Maximal Marginal Relevance.

    Penalizes a candidate by its peak lexical similarity to already-selected
    entries, so near-duplicate restatements of one topic don't crowd the budget
    and a second relevant topic gets a seat. Entries linked by `related_to` are
    exempt from the penalty — those are intentional arcs meant to surface
    together, not redundancy. lam=1.0 reduces to pure relevance order.
    """
    if len(scored) < 2:
        return scored
    max_score = max((s for s, _t, _e in scored), default=0.0) or 1.0
    items = []
    for s, t, e in scored:
        items.append({
            "orig": s, "norm": s / max_score, "type": t, "entry": e,
            "words": _text_words(e), "id": e.get("id"),
            "related": set(e.get("related_to") or []),
        })
    selected, ordered = [], []
    while items:
        best_i, best_val = 0, None
        for i, d in enumerate(items):
            sim = 0.0
            for s in selected:
                if d["id"] in s["related"] or s["id"] in d["related"]:
                    continue  # arc-linked: no diversity penalty
                sim = max(sim, _jaccard(d["words"], s["words"]))
            val = lam * d["norm"] - (1 - lam) * sim
            if best_val is None or val > best_val:
                best_val, best_i = val, i
        chosen = items.pop(best_i)
        selected.append(chosen)
        ordered.append((chosen["orig"], chosen["type"], chosen["entry"]))
    return ordered


def _truncate_entry(entry, max_tokens):
    """Truncate an entry to fit within max_tokens. Returns a copy."""
    text = json.dumps(entry, indent=2)
    if estimate_tokens(text) <= max_tokens:
        return entry
    # Build truncated version with key fields only
    truncated = {"id": entry.get("id", "?")}
    for key in ("summary", "name", "rule"):
        if key in entry:
            truncated[key] = entry[key]
            break
    truncated["tags"] = entry.get("tags", [])
    truncated["status"] = entry.get("status", "active")
    truncated["_truncated"] = "Use get_context with this id for full entry"
    return truncated


# ============================================================
# Helpers for finding entries across files
# ============================================================

_PREFIX_TO_FILE = {"dec": "decisions", "pipe": "pipelines", "con": "constraints"}


def _resolve_paths(base_dir=None):
    """Return type->path mapping, optionally for another project.

    Returns None if no project is resolved and no base_dir is provided.
    """
    if base_dir:
        return {
            "decisions": os.path.join(base_dir, "decisions.json"),
            "pipelines": os.path.join(base_dir, "pipelines.json"),
            "constraints": os.path.join(base_dir, "constraints.json"),
        }
    if CONTEXT_DIR is None:
        return None
    return {
        "decisions": DECISIONS_PATH,
        "pipelines": PIPELINES_PATH,
        "constraints": CONSTRAINTS_PATH,
    }


def _base_dir_from_params(params):
    """Resolve a base .context/ dir from a params dict, or fall back to the
    module-level CONTEXT_DIR. Returns None if nothing is resolvable."""
    project_dir = params.get("project_dir")
    if project_dir:
        return os.path.join(os.path.normpath(project_dir), CONTEXT_DIR_NAME)
    # Resolve PER CALL (env > Xylem session pointer > cwd/.context discovery) so a
    # persistent server follows whichever project the session is in, instead of
    # the single dir resolved once at module import (the static CONTEXT_DIR).
    proj = _resolve_project_dir()
    return os.path.join(proj, CONTEXT_DIR_NAME) if proj else CONTEXT_DIR


def _find_entry_by_id(entry_id, base_dir=None):
    """Find an entry by ID across all files. Returns (entry, type_name, file_path, index)."""
    prefix = entry_id.split("-")[0] if "-" in entry_id else ""
    type_name = _PREFIX_TO_FILE.get(prefix)
    paths = _resolve_paths(base_dir)

    if paths is None:
        return None, None, None, None

    if type_name and type_name in paths:
        entries = read_json_file(paths[type_name])
        for i, e in enumerate(entries):
            if e.get("id") == entry_id:
                return e, type_name, paths[type_name], i
    else:
        # Search all files
        for tname, tpath in paths.items():
            entries = read_json_file(tpath)
            for i, e in enumerate(entries):
                if e.get("id") == entry_id:
                    return e, tname, tpath, i
    return None, None, None, None


def _reload_and_locate(entry_id, file_path):
    """Re-read a store for writing and locate `entry_id` in the FRESH list.

    Returns (entries, live_entry, error_dict). Exactly one of live_entry /
    error_dict is meaningful.

    Every read-modify-write handler here does two reads: one to find the
    entry, one to load the list it will write back. Between them the file can
    change -- another agent's record_*, a mirror pull, or the hand-edit the
    README explicitly invites ("the JSON is plain and safe to edit by hand").
    Carrying the *index* from the first read across to the second means the
    write lands wherever that slot happens to point now, silently overwriting
    an unrelated record. Re-finding by id is the only stable link between the
    two reads, and mutating the live object -- rather than assigning back the
    stale copy -- preserves concurrent edits to fields the caller isn't
    touching.
    """
    entries, load_err = _load_entries_for_write(file_path)
    if load_err is not None:
        return None, None, load_err
    for e in entries:
        if e.get("id") == entry_id:
            return entries, e, None
    return None, None, {
        "error": (
            f"Entry '{entry_id}' disappeared from "
            f"{os.path.basename(file_path)} between read and write "
            f"(concurrent edit?). Nothing was written -- retry.")
    }


# ============================================================
# Validation (v0.4 — schema-enforced rationale depth)
# ============================================================

_FIELD_GUIDANCE = {
    "summary": "1-2 sentences naming what was decided / what the rule is.",
    "problem": "Describe what triggered this decision in 1-3 sentences. What problem does it solve, and what was at stake?",
    "why_chosen": "Explain the actual reasoning. Why this option specifically? What evidence, principle, or constraint backs the choice?",
    "reason": "Explain why this rule exists. What goes wrong if it's violated? Be concrete.",
    "purpose": "Explain why this pipeline exists. What does it accomplish that ad-hoc steps couldn't?",
    "rule": "State the rule in clear imperative language ('Always X', 'Never Y').",
    "name": "A short pipeline name.",
}


def _check_min_lengths(params, requirements):
    """Validate that required string fields meet minimum lengths.

    requirements: dict of {field_name: min_chars}
    Returns an error dict if any field fails, else None. Errors include
    field-specific guidance to teach callers how to fix the entry rather
    than just rejecting it.
    """
    errors = []
    for field, min_len in requirements.items():
        val = params.get(field)
        actual = len(val.strip()) if isinstance(val, str) else 0
        if actual < min_len:
            errors.append({
                "field": field,
                "min_length": min_len,
                "actual_length": actual,
                "guidance": _FIELD_GUIDANCE.get(field, f"Field '{field}' is required."),
            })
    if errors:
        return {
            "error": (
                "Entry rejected: required fields missing or too short. The schema enforces "
                "depth so future sessions can recover the full why, not just a summary. "
                "Re-call with richer content for the fields below."
            ),
            "validation_errors": errors,
        }
    return None


_VALID_ORIGINS = {"user", "agent", "import"}


def _origin_from_params(params):
    """Coerce the origin field to a valid source level (default: agent)."""
    origin = params.get("origin", "agent")
    return origin if origin in _VALID_ORIGINS else "agent"


# ============================================================
# Similar-entry surfacing (dedup / conflict detection at capture)
# ============================================================

# Word-set Jaccard at or above this flags an existing entry as similar.
# Overridable per store via config "similar_threshold".
DEFAULT_SIMILAR_THRESHOLD = 0.30

# Two entries can overlap heavily for opposite reasons: one restates the other
# (dedup), or one reverses it (contradiction — the more dangerous case, because
# a silent contradiction leaves two live rules disagreeing). A lexical overlap
# score alone can't tell them apart. These two signals do, dependency-free:
#   1. Negation asymmetry — one entry carries a negation/prohibition marker the
#      other doesn't ("X is required" vs "X is not required").
#   2. Polarity split — each entry holds an opposing side of an antonym pair
#      ("always" here / "never" there; "enable" / "disable").
# Advisory only, and only consulted on already-high-overlap pairs, so an
# occasional false "contradiction" just prompts a second look — cheap, in
# keeping with the advisory-similar design.
_NEGATION_TOKENS = frozenset("""
never no not none nor cannot cant dont wont shouldnt neither without avoid
disable disabled forbid forbidden prohibit prohibited prevent deny denied
""".split())

_POLARITY_PAIRS = (
    frozenset({"always", "never"}),
    frozenset({"enable", "disable"}),
    frozenset({"enabled", "disabled"}),
    frozenset({"allow", "deny"}),
    frozenset({"allow", "forbid"}),
    frozenset({"require", "forbid"}),
    frozenset({"required", "forbidden"}),
    frozenset({"required", "optional"}),
    frozenset({"use", "avoid"}),
    frozenset({"add", "remove"}),
    frozenset({"include", "exclude"}),
    frozenset({"before", "after"}),
    frozenset({"keep", "drop"}),
    frozenset({"true", "false"}),
)


def _classify_overlap(new_words, other_words):
    """Label a high-overlap entry pair as a likely contradiction or restatement.

    Returns "likely_contradiction" when the two texts hold opposite polarity —
    a negation marker present on one side only, or each side holding an opposing
    member of an antonym pair — else "likely_restatement". Called only after
    Jaccard has already established the pair overlaps heavily, so this is purely
    the direction of that overlap.
    """
    neg_a = bool(new_words & _NEGATION_TOKENS)
    neg_b = bool(other_words & _NEGATION_TOKENS)
    if neg_a != neg_b:
        return "likely_contradiction"
    for pair in _POLARITY_PAIRS:
        x, y = tuple(pair)
        # Split = one side sits in one entry and the other side in the other,
        # and neither entry contains both (both-present = no directional signal).
        a_has = (x in new_words, y in new_words)
        b_has = (x in other_words, y in other_words)
        if a_has[0] and b_has[1] and not a_has[1] and not b_has[0]:
            return "likely_contradiction"
        if a_has[1] and b_has[0] and not a_has[0] and not b_has[1]:
            return "likely_contradiction"
    return "likely_restatement"


def _find_similar_entries(new_entry, base_dir, exclude_ids=None, threshold=None):
    """Compare a new entry's text against all active entries in the store.

    Returns a list of {id, type, summary, similarity} for entries whose
    word-set Jaccard similarity meets the threshold, most similar first
    (top 3). Entries the caller already linked via related_to are
    excluded — those are acknowledged relations, not accidental
    restatements.

    Purpose: surface near-duplicates and potential contradictions at
    capture time, so the store doesn't accumulate restatements that MMR
    then has to mitigate at retrieval time. Advisory only — the write
    always proceeds.
    """
    if threshold is None:
        threshold = read_config(base_dir).get("similar_threshold", DEFAULT_SIMILAR_THRESHOLD)
    exclude = set(exclude_ids or [])
    new_words = _text_words(new_entry)
    if not new_words:
        return []

    paths = _resolve_paths(base_dir)
    if paths is None:
        return []

    matches = []
    for tname, tpath in paths.items():
        for e in read_json_file(tpath):
            eid = e.get("id")
            if not eid or eid in exclude or eid == new_entry.get("id"):
                continue
            if e.get("status", "active") in ("deprecated", "superseded"):
                continue
            e_words = _text_words(e)
            sim = _jaccard(new_words, e_words)
            if sim >= threshold:
                matches.append({
                    "id": eid,
                    "type": tname,
                    "summary": e.get("summary") or e.get("name") or e.get("rule") or "?",
                    "similarity": round(sim, 2),
                    "origin": e.get("origin", "agent"),
                    "relation": _classify_overlap(new_words, e_words),
                })
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches[:3]


_SIMILAR_NOTE = (
    "Existing entries overlap heavily with this one. Each carries a `relation`: "
    "`likely_restatement` (same rule said twice) or `likely_contradiction` "
    "(this entry reverses the other). Review them: for a restatement, deprecate "
    "this entry and use update_entry on the original instead; for a "
    "contradiction, resolve which is current (deprecate_entry with "
    "superseded_by); if genuinely distinct, link them via related_to. "
    "When entries conflict, origin trust decides the default winner: "
    "user-stated overrides agent-inferred overrides imported."
)

_CONTRADICTION_NOTE = (
    "At least one overlap is flagged `likely_contradiction` — the new entry "
    "appears to REVERSE an existing one (opposite negation or antonym polarity), "
    "not restate it. Do not leave both live: pick the current rule and "
    "deprecate_entry the other with superseded_by, so the store never holds two "
    "rules that disagree. The flag is a heuristic; confirm before acting."
)


def _attach_similar(result, entry, base_dir):
    """Add similar-entry warnings to a successful record_* result."""
    try:
        similar = _find_similar_entries(
            entry, base_dir, exclude_ids=entry.get("related_to") or [])
    except Exception:
        return result
    if similar:
        result["similar_entries"] = similar
        result["similar_note"] = _SIMILAR_NOTE
        if any(m.get("relation") == "likely_contradiction" for m in similar):
            result["contradiction_note"] = _CONTRADICTION_NOTE
    return result


# ============================================================
# Markdown projection — DECISIONS.md as a derived, regenerated file
# ============================================================


def _md_entry_num(entry):
    """dec-007 -> 7, for chronological ordering. Unparseable ids sort first."""
    try:
        return int(str(entry.get("id", "")).split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _md_date(entry):
    """MM-DD from created_at, matching the hand-maintained heading convention."""
    dt = _parse_iso_utc(entry.get("created_at"))
    return dt.strftime("%m-%d") if dt else "??-??"


def render_decisions_markdown(decisions):
    """Render the entire DECISIONS.md content from the decisions store.

    Pure string formatting, stdlib only. The output is a derived
    projection: always regenerated whole from decisions.json, never
    parsed back in, never merged with hand edits — a regenerated
    projection has no drift surface. Entry layout mirrors the existing
    hand-maintained convention:

        ### <summary> — MM-DD (`dec-NNN`)
        <problem paragraph>
        - **Why:** ...
        - **Tried:** / **Tradeoff:** / **Rejected:** ...
    """
    lines = [
        "# Decisions",
        "",
        "A running log of the **why** behind this project's design.",
        "",
        "Generated by [context-keeper](https://github.com/jarmstrong158/context-keeper)",
        "from `.context/decisions.json` — **do not edit by hand**; this file is",
        "regenerated whole on every decision write. The JSON store is canonical.",
        "",
        "---",
        "",
    ]
    for e in sorted(decisions, key=_md_entry_num):
        eid = e.get("id", "?")
        title = (e.get("summary") or "").strip() or "(untitled)"
        heading = f"### {title} — {_md_date(e)} (`{eid}`)"
        status = e.get("status", "active")
        if status == "deprecated":
            heading += " — **DEPRECATED**"
        elif status == "superseded":
            sup = e.get("superseded_by")
            heading += f" — **SUPERSEDED**" + (f" by `{sup}`" if sup else "")
        lines.append(heading)

        problem = (e.get("problem") or "").strip()
        if problem:
            lines.append(problem)

        bullets = []
        why = (e.get("why_chosen") or e.get("rationale") or "").strip()
        if why:
            bullets.append(f"- **Why:** {why}")
        tried = (e.get("what_we_tried") or "").strip()
        if tried:
            bullets.append(f"- **Tried:** {tried}")
        tradeoffs = (e.get("tradeoffs") or "").strip()
        if tradeoffs:
            bullets.append(f"- **Tradeoff:** {tradeoffs}")
        for alt in e.get("alternatives", []) or []:
            option = (alt.get("option") or "").strip()
            reason = (alt.get("reason_rejected") or "").strip()
            if option:
                bullets.append(
                    f"- **Rejected:** {option}" + (f" — {reason}" if reason else ""))
        if e.get("status", "active") == "deprecated":
            note = (e.get("deprecated_reason") or "").strip()
            superseded = e.get("superseded_by")
            line = f"- **Deprecated:** {note}" if note else "- **Deprecated.**"
            if superseded:
                line += f" Superseded by `{superseded}`."
            bullets.append(line)

        if bullets:
            if problem:
                lines.append("")
            lines.extend(bullets)
        lines.append("")
    return "\n".join(lines)


def _write_decisions_markdown(base_dir, cfg=None, out_path=None):
    """Regenerate the DECISIONS.md projection. Returns (path, entry_count)."""
    if cfg is None:
        cfg = read_config(base_dir)
    md_cfg = cfg.get("markdown_export") or {}
    if not out_path:
        out_path = md_cfg.get("path") or "DECISIONS.md"
    if not os.path.isabs(out_path):
        project_dir = os.path.dirname(os.path.abspath(base_dir))
        out_path = os.path.join(project_dir, out_path)
    decisions = read_json_file(os.path.join(base_dir, "decisions.json"))
    content = render_decisions_markdown(decisions)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, out_path)
    return out_path, len(decisions)


def _maybe_export_markdown(base_dir):
    """Render-on-write: regenerate DECISIONS.md when markdown_export.enabled.

    Runs after the JSON write completes and before the tool returns, so
    the markdown sits on disk next to the JSON and a subsequent git
    commit captures both together. Deliberately NOT a PostToolUse or
    post-commit git hook — rendering after the commit snapshot is taken
    would reintroduce the drift this projection exists to eliminate.
    Projection failures never fail the canonical write.
    """
    try:
        cfg = read_config(base_dir)
        if (cfg.get("markdown_export") or {}).get("enabled"):
            _write_decisions_markdown(base_dir, cfg)
    except Exception:
        pass


# ============================================================
# .claude/rules/ projection (scoped constraints, path-triggered)
#
# The scope_guard hook delivers a scoped constraint when the agent edits a
# covered file. Claude Code's own `.claude/rules/*.md` files with `paths:`
# frontmatter fire earlier — the harness loads them when Claude *reads* a
# matching file, before an edit is written. Projecting scoped constraints
# into that format buys pre-read delivery from the harness itself, with no
# hook and no tool call.
#
# Same contract as the DECISIONS.md projection (dec-008): JSON stays
# canonical, the markdown is derived, regenerated whole, and never parsed
# back in. This projection additionally owns its own subdirectory so a
# regeneration can retire the files of deprecated constraints without ever
# touching a hand-written rule.
# ============================================================

# Written into every generated file and required before regeneration will
# delete one. Block-level HTML comments are stripped from a rules file before
# it enters the model's context, so this marker is free: it identifies the
# file to the tool and to a human reader without costing context tokens.
_RULES_MARKER = "<!-- context-keeper:generated -->"

# Characters that make a scope unprojectable, for two different reasons with
# one shared consequence: the rule silently never fires.
#
#   Glob metacharacters — Claude Code reads `[` as the start of a bracket
#   expression, and a pattern whose bracket never closes matches nothing.
#
#   A double quote (or a control character) — each pattern is emitted as a
#   double-quoted YAML scalar, so an embedded quote closes it early and the
#   whole frontmatter block stops parsing. The harness then loads NO rule
#   from that file, not even the patterns that were fine.
#
# Both are skipped and REPORTED rather than emitted, because a rule file that
# never fires is indistinguishable from coverage.
_GLOB_META = set("[]{}*?")
_YAML_UNSAFE = set('"')


def _scope_to_paths(scope):
    """Glob patterns for a constraint scope, or [] if it can't be expressed.

    Directory scopes match everything beneath them; file scopes match the
    file. Each gets a repo-root-anchored form and a `**/`-prefixed form, so
    a scope recorded as `hooks/` still fires in a nested checkout.
    """
    s = str(scope or "").replace("\\", "/").strip()
    if not s or s.lower() == "global":
        return []
    if any(ch in _GLOB_META or ch in _YAML_UNSAFE or ord(ch) < 32 for ch in s):
        return []
    s = s.strip("/")
    if not s:
        return []
    if "." in os.path.basename(s):  # looks like a file, not a directory
        return [s, f"**/{s}"]
    return [f"{s}/**/*", f"**/{s}/**/*"]


def _rule_slug(scope):
    """Readable, filesystem-safe filename stem for a scope."""
    s = str(scope or "").replace("\\", "/").strip().strip("/")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.").lower()
    return slug or "scope"


def _rule_filenames(scopes):
    """Map each scope to a unique .md filename, deterministically.

    Two different scopes can slugify to the same stem (`a/b` and `a-b`).
    Resolving collisions in sorted scope order keeps the mapping stable
    across regenerations, so a rename never churns the output directory.
    """
    names = {}
    used = set()
    for scope in sorted(scopes):
        stem = _rule_slug(scope)
        candidate = stem
        n = 2
        while candidate in used:
            candidate = f"{stem}-{n}"
            n += 1
        used.add(candidate)
        names[scope] = candidate + ".md"
    return names


def render_scope_rule(scope, constraints):
    """Render one `.claude/rules/` file for a single constraint scope."""
    paths = _scope_to_paths(scope)
    lines = ["---", "paths:"]
    lines.extend(f'  - "{p}"' for p in paths)
    lines.append("---")
    lines.append("")
    lines.append(_RULES_MARKER)
    lines.append(
        "<!-- Regenerated whole from .context/constraints.json by context-keeper. "
        "Do not edit by hand: your edits are overwritten on the next constraint "
        "write. Change the constraint instead. -->"
    )
    lines.append("")
    lines.append(f"# Project constraints for `{scope}`")
    lines.append("")
    lines.append(
        "You are working in a file this project has recorded rules about. "
        "These are not suggestions from a linter — they were written down "
        "because something went wrong without them."
    )
    lines.append("")

    # Absolute before advisory, then by id, so the file is stable across
    # regenerations regardless of the order entries landed in the store.
    def _key(c):
        return (0 if c.get("hardness", "absolute") == "absolute" else 1,
                str(c.get("id", "")))

    for c in sorted(constraints, key=_key):
        hardness = c.get("hardness", "absolute")
        lines.append(f"## [{c.get('id', '?')}] {hardness}")
        lines.append("")
        lines.append((c.get("rule") or "").strip() or "(no rule text)")
        lines.append("")
        reason = (c.get("reason") or "").strip()
        if reason:
            lines.append(f"**Why:** {reason}")
        incident = (c.get("triggering_incident") or "").strip()
        if incident:
            lines.append(f"**What happened:** {incident}")
        enforced = (c.get("enforced_by") or "").strip()
        if enforced:
            # Named, never executed — see the enforced_by note in CLAUDE.md.
            lines.append(f"**Checked by:** `{enforced}`")
        lines.append("")
    return "\n".join(lines)


class RulesPathOutsideProject(ValueError):
    """The configured rules directory resolves outside the project root."""


def _rules_dir(base_dir, cfg=None, out_dir=None):
    """Absolute path of the generated rules directory for a .context dir.

    The result MUST stay inside the project. This directory is not merely
    written to — it is reaped, so every marker-carrying .md file in it is a
    deletion candidate on each regeneration. A `../../..` or absolute path
    in config would point that deletion at a directory the user never
    associated with context-keeper.

    Containment is also just correct: Claude Code only discovers
    `.claude/rules/` inside the working tree, so a rules directory outside
    the project could never load anyway. There is no legitimate use to
    preserve here, which is why this raises instead of falling back.
    """
    if cfg is None:
        cfg = read_config(base_dir)
    if not out_dir:
        out_dir = (cfg.get("rules_export") or {}).get("path") or RULES_DIR_DEFAULT
    project_dir = os.path.dirname(os.path.abspath(base_dir))
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(project_dir, out_dir)
    resolved = os.path.realpath(out_dir)
    root = os.path.realpath(project_dir)
    # commonpath (not startswith) so `/proj-evil` is not read as inside `/proj`.
    try:
        contained = os.path.commonpath([resolved, root]) == root
    except ValueError:  # different drives on Windows
        contained = False
    if not contained:
        raise RulesPathOutsideProject(
            f"rules_export.path resolves to {resolved}, which is outside the "
            f"project at {root}. Refusing: this directory gets reaped on every "
            "regeneration, and Claude Code only loads .claude/rules/ from "
            "inside the working tree anyway."
        )
    return resolved


def _write_scope_rules(base_dir, cfg=None, out_dir=None):
    """Regenerate the scoped-constraint rules directory.

    Returns (dir, files_written, scopes_skipped). Only active constraints
    with a real (non-global) scope produce a file: a global constraint has
    no path to trigger on and is already session-start material, so writing
    it here would just duplicate the injection unconditionally.
    """
    out_dir = _rules_dir(base_dir, cfg, out_dir)
    constraints = [c for c in read_json_file(os.path.join(base_dir, "constraints.json"))
                   if c.get("status", "active") == "active"]

    by_scope = {}
    skipped = []
    for c in constraints:
        scope = (c.get("scope") or "global").strip()
        if not _scope_to_paths(scope):
            if scope and scope.lower() != "global":
                skipped.append({"id": c.get("id"), "scope": scope})
            continue
        by_scope.setdefault(scope, []).append(c)

    names = _rule_filenames(by_scope.keys())
    os.makedirs(out_dir, exist_ok=True)

    written = []
    for scope, members in by_scope.items():
        path = os.path.join(out_dir, names[scope])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_scope_rule(scope, members))
        os.replace(tmp, path)
        written.append(names[scope])

    # Retire files for scopes that no longer have an active constraint.
    # Only files carrying our marker are removed, so a hand-written rule
    # dropped into this directory is left alone rather than deleted.
    keep = set(written)
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".md") or name in keep:
            continue
        stale_path = os.path.join(out_dir, name)
        try:
            with open(stale_path, "r", encoding="utf-8") as f:
                if _RULES_MARKER not in f.read(2048):
                    continue
            os.remove(stale_path)
        except OSError:
            pass

    return out_dir, len(written), skipped


def _maybe_export_rules(base_dir):
    """Render-on-write: regenerate the rules directory when rules_export.enabled.

    Runs on the same write paths as the DECISIONS.md projection and with the
    same guarantee — a projection failure never fails the canonical write.
    """
    try:
        cfg = read_config(base_dir)
        if (cfg.get("rules_export") or {}).get("enabled"):
            _write_scope_rules(base_dir, cfg)
    except Exception:
        pass


def _maybe_export_projections(base_dir, type_name):
    """Regenerate the derived projections a write to `type_name` can affect.

    One place decides which store feeds which projection, so a new write
    path only has to say what it wrote.
    """
    if type_name == "decisions":
        _maybe_export_markdown(base_dir)
    elif type_name == "constraints":
        _maybe_export_rules(base_dir)


# ============================================================
# Tool handlers
# ============================================================


def _mirror_out(entry, type_name, base_dir):
    """Dual-write hook: push a just-written entry to the remote, fail-soft.

    Wrapped so a mirror problem (module missing, remote down, bug) can
    never affect the local write that already committed. All queueing and
    logging happens inside mirror.mirror_out.
    """
    if _mirror is None:
        return
    try:
        _mirror.mirror_out(entry, type_name, base_dir)
    except Exception:
        pass


def _record_decision_impl(params):
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    # Backward-compat: if caller passed deprecated `rationale` without
    # `why_chosen`, promote it. This keeps old MCP clients working but
    # deliberately doesn't auto-fill `problem` — that field MUST be
    # explicit so we don't silently degrade entry quality.
    if params.get("rationale") and not params.get("why_chosen"):
        params = dict(params)
        params["why_chosen"] = params["rationale"]

    err = _check_min_lengths(params, {
        "summary": 5,
        "problem": 40,
        "why_chosen": 60,
    })
    if err is not None:
        return err

    ensure_context_dir(base_dir)
    dec_path = os.path.join(base_dir, "decisions.json")
    entries, load_err = _load_entries_for_write(dec_path)
    if load_err is not None:
        return load_err
    entry = {
        "id": next_id(entries, "dec"),
        "summary": params["summary"],
        "problem": params["problem"],
        "why_chosen": params["why_chosen"],
        "what_we_tried": params.get("what_we_tried", ""),
        "tradeoffs": params.get("tradeoffs", ""),
        "alternatives": params.get("alternatives", []),
        "constraints_created": params.get("constraints_created", []),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "retrieval_hints": params.get("retrieval_hints", []),
        "origin": _origin_from_params(params),
        "schema_version": 4,
        "status": "active",
        "superseded_by": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "verified_at": now_iso(),
        "verified_sha": _verified_sha(base_dir),
    }
    # Preserve the deprecated `rationale` field on disk if the caller
    # passed it explicitly. Lets old MCP clients (and old test
    # assertions) keep working without losing what they wrote.
    if params.get("rationale"):
        entry["rationale"] = params["rationale"]
    entries.append(entry)

    # Supersession: mark prior decisions this one replaces as 'superseded'
    # in the SAME write, so the store is never momentarily inconsistent.
    # Superseded != deprecated: the old decision stays recallable (history,
    # "why did we change from X") but score_entry demotes it. Unknown or
    # already-deprecated ids are skipped silently — never fail the record.
    superseded = []
    supersedes = params.get("supersedes") or []
    if supersedes:
        by_id = {e.get("id"): e for e in entries}
        for sid in supersedes:
            old = by_id.get(sid)
            if old is None or old is entry:
                continue
            if old.get("status", "active") == "deprecated":
                continue
            old["status"] = "superseded"
            old["superseded_by"] = entry["id"]
            old["updated_at"] = now_iso()
            superseded.append(sid)

    write_json_file(dec_path, entries)
    _maybe_export_projections(base_dir, "decisions")
    _mirror_out(entry, "decisions", base_dir)
    # Superseded siblings changed status in the same write -- mirror them too
    # so the remote reflects the demotion, not just the new decision.
    if superseded:
        by_id = {e.get("id"): e for e in entries}
        for sid in superseded:
            if by_id.get(sid):
                _mirror_out(by_id[sid], "decisions", base_dir)
    result = {"success": True, "id": entry["id"], "entry": entry}
    if superseded:
        result["superseded"] = superseded
    return _attach_similar(result, entry, base_dir)


def _record_pipeline_impl(params):
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    err = _check_min_lengths(params, {
        "name": 3,
        "purpose": 40,
    })
    if err is not None:
        return err
    if not params.get("steps"):
        return {"error": "Pipeline requires at least one step.", "validation_errors": [
            {"field": "steps", "guidance": "Provide an ordered list of steps."}
        ]}

    ensure_context_dir(base_dir)
    pipe_path = os.path.join(base_dir, "pipelines.json")
    entries, load_err = _load_entries_for_write(pipe_path)
    if load_err is not None:
        return load_err
    entry = {
        "id": next_id(entries, "pipe"),
        "name": params["name"],
        "purpose": params["purpose"],
        "when_to_invoke": params.get("when_to_invoke", ""),
        "steps": params["steps"],
        "constraints": params.get("constraints", []),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "retrieval_hints": params.get("retrieval_hints", []),
        "origin": _origin_from_params(params),
        "schema_version": 4,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "verified_at": now_iso(),
        "verified_sha": _verified_sha(base_dir),
    }
    entries.append(entry)
    write_json_file(pipe_path, entries)
    _mirror_out(entry, "pipelines", base_dir)
    return _attach_similar({"success": True, "id": entry["id"], "entry": entry}, entry, base_dir)


def _record_constraint_impl(params):
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    err = _check_min_lengths(params, {
        "rule": 5,
        "reason": 40,
    })
    if err is not None:
        return err

    ensure_context_dir(base_dir)
    con_path = os.path.join(base_dir, "constraints.json")
    entries, load_err = _load_entries_for_write(con_path)
    if load_err is not None:
        return load_err
    entry = {
        "id": next_id(entries, "con"),
        "rule": params["rule"],
        "reason": params["reason"],
        "triggering_incident": params.get("triggering_incident", ""),
        "enforced_by": params.get("enforced_by", ""),
        "scope": params.get("scope", "global"),
        "hardness": params.get("hardness", "absolute"),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "retrieval_hints": params.get("retrieval_hints", []),
        "origin": _origin_from_params(params),
        "schema_version": 4,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "verified_at": now_iso(),
        "verified_sha": _verified_sha(base_dir),
    }
    entries.append(entry)
    write_json_file(con_path, entries)
    _maybe_export_projections(base_dir, "constraints")
    _mirror_out(entry, "constraints", base_dir)
    return _attach_similar({"success": True, "id": entry["id"], "entry": entry}, entry, base_dir)


# Kind -> the per-kind implementation. record_entry is the canonical write tool;
# the three record_* tools remain as thin deprecated wrappers (below) so every
# existing caller keeps working unchanged.
_RECORD_IMPLS = {
    "decision": _record_decision_impl,
    "constraint": _record_constraint_impl,
    "pipeline": _record_pipeline_impl,
}


def handle_record_entry(params):
    """Unified write tool: record_entry(kind="decision"|"constraint"|"pipeline", ...).

    Dispatches to the same per-kind implementation the original record_* tools
    use, so behavior and response shape are identical to calling them directly.
    Kind-specific required fields (e.g. why_chosen for a decision, steps for a
    pipeline) are validated by that implementation with the same guidance.
    """
    kind = params.get("kind")
    impl = _RECORD_IMPLS.get(kind)
    if impl is None:
        return {
            "error": "record_entry requires kind to be 'decision', 'constraint', or 'pipeline'.",
            "validation_errors": [{
                "field": "kind",
                "guidance": "Set kind to one of: decision, constraint, pipeline.",
            }],
        }
    inner = {k: v for k, v in params.items() if k != "kind"}
    return impl(inner)


def handle_record_decision(params):
    """Deprecated: thin wrapper for record_entry(kind="decision"). Kept so
    existing callers of record_decision keep working unchanged."""
    return handle_record_entry({**params, "kind": "decision"})


def handle_record_pipeline(params):
    """Deprecated: thin wrapper for record_entry(kind="pipeline")."""
    return handle_record_entry({**params, "kind": "pipeline"})


def handle_record_constraint(params):
    """Deprecated: thin wrapper for record_entry(kind="constraint")."""
    return handle_record_entry({**params, "kind": "constraint"})


def handle_get_context(params):
    entry_id = params.get("id")
    base_dir = _base_dir_from_params(params)

    # Direct ID lookup — full fidelity, no budget
    if entry_id:
        entry, type_name, _, _ = _find_entry_by_id(entry_id, base_dir)
        if entry is None:
            return {"error": f"No entry found with id '{entry_id}'"}
        return {"type": type_name, "entry": entry}

    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    query = params.get("query")
    tags = params.get("tags")
    scope = params.get("scope")
    cfg = read_config(base_dir)
    budget = params.get("token_budget", cfg.get("token_budget", 4000))
    max_entry = cfg.get("max_entry_tokens", 1000)
    types = params.get("types", ["decisions", "pipelines", "constraints"])

    paths = _resolve_paths(base_dir)

    # Check if context dir exists
    if not os.path.exists(base_dir):
        return {
            "initialized": False,
            "message": "No context directory found. Use record_* tools to start building project memory.",
            "results": [],
        }

    # Gather all entries. Track type alongside the entry instead of
    # mutating the entry dict — keeps the returned payload clean without a
    # pop/filter dance later.
    type_labels = {"decisions": "decision", "pipelines": "pipeline", "constraints": "constraint"}
    typed_entries = []
    for tname in types:
        if tname in paths:
            for e in read_json_file(paths[tname]):
                typed_entries.append((type_labels.get(tname, tname), e))

    # Filter out deprecated
    typed_entries = [(t, e) for t, e in typed_entries if e.get("status", "active") != "deprecated"]

    # Temporal filter (timeline view): since/before against the entry's
    # verified/created timestamp. Entries with unparseable timestamps are
    # excluded while a temporal filter is active — they can't satisfy it.
    since_dt = _parse_iso_utc(params.get("since"))
    before_dt = _parse_iso_utc(params.get("before"))
    if since_dt or before_dt:
        filtered = []
        for t, e in typed_entries:
            ts = _entry_timestamp(e)
            if ts is None:
                continue
            if since_dt and ts < since_dt:
                continue
            if before_dt and ts >= before_dt:
                continue
            filtered.append((t, e))
        typed_entries = filtered

    # Optional semantic blend — opt-in via config "semantic.enabled" (or a per-call
    # params["semantic"] override). Adds embedding-cosine signal to rescue
    # vocabulary-mismatch queries. Any failure (Ollama down, model missing, import
    # error) silently falls back to pure lexical ranking, so zero-dep stays default.
    sem_cfg = {**cfg.get("semantic", {}), **(params.get("semantic") or {})}
    sem_map = {}
    if sem_cfg.get("enabled") and query:
        try:
            import semantic_index
            sem_map = semantic_index.query_cosines(
                query, [e for _t, e in typed_entries], base_dir, sem_cfg) or {}
        except Exception:
            sem_map = {}
    sem_weight = sem_cfg.get("weight", 150)

    # Score and sort
    now_dt = datetime.now(timezone.utc)
    scored = [
        (score_entry(e, tags, query, scope, now_dt) + sem_weight * sem_map.get(e.get("id"), 0.0), t, e)
        for t, e in typed_entries
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Optional MMR diversity — opt-in via config "mmr.enabled" (or per-call
    # params["mmr"]). Reorders the ranked list so near-duplicate entries don't
    # crowd the token budget; arc-linked (related_to) entries are exempt.
    mmr_cfg = {**cfg.get("mmr", {}), **(params.get("mmr") or {})}
    if mmr_cfg.get("enabled"):
        scored = _mmr_reorder(scored, mmr_cfg.get("lambda", 0.7))

    # Pack into budget with truncation
    results = []
    used_tokens = 0
    seen_ids = set()
    for sc, entry_type, entry in scored:
        clean = entry

        text = json.dumps(clean, indent=2)
        cost = estimate_tokens(text)

        # Truncate oversized entries
        if cost > max_entry:
            clean = _truncate_entry(clean, max_entry)
            text = json.dumps(clean, indent=2)
            cost = estimate_tokens(text)

        if used_tokens + cost > budget:
            # Skip entries that don't fit but keep packing: one oversized
            # mid-ranked entry must not block smaller entries behind it.
            continue

        results.append({"score": round(sc, 1), "type": entry_type, "entry": clean})
        seen_ids.add(entry.get("id"))
        used_tokens += cost

    # Graph traversal: pull related entries (depth=1) so arcs come through.
    # Default ON because the whole point of related_to is that retrieval
    # surfaces connective tissue automatically. Caller can disable with
    # include_related=False if they want pure relevance scoring.
    include_related = params.get("include_related", True)
    related_added = 0
    if include_related and results:
        # Collect all related_to ids referenced by the current result set
        related_ids = set()
        for r in results:
            for rid in r["entry"].get("related_to", []) or []:
                if rid not in seen_ids:
                    related_ids.add(rid)

        for rid in related_ids:
            r_entry, r_type, _, _ = _find_entry_by_id(rid, base_dir)
            if r_entry is None:
                continue
            if r_entry.get("status", "active") == "deprecated":
                continue
            clean = r_entry
            text = json.dumps(clean, indent=2)
            cost = estimate_tokens(text)
            if cost > max_entry:
                clean = _truncate_entry(clean, max_entry)
                cost = estimate_tokens(json.dumps(clean, indent=2))
            if used_tokens + cost > budget:
                continue
            r_label = type_labels.get(r_type, r_type)
            results.append({
                "score": 0.0,
                "type": r_label,
                "entry": clean,
                "via": "related_to",
            })
            seen_ids.add(rid)
            used_tokens += cost
            related_added += 1

    response = {
        "results": results,
        "tokens_used": used_tokens,
        "token_budget": budget,
        "total_entries_scored": len(scored),
        "entries_returned": len(results),
        "related_added": related_added,
    }

    # Abstention signal. The composite score is inflated by recency/status/
    # origin, so even a totally irrelevant active entry ranks ~55 and looks
    # confident — meaning a query with NO relevant memory silently gets a
    # confident-looking top result. Key the honesty signal on the relevance
    # signal (tag/text overlap only) instead. We ANNOTATE, never suppress:
    # the vocab-mismatch recall that retrieval_hints and the semantic blend
    # work to preserve must survive, so weak matches are still returned —
    # just flagged so the agent doesn't present them as established fact.
    if (query or tags) and results:
        min_rel = params.get("min_relevance", cfg.get("min_relevance", 0.20))
        # Feed the same cosines that shaped the RANKING into the abstention
        # signal. Without this the two disagreed: an entry surfaced purely on
        # semantic similarity ranked first and was then flagged
        # no_confident_match for lacking lexical overlap, while the guidance
        # text claimed the semantic signal had "already been blended in".
        # It had been blended into the rank and nowhere else.
        sem_lo = sem_cfg.get("relevance_floor", _SEM_REL_LO)
        sem_hi = sem_cfg.get("relevance_ceiling", _SEM_REL_HI)
        top_rel = 0.0
        for r in results:
            if r.get("via") == "related_to":
                continue  # arc-pulled, not relevance-ranked against the query
            sig = _relevance_signal(
                r["entry"], tags, query,
                cosine=sem_map.get(r["entry"].get("id")) if sem_map else None,
                sem_lo=sem_lo, sem_hi=sem_hi)
            if sig is not None:
                top_rel = max(top_rel, sig)
        response["top_relevance"] = round(top_rel, 2)
        if top_rel < min_rel:
            response["no_confident_match"] = True
            basis = "lexical + semantic" if sem_map else "lexical"
            response["guidance"] = (
                f"No strongly relevant memory found ({basis} relevance "
                f"{top_rel:.2f} < {min_rel:.2f}). The entries above are the closest "
                "neighbors, but likely nothing was recorded on this exact "
                "topic. Do NOT present them as established project decisions — treat "
                "this as 'no memory on this' unless an entry genuinely fits. "
                + ("Semantic retrieval was enabled and its cosines are already "
                   "reflected in this number."
                   if sem_map else
                   "Semantic retrieval is off, so this is a lexical judgement only; "
                   "enabling it (config semantic.enabled) improves recall on queries "
                   "phrased differently from the stored entry.")
            )

    # A targeted ask: somebody was looking for this. Distinct from the blanket
    # session-start load, and the distinction is the whole point — see usage.py.
    if usage is not None:
        usage.record(base_dir, [r["entry"].get("id") for r in results], "retrieved")

    return response


# ============================================================
# Structured field query (fact-metadata query) — a deterministic
# complement to get_context's relevance search. This filters entries by
# EXACT predicates over fields that already exist on the schema; it does
# NOT rank, score, blend semantics, or apply the min_relevance abstention
# floor. A structured query either matches or it doesn't.
# ============================================================


def _as_lower_set(val):
    """Normalize a string-or-list predicate into a lowercased set, or None
    when the predicate is absent (absent == not filtered on)."""
    if val is None:
        return None
    vals = [val] if isinstance(val, str) else list(val)
    return {str(v).strip().lower() for v in vals if str(v).strip()}


def _query_id_sort_key(entry):
    """Natural sort key for readable IDs like 'dec-007': group by type
    prefix, then by numeric suffix so dec-2 precedes dec-10."""
    eid = entry.get("id", "") or ""
    prefix, _, num = eid.partition("-")
    try:
        n = int(num)
    except ValueError:
        n = 0
    return (prefix, n, eid)


def _entry_matches_query(entry, type_name, f, superseding_ids):
    """True iff entry satisfies every active predicate in f (AND semantics).

    `type_name` is the plural store name ('decisions'/'pipelines'/
    'constraints'), matching the `types` param vocabulary get_context uses.
    Every predicate is a hard, exact match over an existing field. Any
    predicate left as None is simply not applied. Fields that don't exist
    on a given entry type (e.g. hardness on a decision) never match a
    predicate that requires them, which is the intended behavior.
    """
    if f["types"] is not None and type_name not in f["types"]:
        return False
    if f["status"] is not None and entry.get("status", "active").lower() not in f["status"]:
        return False
    if f["origin"] is not None and entry.get("origin", "agent").lower() not in f["origin"]:
        return False
    if f["hardness"] is not None and str(entry.get("hardness", "")).lower() not in f["hardness"]:
        return False
    if f["scope"] is not None and entry.get("scope") != f["scope"]:
        return False
    if f["tags_any"] is not None or f["tags_all"] is not None:
        etags = {str(t).lower() for t in entry.get("tags", []) or []}
        if f["tags_any"] is not None and not (etags & f["tags_any"]):
            return False
        if f["tags_all"] is not None and not (f["tags_all"] <= etags):
            return False
    if f["superseded_by"] is not None and entry.get("superseded_by") != f["superseded_by"]:
        return False
    # supersedes: X supersedes target T iff T.superseded_by == X.id. The set
    # of such X ids was resolved once from the target's back-reference.
    if f["supersedes"] is not None and entry.get("id") not in superseding_ids:
        return False
    # Free text over the entry's rationale/text fields: every term must appear
    # (AND). Substring match against the entry's word blob, so "auth" hits
    # "authentication". Uses _text_words — the same fields retrieval indexes.
    if f["text"] is not None:
        blob = " ".join(_text_words(entry))
        if not all(term in blob for term in f["text"]):
            return False
    return True


_KIND_TO_TYPE = {"decision": "decisions", "constraint": "constraints", "pipeline": "pipelines"}


def _kind_to_types(val):
    """Map a singular kind (or list) to the plural type name(s) `types` uses.
    An already-plural or unknown value passes through unchanged."""
    vals = [val] if isinstance(val, str) else list(val)
    return [_KIND_TO_TYPE.get(str(v).strip().lower(), str(v).strip().lower()) for v in vals]


def handle_query_entries(params):
    """Deterministic structured-field filter over existing entry fields.

    Complements get_context (relevance search) — it does not rank, score,
    or abstain. Predicates AND together; results are returned in stable ID
    order and packed to the same token budget get_context uses.
    """
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not os.path.exists(base_dir):
        return {
            "initialized": False,
            "message": "No context directory found. Use record_* tools to start building project memory.",
            "results": [],
            "entries_returned": 0,
            "matched_entries": 0,
        }

    cfg = read_config(base_dir)
    budget = params.get("token_budget", cfg.get("token_budget", 4000))
    max_entry = cfg.get("max_entry_tokens", 1000)

    # Normalize predicates. `types` restricts which files we read at all.
    # `kind` is a convenience alias (singular) for `types` — if both are given,
    # `types` wins. This lets query shapes stop needing new tools.
    raw_types = params.get("types")
    if raw_types is None and params.get("kind") is not None:
        raw_types = _kind_to_types(params["kind"])
    type_filter = _as_lower_set(raw_types)
    text = params.get("text")
    text_terms = [t for t in str(text).lower().split() if t] if text else None
    limit = params.get("limit")
    f = {
        "types": type_filter,
        "status": _as_lower_set(params.get("status")),
        "origin": _as_lower_set(params.get("origin")),
        "hardness": _as_lower_set(params.get("hardness")),
        "scope": params.get("scope"),  # exact match, case-sensitive (paths)
        "tags_any": _as_lower_set(params.get("tags_any")),
        "tags_all": _as_lower_set(params.get("tags_all")),
        "superseded_by": params.get("superseded_by"),
        "supersedes": params.get("supersedes"),
        "text": text_terms,
    }

    type_labels = {"decisions": "decision", "pipelines": "pipeline", "constraints": "constraint"}
    paths = _resolve_paths(base_dir)
    if paths is None:
        return UNRESOLVED_PROJECT_ERROR

    # Load once through the shared reader. Reading every type keeps the
    # supersedes back-reference resolvable even when `types` narrows output.
    typed_entries = []
    id_map = {}
    for tname, tpath in paths.items():
        for e in read_json_file(tpath):
            typed_entries.append((tname, e))
            if e.get("id"):
                id_map[e["id"]] = e

    # Resolve `supersedes: T` to the id(s) that superseded T, via T's stored
    # back-reference. Empty when T is unknown or was never superseded.
    superseding_ids = set()
    if f["supersedes"] is not None:
        target = id_map.get(f["supersedes"])
        if target and target.get("superseded_by"):
            superseding_ids.add(target["superseded_by"])

    # Temporal predicate — identical semantics to get_context's since/before:
    # compare against the entry's verified/created timestamp; entries with an
    # unparseable timestamp are excluded while a temporal filter is active.
    since_dt = _parse_iso_utc(params.get("since"))
    before_dt = _parse_iso_utc(params.get("before"))

    matched = []
    for tname, e in typed_entries:
        if not _entry_matches_query(e, tname, f, superseding_ids):
            continue
        if since_dt or before_dt:
            ts = _entry_timestamp(e)
            if ts is None:
                continue
            if since_dt and ts < since_dt:
                continue
            if before_dt and ts >= before_dt:
                continue
        matched.append((type_labels.get(tname, tname), e))

    # Stable order by natural ID — deterministic, no relevance involved.
    matched.sort(key=lambda te: _query_id_sort_key(te[1]))

    # Pack into the token budget exactly as get_context does: serialize with
    # the same shaping, truncate oversized entries, skip-but-keep-packing so
    # one large entry doesn't starve smaller matches behind it.
    results = []
    used_tokens = 0
    for entry_type, entry in matched:
        if limit is not None and len(results) >= limit:
            break
        clean = entry
        text_json = json.dumps(clean, indent=2)
        cost = estimate_tokens(text_json)
        if cost > max_entry:
            clean = _truncate_entry(clean, max_entry)
            cost = estimate_tokens(json.dumps(clean, indent=2))
        if used_tokens + cost > budget:
            continue
        results.append({"type": entry_type, "entry": clean})
        used_tokens += cost

    # No abstention, no relevance floor, no guidance: a structured query that
    # matches nothing simply returns an empty set. Emptiness is a real answer.
    # Structured predicates are as targeted as a tag query — somebody asked.
    if usage is not None:
        usage.record(base_dir, [r.get("id") for r in results], "retrieved")

    return {
        "results": results,
        "matched_entries": len(matched),
        "entries_returned": len(results),
        "tokens_used": used_tokens,
        "token_budget": budget,
        "budget_truncated": len(results) < len(matched),
    }


def _constraint_lines(constraints):
    """Format active constraints into the Absolute/Advisory line list.

    Single source of truth for how constraints render, so the SessionStart
    summary, the reload_constraints tool, and the re-injection hook all
    surface them identically. Absolute first (true invariants), advisory
    second, a blank line between the two sections when both exist. Each
    entry is `  [con-XXX] <rule>` — the exact shape get_project_summary
    has always emitted.
    """
    absolute = [c for c in constraints if c.get("hardness") == "absolute"]
    advisory = [c for c in constraints if c.get("hardness") != "absolute"]
    lines = []
    if absolute:
        lines.append(f"Absolute Constraints ({len(absolute)}):")
        for c in absolute:
            lines.append(f"  [{c['id']}] {c['rule']}")
    if advisory:
        if lines:
            lines.append("")
        lines.append(f"Advisory Constraints ({len(advisory)}):")
        for c in advisory:
            lines.append(f"  [{c['id']}] {c['rule']}")
    return lines


def build_constraints_block(base_dir=None):
    """Return the constraints-only re-injection block.

    Deliberately narrow: the SAME Absolute/Advisory constraint lines
    SessionStart surfaces, and nothing else from the store — no decisions,
    no pipelines. The whole point of re-injection is a lightweight rules
    refresh, not dumping the full store again. Shared by the
    reload_constraints tool and the constraint_reinject.py hook.

    Returns {"initialized": bool, "count": int, "text": str}.
    """
    if base_dir is None:
        base_dir = CONTEXT_DIR
    if base_dir is None or not os.path.exists(base_dir):
        return {"initialized": False, "count": 0, "text": ""}
    constraints = [c for c in read_json_file(os.path.join(base_dir, "constraints.json"))
                   if c.get("status", "active") == "active"]
    lines = _constraint_lines(constraints)
    return {"initialized": True, "count": len(constraints), "text": "\n".join(lines)}


def handle_reload_constraints(params):
    """On-demand constraints-only recall. Returns the current constraints
    block so the agent can pull rules back into working context mid-session
    without waiting for any automatic trigger."""
    base_dir = _base_dir_from_params(params)
    if base_dir is None or not os.path.exists(base_dir):
        return {
            "initialized": False,
            "count": 0,
            "constraints": "",
            "message": "No context directory found. Nothing to re-surface.",
        }
    block = build_constraints_block(base_dir)
    return {
        "initialized": True,
        "count": block["count"],
        "constraints": block["text"],
    }


def handle_get_project_summary(params):
    base_dir = _base_dir_from_params(params)
    budget = params.get("token_budget", 2000)

    # First-run hydrate: if this is a fresh clone (empty/absent working store)
    # but a committed team snapshot is present, import it before summarizing so
    # the agent starts oriented. No-op when a store already exists, so no
    # current caller's behavior changes.
    _maybe_bootstrap_from_snapshot(base_dir)

    if base_dir is None or not os.path.exists(base_dir):
        return {
            "initialized": False,
            "message": "No context directory found. Use record_* tools to start building project memory.",
            "usage_guidance": USAGE_GUIDANCE,
        }

    cfg = read_config(base_dir)
    decisions = [d for d in read_json_file(os.path.join(base_dir, "decisions.json"))
                 if d.get("status", "active") == "active"]
    pipelines = [p for p in read_json_file(os.path.join(base_dir, "pipelines.json"))
                 if p.get("status", "active") == "active"]
    constraints = [c for c in read_json_file(os.path.join(base_dir, "constraints.json"))
                   if c.get("status", "active") == "active"]

    # Build compact summary
    lines = []
    project_name = cfg.get("project_name") or os.path.basename(os.path.dirname(base_dir))
    lines.append(f"Project: {project_name}")

    # Absolute constraints first (most important). Formatting is shared with
    # the reload_constraints tool and the re-injection hook via
    # _constraint_lines, so every surface renders constraints identically.
    absolute = [c for c in constraints if c.get("hardness") == "absolute"]
    advisory = [c for c in constraints if c.get("hardness") != "absolute"]
    con_lines = _constraint_lines(constraints)
    if con_lines:
        lines.append("")
        lines.extend(con_lines)

    # Above this many decisions, a flat list stops being scannable —
    # cluster by topic (most-frequent shared tag) instead.
    CLUSTER_THRESHOLD = 8
    if decisions and len(decisions) <= CLUSTER_THRESHOLD:
        lines.append(f"\nActive Decisions ({len(decisions)}):")
        for d in decisions:
            tags = ", ".join(d.get("tags", []))
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"  [{d['id']}] {d['summary']}{tag_str}")
    elif decisions:
        # Assign each decision to its most-frequent tag across the store,
        # so entries sharing a dominant topic land in the same cluster.
        tag_freq = {}
        for d in decisions:
            for t in d.get("tags", []):
                tag_freq[t] = tag_freq.get(t, 0) + 1
        clusters = {}
        for d in decisions:
            d_tags = d.get("tags", [])
            topic = max(d_tags, key=lambda t: tag_freq[t]) if d_tags else "(untagged)"
            clusters.setdefault(topic, []).append(d)
        lines.append(f"\nActive Decisions ({len(decisions)}, clustered by topic):")
        for topic, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {topic} ({len(members)}):")
            for d in members:
                lines.append(f"    [{d['id']}] {d['summary']}")

    if pipelines:
        lines.append(f"\nPipelines ({len(pipelines)}):")
        for p in pipelines:
            step_count = len(p.get("steps", []))
            lines.append(f"  [{p['id']}] {p['name']} ({step_count} steps)")

    summary_text = "\n".join(lines)

    # Check stale entries
    now_dt = datetime.now(timezone.utc)
    stale_days = cfg.get("stale_threshold_days", 30)
    stale = []
    for entries_list in [decisions, pipelines, constraints]:
        for e in entries_list:
            verified = e.get("verified_at") or e.get("created_at", "")
            try:
                v_dt = datetime.fromisoformat(verified.replace("Z", "+00:00"))
                if (now_dt - v_dt).days > stale_days:
                    stale.append({"id": e.get("id"), "days_since_verified": (now_dt - v_dt).days})
            except Exception:
                pass

    # Truncate summary to budget. The estimate must be recomputed from the
    # REMAINING lines each iteration — evaluating the original summary_text
    # in the loop condition never changes, which silently popped every line
    # and injected an EMPTY summary for any store over budget (found by the
    # token-reduction measurement: 78-entry stores were injecting ~0 tokens
    # of memory at session start).
    #
    # Trimming has a FLOOR and is REPORTED. Popping from the end walks up
    # through decisions and into the constraints block, and with a small
    # enough budget it empties the summary entirely -- which is exactly the
    # v0.9 failure (dec-009) in a different guise: a store that looks
    # healthy injecting nothing. The floor keeps the constraints block,
    # which is the part a session cannot function without, and the response
    # says out loud when the cap bit. Anthropic's own auto-memory index
    # errors rather than silently dropping past its read limit; the same
    # principle applies here -- an over-budget summary is a condition to
    # surface, not to absorb.
    summary_truncated = False
    lines_dropped = 0
    if estimate_tokens(summary_text) > budget:
        floor = 1 + len(con_lines) + (1 if con_lines else 0)  # header + constraints
        original_len = len(lines)
        while len(lines) > floor and estimate_tokens("\n".join(lines)) > budget:
            lines.pop()
        # Don't leave a dangling section header (e.g. "Pipelines (3):") or a
        # trailing blank line after trimming the entries out from under it.
        while len(lines) > floor and (
                not lines[-1].strip() or lines[-1].rstrip().endswith(":")):
            lines.pop()
        lines_dropped = original_len - len(lines)
        summary_truncated = lines_dropped > 0
        summary_text = "\n".join(lines)

    # Task focus, appended AFTER everything above and after truncation.
    #
    # Order matters twice. The block varies with the working tree, so putting it
    # last keeps the stable prefix byte-identical across sessions and the prompt
    # cache hitting — interleaving it by relevance would invalidate the cache on
    # every file touched. And appending after truncation means a large store
    # trims its own decisions rather than evicting the rules for the code
    # actually in front of the reader. Its size is capped in work_focus and
    # accounted separately, exactly like the additive orientation fields below.
    focus = []
    if work_focus is not None:
        try:
            root = os.path.dirname(base_dir.rstrip(os.sep)) or None
            focus = work_focus.focus_lines(constraints, root)
        except Exception:
            focus = []
        if focus:
            summary_text = summary_text + "\n" + "\n".join(focus)

    # Additive orientation fields so get_project_summary is the single
    # "whole lay of the land" call — an agent can orient without further
    # probing. Every existing key above (summary, counts, stale_entries,
    # usage_guidance) is unchanged; these are added alongside and are not
    # subject to the summary-text token budget (they are compact by design).
    def _status_counts(path):
        c = {"active": 0, "superseded": 0, "deprecated": 0}
        for e in read_json_file(path):
            st = e.get("status", "active")
            c[st] = c.get(st, 0) + 1
        return c

    counts_by_status = {
        "decisions": _status_counts(os.path.join(base_dir, "decisions.json")),
        "pipelines": _status_counts(os.path.join(base_dir, "pipelines.json")),
        "constraints": _status_counts(os.path.join(base_dir, "constraints.json")),
    }
    active_constraints = [
        {
            "id": c.get("id"),
            "rule": c.get("rule", ""),
            "hardness": c.get("hardness", "absolute"),
            "scope": c.get("scope", "global"),
        }
        for c in constraints
    ]

    def _recency_key(e):
        return e.get("updated_at") or e.get("verified_at") or e.get("created_at") or ""

    recent_decisions = [
        {"id": d.get("id"), "summary": d.get("summary", "")}
        for d in sorted(decisions, key=_recency_key, reverse=True)[:5]
    ]
    entry_ids = {
        "decisions": [d.get("id") for d in decisions],
        "pipelines": [p.get("id") for p in pipelines],
        "constraints": [c.get("id") for c in constraints],
    }

    # A blanket load: this entry arrived in context because the session started,
    # not because anyone wanted it. Counting these separately from targeted
    # retrievals is what later distinguishes a load-bearing entry from one the
    # store has been paying to carry.
    if usage is not None:
        usage.record(
            base_dir,
            entry_ids["decisions"] + entry_ids["pipelines"] + entry_ids["constraints"],
            "injected",
        )

    result = {
        "initialized": True,
        "project_name": project_name,
        "summary": summary_text,
        "counts": {
            "decisions": len(decisions),
            "pipelines": len(pipelines),
            "constraints_absolute": len(absolute),
            "constraints_advisory": len(advisory),
        },
        "counts_by_status": counts_by_status,
        "active_constraints": active_constraints,
        "recent_decisions": recent_decisions,
        "entry_ids": entry_ids,
        "stale_entries": stale if stale else None,
        "usage_guidance": USAGE_GUIDANCE,
    }
    if summary_truncated:
        # Emitted only when it happened, so the common case stays byte-stable
        # for the session-start prompt cache (dec-012's cache-stable prefix).
        result["summary_truncated"] = True
        result["summary_lines_dropped"] = lines_dropped
        result["summary_truncation_note"] = (
            f"The summary exceeded the {budget}-token budget and {lines_dropped} "
            "line(s) were dropped from the end; constraints were kept. Entries "
            "that fell off are still in the store -- reach them with get_context "
            "or query_entries, or raise token_budget. Consider deprecating "
            "entries that have stopped earning their place."
        )
    return result


# Min lengths enforced when a structured field is *updated*. Without this,
# update_entry could hollow out the very fields the v0.4 record_* schema
# protects (e.g. set why_chosen to "").
_UPDATE_MIN_LENGTHS = {
    "decisions": {"summary": 5, "problem": 40, "why_chosen": 60},
    "pipelines": {"name": 3, "purpose": 40},
    "constraints": {"rule": 5, "reason": 40},
}


def handle_update_entry(params):
    entry_id = params["id"]
    updates = params["updates"]
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    entry, type_name, file_path, _ = _find_entry_by_id(entry_id, base_dir)
    if entry is None:
        return {"error": f"No entry found with id '{entry_id}'"}

    # Validate any structured fields being updated, so update_entry can't
    # bypass the min-length schema that record_* enforces.
    requirements = {
        field: min_len
        for field, min_len in _UPDATE_MIN_LENGTHS.get(type_name, {}).items()
        if field in updates
    }
    if requirements:
        err = _check_min_lengths(updates, requirements)
        if err is not None:
            return err
    if type_name == "pipelines" and "steps" in updates and not updates["steps"]:
        return {"error": "Pipeline requires at least one step.", "validation_errors": [
            {"field": "steps", "guidance": "Provide an ordered list of steps."}
        ]}

    # Re-read for the write and re-find by id. Validation above ran against
    # the first read, but the APPLY must target the live object from the
    # second read -- see _reload_and_locate for why an index cannot survive
    # the gap between the two reads.
    entries, entry, err = _reload_and_locate(entry_id, file_path)
    if err is not None:
        return err

    # Apply updates (protect id and created_at)
    protected = {"id", "created_at"}
    for key, val in updates.items():
        if key not in protected:
            entry[key] = val

    entry["verified_at"] = now_iso()
    entry["verified_sha"] = _verified_sha(base_dir)
    entry["updated_at"] = now_iso()

    write_json_file(file_path, entries)
    _maybe_export_projections(base_dir, type_name)
    _mirror_out(entry, type_name, base_dir)

    return {"success": True, "entry": entry}


# Fields folded when merging a duplicate into a surviving entry. List fields
# are unioned (the target keeps its order, the source's new items append);
# text fields are only *backfilled* — a non-empty value on the target is never
# overwritten, because the target is the canonical entry being kept. This makes
# merge additive and non-destructive: the survivor can only gain content.
_MERGE_UNION_FIELDS = ("tags", "retrieval_hints", "related_to",
                       "constraints_created", "constraints")
_MERGE_BACKFILL_FIELDS = ("summary", "problem", "why_chosen", "what_we_tried",
                          "tradeoffs", "purpose", "when_to_invoke", "rule",
                          "reason", "triggering_incident")


def _merge_entry_fields(target, source, base_dir=None):
    """Fold source's unique content into target in place; return a summary of
    what changed. Never overwrites a non-empty target field."""
    added_tags = []
    backfilled = []
    for field in _MERGE_UNION_FIELDS:
        src_list = source.get(field)
        if not isinstance(src_list, list) or not src_list:
            continue
        tgt_list = target.get(field)
        if not isinstance(tgt_list, list):
            tgt_list = []
        existing = set(tgt_list)
        for item in src_list:
            if item not in existing:
                tgt_list.append(item)
                existing.add(item)
                if field == "tags":
                    added_tags.append(item)
        target[field] = tgt_list
    for field in _MERGE_BACKFILL_FIELDS:
        if not target.get(field) and source.get(field):
            target[field] = source[field]
            backfilled.append(field)
    target["updated_at"] = now_iso()
    target["verified_at"] = now_iso()
    target["verified_sha"] = _verified_sha(base_dir)
    return {"tags_added": added_tags, "fields_backfilled": backfilled}


def handle_deprecate_entry(params):
    entry_id = params["id"]
    reason = params["reason"]
    superseded_by = params.get("superseded_by")
    merge_into = params.get("merge_into")
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    entry, type_name, file_path, _ = _find_entry_by_id(entry_id, base_dir)
    if entry is None:
        return {"error": f"No entry found with id '{entry_id}'"}

    # Merge path (opt-in): fold this entry's unique content into a surviving
    # target of the SAME type, then deprecate this one pointing at the target.
    # This turns the manual "deprecate the dupe + update the original" dance
    # into one atomic write. Validate the target fully BEFORE any write, so a
    # bad merge_into never leaves a half-applied state.
    merge_result = None
    if merge_into is not None:
        if merge_into == entry_id:
            return {"error": "merge_into cannot be the same entry being deprecated."}
        target, target_type, target_file, _ = _find_entry_by_id(merge_into, base_dir)
        if target is None:
            return {"error": f"merge_into target '{merge_into}' not found."}
        if target_type != type_name:
            return {"error": (
                f"merge_into requires the same entry type: '{entry_id}' is a "
                f"{type_name} entry but '{merge_into}' is a {target_type} entry.")}
        # Same type => same file. Load once, mutate both, write once, so the
        # two updates can't clobber each other.
        entries, load_err = _load_entries_for_write(file_path)
        if load_err is not None:
            return load_err
        by_id = {e.get("id"): i for i, e in enumerate(entries)}
        if entry_id not in by_id or merge_into not in by_id:
            return {"error": "Entry moved during merge; retry."}
        dep = entries[by_id[entry_id]]
        keep = entries[by_id[merge_into]]
        merge_result = _merge_entry_fields(keep, dep, base_dir)
        dep["status"] = "deprecated"
        dep["deprecated_reason"] = reason
        dep["superseded_by"] = merge_into
        dep["updated_at"] = now_iso()
        write_json_file(file_path, entries)
        _maybe_export_projections(base_dir, type_name)
        # Mirror BOTH sides of the merge: the deprecation on `dep` and the
        # survivor's folded-in content on `keep`. Without this the remote keeps
        # showing the merged-away entry as active -- the exact stale-status
        # drift the upsert mirror exists to prevent.
        _mirror_out(dep, type_name, base_dir)
        _mirror_out(keep, type_name, base_dir)
        return {
            "success": True,
            "id": entry_id,
            "status": "deprecated",
            "merged_into": merge_into,
            "merged": merge_result,
        }

    # Re-read for the write and re-find by id, same as update_entry and the
    # merge path above -- the index from the first read is not a stable
    # handle across the two reads.
    entries, entry, err = _reload_and_locate(entry_id, file_path)
    if err is not None:
        return err

    entry["status"] = "deprecated"
    entry["deprecated_reason"] = reason
    entry["updated_at"] = now_iso()
    if superseded_by and type_name == "decisions":
        entry["superseded_by"] = superseded_by

    write_json_file(file_path, entries)
    _maybe_export_projections(base_dir, type_name)
    _mirror_out(entry, type_name, base_dir)

    return {"success": True, "id": entry_id, "status": "deprecated"}


def handle_prune_stale(params):
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    cfg = read_config(base_dir)
    days = params.get("days", cfg.get("stale_threshold_days", 30))
    now_dt = datetime.now(timezone.utc)

    if not os.path.exists(base_dir):
        return {"stale": [], "message": "No context directory found."}

    paths = _resolve_paths(base_dir)
    stale = []
    for tname, tpath in paths.items():
        for e in read_json_file(tpath):
            # Superseded entries are intentionally-kept history, not stale
            # work needing review — skip them like deprecated ones.
            if e.get("status", "active") in ("deprecated", "superseded"):
                continue
            verified = e.get("verified_at") or e.get("created_at", "")
            try:
                v_dt = datetime.fromisoformat(verified.replace("Z", "+00:00"))
                age = (now_dt - v_dt).days
                if age > days:
                    summary = e.get("summary") or e.get("name") or e.get("rule") or "?"
                    stale.append({
                        "id": e.get("id"),
                        "type": tname,
                        "summary": summary,
                        "days_since_verified": age,
                        "verified_at": verified,
                    })
            except Exception:
                pass

    stale.sort(key=lambda x: x["days_since_verified"], reverse=True)
    return {
        "stale": stale,
        "count": len(stale),
        "threshold_days": days,
        "action": "Review each entry. Call update_entry to refresh verified_at, or deprecate_entry to retire it.",
    }


def handle_get_compaction_report(params):
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return {"has_report": False, "message": "No project resolved; no compaction report available."}
    report_path = os.path.join(base_dir, "compaction_report.json")
    if not os.path.exists(report_path):
        return {"has_report": False, "message": "No compaction report found. No compaction has been detected yet."}

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception as e:
        return {"error": f"Failed to read compaction report: {e}"}

    report["has_report"] = True
    if report.get("status") == "discrepancies_found":
        report["action"] = (
            "Discrepancies detected after last compaction. Review missing and modified entries "
            "with the user before making changes. Missing entries may need to be re-recorded."
        )
    return report


def _verified_sha(base_dir):
    """HEAD at the moment an entry is verified, or "" outside a repo.

    Server-set, never a tool parameter: it is an observation about the machine
    doing the write, not something a caller should be able to assert. Keeping
    it off the schema also keeps it off the tools/list token budget (con-004).
    """
    if code_drift is None:
        return ""
    try:
        return code_drift.head_sha(base_dir)
    except Exception:
        return ""


def handle_verify_quality(params):
    """Scan entries for quality issues and return a flagged list.

    Issue types:
      - legacy: pre-v0.4 entry, missing structured fields (problem,
        why_chosen, purpose, triggering_incident). Suggest enrichment.
      - thin_reason: rationale/reason text below min_reason_chars
        threshold. Suggest expanding.
      - no_tags: entry has zero tags, hurting retrieval.
      - isolated: entry has no related_to but shares a tag with at least
        one sibling — suggests a missed link.

    The PreCompact hook calls this automatically and surfaces the result
    so Claude can enrich entries before context is compressed. Also
    callable manually from the chat.
    """
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not os.path.exists(base_dir):
        return {"flagged": [], "count": 0, "message": "No context directory found."}

    min_reason = params.get("min_reason_chars", 80)
    paths = _resolve_paths(base_dir)

    # Build a tag→[id] index across all active entries so we can detect
    # isolated entries (tag overlap but no related_to link).
    all_active = []  # list of (type_name, entry)
    for tname, tpath in paths.items():
        for e in read_json_file(tpath):
            if e.get("status", "active") in ("deprecated", "superseded"):
                continue
            all_active.append((tname, e))

    tag_index = {}
    for tname, e in all_active:
        for tag in e.get("tags", []):
            tag_index.setdefault(tag.lower(), set()).add(e.get("id"))

    # Verify against the artifact, not the calendar. `prune_stale` asks how long
    # since someone looked; this asks whether the code under the entry moved,
    # which is the question that actually decides whether it is still true.
    # None (no git, no repo) is distinct from {} (a repo with nothing scoped).
    drift = None
    if code_drift is not None and params.get("check_drift", True):
        try:
            drift = code_drift.scan([e for _, e in all_active], base_dir)
        except Exception:
            drift = None

    usage_data = usage.read(base_dir) if usage is not None else {}

    flagged = []
    for tname, e in all_active:
        issues = []
        eid = e.get("id", "?")

        # Legacy detection — schema_version is set to 4 by v0.4 writes.
        # Older entries lack it AND lack the new structured fields.
        is_v4 = e.get("schema_version") == 4
        if not is_v4:
            if tname == "decisions" and not e.get("why_chosen"):
                issues.append({
                    "type": "legacy",
                    "detail": "Pre-v0.4 decision: missing structured fields (problem, why_chosen). The freeform 'rationale' is preserved but won't show the full why. Consider re-recording with the v0.4 schema.",
                })
            elif tname == "pipelines" and not e.get("purpose"):
                issues.append({
                    "type": "legacy",
                    "detail": "Pre-v0.4 pipeline: missing 'purpose' field. Re-record so future sessions know why this pipeline exists, not just what it does.",
                })
            elif tname == "constraints" and not e.get("triggering_incident"):
                # triggering_incident is optional, so only flag if reason is also thin
                pass

        # Thin reason text
        if tname == "decisions":
            reason_text = (e.get("why_chosen") or "") + " " + (e.get("rationale") or "")
        elif tname == "constraints":
            reason_text = e.get("reason") or ""
        elif tname == "pipelines":
            reason_text = e.get("purpose") or ""
        else:
            reason_text = ""
        if len(reason_text.strip()) < min_reason:
            issues.append({
                "type": "thin_reason",
                "detail": f"Reasoning text is only {len(reason_text.strip())} chars (threshold: {min_reason}). Use update_entry to expand the rationale with concrete context.",
            })

        # No tags
        if not e.get("tags"):
            issues.append({
                "type": "no_tags",
                "detail": "Entry has no tags. Tags are the primary retrieval signal — add 2-4 lowercase, hyphen-separated tags.",
            })

        # Isolated: tag overlap with siblings but no related_to link
        own_tags = set(t.lower() for t in e.get("tags", []))
        own_links = set(e.get("related_to", []) or [])
        sibling_ids = set()
        for tag in own_tags:
            sibling_ids |= tag_index.get(tag, set())
        sibling_ids.discard(eid)
        unlinked_siblings = sibling_ids - own_links
        if own_tags and unlinked_siblings and not own_links:
            issues.append({
                "type": "isolated",
                "detail": f"Shares tags with {len(unlinked_siblings)} other entries but has no related_to links. Suggested links: {sorted(unlinked_siblings)[:5]}",
            })

        # The code this entry describes moved, or moved away entirely.
        if drift and code_drift is not None:
            issues.extend(code_drift.issues_for(drift.get(eid)))

        # A constraint naming its own check is only useful while the name
        # resolves. Never executed -- see code_drift.enforcement_issues.
        if code_drift is not None and e.get("enforced_by"):
            try:
                issues.extend(code_drift.enforcement_issues(
                    e, code_drift.repo_root(base_dir)))
            except Exception:
                pass

        # Carried into every session and never actually sought.
        if usage is not None:
            issues.extend(usage.issues_for(usage.stats_for(base_dir, eid, usage_data)))

        if issues:
            flagged.append({
                "id": eid,
                "type": tname,
                "summary": e.get("summary") or e.get("name") or e.get("rule") or "?",
                "issues": issues,
            })

    return {
        "flagged": flagged,
        "count": len(flagged),
        "total_active": len(all_active),
        "min_reason_chars": min_reason,
        # Absent git or a work tree there is no drift signal at all. Saying so
        # keeps "nothing drifted" and "we could not look" distinguishable.
        "drift_checked": drift is not None,
        "scoped_entries_checked": len(drift) if drift else 0,
        "action": (
            "Use update_entry to enrich flagged entries. For 'legacy' issues, "
            "the original entry stays valid but a v0.4 re-record captures the full why. "
            "Skip flags that don't apply — verification is advisory, not blocking."
        ),
    }


def handle_export_markdown(params):
    """Manual regeneration of the DECISIONS.md projection — works without
    the markdown_export flag, so existing repos can be backfilled."""
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not os.path.exists(base_dir):
        return {"error": "No context directory found."}
    try:
        out_path, count = _write_decisions_markdown(base_dir, out_path=params.get("path"))
    except Exception as e:
        return {"error": f"Failed to write markdown projection: {e}"}
    return {
        "success": True,
        "path": out_path,
        "decisions_rendered": count,
        "note": (
            "Derived projection regenerated whole from decisions.json. Do not "
            "hand-edit; set config markdown_export.enabled=true to regenerate "
            "automatically on every decision write."
        ),
    }


def handle_export_rules(params):
    """Manual regeneration of the .claude/rules/ scoped-constraint projection.

    Deliberately NOT in TOOLS. con-004 caps the tools/list payload every
    client pays for at session start, and this is a backfill operation --
    run once when adopting the projection, after which render-on-write
    keeps it current with no tool call. It stays in HANDLERS so the CLI
    (`context-keeper export_rules '{}'`) and scripted callers can reach it,
    the same hidden-handler pattern the retired record_* aliases use.
    """
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not os.path.exists(base_dir):
        return {"error": "No context directory found."}
    try:
        out_dir, count, skipped = _write_scope_rules(base_dir, out_dir=params.get("path"))
    except RulesPathOutsideProject as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to write rules projection: {e}"}
    result = {
        "success": True,
        "path": out_dir,
        "rules_written": count,
        "note": (
            "Derived projection regenerated whole from constraints.json. Do not "
            "hand-edit; set config rules_export.enabled=true to regenerate "
            "automatically on every constraint write. Only scoped constraints "
            "are projected -- a global constraint has no path to trigger on."
        ),
    }
    if skipped:
        # Silence here would read as "everything is covered". It isn't: these
        # constraints have a scope that cannot become a safe glob, so no rule
        # file fires for them and only the session-start block carries them.
        result["skipped_scopes"] = skipped
        result["skipped_note"] = (
            "These scopes contain glob metacharacters ([ ] { } * ?) and were "
            "skipped -- a pattern Claude Code cannot parse matches nothing, so "
            "the rule would never fire. Re-record them with a literal path."
        )
    return result


# ============================================================
# Team-shared snapshot (Item 5)
#
# The working store in .context/ is per-machine (and usually gitignored). The
# snapshot is a single compressed artifact committed NEXT TO the project in
# .context-keeper/, so a team can share project memory through git. Committing
# it is opt-in. On first run (empty local store + snapshot present) it is
# imported automatically so a fresh clone starts with the shared memory.
#
# Codec note: stdlib gzip, not zstd. A real .zst needs the third-party
# `zstandard` package, which would break this project's zero-dependency
# guarantee; gzip gives a comparable ratio on JSON with no dependency.
# ============================================================

SNAPSHOT_DIR_NAME = ".context-keeper"
SNAPSHOT_FILE_NAME = "memory.json.gz"
SNAPSHOT_SCHEMA = "context-keeper/snapshot@1"
_SNAPSHOT_STORES = ("decisions", "pipelines", "constraints")


def _snapshot_paths(base_dir):
    """Return (project_root, snapshot_dir, snapshot_file) for a .context dir."""
    project_root = os.path.dirname(os.path.abspath(base_dir))
    snap_dir = os.path.join(project_root, SNAPSHOT_DIR_NAME)
    return project_root, snap_dir, os.path.join(snap_dir, SNAPSHOT_FILE_NAME)


def _ensure_snapshot_gitattributes(project_root):
    """Add `<.context-keeper/memory.json.gz> merge=ours` to .gitattributes so the
    committed artifact never produces a merge conflict. Idempotent."""
    line = f"{SNAPSHOT_DIR_NAME}/{SNAPSHOT_FILE_NAME} merge=ours"
    path = os.path.join(project_root, ".gitattributes")
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
        if line in existing.splitlines():
            return "present"
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    return "added"


def handle_export_snapshot(params):
    """Write the whole store to a compressed, committable snapshot and ensure
    the .gitattributes merge=ours guard. Non-destructive to the working store."""
    import gzip
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    ensure_context_dir(base_dir)

    stores, counts = {}, {}
    for name in _SNAPSHOT_STORES:
        entries = read_json_file(os.path.join(base_dir, name + ".json"))
        stores[name] = entries
        counts[name] = len(entries)
    # Deliberately no export timestamp in the payload: combined with mtime=0
    # below, the snapshot is byte-identical when the store is unchanged, so
    # re-exporting doesn't churn the git history (git records the commit time).
    blob = {"schema": SNAPSHOT_SCHEMA, "stores": stores}
    # mtime=0 keeps the gzip byte-stable across runs when content is unchanged.
    packed = gzip.compress(json.dumps(blob, indent=2).encode("utf-8"),
                           compresslevel=9, mtime=0)

    project_root, snap_dir, snap_path = _snapshot_paths(base_dir)
    os.makedirs(snap_dir, exist_ok=True)
    tmp = snap_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(packed)
    os.replace(tmp, snap_path)
    ga = _ensure_snapshot_gitattributes(project_root)

    return {
        "success": True,
        "path": snap_path,
        "counts": counts,
        "bytes": len(packed),
        "gitattributes": ga,
        "note": (
            "Snapshot written. Committing it is opt-in: `git add "
            f"{SNAPSHOT_DIR_NAME}/{SNAPSHOT_FILE_NAME} .gitattributes` to share "
            "project memory with your team. For merge=ours to take effect, run "
            "once per clone: `git config merge.ours.driver true`."
        ),
    }


def handle_import_snapshot(params):
    """Import the committed snapshot into the working store. Non-destructive:
    a store that already has entries is left untouched (reported as skipped)."""
    import gzip
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    _, _, snap_path = _snapshot_paths(base_dir)
    if not os.path.exists(snap_path):
        return {"error": f"No snapshot found at {snap_path}."}
    try:
        with gzip.open(snap_path, "rb") as f:
            blob = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"Could not read snapshot at {snap_path}: {e}"}

    stores = blob.get("stores", {}) if isinstance(blob, dict) else {}
    ensure_context_dir(base_dir)
    imported, skipped = {}, {}
    for name in _SNAPSHOT_STORES:
        path = os.path.join(base_dir, name + ".json")
        if read_json_file(path):  # existing entries — never overwrite
            skipped[name] = True
            continue
        entries = stores.get(name, [])
        if entries:
            write_json_file(path, entries)
            # Entries arriving from a snapshot are still writes to the store,
            # so the derived projections have to catch up. Without this a
            # fresh clone imports the constraints and gets no rule files
            # until someone happens to record another one -- the moment the
            # rules are most wanted is the moment they would be absent.
            _maybe_export_projections(base_dir, name)
        imported[name] = len(entries)
    return {
        "success": True,
        "imported": imported,
        "skipped": skipped or None,
    }


def _maybe_bootstrap_from_snapshot(base_dir):
    """First-run hydrate: when the working store is empty/absent but a committed
    snapshot exists, import it so a fresh clone starts with the shared memory.
    A no-op when any store already has entries or no snapshot exists. Never
    raises — bootstrap must not break a normal call."""
    try:
        if base_dir is None:
            return None
        for name in _SNAPSHOT_STORES:
            if read_json_file(os.path.join(base_dir, name + ".json")):
                return None  # store already populated -> not a first run
        _, _, snap_path = _snapshot_paths(base_dir)
        if not os.path.exists(snap_path):
            return None
        return handle_import_snapshot(
            {"project_dir": os.path.dirname(os.path.abspath(base_dir))})
    except Exception:
        return None


def handle_pull_remote(params):
    """MCP tool: merge remote-recorded entries into the local store."""
    if _mirror is None:
        return {"pulled": 0, "reason": "mirror module unavailable"}
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not _mirror.mirror_enabled():
        return {"pulled": 0, "reason": "disabled",
                "note": "Set CONTEXT_KEEPER_REMOTE_URL to enable mirroring."}
    try:
        return _mirror.pull_remote(base_dir)
    except Exception as e:
        return {"pulled": 0, "error": str(e)}


def handle_backfill_remote(params):
    """MCP tool: push all local entries for the project to the remote."""
    if _mirror is None:
        return {"backfilled": 0, "reason": "mirror module unavailable"}
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR
    if not _mirror.mirror_enabled():
        return {"backfilled": 0, "reason": "disabled",
                "note": "Set CONTEXT_KEEPER_REMOTE_URL to enable mirroring."}
    try:
        return _mirror.backfill_remote(base_dir)
    except Exception as e:
        return {"backfilled": 0, "error": str(e)}


def handle_mirror(params):
    """MCP tool: mirror(op='pull'|'backfill'). Routes to the pull/backfill impls,
    so behavior is identical to the retired pull_remote / backfill_remote tools."""
    op = params.get("op")
    inner = {k: v for k, v in params.items() if k != "op"}
    if op == "pull":
        return handle_pull_remote(inner)
    if op == "backfill":
        return handle_backfill_remote(inner)
    return {
        "error": "mirror requires op to be 'pull' or 'backfill'.",
        "validation_errors": [{
            "field": "op",
            "guidance": "Set op to 'pull' or 'backfill'.",
        }],
    }


HANDLERS = {
    "record_entry": handle_record_entry,
    "record_decision": handle_record_decision,
    "record_pipeline": handle_record_pipeline,
    "record_constraint": handle_record_constraint,
    "get_context": handle_get_context,
    "query_entries": handle_query_entries,
    "get_project_summary": handle_get_project_summary,
    "update_entry": handle_update_entry,
    "export_markdown": handle_export_markdown,
    "deprecate_entry": handle_deprecate_entry,
    "prune_stale": handle_prune_stale,
    "get_compaction_report": handle_get_compaction_report,
    "verify_quality": handle_verify_quality,
    "reload_constraints": handle_reload_constraints,
    "export_snapshot": handle_export_snapshot,
    "import_snapshot": handle_import_snapshot,
    "mirror": handle_mirror,
    # Hidden back-compat: retired from tools/list (folded into `mirror`) but
    # still dispatchable by name so existing CLI/scripted callers don't break.
    "pull_remote": handle_pull_remote,
    "backfill_remote": handle_backfill_remote,
    # Hidden by design (never in tools/list): a one-time backfill the CLI
    # runs when adopting the rules projection. See handle_export_rules.
    "export_rules": handle_export_rules,
}

# ============================================================
# JSON-RPC stdio transport
# ============================================================


def _force_utf8_stdio():
    """Force UTF-8 on the MCP stdio transport.

    On Windows the interpreter opens stdin/stdout in the ANSI code page
    (cp1252), so UTF-8 bytes sent by the MCP client are decoded as cp1252:
    an em-dash (U+2014, wire bytes b'\\xe2\\x80\\x94') arrives as the mojibake
    'a\\u20ac"' and is then persisted verbatim, corrupting the store. The
    on-disk json.dump is fine (ensure_ascii) -- the damage is at the INPUT
    boundary, so the fix belongs here. Guarded for interpreters/streams
    without reconfigure() (added 3.7; absent on already-wrapped streams).
    """
    for stream_name in ("stdin", "stdout"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _dispatch_message(msg):
    """Handle ONE JSON-RPC message object.

    Returns the response dict to write, or None when nothing should be
    written (a notification). Never raises on malformed input: anything
    that isn't a JSON-RPC request object comes back as a -32600 Invalid
    Request instead of propagating out and killing the transport.

    Split out of _serve_stdio so a batch (a top-level JSON array, which is
    legal JSON-RPC 2.0 and which a client may send at any time) can be
    fanned out over the same logic. Previously the loop did msg.get("id")
    directly on the parsed line, so a batch array -- or any non-object
    top-level JSON -- raised AttributeError and took the whole server down.
    """
    if not isinstance(msg, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600,
                      "message": "Invalid Request: expected a JSON-RPC object"},
        }

    msg_id = msg.get("id")
    method = msg.get("method", "")
    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": msg_id if isinstance(msg_id, (str, int, float)) else None,
            "error": {"code": -32600, "message": "Invalid Request: 'method' must be a string"},
        }
    params = msg.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "context-keeper", "version": __version__},
            },
        }
    if method.startswith("notifications/"):
        # Notifications take no reply, per JSON-RPC 2.0.
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        if not isinstance(tool_args, dict):
            tool_args = {}
        handler = HANDLERS.get(tool_name)

        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}],
                    "isError": True,
                },
            }
        raised = False
        try:
            result = handler(tool_args)
        except Exception as e:
            result = {"error": f"Tool '{tool_name}' failed: {e}"}
            raised = True
        tool_result = {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
        }
        # A handler that raised is an error result, same as the
        # unknown-tool branch -- flag it so the client doesn't treat
        # the exception text as a successful payload.
        if raised:
            tool_result["isError"] = True
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": tool_result,
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _handle_line(line):
    """Parse one transport line and return the JSON text to write back, or None.

    Handles the three top-level shapes a conforming JSON-RPC 2.0 client may
    send: a single request object, a batch (non-empty array of request
    objects -> array of the responses that aren't notifications), and
    garbage (-32700 / -32600). A batch whose members are all notifications
    produces no output, per spec.
    """
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        # Malformed JSON: answer with Parse Error rather than going silent,
        # so a client isn't left waiting on a request the server dropped.
        return json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        })

    if isinstance(msg, list):
        if not msg:
            # "If the batch rpc call itself fails to be recognized ... the
            # response ... MUST be a single Response object" -- spec.
            return json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request: empty batch"},
            })
        responses = [r for r in (_dispatch_message(m) for m in msg) if r is not None]
        if not responses:
            return None
        return json.dumps(responses)

    response = _dispatch_message(msg)
    return None if response is None else json.dumps(response)


def _serve_stdio():
    _force_utf8_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out = _handle_line(line)
        if out is None:
            continue
        sys.stdout.write(out + "\n")
        sys.stdout.flush()


def _run_cli(argv):
    """CLI parity: `context-keeper <tool> '<json-args>'`.

    Dispatches to the SAME HANDLERS the MCP tools use — no duplicated logic —
    and prints the handler's result as JSON. Exit codes: 2 for a usage error
    (unknown tool / bad JSON), 1 if the handler returns an {"error": ...}, 0
    otherwise. Project resolution is identical to the MCP path (CONTEXT_KEEPER_
    PROJECT env var, cwd/.context, or a project_dir key in the JSON args).
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        names = ", ".join(sorted(HANDLERS))
        sys.stderr.write(
            "Usage: context-keeper <tool> '<json-args>'\n"
            f"Tools: {names}\n"
            'Example: context-keeper query_entries \'{"kind": "constraint"}\'\n')
        return 0 if argv else 2

    tool = argv[0]
    handler = HANDLERS.get(tool)
    if handler is None:
        sys.stderr.write(
            f"Unknown tool: {tool}\nRun 'context-keeper --help' for the list.\n")
        return 2

    raw = argv[1] if len(argv) > 1 else "{}"
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Invalid JSON args: {e}\n")
        return 2
    if not isinstance(args, dict):
        sys.stderr.write("JSON args must be a JSON object.\n")
        return 2

    try:
        result = handler(args)
    except Exception as e:
        result = {"error": f"Tool '{tool}' failed: {e}"}
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 1 if isinstance(result, dict) and result.get("error") else 0


def main():
    # CLI mode when args are present; otherwise the stdio MCP server (unchanged),
    # so `context-keeper = server:main` keeps serving stdio with no args.
    argv = sys.argv[1:]
    if argv:
        sys.exit(_run_cli(argv))
    _serve_stdio()


if __name__ == "__main__":
    main()
