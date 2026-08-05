#!/usr/bin/env python3
"""Context Keeper -- scoped-constraint injector for Edit/Write tool calls.

Session-start injection tells the model the project's rules once, at
turn one, when they are abstract. This hook is the enforcement half: when
the agent touches a file that a constraint's `scope` covers, the
constraint is injected right there via hookSpecificOutput.additionalContext
-- the rule arrives at the exact moment it is about to matter.

PREFER WIRING IT UNDER PreToolUse (matcher "Edit|Write|NotebookEdit").
PostToolUse also works and stays supported, but it fires *after* the write
has already landed, which makes the rule a review note rather than a
guardrail. PreToolUse additionalContext is injected next to the tool
result, so the model sees the constraint before it commits the edit. The
hook reads hook_event_name from the payload and answers with the matching
hookEventName, so one script serves either wiring -- and both at once,
should a config carry the old and new entries during an upgrade (the
once-per-session dedupe below means the rule still shows up only once).

Matching is on whole path COMPONENTS (see _scope_covers), not a substring:
a constraint scoped to "hooks/" fires for hooks/a.py but NOT for
webhooks/send.py. Constraints scoped "global" never fire here -- they are
session-start material.

Each constraint is injected at most once per session (state kept in
.context/scope_guard_state.json), so repeated edits to the same area do
not spam the context.

Opt-in escalation: with `scope_guard.confirm_absolute` set in
.context/config.json, a PreToolUse hit on an ABSOLUTE constraint also
returns permissionDecision "ask", so the edit pauses for the user instead
of merely being annotated. Default off -- it interrupts, and most projects
want the rule stated, not the edit halted. Note the honest limit: this
escalates on *scope*, not on violation. Nothing here reads the diff, so it
cannot know the edit actually breaks the rule.

Output is ASCII-only by deliberate constraint (con-001): Windows hook
stdout is cp1252 and non-ASCII chars raise UnicodeEncodeError. Per
con-002, additionalContext on Pre/PostToolUse is one of the few hook
surfaces the model actually sees.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# store_paths, NOT server. This hook runs before every Edit/Write, so its
# cost is paid on the critical path of each one; importing server would add
# ~73ms per edit for machinery this hook never touches (mirror's urllib
# stack, the semantic index, drift scanning). store_paths carries the
# resolution rules and the raw reads, and imports only json and os.
import store_paths
# The scope rule itself, shared with work_focus / score_entry / the rules
# projection so they cannot disagree (con-011-76f8). Imports nothing, so it
# costs this hot-path hook nothing beyond the module load.
import scope_rules

MAX_INJECT = 3

# Events this hook knows how to answer. Anything else (or a payload with no
# event at all, as older Claude Code builds sent) falls back to PostToolUse,
# which is where every existing installation has it wired.
_SUPPORTED_EVENTS = ("PreToolUse", "PostToolUse")
_DEFAULT_EVENT = "PostToolUse"


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


def _scope_covers(scope, path):
    """Does `scope` cover `path`? Delegates to scope_rules, the one implementation.

    A raw `scope in path` check was wrong in a way that only looked harmless
    while this hook ran after the edit: `src/` fired on `mysrc/main.py`,
    `hooks/` on `webhooks/send.py`, and `server.py` on `test_server.py`. On
    PreToolUse a false positive is worse than noise -- it states an unrelated
    rule right before an unrelated edit, and the once-per-session dedupe then
    BURNS that constraint, so the file it really governs never gets it.

    Kept as a named function because the tests and this module's own docs refer
    to it, but the rule itself lives in scope_rules.covers so the other surfaces
    (work_focus, score_entry, the rules projection) cannot drift from it --
    which they had (con-011-76f8).
    """
    return scope_rules.covers(scope, path)


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _confirm_absolute_enabled(context_dir):
    """Read the opt-in escalation flag; any problem reading it means off.

    Reads the config raw, with no defaults merged, which is safe here
    precisely because the default is False: a missing file, a missing key
    and an explicit false are all the same answer, so there is no default
    to drift away from.
    """
    return bool((store_paths.read_raw_config(context_dir).get("scope_guard")
                 or {}).get("confirm_absolute"))


def _save_state(path, state):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input -- never block the tool flow

    event = str(payload.get("hook_event_name") or "").strip()
    if event not in _SUPPORTED_EVENTS:
        event = _DEFAULT_EVENT

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return

    context_dir = store_paths.resolve_context_dir()
    if context_dir is None:
        return

    constraints = store_paths.read_json_file(
        store_paths.constraints_path(context_dir))
    if not constraints:
        return

    hits = []
    for c in constraints:
        if c.get("status", "active") == "deprecated":
            continue
        scope = (c.get("scope") or "global").strip()
        if not scope or scope.lower() == "global":
            continue
        if _scope_covers(scope, file_path):
            hits.append(c)
    if not hits:
        return

    # Once-per-session dedupe, keyed to the session id from the payload.
    session_id = str(payload.get("session_id") or "unknown")
    state_path = os.path.join(context_dir, "scope_guard_state.json")
    state = _load_state(state_path)
    if state.get("session_id") != session_id:
        state = {"session_id": session_id, "injected": []}
    injected = set(state.get("injected") or [])

    fresh = [c for c in hits if c.get("id") not in injected][:MAX_INJECT]
    if not fresh:
        return

    basename = os.path.basename(file_path)
    if event == "PreToolUse":
        opening = f"Scoped constraint(s) cover the file you are about to write ({basename}):"
        closing = ("Check your edit against these BEFORE writing it. Full entries "
                   "via get_context with the id.")
    else:
        opening = f"Scoped constraint(s) apply to the file you just edited ({basename}):"
        closing = ("Check your change against these before moving on. Full entries "
                   "via get_context with the id.")

    lines = [opening]
    for c in fresh:
        hardness = c.get("hardness", "absolute")
        lines.append(f"  [{c.get('id')}] ({hardness}) {c.get('rule', '?')}")
        reason = (c.get("reason") or "").strip()
        if reason:
            lines.append(f"      why: {reason}")
    lines.append(closing)

    state["injected"] = sorted(injected | {c.get("id") for c in fresh})
    _save_state(state_path, state)

    out = {
        "hookEventName": event,
        "additionalContext": _ascii("[Context Keeper] " + "\n".join(lines)),
    }

    # Opt-in: pause for the user when an absolute rule governs this path.
    # Only under PreToolUse -- asking about an edit that already happened
    # would be theatre.
    if event == "PreToolUse":
        absolutes = [c for c in fresh if c.get("hardness", "absolute") == "absolute"]
        if absolutes and _confirm_absolute_enabled(context_dir):
            ids = ", ".join(str(c.get("id")) for c in absolutes)
            out["permissionDecision"] = "ask"
            out["permissionDecisionReason"] = _ascii(
                f"{basename} is covered by absolute constraint(s) {ids}. "
                "Confirm the edit respects them."
            )

    print(json.dumps({"hookSpecificOutput": out}))


if __name__ == "__main__":
    main()
