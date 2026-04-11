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

# Resolve project directory: env var > cwd
PROJECT_DIR = os.environ.get("CONTEXT_KEEPER_PROJECT", os.getcwd())
CONTEXT_DIR = os.path.join(PROJECT_DIR, CONTEXT_DIR_NAME)
DECISIONS_PATH = os.path.join(CONTEXT_DIR, "decisions.json")
PIPELINES_PATH = os.path.join(CONTEXT_DIR, "pipelines.json")
CONSTRAINTS_PATH = os.path.join(CONTEXT_DIR, "constraints.json")
CONFIG_PATH = os.path.join(CONTEXT_DIR, "config.json")

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
            "Record an architectural or design decision with rationale and alternatives considered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was decided (1-2 sentences)"},
                "rationale": {"type": "string", "description": "Why this choice was made"},
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
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization and retrieval",
                },
            },
            "required": ["summary", "rationale"],
        },
    },
    {
        "name": "record_pipeline",
        "description": "Record a multi-step workflow or data pipeline that must be followed in order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pipeline name"},
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
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "steps"],
        },
    },
    {
        "name": "record_constraint",
        "description": "Record a rule or constraint that must be followed in this project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The constraint in clear imperative language"},
                "reason": {"type": "string", "description": "Why this constraint exists"},
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
                "tags": {"type": "array", "items": {"type": "string"}},
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
        "inputSchema": {"type": "object", "properties": {}},
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
    if not os.path.exists(cfg_path):
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
    """Extract searchable words from an entry's text fields."""
    parts = []
    for key in ("summary", "rationale", "rule", "reason", "name"):
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
_TYPE_TO_PATH = {
    "decisions": DECISIONS_PATH,
    "pipelines": PIPELINES_PATH,
    "constraints": CONSTRAINTS_PATH,
}


def _resolve_paths(base_dir=None):
    """Return type->path mapping, optionally for another project."""
    if base_dir:
        return {
            "decisions": os.path.join(base_dir, "decisions.json"),
            "pipelines": os.path.join(base_dir, "pipelines.json"),
            "constraints": os.path.join(base_dir, "constraints.json"),
        }
    return dict(_TYPE_TO_PATH)


def _find_entry_by_id(entry_id, base_dir=None):
    """Find an entry by ID across all files. Returns (entry, type_name, file_path, index)."""
    prefix = entry_id.split("-")[0] if "-" in entry_id else ""
    type_name = _PREFIX_TO_FILE.get(prefix)
    paths = _resolve_paths(base_dir)

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
# Tool handlers
# ============================================================


def handle_record_decision(params):
    ensure_context_dir()
    entries = read_json_file(DECISIONS_PATH)
    entry = {
        "id": next_id(entries, "dec"),
        "summary": params["summary"],
        "rationale": params["rationale"],
        "alternatives": params.get("alternatives", []),
        "constraints_created": params.get("constraints_created", []),
        "tags": params.get("tags", []),
        "status": "active",
        "superseded_by": None,
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(DECISIONS_PATH, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


def handle_record_pipeline(params):
    ensure_context_dir()
    entries = read_json_file(PIPELINES_PATH)
    entry = {
        "id": next_id(entries, "pipe"),
        "name": params["name"],
        "steps": params["steps"],
        "constraints": params.get("constraints", []),
        "tags": params.get("tags", []),
        "status": "active",
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(PIPELINES_PATH, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


def handle_record_constraint(params):
    ensure_context_dir()
    entries = read_json_file(CONSTRAINTS_PATH)
    entry = {
        "id": next_id(entries, "con"),
        "rule": params["rule"],
        "reason": params["reason"],
        "scope": params.get("scope", "global"),
        "hardness": params.get("hardness", "absolute"),
        "tags": params.get("tags", []),
        "status": "active",
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    entries.append(entry)
    write_json_file(CONSTRAINTS_PATH, entries)
    return {"success": True, "id": entry["id"], "entry": entry}


def handle_get_context(params):
    entry_id = params.get("id")
    project_dir = params.get("project_dir")
    base_dir = os.path.join(os.path.normpath(project_dir), CONTEXT_DIR_NAME) if project_dir else CONTEXT_DIR

    # Direct ID lookup — full fidelity, no budget
    if entry_id:
        entry, type_name, _, _ = _find_entry_by_id(entry_id, base_dir)
        if entry is None:
            return {"error": f"No entry found with id '{entry_id}'"}
        return {"type": type_name, "entry": entry}

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

    # Gather all entries
    type_labels = {"decisions": "decision", "pipelines": "pipeline", "constraints": "constraint"}
    all_entries = []
    for tname in types:
        if tname in paths:
            for e in read_json_file(paths[tname]):
                e["_type"] = type_labels.get(tname, tname)
                all_entries.append(e)

    # Filter out deprecated
    all_entries = [e for e in all_entries if e.get("status", "active") != "deprecated"]

    # Score and sort
    now_dt = datetime.now(timezone.utc)
    scored = [(score_entry(e, tags, query, scope, now_dt), e) for e in all_entries]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Pack into budget with truncation
    results = []
    used_tokens = 0
    for sc, entry in scored:
        entry_type = entry.pop("_type", "unknown")
        clean = {k: v for k, v in entry.items() if not k.startswith("_")}

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
        used_tokens += cost

    return {
        "results": results,
        "tokens_used": used_tokens,
        "token_budget": budget,
        "total_entries_scored": len(scored),
        "entries_returned": len(results),
    }


def handle_get_project_summary(params):
    project_dir = params.get("project_dir")
    base_dir = os.path.join(os.path.normpath(project_dir), CONTEXT_DIR_NAME) if project_dir else CONTEXT_DIR
    budget = params.get("token_budget", 2000)

    if not os.path.exists(base_dir):
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

    entry, type_name, file_path, index = _find_entry_by_id(entry_id)
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

    entry, type_name, file_path, index = _find_entry_by_id(entry_id)
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
    cfg = read_config()
    days = params.get("days", cfg.get("stale_threshold_days", 30))
    now_dt = datetime.now(timezone.utc)

    if not os.path.exists(CONTEXT_DIR):
        return {"stale": [], "message": "No context directory found."}

    stale = []
    for tname, tpath in _TYPE_TO_PATH.items():
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


def handle_get_compaction_report(_params):
    report_path = os.path.join(CONTEXT_DIR, "compaction_report.json")
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
                    "serverInfo": {"name": "context-keeper", "version": "0.1.1"},
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
