#!/usr/bin/env python3
"""Context Keeper MCP Server — Project memory for Claude.

Records and retrieves design decisions, pipeline flows, and constraints
so Claude maintains context across conversations. Zero external dependencies.
"""

import json
import os
import sys
from datetime import datetime, timezone

CONTEXT_DIR_NAME = ".context"


def _resolve_project_dir():
    """Resolve the project directory with a safe cwd fallback.

    Order of precedence:
      1. CONTEXT_KEEPER_PROJECT env var, if set (trusted — user opted in)
      2. cwd, ONLY if it already contains a .context/ directory
      3. Walk parent dirs from cwd, returning the first ancestor that
         already contains a .context/ directory (git-style discovery)
      4. None — refuse to default, callers must pass project_dir explicitly

    Steps 2 and 3 only resolve to directories that ALREADY contain
    .context/. We never create one implicitly, so the footgun where
    Claude Code is launched from a parent directory and context-keeper
    silently pollutes it stays fixed. The upward walk just lets the
    server find your project when launched from a subdirectory of it.
    """
    explicit = os.environ.get("CONTEXT_KEEPER_PROJECT")
    if explicit:
        return explicit
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, CONTEXT_DIR_NAME)):
        return cwd
    # Walk up the parent chain looking for an existing .context/ dir.
    # Stops at the filesystem root (parent == current). Bounded iteration
    # for safety in case a pathological FS confuses os.path.dirname.
    current = cwd
    for _ in range(64):
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        if os.path.isdir(os.path.join(parent, CONTEXT_DIR_NAME)):
            return parent
        current = parent
    return None


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
        "name": "record_decision",
        "description": (
            "Record an architectural or design decision. v0.4 schema: rationale is split "
            "into structured fields (problem, why_chosen, what_we_tried, tradeoffs) so future "
            "sessions can recover the full why, not just a summary. Min-length validation is "
            "enforced server-side — thin entries are rejected with guidance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was decided (1-2 sentences). Min 20 chars."},
                "problem": {
                    "type": "string",
                    "description": (
                        "What forced this decision? Describe the problem context, the trigger, "
                        "and what was at stake. 1-3 sentences. Min 40 chars."
                    ),
                },
                "why_chosen": {
                    "type": "string",
                    "description": (
                        "Actual reasoning. Why this option specifically? What evidence, principle, "
                        "or constraint drove the choice? 2-4 sentences. Min 60 chars."
                    ),
                },
                "what_we_tried": {
                    "type": "string",
                    "description": (
                        "Optional but encouraged: prior attempts that didn't work, dead ends "
                        "explored, hypotheses ruled out. The 'we tried X 3 times before Y' arc."
                    ),
                },
                "tradeoffs": {
                    "type": "string",
                    "description": (
                        "Optional but encouraged: what was given up by choosing this. "
                        "Future sessions need to know the cost, not just the benefit."
                    ),
                },
                "alternatives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "option": {"type": "string"},
                            "reason_rejected": {"type": "string"},
                        },
                    },
                    "description": "Other options considered and why they were rejected",
                },
                "constraints_created": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New constraints this decision introduces",
                },
                "related_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs of related entries (e.g. ['dec-005', 'con-006']). Use this to "
                        "link entries from the same arc — get_context can traverse the graph "
                        "and surface connective tissue that would otherwise be lost."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization and retrieval",
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "DEPRECATED in v0.4. Provided for backward compatibility only — if "
                        "supplied without why_chosen, it will be auto-mapped to why_chosen. "
                        "Prefer the structured fields above."
                    ),
                },
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to the target project. Creates .context/ if needed.",
                },
            },
            "required": ["summary", "problem", "why_chosen"],
        },
    },
    {
        "name": "record_pipeline",
        "description": (
            "Record a multi-step workflow or data pipeline that must be followed in order. "
            "v0.4 schema adds purpose (required) and when_to_invoke (optional) so future "
            "sessions know not just what the pipeline does but why it exists and when to use it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pipeline name"},
                "purpose": {
                    "type": "string",
                    "description": (
                        "Why this pipeline exists. What does it accomplish that ad-hoc steps "
                        "couldn't? 1-3 sentences. Min 40 chars."
                    ),
                },
                "when_to_invoke": {
                    "type": "string",
                    "description": (
                        "Optional but encouraged: what triggers or conditions should make a "
                        "future session reach for this pipeline? The reusable 'when' knowledge."
                    ),
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "order": {"type": "integer"},
                            "action": {"type": "string", "description": "What this step does"},
                            "output": {"type": "string", "description": "What this step produces"},
                        },
                        "required": ["order", "action"],
                    },
                    "description": "Ordered list of steps",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Rules that apply to this pipeline",
                },
                "related_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of related entries (decisions, constraints, other pipelines)",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to the target project. Creates .context/ if needed.",
                },
            },
            "required": ["name", "purpose", "steps"],
        },
    },
    {
        "name": "record_constraint",
        "description": (
            "Record a rule or constraint that must be followed in this project. v0.4 schema "
            "enforces a min-length reason and adds an optional triggering_incident field — "
            "the gotcha story behind the rule, not just the rule itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The constraint in clear imperative language. Min 20 chars."},
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this constraint exists. What goes wrong if it's violated? 1-3 sentences. "
                        "Min 40 chars."
                    ),
                },
                "triggering_incident": {
                    "type": "string",
                    "description": (
                        "Optional but encouraged: the specific bug, gotcha, or incident that "
                        "led to this rule. Concrete > abstract for future sessions."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": "Where this applies: 'global' for whole project, or a file/module path",
                    "default": "global",
                },
                "hardness": {
                    "type": "string",
                    "enum": ["absolute", "advisory"],
                    "description": "absolute = never violate. advisory = prefer but exceptions exist.",
                    "default": "absolute",
                },
                "related_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of related entries (the decision that created this constraint, etc.)",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "project_dir": {
                    "type": "string",
                    "description": "Absolute path to the target project. Creates .context/ if needed.",
                },
            },
            "required": ["rule", "reason"],
        },
    },
    {
        "name": "get_context",
        "description": (
            "Retrieve relevant project context. Returns decisions, pipelines, and constraints "
            "sorted by relevance, capped by token budget. Pass an id to fetch a single entry "
            "at full fidelity."
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
                "include_related": {
                    "type": "boolean",
                    "description": (
                        "If true, after scoring also pull in entries linked via related_to "
                        "(depth=1) so arcs come through together. Default: true."
                    ),
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
        "name": "get_project_summary",
        "description": (
            "Return a concise overview of all active context: decisions, pipeline names, "
            "and absolute constraints. Includes usage guidance. Designed for conversation start."
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
            "Call this at session start before get_project_summary. If discrepancies "
            "are found, surface them to the user before proceeding."
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
            "Scan all entries for quality issues: legacy entries (pre-v0.4 schema, "
            "missing structured fields), thin reasons/why_chosen text, missing tags, "
            "isolated entries (no related_to despite tag overlap with siblings). "
            "Returns flagged entries with specific issues so you can enrich them. "
            "Auto-called by the PreCompact hook — also call manually before recording "
            "many entries from one arc, or when get_project_summary feels too sparse."
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
]

# ============================================================
# File helpers
# ============================================================


def ensure_context_dir(path=None):
    os.makedirs(path or CONTEXT_DIR, exist_ok=True)


def read_json_file(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, Exception):
        return []


def write_json_file(path, data):
    ensure_context_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


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


def next_id(entries, prefix):
    max_num = 0
    for e in entries:
        eid = e.get("id", "")
        if eid.startswith(prefix + "-"):
            try:
                num = int(eid.split("-", 1)[1])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"{prefix}-{max_num + 1:03d}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
            recency = max(0, 1 - (days_ago / 90))
            score += 20 * recency
        except Exception:
            score += 10  # can't parse, give middle score

    # Status (0-20)
    status = entry.get("status", "active")
    if status == "active":
        score += 20
    elif status == "superseded":
        score += 5

    return score


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
    return CONTEXT_DIR


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


# ============================================================
# Tool handlers
# ============================================================


def handle_record_decision(params):
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
    entries = read_json_file(dec_path)
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
        "schema_version": 4,
        "status": "active",
        "superseded_by": None,
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    # Preserve the deprecated `rationale` field on disk if the caller
    # passed it explicitly. Lets old MCP clients (and old test
    # assertions) keep working without losing what they wrote.
    if params.get("rationale"):
        entry["rationale"] = params["rationale"]
    entries.append(entry)
    write_json_file(dec_path, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


def handle_record_pipeline(params):
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
    entries = read_json_file(pipe_path)
    entry = {
        "id": next_id(entries, "pipe"),
        "name": params["name"],
        "purpose": params["purpose"],
        "when_to_invoke": params.get("when_to_invoke", ""),
        "steps": params["steps"],
        "constraints": params.get("constraints", []),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "schema_version": 4,
        "status": "active",
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(pipe_path, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


def handle_record_constraint(params):
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
    entries = read_json_file(con_path)
    entry = {
        "id": next_id(entries, "con"),
        "rule": params["rule"],
        "reason": params["reason"],
        "triggering_incident": params.get("triggering_incident", ""),
        "scope": params.get("scope", "global"),
        "hardness": params.get("hardness", "absolute"),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "schema_version": 4,
        "status": "active",
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(con_path, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


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

    # Score and sort
    now_dt = datetime.now(timezone.utc)
    scored = [(score_entry(e, tags, query, scope, now_dt), t, e) for t, e in typed_entries]
    scored.sort(key=lambda x: x[0], reverse=True)

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
            break

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
                break
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

    return {
        "results": results,
        "tokens_used": used_tokens,
        "token_budget": budget,
        "total_entries_scored": len(scored),
        "entries_returned": len(results),
        "related_added": related_added,
    }


def handle_get_project_summary(params):
    base_dir = _base_dir_from_params(params)
    budget = params.get("token_budget", 2000)

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

    # Absolute constraints first (most important)
    absolute = [c for c in constraints if c.get("hardness") == "absolute"]
    advisory = [c for c in constraints if c.get("hardness") != "absolute"]
    if absolute:
        lines.append(f"\nAbsolute Constraints ({len(absolute)}):")
        for c in absolute:
            lines.append(f"  [{c['id']}] {c['rule']}")

    if advisory:
        lines.append(f"\nAdvisory Constraints ({len(advisory)}):")
        for c in advisory:
            lines.append(f"  [{c['id']}] {c['rule']}")

    if decisions:
        lines.append(f"\nActive Decisions ({len(decisions)}):")
        for d in decisions:
            tags = ", ".join(d.get("tags", []))
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"  [{d['id']}] {d['summary']}{tag_str}")

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

    # Truncate summary to budget
    if estimate_tokens(summary_text) > budget:
        # Keep constraints, trim decisions/pipelines
        while estimate_tokens(summary_text) > budget and lines:
            lines.pop()
        summary_text = "\n".join(lines)

    return {
        "initialized": True,
        "summary": summary_text,
        "counts": {
            "decisions": len(decisions),
            "pipelines": len(pipelines),
            "constraints_absolute": len(absolute),
            "constraints_advisory": len(advisory),
        },
        "stale_entries": stale if stale else None,
        "usage_guidance": USAGE_GUIDANCE,
    }


def handle_update_entry(params):
    entry_id = params["id"]
    updates = params["updates"]
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    entry, type_name, file_path, index = _find_entry_by_id(entry_id, base_dir)
    if entry is None:
        return {"error": f"No entry found with id '{entry_id}'"}

    # Apply updates (protect id and created_at)
    protected = {"id", "created_at"}
    for key, val in updates.items():
        if key not in protected:
            entry[key] = val

    entry["verified_at"] = now_iso()
    entry["updated_at"] = now_iso()

    # Write back
    entries = read_json_file(file_path)
    entries[index] = entry
    write_json_file(file_path, entries)

    return {"success": True, "entry": entry}


def handle_deprecate_entry(params):
    entry_id = params["id"]
    reason = params["reason"]
    superseded_by = params.get("superseded_by")
    base_dir = _base_dir_from_params(params)
    if base_dir is None:
        return UNRESOLVED_PROJECT_ERROR

    entry, type_name, file_path, index = _find_entry_by_id(entry_id, base_dir)
    if entry is None:
        return {"error": f"No entry found with id '{entry_id}'"}

    entry["status"] = "deprecated"
    entry["deprecated_reason"] = reason
    entry["updated_at"] = now_iso()
    if superseded_by and type_name == "decisions":
        entry["superseded_by"] = superseded_by

    entries = read_json_file(file_path)
    entries[index] = entry
    write_json_file(file_path, entries)

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
            if e.get("status", "active") == "deprecated":
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
            if e.get("status", "active") == "deprecated":
                continue
            all_active.append((tname, e))

    tag_index = {}
    for tname, e in all_active:
        for tag in e.get("tags", []):
            tag_index.setdefault(tag.lower(), set()).add(e.get("id"))

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
        "action": (
            "Use update_entry to enrich flagged entries. For 'legacy' issues, "
            "the original entry stays valid but a v0.4 re-record captures the full why. "
            "Skip flags that don't apply — verification is advisory, not blocking."
        ),
    }


HANDLERS = {
    "record_decision": handle_record_decision,
    "record_pipeline": handle_record_pipeline,
    "record_constraint": handle_record_constraint,
    "get_context": handle_get_context,
    "get_project_summary": handle_get_project_summary,
    "update_entry": handle_update_entry,
    "deprecate_entry": handle_deprecate_entry,
    "prune_stale": handle_prune_stale,
    "get_compaction_report": handle_get_compaction_report,
    "verify_quality": handle_verify_quality,
}

# ============================================================
# JSON-RPC stdio transport
# ============================================================


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "context-keeper", "version": "0.4.0"},
                },
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS},
            }
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            handler = HANDLERS.get(tool_name)

            if handler is None:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}],
                        "isError": True,
                    },
                }
            else:
                try:
                    result = handler(tool_args)
                except Exception as e:
                    result = {"error": f"Tool '{tool_name}' failed: {e}"}
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    },
                }
        elif method.startswith("notifications/"):
            continue
        else:
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
