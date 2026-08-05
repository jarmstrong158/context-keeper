"""Shared fixtures, builders and probes for the split test suite.

tests/test_server.py was 5,213 lines and 78 classes in one file: everything
about the project lived in it, so any change touched it and nothing about its
organisation told you where a behaviour was tested. The classes moved out by
theme (quality, projections, hooks, packaging, transport) and everything they
share landed here.

`__all__` lists the underscore-prefixed probes deliberately -- `import *` would
skip them otherwise, and they are the point: _run_scope_guard, _run_cli and
friends are how the suite exercises the hooks and the CLI as real subprocesses
rather than as imported functions.
"""

"""Comprehensive test suite for context-keeper server.py.

Strategy: All tests pass ``project_dir`` explicitly in params so that the
module-level CONTEXT_DIR (resolved once at import time) is irrelevant.  This
lets us use ``tmp_path`` for full isolation without mocking any file I/O.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the handler functions directly from server.py
# ---------------------------------------------------------------------------
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import (
    CONTEXT_DIR_NAME,
    UNRESOLVED_PROJECT_ERROR,
    _base_dir_from_params,
    _classify_overlap,
    _resolve_project_dir,
    _text_words,
    build_constraints_block,
    handle_deprecate_entry,
    handle_get_compaction_report,
    handle_get_context,
    handle_get_project_summary,
    handle_prune_stale,
    handle_record_constraint,
    handle_record_decision,
    handle_record_entry,
    handle_query_entries,
    handle_record_pipeline,
    handle_reload_constraints,
    handle_update_entry,
    next_id,
    read_json_file,
    score_entry,
)


# ---------------------------------------------------------------------------
# Helper: build a params dict that targets a tmp project directory
# ---------------------------------------------------------------------------

# Sentinel strings that satisfy v0.4 min-length validation. Tests use
# these so they exercise real code paths instead of the validation
# rejection path. Use _DECISION_DEFAULTS / _CONSTRAINT_DEFAULTS /
# _PIPELINE_DEFAULTS to populate required fields without obscuring
# test intent at every call site.
_LONG_PROBLEM = (
    "Test scenario requires a problem description long enough to satisfy "
    "the v0.4 min-length validation enforced by the record handlers."
)
_LONG_WHY = (
    "Test scenario requires a why_chosen explanation long enough to clear "
    "the 60-character minimum that the v0.4 schema enforces server-side."
)
_LONG_REASON = (
    "Test scenario requires reasoning text long enough to satisfy the "
    "v0.4 minimum length requirement so the handler proceeds normally."
)
_LONG_PURPOSE = (
    "Test pipeline purpose long enough to clear the v0.4 minimum length "
    "requirement applied during pipeline registration."
)


def project_params(tmp_path: Path, extra: dict = None) -> dict:
    """Return a params dict with project_dir set to ``tmp_path``.

    v0.4 compat shim: when ``extra`` looks like a record_* call (has
    ``summary``, ``rule``, or ``name``+``steps``), auto-fill the v0.4
    required fields (``problem``/``why_chosen``/``reason``/``purpose``)
    if they aren't already set, so legacy test cases keep working
    without rewriting every call site.
    """
    params = {"project_dir": str(tmp_path)}
    if extra:
        params.update(extra)
    # decision shape — extend short summaries to clear min-length
    if "summary" in params:
        params.setdefault("problem", _LONG_PROBLEM)
        params.setdefault("why_chosen", _LONG_WHY)
        if isinstance(params["summary"], str) and len(params["summary"]) < 5:
            params["summary"] = params["summary"] + " (test entry)"
    # constraint shape — extend the existing 'reason' if it's too short
    if "rule" in params and "reason" in params:
        if isinstance(params["reason"], str) and len(params["reason"]) < 40:
            params["reason"] = params["reason"] + " " + _LONG_REASON
        if isinstance(params["rule"], str) and len(params["rule"]) < 5:
            params["rule"] = params["rule"] + " (test rule placeholder)"
    # pipeline shape
    if "name" in params and "steps" in params:
        params.setdefault("purpose", _LONG_PURPOSE)
        if isinstance(params["name"], str) and len(params["name"]) < 3:
            params["name"] = params["name"] + " (test pipeline)"
    return params


def decision_params(tmp_path: Path, **overrides) -> dict:
    """Build a valid v0.4 record_decision params dict. Overrides
    win, so a test can pass a longer summary or extra tags."""
    base = {
        "project_dir": str(tmp_path),
        "summary": "Test decision summary text",
        "problem": _LONG_PROBLEM,
        "why_chosen": _LONG_WHY,
    }
    base.update(overrides)
    return base


def constraint_params(tmp_path: Path, **overrides) -> dict:
    """Build a valid v0.4 record_constraint params dict."""
    base = {
        "project_dir": str(tmp_path),
        "rule": "Test rule that is long enough",
        "reason": _LONG_REASON,
    }
    base.update(overrides)
    return base


def pipeline_params(tmp_path: Path, **overrides) -> dict:
    """Build a valid v0.4 record_pipeline params dict."""
    base = {
        "project_dir": str(tmp_path),
        "name": "Test Pipeline",
        "purpose": _LONG_PURPOSE,
        "steps": [{"order": 1, "action": "do something"}],
    }
    base.update(overrides)
    return base


def context_dir(tmp_path: Path) -> Path:
    """Return the .context/ path inside tmp_path."""
    return tmp_path / CONTEXT_DIR_NAME


# ===========================================================================
# 1. Project resolution
# ===========================================================================




# ===========================================================================
# 9. prune_stale
# ===========================================================================


def _backdate_entry(file_path: Path, entry_id: str, days_ago: int):
    """Helper: set verified_at on an entry to ``days_ago`` days in the past."""
    data = json.loads(file_path.read_text(encoding="utf-8"))
    old_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    for e in data:
        if e["id"] == entry_id:
            e["verified_at"] = old_date
    file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ===========================================================================
# scope_guard hook (v0.6): edit-time injection of scoped constraints
# ===========================================================================

import subprocess

_HOOK = str(Path(__file__).parent.parent / "hooks" / "scope_guard.py")


def _run_scope_guard(project: Path, file_path: str, session_id: str = "s1",
                     event: str = None):
    payload = {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    }
    # Omitted entirely by default: that is the pre-upgrade payload shape, and
    # every existing installation has this hook wired under PostToolUse.
    if event is not None:
        payload["hook_event_name"] = event
    payload = json.dumps(payload)
    env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(project))
    proc = subprocess.run(
        [sys.executable, _HOOK], input=payload, capture_output=True,
        text=True, env=env, timeout=30,
    )
    return proc.stdout.strip()


# ===========================================================================
# DECISIONS.md projection (v0.8): render-on-write + export_markdown
# ===========================================================================

from server import handle_export_markdown


def _enable_md_export(tmp_path, path=None):
    ctx = context_dir(tmp_path)
    ctx.mkdir(exist_ok=True)
    cfg = {"markdown_export": {"enabled": True}}
    if path:
        cfg["markdown_export"]["path"] = path
    (ctx / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


# ===========================================================================
# Constraint re-injection (v0.11): mid-session rules refresh
# ===========================================================================

_REINJECT_HOOK = str(Path(__file__).parent.parent / "hooks" / "constraint_reinject.py")


def _write_config(project: Path, cfg: dict):
    ctx = project / CONTEXT_DIR_NAME
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _run_reinject(project: Path, session_id: str = "s1"):
    payload = json.dumps({"session_id": session_id, "tool_name": "Bash"})
    env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(project))
    proc = subprocess.run(
        [sys.executable, _REINJECT_HOOK], input=payload, capture_output=True,
        text=True, env=env, timeout=30,
    )
    return proc.stdout.strip()


# ===========================================================================
# CLI parity (Item 6): context-keeper <tool> '<json>' -> same HANDLERS
# ===========================================================================

_SERVER = str(Path(__file__).parent.parent / "server.py")


def _run_cli(project, *args, env_project=True):
    env = dict(os.environ)
    if env_project:
        env["CONTEXT_KEEPER_PROJECT"] = str(project)
    return subprocess.run(
        [sys.executable, _SERVER, *args], capture_output=True, text=True,
        env=env, timeout=30,
    )


# ===========================================================================
# Team-shared snapshot (Item 5): export / import / first-run bootstrap
# ===========================================================================

from server import (  # noqa: E402
    handle_export_snapshot,
    handle_import_snapshot,
    SNAPSHOT_DIR_NAME,
    SNAPSHOT_FILE_NAME,
)


# ===========================================================================
# UTF-8 stdio boundary: non-ASCII input must not be decoded as cp1252 mojibake
# ===========================================================================

_SERVER_PATH = str(Path(__file__).parent.parent / "server.py")


# ===========================================================================
# Transport: JSON-RPC batch handling (audit #1)
# ===========================================================================

from server import _handle_line, _dispatch_message  # noqa: E402


# ===========================================================================
# Distribution metadata: packaging, manifest, version (audit #4 and #6)
# ===========================================================================

_REPO_ROOT = Path(__file__).parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_MANIFEST = _REPO_ROOT / "mcpb" / "manifest.json"
_SERVER_JSON = _REPO_ROOT / "server.json"

# Top-level .py files that are deliberately NOT shipped to pip users.
# Keep this list tiny and justified -- it is the only escape hatch from the
# completeness check below, so anything added here needs a reason.
_NOT_SHIPPED = {
    "setup.py",      # (none today) build shim, not runtime
    "conftest.py",   # test-only
}


def _pyproject_section(name):
    """Return the raw text of one [section] of pyproject.toml.

    Deliberately a text scan rather than tomllib: tomllib is 3.11+, the
    project supports 3.10, and importorskip would let this check silently
    vanish on exactly the interpreter someone might be releasing from.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    start = text.index(f"[{name}]") + len(name) + 2
    rest = text[start:]
    nxt = rest.find("\n[")
    return rest if nxt == -1 else rest[:nxt]


