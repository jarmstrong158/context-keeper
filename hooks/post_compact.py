#!/usr/bin/env python3
"""Context Keeper — PostCompact hook.

Compares current context state against the pre-compaction snapshot and
writes a discrepancy report. Wired under Stop (see README) as a safety
net, but the same comparison is also invoked directly by
session_start.py, because SessionStart(source=compact) fires immediately
after compaction — before any Stop — and the report must exist by the
time session_start injects it.

Idempotent: the comparison is keyed to the snapshot timestamp, so
re-running against an already-compared snapshot is a no-op.

Project resolution and file reading are imported from server.py (same
sys.path trick as session_start.py) instead of being duplicated here.
"""

import json
import os
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    import server as _server
except Exception:
    _server = None  # never break the hook flow; everything below no-ops

CONTEXT_DIR = _server.CONTEXT_DIR if _server else None
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json") if CONTEXT_DIR else None
REPORT_PATH = os.path.join(CONTEXT_DIR, "compaction_report.json") if CONTEXT_DIR else None
LOG_PATH = os.path.join(CONTEXT_DIR, "hook.log") if CONTEXT_DIR else None

FILES = {
    "decisions": os.path.join(CONTEXT_DIR, "decisions.json"),
    "pipelines": os.path.join(CONTEXT_DIR, "pipelines.json"),
    "constraints": os.path.join(CONTEXT_DIR, "constraints.json"),
} if CONTEXT_DIR else {}


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


def generate_report():
    """Run the snapshot-vs-current comparison and write the report file.

    Returns the report dict, or None if there is nothing to do (no
    project, no snapshot, or this snapshot was already compared).
    """
    if _server is None or SNAPSHOT_PATH is None or not os.path.exists(SNAPSHOT_PATH):
        return None

    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception:
        log("POST_COMPACT: Could not read snapshot file")
        return None

    if _already_compared(snapshot.get("timestamp")):
        return None

    missing = []
    modified = []

    for type_name, before_entries in snapshot.get("entries", {}).items():
        path = FILES.get(type_name)
        if not path:
            continue

        after_entries = _server.read_json_file(path)
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
    tmp = REPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    os.replace(tmp, REPORT_PATH)

    if has_discrepancies:
        log(f"POST_COMPACT: DISCREPANCIES — {len(missing)} missing, {len(modified)} modified")
    else:
        log("POST_COMPACT: clean — no discrepancies")

    return report


def main():
    report = generate_report()
    if report is None:
        return
    if report.get("status") == "discrepancies_found":
        # Transcript/verbose-visible only — the model-visible surface for
        # this is session_start.py, which injects the report at turn one.
        print("[Context Keeper] WARNING: Compaction discrepancies detected!", file=sys.stderr)
        print(f"  Missing entries: {report['missing_count']}", file=sys.stderr)
        print(f"  Modified entries: {report['modified_count']}", file=sys.stderr)
        print(f"  Report: {REPORT_PATH}", file=sys.stderr)
        print("  Call get_compaction_report to review details.", file=sys.stderr)


if __name__ == "__main__":
    main()
