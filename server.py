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
    # Opt-in derived projection: regenerate a human-readable DECISIONS.md
    # from decisions.json on every decision write. The markdown is
    # read-only output — never parsed back in, never merged.
    "markdown_export": {"enabled": False, "path": "DECISIONS.md"},
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
            "Record an architectural or design decision with structured rationale. "
            "Min lengths enforced server-side; thin entries are rejected with guidance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was decided (1-2 sentences)."},
                "problem": {
                    "type": "string",
                    "description": "What forced this decision — trigger, context, stakes. Min 40 chars.",
                },
                "why_chosen": {
                    "type": "string",
                    "description": (
                        "The actual reasoning: evidence, principle, or constraint behind the "
                        "choice. 2-4 sentences, min 60 chars."
                    ),
                },
                "what_we_tried": {
                    "type": "string",
                    "description": "Prior attempts and dead ends — the 'tried X before Y' arc. Encouraged.",
                },
                "tradeoffs": {
                    "type": "string",
                    "description": "What was given up by choosing this. Encouraged.",
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
                    "description": "Options considered and why rejected",
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
                        "IDs of related entries (e.g. ['dec-005', 'con-006']); "
                        "get_context traverses these links."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization and retrieval",
                },
                "retrieval_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "2-4 alternate phrasings a future session might search for (synonyms, "
                        "symptom descriptions, error messages). Indexed for retrieval — rescues "
                        "vocabulary-mismatch queries without needing embeddings."
                    ),
                },
                "origin": {
                    "type": "string",
                    "enum": ["user", "agent", "import"],
                    "description": (
                        "'user' = explicitly stated by the user; 'agent' = inferred (default); "
                        "'import' = backfilled. User-origin ranks higher."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "DEPRECATED: auto-maps to why_chosen if that field is absent.",
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
        "description": "Record a multi-step workflow that must be followed in order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pipeline name"},
                "purpose": {
                    "type": "string",
                    "description": "Why this pipeline exists — what ad-hoc steps couldn't do. Min 40 chars.",
                },
                "when_to_invoke": {
                    "type": "string",
                    "description": "What should make a future session reach for this pipeline. Encouraged.",
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
                "retrieval_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 alternate phrasings a future session might search for. Indexed for retrieval.",
                },
                "origin": {
                    "type": "string",
                    "enum": ["user", "agent", "import"],
                    "description": "Who authored this entry (user/agent/import). Default: agent.",
                },
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
        "description": "Record a rule or constraint that must be followed in this project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The constraint in clear imperative language."},
                "reason": {
                    "type": "string",
                    "description": "Why this exists — what goes wrong if violated, concretely. Min 40 chars.",
                },
                "triggering_incident": {
                    "type": "string",
                    "description": "The specific bug/gotcha/incident that led to this rule. Encouraged.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "'global', or a file/module path — scoped constraints are re-injected "
                        "when a covered file is edited (scope_guard hook)."
                    ),
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
                "retrieval_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 alternate phrasings a future session might search for. Indexed for retrieval.",
                },
                "origin": {
                    "type": "string",
                    "enum": ["user", "agent", "import"],
                    "description": "Who authored this entry (user/agent/import). Default: agent.",
                },
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
                    "description": (
                        "Temporal filter: only entries verified/created on or after this ISO "
                        "date (e.g. '2026-06-01' or full ISO timestamp)."
                    ),
                },
                "before": {
                    "type": "string",
                    "description": (
                        "Temporal filter: only entries verified/created strictly before this "
                        "ISO date."
                    ),
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
        "name": "get_project_summary",
        "description": (
            "Concise overview of all active context (constraints, decisions, pipelines). "
            "Designed for conversation start."
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
]

# ============================================================
# File helpers
# ============================================================


def ensure_context_dir(path=None):
    os.makedirs(path or CONTEXT_DIR, exist_ok=True)


def _read_json_file_checked(path):
    """Read a JSON entry file, distinguishing missing from corrupt.

    Returns (entries, error). A missing file is ([], None) — a fresh
    store. A file that exists but cannot be parsed
    (or isn't a list) returns ([], "<description>") so write paths can
    refuse instead of silently treating the store as empty and wiping
    history on the next write.
    """
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [], f"unparseable JSON ({e})"
    if not isinstance(data, list):
        return [], "top-level value is not a JSON list"
    return data, None


def read_json_file(path):
    """Soft read for retrieval paths: missing or corrupt both yield [].

    Write paths must use _load_entries_for_write instead — silently
    treating a corrupt store as empty turns the next append into a
    full-history wipe.
    """
    entries, _err = _read_json_file_checked(path)
    return entries


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
    return entries, None


def write_json_file(path, data):
    """Atomically write entries: write to a temp file, then os.replace.

    A crash mid-write leaves the old file intact instead of a truncated
    JSON document that the next read would treat as an empty store.
    """
    ensure_context_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
            if e.get("status", "active") == "deprecated":
                continue
            sim = _jaccard(new_words, _text_words(e))
            if sim >= threshold:
                matches.append({
                    "id": eid,
                    "type": tname,
                    "summary": e.get("summary") or e.get("name") or e.get("rule") or "?",
                    "similarity": round(sim, 2),
                })
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches[:3]


_SIMILAR_NOTE = (
    "Existing entries overlap heavily with this one. Review them: if this is a "
    "restatement, deprecate this entry and use update_entry on the original "
    "instead; if it contradicts one, resolve the conflict (deprecate_entry with "
    "superseded_by); if genuinely distinct, link them via related_to."
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
        if e.get("status", "active") == "deprecated":
            heading += " — **DEPRECATED**"
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
        "verified_at": now_iso(),
    }
    # Preserve the deprecated `rationale` field on disk if the caller
    # passed it explicitly. Lets old MCP clients (and old test
    # assertions) keep working without losing what they wrote.
    if params.get("rationale"):
        entry["rationale"] = params["rationale"]
    entries.append(entry)
    write_json_file(dec_path, entries)
    _maybe_export_markdown(base_dir)
    return _attach_similar({"success": True, "id": entry["id"], "entry": entry}, entry, base_dir)


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
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(pipe_path, entries)
    return _attach_similar({"success": True, "id": entry["id"], "entry": entry}, entry, base_dir)


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
    entries, load_err = _load_entries_for_write(con_path)
    if load_err is not None:
        return load_err
    entry = {
        "id": next_id(entries, "con"),
        "rule": params["rule"],
        "reason": params["reason"],
        "triggering_incident": params.get("triggering_incident", ""),
        "scope": params.get("scope", "global"),
        "hardness": params.get("hardness", "absolute"),
        "related_to": params.get("related_to", []),
        "tags": params.get("tags", []),
        "retrieval_hints": params.get("retrieval_hints", []),
        "origin": _origin_from_params(params),
        "schema_version": 4,
        "status": "active",
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(con_path, entries)
    return _attach_similar({"success": True, "id": entry["id"], "entry": entry}, entry, base_dir)


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
        # Don't leave a dangling section header (e.g. "Pipelines (3):") or a
        # trailing blank line after trimming the entries out from under it.
        while lines and (not lines[-1].strip() or lines[-1].rstrip().endswith(":")):
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

    entry, type_name, file_path, index = _find_entry_by_id(entry_id, base_dir)
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

    # Apply updates (protect id and created_at)
    protected = {"id", "created_at"}
    for key, val in updates.items():
        if key not in protected:
            entry[key] = val

    entry["verified_at"] = now_iso()
    entry["updated_at"] = now_iso()

    # Write back
    entries, load_err = _load_entries_for_write(file_path)
    if load_err is not None:
        return load_err
    entries[index] = entry
    write_json_file(file_path, entries)
    if type_name == "decisions":
        _maybe_export_markdown(base_dir)

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

    entries, load_err = _load_entries_for_write(file_path)
    if load_err is not None:
        return load_err
    entries[index] = entry
    write_json_file(file_path, entries)
    if type_name == "decisions":
        _maybe_export_markdown(base_dir)

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


HANDLERS = {
    "record_decision": handle_record_decision,
    "record_pipeline": handle_record_pipeline,
    "record_constraint": handle_record_constraint,
    "get_context": handle_get_context,
    "get_project_summary": handle_get_project_summary,
    "update_entry": handle_update_entry,
    "export_markdown": handle_export_markdown,
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
                    "serverInfo": {"name": "context-keeper", "version": "0.8.0"},
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
