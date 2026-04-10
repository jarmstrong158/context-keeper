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
PROJECT_DIR = os.environ.get("CONTEXT_KEEPER_PROJECT", os.getcwd())
CONTEXT_DIR = os.path.join(PROJECT_DIR, CONTEXT_DIR_NAME)
SNAPSHOT_PATH = os.path.join(CONTEXT_DIR, "compaction_snapshot.json")
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


def main():
    if not os.path.exists(CONTEXT_DIR):
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


if __name__ == "__main__":
    main()
