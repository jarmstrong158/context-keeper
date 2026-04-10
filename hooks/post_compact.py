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
PROJECT_DIR = os.environ.get("CONTEXT_KEEPER_PROJECT", os.getcwd())
CONTEXT_DIR = os.path.join(PROJECT_DIR, CONTEXT_DIR_NAME)
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json")
REPORT_PATH = os.path.join(CONTEXT_DIR, "compaction_report.json")
LOG_PATH = os.path.join(CONTEXT_DIR, "hook.log")

FILES = {
    "decisions": os.path.join(CONTEXT_DIR, "decisions.json"),
    "pipelines": os.path.join(CONTEXT_DIR, "pipelines.json"),
    "constraints": os.path.join(CONTEXT_DIR, "constraints.json"),
}


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


def main():
    if not os.path.exists(SNAPSHOT_PATH):
        return

    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception:
        log("POST_COMPACT: Could not read snapshot file")
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
