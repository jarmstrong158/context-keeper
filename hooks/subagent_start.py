#!/usr/bin/env python3
"""Context Keeper -- SubagentStart hook: project rules for spawned subagents.

SessionStart does not fire for subagents, and a subagent does not inherit
the parent conversation's injected context. So every subagent has been
starting with NO project memory: it cannot see the constraints, it was
never told they exist, and it will happily do the thing the project
recorded a rule against. On a workflow that fans out a dozen agents, that
is a dozen contributors who never read the rules.

This hook closes that. It injects the constraints-only block -- the same
Absolute/Advisory lines SessionStart surfaces -- via
hookSpecificOutput.additionalContext, which SubagentStart supports.

CONSTRAINTS ONLY, deliberately. This fires once per spawned subagent, so a
fan-out pays it N times; the full project summary would be the wrong thing
to multiply. Rules are what a focused subagent cannot afford to be missing
and cannot easily infer, and everything else is one get_project_summary or
get_context call away -- which the closing line tells it about.

Wire it under SubagentStart. A matcher filters on agent type; omit it (or
use "") to brief every subagent, which is the sane default -- an agent
type you forgot to list is exactly the one that will break a rule.

Imports store_paths, never server: this is a spawn-path hook and a fan-out
multiplies its cost (con-010). Output is ASCII-only per con-001.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import store_paths

# A subagent's context is a scarce, single-purpose resource. Past this many
# constraints, injecting all of them stops being a briefing and starts being
# a wall of text the agent skims; the pointer to get_context does better.
MAX_CONSTRAINTS = 12


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input -- never interfere with the spawn

    context_dir = store_paths.resolve_context_dir()
    if context_dir is None:
        return

    block = store_paths.build_constraints_block(context_dir)
    if not block.get("initialized") or not block.get("count"):
        return  # no rules recorded -- stay silent rather than emit a header

    constraints = [c for c in store_paths.read_json_file(
        store_paths.constraints_path(context_dir))
        if c.get("status", "active") == "active"]

    total = len(constraints)
    if total > MAX_CONSTRAINTS:
        # Absolute first: if we must drop some, drop the advisory ones.
        absolute = [c for c in constraints if c.get("hardness") == "absolute"]
        advisory = [c for c in constraints if c.get("hardness") != "absolute"]
        shown = (absolute + advisory)[:MAX_CONSTRAINTS]
        text = "\n".join(store_paths.constraint_lines(shown))
        omitted = total - len(shown)
    else:
        text = block["text"]
        omitted = 0

    lines = [
        "PROJECT RULES (recorded by this project's team -- treat as "
        "authoritative). You are a subagent and did not receive the session's "
        "project memory, so these are stated here:",
        "",
        text,
    ]
    if omitted:
        lines.append(f"\n({omitted} further constraint(s) not shown -- "
                     "call reload_constraints for the full set.)")
    lines.append(
        "\nBefore any architectural change, call get_context with what you are "
        "about to touch; call get_project_summary for the decisions and "
        "pipelines behind these rules."
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": _ascii("[Context Keeper] " + "\n".join(lines)),
        }
    }))


if __name__ == "__main__":
    main()
