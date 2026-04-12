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


if __name__ == "__main__":
    main()
