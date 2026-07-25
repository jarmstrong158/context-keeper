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
contains "git commit" it reminds the agent to record the matching entry
NOW, in the same work cycle, instead of batching it.

Two things it deliberately does NOT do:

  It does not fire on every commit. "fix typo" and "wip" carry no
  rationale, and a hook that interrupts identically for those as for a
  real design change trains the reader to skip it -- at which point the
  reminder that matters is skipped too. Silence on trivial commits is
  what buys attention for the rest.

  It does not record anything itself. Auto-recording would fill the store
  with restated commit messages, and an entry nobody chose is an entry
  nobody trusts. The hook proposes; the agent decides.

What it adds instead is specificity: it reads the commit message, and
when the message carries rationale shape ("instead of", "turns out",
"never", "root cause") it names the phrase it saw and the entry kind that
phrase implies, so the agent is answering a concrete question rather than
a generic nag.

Wire it under PostToolUse with matcher "Bash" (see README). Output is
ASCII-only by deliberate constraint (con-001): Windows hook stdout is
cp1252 and non-ASCII chars raise UnicodeEncodeError, which would crash
the hook.
"""

import json
import re
import sys

# Phrases that mean a choice was made and an alternative was passed over.
# A commit saying "X instead of Y" is a decision with its alternatives
# already written down -- the most valuable and most often lost entry.
DECISION_MARKERS = (
    "instead of", "rather than", "in favour of", "in favor of", "chose ",
    "we now", "switched to", "moved to", "rejected", "decided",
    "over the", "replaces", "supersedes",
)

# Phrases that mean a rule was established -- something that must or must
# not happen from now on. These become constraints, not decisions.
CONSTRAINT_MARKERS = (
    "never ", "always ", "must ", "must not", "do not ", "don't ",
    "should not", "required", "enforce", "guard against", "prevent",
)

# Phrases that mean something was learned the hard way. The commit is the
# only place the cause is written down, and it is exactly what a future
# session needs and cannot reconstruct from the diff.
GOTCHA_MARKERS = (
    "turns out", "root cause", "was silently", "silently", "actually",
    "the real", "caused by", "because it", "only happens", "surprisingly",
    "does not actually", "never fired", "no longer",
)

KINDS = (
    ("decision", DECISION_MARKERS,
     "record_entry(kind='decision') -- capture what was chosen, what was "
     "passed over, and why"),
    ("constraint", CONSTRAINT_MARKERS,
     "record_entry(kind='constraint') -- capture the rule and the incident "
     "that motivated it"),
    ("constraint", GOTCHA_MARKERS,
     "record_entry(kind='constraint') -- a gotcha is a constraint with a "
     "triggering_incident; the cause is in this commit and nowhere else"),
)

# Messages this short are almost never carrying rationale.
MIN_INTERESTING_CHARS = 24

TRIVIAL = re.compile(
    r"^\s*(wip|typo|fix typo|formatting|lint|whitespace|bump|merge branch|"
    r"revert|chore\(deps\)|update readme)\b", re.I)


def extract_message(command):
    """Best-effort commit message from a shell command string.

    Handles -m "..." / -m '...' and heredoc bodies. Returns "" when the
    message is not inline (e.g. -F file, or an editor commit), in which
    case the hook falls back to the generic reminder rather than guessing.
    """
    m = re.search(r"-m\s+(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", command, re.S)
    if m:
        return m.group(1)[1:-1]
    # git commit -F - <<'MSG' ... MSG
    m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\1", command, re.S)
    if m:
        return m.group(2)
    return ""


def classify(message):
    """(kind, matched_phrase, guidance) for the first marker found, else None."""
    low = message.lower()
    for kind, markers, guidance in KINDS:
        for marker in markers:
            if marker in low:
                return kind, marker.strip(), guidance
    return None


def build_context(command):
    """The additionalContext string, or None to stay silent."""
    message = extract_message(command)

    if not message:
        # Could not read the message -- do not guess at its content, but a
        # commit still happened, so the generic reminder still applies.
        return ("Commit detected. If it established a decision, constraint, "
                "gotcha, or pipeline change, record it in context keeper "
                "(record_* / update_entry) NOW, in this same work cycle.")

    subject = message.strip().splitlines()[0] if message.strip() else ""
    if TRIVIAL.match(subject) or len(message.strip()) < MIN_INTERESTING_CHARS:
        return None  # nothing to capture; staying quiet is what keeps the rest read

    hit = classify(message)
    if not hit:
        return None

    kind, phrase, guidance = hit
    return (
        "Commit detected, and its message reads like a recordable {kind}: it "
        "says \"{phrase}\". The reasoning is in the commit and nowhere the next "
        "session will look. Record it NOW, in this same work cycle -- {guidance}. "
        "Subject: \"{subject}\""
    ).format(kind=kind, phrase=phrase, guidance=guidance, subject=subject[:120])


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input -- never block the tool flow

    command = (payload.get("tool_input") or {}).get("command") or ""
    if "git commit" not in command:
        return

    try:
        context = build_context(command)
    except Exception:
        return  # a reminder is never worth failing a tool call over

    if not context:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
