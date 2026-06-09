#!/usr/bin/env python3
"""Context Keeper — PreCompact hook.

Fires before Claude Code compaction. Snapshots all active context entries
so post_compact.py can detect if anything was lost.
"""

import json
import os
import sys
from datetime import datetime, timezone

CONTEXT_DIR_NAME = ".context"


def _resolve_project_dir():
    """Same resolution logic as server.py: env var, then cwd-if-exists,
    then walk parents looking for an existing .context/, then None.
    Never creates a .context/ implicitly."""
    explicit = os.environ.get("CONTEXT_KEEPER_PROJECT")
    if explicit:
        return explicit
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, CONTEXT_DIR_NAME)):
        return cwd
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
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json") if CONTEXT_DIR else None
LOG_PATH = os.path.join(CONTEXT_DIR, "hook.log") if CONTEXT_DIR else None

FILES = {
    "decisions": os.path.join(CONTEXT_DIR, "decisions.json"),
    "pipelines": os.path.join(CONTEXT_DIR, "pipelines.json"),
    "constraints": os.path.join(CONTEXT_DIR, "constraints.json"),
} if CONTEXT_DIR else {}


def read_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def log(message):
    if CONTEXT_DIR is None:
        return
    try:
        os.makedirs(CONTEXT_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _scan_quality(entries_by_type, min_reason_chars=80):
    """Lightweight quality scan — same logic as server.handle_verify_quality
    but inlined here so the hook stays dependency-free (the hook can't
    import the MCP server at runtime — it's a separate process).

    KEEP IN SYNC with server.handle_verify_quality: the two flag-detection
    rules (legacy / thin_reason / no_tags / isolated) are intentionally
    duplicated. If you change the rules in one place, change them in both.

    Returns a list of {id, type, summary, issues} flagged entries.
    """
    # Build tag→ids index for isolation detection
    all_active = []
    for tname, entries in entries_by_type.items():
        for e in entries:
            all_active.append((tname, e))
    tag_index = {}
    for tname, e in all_active:
        for tag in e.get("tags", []):
            tag_index.setdefault(tag.lower(), set()).add(e.get("id"))

    flagged = []
    for tname, e in all_active:
        issues = []
        eid = e.get("id", "?")
        is_v4 = e.get("schema_version") == 4

        if not is_v4:
            if tname == "decisions" and not e.get("why_chosen"):
                issues.append("legacy: missing structured fields (problem, why_chosen)")
            elif tname == "pipelines" and not e.get("purpose"):
                issues.append("legacy: missing 'purpose' field")

        if tname == "decisions":
            reason_text = (e.get("why_chosen") or "") + " " + (e.get("rationale") or "")
        elif tname == "constraints":
            reason_text = e.get("reason") or ""
        elif tname == "pipelines":
            reason_text = e.get("purpose") or ""
        else:
            reason_text = ""
        if len(reason_text.strip()) < min_reason_chars:
            issues.append(f"thin reasoning ({len(reason_text.strip())} chars, threshold {min_reason_chars})")

        if not e.get("tags"):
            issues.append("no tags")

        own_tags = set(t.lower() for t in e.get("tags", []))
        own_links = set(e.get("related_to", []) or [])
        sibling_ids = set()
        for tag in own_tags:
            sibling_ids |= tag_index.get(tag, set())
        sibling_ids.discard(eid)
        if own_tags and (sibling_ids - own_links) and not own_links:
            issues.append(f"isolated (shares tags with {len(sibling_ids)} entries, no related_to links)")

        if issues:
            flagged.append({
                "id": eid,
                "type": tname,
                "summary": (e.get("summary") or e.get("name") or e.get("rule") or "?")[:80],
                "issues": issues,
            })
    return flagged


def main():
    if CONTEXT_DIR is None or not os.path.exists(CONTEXT_DIR):
        return

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entries": {},
        "counts": {},
    }

    for type_name, path in FILES.items():
        entries = read_json(path)
        active = [e for e in entries if e.get("status", "active") != "deprecated"]
        snapshot["entries"][type_name] = active
        snapshot["counts"][type_name] = len(active)

    total = sum(snapshot["counts"].values())

    os.makedirs(CONTEXT_DIR, exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    counts_str = ", ".join(f"{k}={v}" for k, v in snapshot["counts"].items())
    log(f"PRE_COMPACT: {total} active entries ({counts_str})")

    # Quality scan — surface thin/legacy/isolated entries before
    # compaction so Claude can enrich them while the session context
    # is still warm. Cheap to run (single pass over entries).
    flagged = _scan_quality(snapshot["entries"])
    log(f"PRE_COMPACT: quality scan flagged {len(flagged)} entries")

    # Capture prompt: this message becomes part of the context that gets
    # compacted. Post-compaction, Claude sees the trace and is primed to
    # review the session for unrecorded decisions/constraints.
    print(
        "[Context Keeper] COMPACTION IMMINENT -- context is about to be "
        "compressed. After compaction, review what you remember from this "
        "session and record anything important:\n"
        "  - Architectural decisions or trade-offs: use record_decision\n"
        "  - Bugs, gotchas, or 'never do X' rules: use record_constraint\n"
        "  - Multi-step workflows established: use record_pipeline\n"
        "Skip trivial details. Only record what future sessions need to know."
    )

    if flagged:
        print(
            f"\n[Context Keeper] QUALITY SCAN: {len(flagged)} entries look thin or "
            f"underspecified. Consider enriching them via update_entry before "
            f"compaction so future sessions recover the full why:"
        )
        for f in flagged[:10]:  # cap output to keep hook noise bounded
            issues_str = "; ".join(f["issues"])
            print(f"  [{f['id']}] ({f['type']}) {f['summary']}")
            print(f"      issues: {issues_str}")
        if len(flagged) > 10:
            print(f"  ... and {len(flagged) - 10} more. Call verify_quality for the full list.")


if __name__ == "__main__":
    main()
