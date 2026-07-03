#!/usr/bin/env python3
"""Context Keeper -- PostToolUse(Edit|Write) scoped-constraint injector.

Session-start injection tells the model the project's rules once, at
turn one, when they are abstract. This hook is the enforcement half: the
moment the agent edits a file that a constraint's `scope` covers, the
constraint is injected right there via hookSpecificOutput.additionalContext
-- the rule arrives at the exact moment it is about to matter.

Wire it under PostToolUse with matcher "Edit|Write|NotebookEdit" (see
README). Matching is a normalized substring check: a constraint scoped
to "hooks/" fires for any edit whose path contains hooks/. Constraints
scoped "global" never fire here -- they are session-start material.

Each constraint is injected at most once per session (state kept in
.context/scope_guard_state.json), so repeated edits to the same area do
not spam the context.

Output is ASCII-only by deliberate constraint (con-001): Windows hook
stdout is cp1252 and non-ASCII chars raise UnicodeEncodeError. Per
con-002, PostToolUse additionalContext is one of the few hook surfaces
the model actually sees.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

MAX_INJECT = 3


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


def _norm(path):
    return str(path).replace("\\", "/").lower()


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return

    try:
        import server
    except Exception:
        return
    if server.CONSTRAINTS_PATH is None:
        return

    constraints = server.read_json_file(server.CONSTRAINTS_PATH)
    path_norm = _norm(file_path)
    hits = []
    for c in constraints:
        if c.get("status", "active") == "deprecated":
            continue
        scope = (c.get("scope") or "global").strip()
        if not scope or scope.lower() == "global":
            continue
        if _norm(scope) in path_norm:
            hits.append(c)
    if not hits:
        return

    # Once-per-session dedupe, keyed to the session id from the payload.
    session_id = str(payload.get("session_id") or "unknown")
    state_path = os.path.join(server.CONTEXT_DIR, "scope_guard_state.json")
    state = _load_state(state_path)
    if state.get("session_id") != session_id:
        state = {"session_id": session_id, "injected": []}
    injected = set(state.get("injected") or [])

    fresh = [c for c in hits if c.get("id") not in injected][:MAX_INJECT]
    if not fresh:
        return

    lines = [
        f"Scoped constraint(s) apply to the file you just edited ({os.path.basename(file_path)}):"
    ]
    for c in fresh:
        hardness = c.get("hardness", "absolute")
        lines.append(f"  [{c.get('id')}] ({hardness}) {c.get('rule', '?')}")
        reason = (c.get("reason") or "").strip()
        if reason:
            lines.append(f"      why: {reason}")
    lines.append(
        "Check your change against these before moving on. Full entries via "
        "get_context with the id."
    )

    state["injected"] = sorted(injected | {c.get("id") for c in fresh})
    _save_state(state_path, state)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _ascii("[Context Keeper] " + "\n".join(lines)),
        }
    }))


if __name__ == "__main__":
    main()
