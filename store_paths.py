#!/usr/bin/env python3
"""Locating and reading the .context/ store -- the cheap half of server.py.

This module exists for LATENCY. The hooks need three things: resolve the
project directory, read an entry file, read the config. server.py can do all
three, but importing it costs ~73ms (mirror pulls in urllib.request ->
http.client -> email.parser; plus secrets, usage, code_drift), and a hook
that runs on PreToolUse pays that on the critical path of every single edit.
This module imports json and os and nothing else.

It exists as a MODULE rather than as copied code in each hook because
resolution order is a correctness rule, not a convenience: env var, then the
Xylem session pointer, then cwd, then the parent walk -- and steps 3 and 4
only ever resolve to a directory that ALREADY contains .context/, which is
what keeps context-keeper from silently creating a store in the wrong place.
Two copies of that logic would drift, and the copy that drifts is the one
nothing executes.

server.py imports these names rather than defining its own, so there is
exactly one implementation.
"""

import json
import os

CONTEXT_DIR_NAME = ".context"


def _xylem_active_project_file():
    """Path to the shared Xylem session pointer (overridable for tests)."""
    override = os.environ.get("XYLEM_ACTIVE_PROJECT_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".xylem", "active_project.json")


def _xylem_session_project():
    """The session's project path from the shared Xylem pointer the SessionStart
    hook writes, or None. Never raises — a missing/garbage pointer is just 'no
    session hint'."""
    try:
        with open(_xylem_active_project_file(), encoding="utf-8") as f:
            proj = json.load(f).get("project")
    except (OSError, ValueError, AttributeError):
        return None
    return proj if isinstance(proj, str) and os.path.isdir(proj) else None


def _resolve_project_dir():
    """Resolve the project directory with a safe cwd fallback.

    Order of precedence:
      1. CONTEXT_KEEPER_PROJECT env var, if set (trusted — user opted in)
      2. The Xylem session pointer (~/.xylem/active_project.json, override
         with XYLEM_ACTIVE_PROJECT_FILE) written by the SessionStart hook.
         Also an explicit opt-in — it exists so a persistent server follows
         the session's project instead of the dir it was launched from — so
         it outranks the cwd-based discovery below.
      3. cwd, ONLY if it already contains a .context/ directory
      4. Walk parent dirs from cwd, returning the first ancestor that
         already contains a .context/ directory (git-style discovery)
      5. None — refuse to default, callers must pass project_dir explicitly

    Steps 3 and 4 only resolve to directories that ALREADY contain
    .context/. We never create one implicitly, so the footgun where
    Claude Code is launched from a parent directory and context-keeper
    silently pollutes it stays fixed. The upward walk just lets the
    server find your project when launched from a subdirectory of it.
    """
    explicit = os.environ.get("CONTEXT_KEEPER_PROJECT")
    if explicit:
        return explicit
    # The Xylem SessionStart hook records which project this session is in; honor
    # it like CONTEXT_KEEPER_PROJECT (an explicit opt-in) so a persistent server
    # follows the session's project instead of the dir it was launched from.
    session = _xylem_session_project()
    if session:
        return session
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, CONTEXT_DIR_NAME)):
        return cwd
    # Walk up the parent chain looking for an existing .context/ dir.
    # Stops at the filesystem root (parent == current). Bounded iteration
    # for safety in case a pathological FS confuses os.path.dirname.
    current = cwd
    for _ in range(64):
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        if os.path.isdir(os.path.join(parent, CONTEXT_DIR_NAME)):
            return parent
        current = parent
    return None


def _read_json_file_checked(path):
    """Read a JSON list. Returns (entries, error_string_or_None).

    A missing file is not an error — it is an empty store. A file that
    exists but cannot be parsed IS an error, and callers on a write path
    must refuse to proceed rather than treat it as empty.
    """
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [], f"unparseable JSON ({e})"
    if not isinstance(data, list):
        return [], "top-level value is not a JSON list"
    return data, None


def read_json_file(path):
    """Soft read for retrieval paths: missing or corrupt both yield [].

    Write paths must use server._load_entries_for_write instead — silently
    treating a corrupt store as empty turns the next append into a
    full-history wipe.
    """
    entries, _err = _read_json_file_checked(path)
    return entries


def read_raw_config(context_dir):
    """The config file as written, with NO defaults merged in. {} on any problem.

    Callers that need a defaulted value use server.read_config. This raw
    read is for hooks asking about a flag whose default is falsy — a
    missing file, a missing key and a false value all mean the same thing,
    so there is no default to drift away from.
    """
    if not context_dir:
        return {}
    try:
        with open(os.path.join(context_dir, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def constraints_path(context_dir):
    """Path to the constraints store inside a resolved .context/ dir."""
    return os.path.join(context_dir, "constraints.json") if context_dir else None


def resolve_context_dir():
    """The resolved .context/ directory for this process, or None."""
    project = _resolve_project_dir()
    return os.path.join(project, CONTEXT_DIR_NAME) if project else None
