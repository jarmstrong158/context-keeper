#!/usr/bin/env python3
"""Context Keeper — PreCompact hook.

Fires before Claude Code compaction. Snapshots all active context entries
so post_compact.py / session_start.py can detect if anything was lost.

Also runs the verify_quality scan and prints flagged entries. Note the
honest scope of that print: PreCompact hook stdout is shown in the
transcript/verbose view for the *user* — Claude Code does not inject it
into the model's context (only SessionStart and UserPromptSubmit stdout
are injected). The model-visible quality nudge therefore lives in
session_start.py, which injects a summary of the same scan at turn one.

Project resolution and the quality scan are imported from server.py
(same sys.path trick as session_start.py) — previously both were
duplicated here with a KEEP IN SYNC comment; now there is one
implementation.
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
    _server = None  # never break compaction because memory could not load

CONTEXT_DIR = _server.CONTEXT_DIR if _server else None
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json") if CONTEXT_DIR else None
LOG_PATH = os.path.join(CONTEXT_DIR, "hook.log") if CONTEXT_DIR else None

FILES = {
    "decisions": os.path.join(CONTEXT_DIR, "decisions.json"),
    "pipelines": os.path.join(CONTEXT_DIR, "pipelines.json"),
    "constraints": os.path.join(CONTEXT_DIR, "constraints.json"),
} if CONTEXT_DIR else {}


def _ascii(text):
    """Coerce to ASCII so a summary containing arrows/checkmarks can't crash
    the hook. Windows hook stdout is cp1252 and a non-ASCII char (e.g. an
    em-dash or arrow in an entry summary the user wrote) raises
    UnicodeEncodeError, which would kill the hook and lose its output. Same
    guarantee session_start.py already applies to its own prints (con-001)."""
    return str(text).encode("ascii", "replace").decode("ascii")


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


def main():
    if _server is None or CONTEXT_DIR is None or not os.path.exists(CONTEXT_DIR):
        return

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entries": {},
        "counts": {},
    }

    for type_name, path in FILES.items():
        entries = _server.read_json_file(path)
        active = [e for e in entries if e.get("status", "active") != "deprecated"]
        snapshot["entries"][type_name] = active
        snapshot["counts"][type_name] = len(active)

    total = sum(snapshot["counts"].values())

    os.makedirs(CONTEXT_DIR, exist_ok=True)
    tmp = SNAPSHOT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, SNAPSHOT_PATH)

    counts_str = ", ".join(f"{k}={v}" for k, v in snapshot["counts"].items())
    log(f"PRE_COMPACT: {total} active entries ({counts_str})")

    # Quality scan — single implementation lives in server.verify_quality.
    try:
        scan = _server.handle_verify_quality({})
        flagged = scan.get("flagged", [])
    except Exception:
        flagged = []
    log(f"PRE_COMPACT: quality scan flagged {len(flagged)} entries")

    # Transcript-visible status for the user (not injected into model
    # context — see module docstring).
    print(_ascii(
        "[Context Keeper] Compaction imminent -- snapshot of "
        f"{total} active entries saved for post-compaction integrity check."
    ))

    if flagged:
        print(_ascii(
            f"\n[Context Keeper] QUALITY SCAN: {len(flagged)} entries look thin or "
            f"underspecified. Consider enriching them via update_entry so "
            f"future sessions recover the full why:"
        ))
        for entry in flagged[:10]:  # cap output to keep hook noise bounded
            issues_str = "; ".join(i.get("type", "?") for i in entry.get("issues", []))
            summary = str(entry.get("summary", "?"))[:80]
            print(_ascii(f"  [{entry.get('id', '?')}] ({entry.get('type', '?')}) {summary}"))
            print(_ascii(f"      issues: {issues_str}"))
        if len(flagged) > 10:
            print(_ascii(f"  ... and {len(flagged) - 10} more. Call verify_quality for the full list."))


if __name__ == "__main__":
    main()