def _shipped_python_modules():
    """Every Python module the installed package needs at runtime, derived
    from the repo rather than from a hand-maintained list."""
    mods = {p.name for p in _REPO_ROOT.glob("*.py")} - _NOT_SHIPPED
    mods |= {f"hooks/{p.name}" for p in (_REPO_ROOT / "hooks").glob("*.py")}
    return mods


# ===========================================================================
# .claude/rules/ projection: scoped constraints as path-triggered rules
#
# Claude Code loads a rule file with `paths:` frontmatter when it READS a
# matching file -- earlier than the scope_guard hook, which fires after the
# write. These tests pin the properties that make the projection safe to
# point at a directory inside the user's repo: it only ever writes its own
# generated files, and it only ever deletes its own.
# ===========================================================================

from server import (
    _RULES_MARKER,
    _rule_filenames,
    _scope_to_paths,
    handle_export_rules,
    render_scope_rule,
)


def _enable_rules_export(tmp_path, path=None):
    ctx = context_dir(tmp_path)
    ctx.mkdir(exist_ok=True)
    cfg = {"rules_export": {"enabled": True}}
    if path:
        cfg["rules_export"]["path"] = path
    (ctx / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _rules_dir(tmp_path):
    return tmp_path / ".claude" / "rules" / "context-keeper"


# ===========================================================================
# Edit-path latency: what a PreToolUse hook is allowed to cost
#
# PostToolUse ran after the tool, so its cost trailed the edit. PreToolUse
# runs BEFORE it, which puts every millisecond on the critical path of every
# Edit and Write in every project. Importing server cost ~73ms of machinery
# this hook never touches (mirror's urllib.request -> http.client ->
# email.parser stack, secrets, usage, code_drift) and took a hook run to
# ~142ms; going through store_paths instead put it at ~69ms against a ~62ms
# bare-interpreter floor.
#
# These tests defend the PROPERTY, not the measurement: a hook on the edit
# path must not import the heavy module. Timing assertions would be flaky on
# shared CI; "did this module get imported" is exact.
# ===========================================================================

_EDIT_PATH_HOOKS = ["scope_guard.py"]


def _modules_after_running_hook(hook_name, payload):
    """Run a hook in a fresh interpreter; report whether it imported server."""
    repo = str(Path(__file__).parent.parent)
    hook = str(Path(repo) / "hooks" / hook_name)
    probe = (
        "import sys, io, json, runpy\n"
        f"sys.path.insert(0, {repo!r})\n"
        f"sys.stdin = io.StringIO({payload!r})\n"
        "try:\n"
        f"    runpy.run_path({hook!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "sys.stderr.write('server' in sys.modules and 'HEAVY' or 'LIGHT')\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, timeout=60)
    return proc.stderr.strip()[-5:]


# ===========================================================================
# Scope matching: components, not substrings
#
# `scope in path` fired a constraint scoped to `src/` on `mysrc/main.py`, and
# one scoped to `server.py` on `test_server.py`. That was survivable while the
# hook ran AFTER the edit. On PreToolUse it states an unrelated rule right
# before an unrelated edit -- and the once-per-session dedupe then burns that
# constraint, so the file it actually governs never receives it.
# ===========================================================================

_SCOPE_CASES = [
    # (scope, path, covered?)
    ("src/", "C:/proj/src/main.py", True),
    ("src/", "C:/proj/mysrc/main.py", False),
    ("src/", "C:/proj/src2/main.py", False),
    ("hooks/", "C:/proj/hooks/a.py", True),
    ("hooks/", "C:/proj/webhooks/send.py", False),
    ("hooks/", "C:/proj/hooks/nested/deep.py", True),
    ("server.py", "C:/proj/server.py", True),
    ("server.py", "C:/proj/test_server.py", False),
    ("server.py", "C:/proj/pkg/server.py", True),
    ("tests/test_server.py", "C:/proj/tests/test_server.py", True),
    ("tests/test_server.py", "C:/proj/tests/test_server.py.bak", False),
    ("api/", "C:/proj/rapid/api2/x.py", False),
    # A directory scope must not match the directory entry itself, only
    # things inside it.
    ("hooks/", "C:/proj/hooks", False),
]


# ===========================================================================
# Findings from the v0.16 audit. Each of these shipped in the first pass and
# was found by probing the projection adversarially rather than by reading it.
# ===========================================================================

import shutil

# Aliased: this module already has a local _rules_dir(tmp_path) helper, and a
# bare import would silently rebind it for every test defined above.
from server import RulesPathOutsideProject
from server import _rules_dir as _server_rules_dir


# ===========================================================================
# Mojibake: the damage con-008-dc30 stopped causing but never repaired
#
# Forcing UTF-8 on the stdio transport meant no NEW entry gets corrupted. It
# did nothing for entries already written, and that damage is invisible in
# the worst way: the text stays legible enough that nobody re-reads it, so a
# corrupted rationale quietly degrades every retrieval that surfaces it.
# ===========================================================================

from server import (
    demojibake,
    handle_repair_mojibake,
    handle_verify_quality,
    looks_like_mojibake,
)

# What "the local store is canonical -- the exact operations" becomes when
# its UTF-8 bytes are decoded as cp1252.
_GARBLED = "the local store is canonical â€” the exact operations"
_CLEAN = "the local store is canonical — the exact operations"


# ===========================================================================
# Closing the delivery gaps measured after v0.16
#
# Measuring real stores turned up two problems that compound: the largest
# store dropped 116 lines at session start with no trace, and 47% of all
# constraints were `global`-scoped, meaning their ONLY delivery route was
# that same truncating summary. Everything path-based built in v0.16 routed
# around half the rules.
# ===========================================================================

from server import _scope_to_paths, estimate_tokens

_SUBAGENT_HOOK = str(Path(__file__).parent.parent / "hooks" / "subagent_start.py")

__all__ = [
    "CONTEXT_DIR_NAME",
    "Path",
    "RulesPathOutsideProject",
    "SNAPSHOT_DIR_NAME",
    "SNAPSHOT_FILE_NAME",
    "UNRESOLVED_PROJECT_ERROR",
    "_CLEAN",
    "_EDIT_PATH_HOOKS",
    "_GARBLED",
    "_HOOK",
    "_LONG_PROBLEM",
    "_LONG_PURPOSE",
    "_LONG_REASON",
    "_LONG_WHY",
    "_MANIFEST",
    "_NOT_SHIPPED",
    "_PYPROJECT",
    "_REINJECT_HOOK",
    "_REPO_ROOT",
    "_RULES_MARKER",
    "_SCOPE_CASES",
    "_SERVER",
    "_SERVER_JSON",
    "_SERVER_PATH",
    "_SUBAGENT_HOOK",
    "_backdate_entry",
    "_base_dir_from_params",
    "_classify_overlap",
    "_dispatch_message",
    "_enable_md_export",
    "_enable_rules_export",
    "_handle_line",
    "_modules_after_running_hook",
    "_pyproject_section",
    "_resolve_project_dir",
    "_rule_filenames",
    "_rules_dir",
    "_run_cli",
    "_run_reinject",
    "_run_scope_guard",
    "_scope_to_paths",
    "_server_rules_dir",
    "_shipped_python_modules",
    "_text_words",
    "_write_config",
    "build_constraints_block",
    "constraint_params",
    "context_dir",
    "datetime",
    "decision_params",
    "demojibake",
    "estimate_tokens",
    "handle_deprecate_entry",
    "handle_export_markdown",
    "handle_export_rules",
    "handle_export_snapshot",
    "handle_get_compaction_report",
    "handle_get_context",
    "handle_get_project_summary",
    "handle_import_snapshot",
    "handle_prune_stale",
    "handle_query_entries",
    "handle_record_constraint",
    "handle_record_decision",
    "handle_record_entry",
    "handle_record_pipeline",
    "handle_reload_constraints",
    "handle_repair_mojibake",
    "handle_update_entry",
    "handle_verify_quality",
    "json",
    "looks_like_mojibake",
    "next_id",
    "os",
    "pipeline_params",
    "project_params",
    "pytest",
    "re",
    "read_json_file",
    "render_scope_rule",
    "score_entry",
    "shutil",
    "subprocess",
    "sys",
    "timedelta",
    "timezone",
]
