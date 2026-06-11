#!/usr/bin/env python3
"""Context Keeper -- PostToolUse(Bash) commit capture reminder.

The capture half of the loop has a known weakness: recording depends on
the agent calling record_* mid-session, and during incident-heavy work
(diagnose -> fix -> deploy -> verify, repeat) the sync gets mentally
batched "for later" -- which never comes. In field use, a user had to say
"update context keeper" three times in one night while the agent shipped
a dozen commits.

A git commit is the single best capture trigger: it is the exact moment a
decision, constraint, or gotcha became real enough to persist in version
control. This hook fires after every Bash tool call; when the command
contains "git commit" it injects a reminder into the agent's context to
record the matching entry NOW, in the same work cycle, instead of
batching it.

Wire it under PostToolUse with matcher "Bash" (see README). Output is
ASCII-only by deliberate constraint (con-001): Windows hook stdout is
cp1252 and non-ASCII chars raise UnicodeEncodeError, which would crash
the hook.
"""

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input -- never block the tool flow

    command = (payload.get("tool_input") or {}).get("command") or ""
    if "git commit" not in command:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "Commit detected. If this commit established a decision, "
                "constraint, gotcha, or pipeline change, record it in "
                "context keeper (record_* / update_entry) NOW, in this "
                "same work cycle - do not batch capture for later."
            ),
        }
    }))


if __name__ == "__main__":
    main()
