#!/usr/bin/env python3
"""Context Keeper -- PostToolUse periodic constraint re-injection.

SessionStart injects the project's constraints once, at turn one. As a
long session fills with tool output, those rules scroll out of the
model's working attention and effectively decay -- the model can violate
a constraint it was briefed on an hour ago simply because it is buried.

This hook re-surfaces the constraints-ONLY block (not the full store)
part-way through a long session. It is deliberately event-driven, not a
timer: an MCP server has no wall-clock inside the context window, and
PreCompact stdout is not injected into the model (only user-visible), so
the honest mid-session surface is PostToolUse additionalContext -- which
IS injected (con-002) and whose firing rate tracks tool-output volume,
the thing actually burying the rules.

Cadence: it counts tool calls per session in .context/reinject_state.json
and re-injects once every `every_n_tools` calls. OPT-IN and default OFF:
gated on config constraint_reinjection.enabled, so a project that does
not want it sees no change from before this hook existed.

Wire it under PostToolUse (see README). Matching is unrestricted (matcher
"") so every tool call advances the counter. Output is ASCII-only by
deliberate constraint (con-001): Windows hook stdout is cp1252 and
non-ASCII chars raise UnicodeEncodeError, which would crash the hook.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


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

    try:
        import server
    except Exception:
        return  # never break the tool flow because memory could not load
    if server.CONTEXT_DIR is None or not os.path.exists(server.CONTEXT_DIR):
        return

    cfg = server.read_config(server.CONTEXT_DIR).get("constraint_reinjection") or {}
    if not cfg.get("enabled"):
        return  # opt-in, default off: unchanged behavior when disabled

    try:
        every = int(cfg.get("every_n_tools", 25))
    except (TypeError, ValueError):
        every = 25
    if every < 1:
        every = 1

    # Per-session tool counter. Reset when the session id changes so a new
    # session does not inherit a stale count from a previous one.
    session_id = str(payload.get("session_id") or "unknown")
    state_path = os.path.join(server.CONTEXT_DIR, "reinject_state.json")
    state = _load_state(state_path)
    if state.get("session_id") != session_id:
        state = {"session_id": session_id, "tool_count": 0, "last_injected_at": 0}

    state["tool_count"] = int(state.get("tool_count", 0)) + 1
    last = int(state.get("last_injected_at", 0))

    if state["tool_count"] - last < every:
        _save_state(state_path, state)  # persist the advanced counter
        return

    block = server.build_constraints_block(server.CONTEXT_DIR)
    if not block.get("initialized") or not block.get("text"):
        # No constraints to surface -- still advance the counter so we do
        # not rescan on literally every tool call once one is recorded.
        state["last_injected_at"] = state["tool_count"]
        _save_state(state_path, state)
        return

    state["last_injected_at"] = state["tool_count"]
    _save_state(state_path, state)

    text = (
        "Constraint refresh (re-surfaced mid-session -- these were injected "
        "at session start and may have scrolled out of context). Still "
        "authoritative for this project:\n" + block["text"]
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _ascii("[Context Keeper] " + text),
        }
    }))


if __name__ == "__main__":
    main()
