#!/usr/bin/env python3
"""Build the committed history fixtures under evals/fixtures/.

Run once; the OUTPUT is committed. This script exists for provenance -- so a
reader can see exactly which real entries were copied and which lifecycle edges
were added -- not as a step anyone needs to re-run.

Why a fixture at all. The golden set needs cases whose correct answer is a
SUPERSEDED entry, to check that history stays reachable when you ask for it.
Across all 23 real stores on this machine there are 4 deprecated entries and
**zero** superseded ones, so there was nothing real to ask about. (That absence
is the same finding that motivated the v0.19 supersession work: reversals were
being made as in-place edits.)

So the fixture is real CONTENT with synthesized LIFECYCLE. Every entry is copied
verbatim from a live store; the only thing added is the supersedes edge. Each
edge is one the entries' own text already asserts -- xylem's dec-013-001d
literally opens "Thread 3 Tier A made the discipline say cambium was local-only
..., which was true then" -- so the fixture encodes history that genuinely
happened and simply was never linked.

It is NOT a snapshot of live state and must not be treated as one.
"""

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPOS = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")

# (fixture_name, source_store, [(older_id, newer_id, why_it_changed)])
#
# Every edge below is asserted by the newer entry's own recorded text. The
# `reason` is quoted/condensed from it rather than invented, because the
# predecessor line renders it and a fabricated reason would make the fixture
# teach something false.
FIXTURES_SPEC = [
    ("ck_history", "context-keeper", [
        ("dec-002", "dec-003",
         "The PreCompact capture prompt never reached the model -- that stream is "
         "user-visible only. Capture moved to a PostToolUse hook on git commit."),
        ("dec-005", "dec-019-9c32",
         "scope_guard fired on PostToolUse, so the rule arrived after the edit was "
         "already written -- a review note, not a guardrail. Moved to PreToolUse."),
        ("dec-012", "dec-013-4eb5",
         "The additive push/pull skipped any id that already existed, so an update "
         "or deprecation on one store never reached the other. Replaced by a "
         "timestamp-based newest-wins merge."),
    ]),
    ("xylem_history", "xylem", [
        ("dec-004-21df", "dec-006-723c",
         "The Workers had moved to path-token auth; the Authorization: Bearer "
         "header dec-004 added is not used and was removed from the manifest."),
        ("dec-011-7cbf", "dec-013-001d",
         "cambium-remote is now deployed and connected on claude.ai, so the "
         "discipline's 'cambium is local-only, unavailable on mobile' is no "
         "longer true."),
        ("dec-007-cc30", "dec-008-7319",
         "source.ref was left null by the fetch work, so a fresh install tracked "
         "each server's moving main mid-refactor. Now pinned to release tags."),
    ]),
]

STORE_FILES = ("decisions.json", "pipelines.json", "constraints.json")


def build(name, source_store, edges):
    src = os.path.join(REPOS, source_store, ".context")
    dst = os.path.join(FIXTURES, name, ".context")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)

    applied = []
    for filename in STORE_FILES:
        spath = os.path.join(src, filename)
        entries = []
        if os.path.exists(spath):
            with open(spath, "r", encoding="utf-8") as f:
                entries = json.load(f)
        by_id = {e.get("id"): e for e in entries}
        for older_id, newer_id, reason in edges:
            old = by_id.get(older_id)
            if old is None or newer_id not in by_id:
                continue
            old["status"] = "superseded"
            old["superseded_by"] = newer_id
            old["deprecated_reason"] = reason
            applied.append((older_id, newer_id))
        with open(os.path.join(dst, filename), "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=True)

    # A config naming the fixture, so nothing mistakes it for the live store.
    with open(os.path.join(dst, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "project_name": f"fixture-{name}",
            "_note": ("Real entries copied from the %s store; supersedes edges "
                      "added for the retrieval eval. Not live state." % source_store),
        }, f, indent=2)

    missing = [e[:2] for e in edges if tuple(e[:2]) not in applied]
    print("fixture %-16s from %-16s edges applied: %d %s"
          % (name, source_store, len(applied),
             ("MISSING: %s" % missing) if missing else ""))
    return len(applied)


def main():
    total = 0
    for name, store, edges in FIXTURES_SPEC:
        total += build(name, store, edges)
    print("total supersedes edges: %d" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
