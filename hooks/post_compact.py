#!/usr/bin/env python3
"""Context Keeper — PostCompact hook.

Fires after Claude Code compaction (via Stop hook). Compares current context
state against the pre-compaction snapshot and reports any discrepancies.
"""

import json
import os
import sys
from datetime import datetime, timezone

CONTEXT_DIR_NAME = ".context"


def _resolve_project_dir():
    """Same resolution logic as server.py — env var, then cwd-if-exists, then None."""
    explicit = os.environ.get("CONTEXT_KEEPER_PROJECT")
    if explicit:
        return explicit
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, CONTEXT_DIR_NAME)):
        return cwd
    return None


PROJECT_DIR = _resolve_project_dir()
CONTEXT_DIR = os.path.join(PROJECT_DIR, CONTEXT_DIR_NAME) if PROJECT_DIR else None
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json") if CONTEXT_DIR else None
REPORT_PATH = os.path.join(CONTEXT_DIR, "compaction_report.json") if CONTEXT_DIR else None
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


def diff_entries(before, after):
    """Compare two entry dicts. Returns dict of changed fields."""
    changes = {}
    all_keys = set(before.keys()) | set(after.keys())
    skip = {"verified_at", "updated_at"}  # timestamps change naturally
    for key in all_keys:
        if key in skip:
            continue
        bval = before.get(key)
        aval = after.get(key)
        if bval != aval:
            changes[key] = {"before": bval, "after": aval}
    return changes


def _already_compared(snapshot_ts):
    """Return True if REPORT_PATH already records a comparison against this
    exact snapshot timestamp. Prevents re-running on every Stop hook fire when
    nothing new has happened since the last run."""
    if not snapshot_ts or not os.path.exists(REPORT_PATH):
        return False
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            prev = json.load(f)
        return prev.get("snapshot_timestamp") == snapshot_ts
    except Exception:
        return False


def main():
    if SNAPSHOT_PATH is None or not os.path.exists(SNAPSHOT_PATH):
        return

    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception:
        log("POST_COMPACT: Could not read snapshot file")
        return

    # Idempotency: the Stop hook fires on every assistant response, but the
    # snapshot only changes when PreCompact runs. Skip if we've already
    # compared against this exact snapshot.
    if _already_compared(snapshot.get("timestamp")):
        return

    missing = []
    modified = []

    for type_name, before_entries in snapshot.get("entries", {}).items():
        path = FILES.get(type_name)
        if not path:
            continue

        after_entries = read_json(path)
        after_by_id = {e.get("id"): e for e in after_entries}

        for before_entry in before_entries:
            eid = before_entry.get("id")
            if not eid:
                continue

            after_entry = after_by_id.get(eid)
            if after_entry is None:
                missing.append({
                    "type": type_name,
                    "entry": before_entry,
                })
            else:
                changes = diff_entries(before_entry, after_entry)
                if changes:
                    modified.append({
                        "type": type_name,
                        "id": eid,
                        "changes": changes,
                    })

    has_discrepancies = len(missing) > 0 or len(modified) > 0
    status = "discrepancies_found" if has_discrepancies else "clean"

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_timestamp": snapshot.get("timestamp"),
        "status": status,
        "missing_entries": missing,
        "modified_entries": modified,
        "missing_count": len(missing),
        "modified_count": len(modified),
    }

    os.makedirs(CONTEXT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    if has_discrepancies:
        print(f"[Context Keeper] WARNING: Compaction discrepancies detected!", file=sys.stderr)
        print(f"  Missing entries: {len(missing)}", file=sys.stderr)
        print(f"  Modified entries: {len(modified)}", file=sys.stderr)
        print(f"  Report: {REPORT_PATH}", file=sys.stderr)
        print(f"  Call get_compaction_report to review details.", file=sys.stderr)
        log(f"POST_COMPACT: DISCREPANCIES — {len(missing)} missing, {len(modified)} modified")
    else:
        log("POST_COMPACT: clean — no discrepancies")


if __name__ == "__main__":
    main()
