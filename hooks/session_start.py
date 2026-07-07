#!/usr/bin/env python3
"""Context Keeper -- SessionStart hook.

Earlier versions printed an *instruction* telling the agent to call
get_compaction_report and get_project_summary. That lever was skippable:
the tools are deferred, and a task-focused first turn reliably wins over a
soft reminder, so retrieval silently never happened.

This hook removes the tool call from the loop entirely. It imports the
server's own handlers and prints the actual compaction report + project
summary text to stdout, which Claude Code injects directly into session
context. The memory is now in front of the agent at turn one with nothing
to opt into.

Importing server.py is side-effect-free: main() (the stdio loop) only runs
under __main__, and module import just resolves the project dir + sets
constants -- the same resolution the MCP tools use, so this emits identical
content to calling the tools by hand.

Output is ASCII-only by deliberate constraint (con-001): Windows hook
stdout is cp1252 and non-ASCII chars raise UnicodeEncodeError, which would
crash the hook and silently lose memory injection.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _ascii(text):
    """Hard guarantee against con-001 violations from recorded content
    (entry text the user wrote may contain Unicode we did not author)."""
    return str(text).encode("ascii", "replace").decode("ascii")


def _fmt_compaction(report):
    if not report or not report.get("has_report"):
        return None
    if report.get("status") != "discrepancies_found":
        return None  # clean compaction -- nothing the agent must act on
    lines = ["COMPACTION DISCREPANCIES DETECTED -- surface these to the "
             "user before making changes:"]
    for key in ("missing_entries", "modified_entries"):
        items = report.get(key) or []
        if items:
            lines.append(f"  {key.replace('_', ' ')}: {len(items)}")
            for it in items[:10]:
                eid = it.get("id") if isinstance(it, dict) else it
                lines.append(f"    - {eid}")
    if report.get("action"):
        lines.append(f"  {report['action']}")
    return "\n".join(lines)


def _fmt_summary(summary):
    if not summary or not summary.get("initialized"):
        return None
    out = [summary.get("summary", "").strip()]
    stale = summary.get("stale_entries")
    if stale:
        ids = ", ".join(str(s.get("id")) for s in stale[:10])
        out.append(f"\nStale (unverified > threshold): {ids}\n"
                   f"Consider prune_stale / confirming these are still "
                   f"accurate.")
    return "\n".join(p for p in out if p)


def main():
    try:
        import server
    except Exception:
        # Never break session start because memory could not load.
        return

    # Generate the compaction report NOW if a fresh snapshot is pending.
    # SessionStart(source=compact) fires immediately after compaction --
    # before any Stop hook -- so without this the report we inject below
    # could be one compaction stale. generate_report() is idempotent
    # (keyed to the snapshot timestamp), so this is a no-op when the
    # Stop hook already compared this snapshot.
    try:
        import post_compact
        post_compact.generate_report()
    except Exception:
        pass

    try:
        report = server.handle_get_compaction_report({})
    except Exception:
        report = None
    try:
        summary = server.handle_get_project_summary({})
    except Exception:
        summary = None
    try:
        quality = server.handle_verify_quality({})
    except Exception:
        quality = None

    blocks = []
    c = _fmt_compaction(report)
    if c:
        blocks.append(c)
    s = _fmt_summary(summary)
    if s:
        blocks.append("PROJECT MEMORY (recorded decisions, constraints, "
                       "pipelines -- treat as authoritative for this "
                       "project):\n" + s)

    if not blocks:
        # No project memory yet -- stay silent rather than nag every session.
        return

    # Static capture guidance goes ahead of the volatile quality nudge below.
    # Ordering is deliberate for prompt-cache friendliness: the memory block
    # and this fixed guidance form a stable prefix that repeats verbatim across
    # sessions (unchanged store -> byte-identical text -> cacheable), while the
    # one line that changes session to session -- the quality scan's flagged
    # IDs -- is emitted last so it never shifts the stable prefix behind it.
    blocks.append(
        "Capture as you go: when a decision, constraint, or pipeline is "
        "established this session, record it immediately with the "
        "record_* tools. Do not wait for compaction."
    )

    # Quality nudge: PreCompact stdout is not injected into model context,
    # so SessionStart is the model-visible surface for the scan. One line
    # only -- the full detail is a verify_quality call away. Kept last: its
    # flagged IDs are the most volatile content in the block.
    if quality and quality.get("count"):
        ids = ", ".join(str(f.get("id")) for f in quality.get("flagged", [])[:5])
        blocks.append(
            f"Quality scan: {quality['count']} entries look thin, untagged, "
            f"or unlinked (e.g. {ids}). Call verify_quality for details and "
            f"enrich via update_entry when convenient."
        )

    print(_ascii("[Context Keeper] " + "\n\n".join(blocks)))


if __name__ == "__main__":
    main()
