"""Track which entries actually get read, and how.

Every entry in a store is injected at session start with equal standing, forever.
Nothing distinguishes an entry that shapes real decisions from one that has been
loaded two hundred times and never mattered — so the store can only grow, and
every addition is a permanent tax on every future session's context window.

This records two different events, because they mean different things:

  injected   the entry arrived in context as part of a blanket load
             (get_project_summary / the SessionStart hook). Nobody asked for it.
  retrieved  the entry came back from a targeted request — get_context with
             tags, or query_entries with predicates. Somebody was looking for
             this.

A high injected count with zero retrievals is the signal worth acting on: the
entry has been paid for hundreds of times and never sought once. That is a
proxy for usefulness rather than a measurement of it — an entry can be read and
acted on without being queried again — so it feeds an advisory flag, never an
automatic deletion.

Counts live in a sidecar (`.context/usage.json`), not on the entries. Entry
files are the durable record and are diffed in git; a counter that changes on
every read would churn the history of the thing it is meant to describe.

Every function is best-effort. Usage tracking must never be the reason a read
tool fails: a corrupt or unwritable sidecar degrades to no statistics.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

USAGE_FILENAME = "usage.json"
KINDS = ("injected", "retrieved")


def _path(base_dir):
    return os.path.join(base_dir, USAGE_FILENAME)


def read(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(base_dir, data):
    """Write via temp + replace so a crash mid-write cannot truncate the file."""
    target = _path(base_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=base_dir, prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, target)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def record(base_dir, entry_ids, kind):
    """Count an access. Silent no-op on any failure."""
    if kind not in KINDS or not entry_ids or not base_dir:
        return
    if not os.path.isdir(base_dir):
        return
    ids = [i for i in entry_ids if i]
    if not ids:
        return
    try:
        data = read(base_dir)
        now = datetime.now(timezone.utc).isoformat()
        for eid in ids:
            rec = data.get(eid) or {"injected": 0, "retrieved": 0}
            rec[kind] = int(rec.get(kind, 0)) + 1
            rec["last_" + kind] = now
            data[eid] = rec
        _write_atomic(base_dir, data)
    except Exception:
        return


def stats_for(base_dir, entry_id, data=None):
    data = read(base_dir) if data is None else data
    rec = data.get(entry_id) or {}
    return {
        "injected": int(rec.get("injected", 0)),
        "retrieved": int(rec.get("retrieved", 0)),
    }


# An entry has to have been carried this many times before "never retrieved"
# means anything. Below it, the entry is simply new.
UNUSED_INJECTION_THRESHOLD = 25


def issues_for(stats, threshold=UNUSED_INJECTION_THRESHOLD):
    """Advisory flag for entries that are loaded constantly and never sought."""
    if not stats:
        return []
    if stats["retrieved"] == 0 and stats["injected"] >= threshold:
        return [{
            "type": "unused",
            "detail": (
                f"Loaded into context {stats['injected']} times and never returned "
                "by a targeted query. It may be too vague to match a real question, "
                "or no longer relevant. Sharpen its tags and retrieval_hints, or "
                "deprecate it — every session is paying for this entry."
            ),
        }]
    return []
