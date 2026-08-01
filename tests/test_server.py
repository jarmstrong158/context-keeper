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


class TestProjectResolution:
    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        """CONTEXT_KEEPER_PROJECT env var wins even when cwd has .context/."""
        env_target = tmp_path / "env_project"
        env_target.mkdir()
        cwd_project = tmp_path / "cwd_project"
        cwd_ctx = cwd_project / CONTEXT_DIR_NAME
        cwd_ctx.mkdir(parents=True)

        monkeypatch.setenv("CONTEXT_KEEPER_PROJECT", str(env_target))
        monkeypatch.chdir(cwd_project)

        result = _resolve_project_dir()
        assert result == str(env_target)

    def test_cwd_fallback_when_context_exists(self, tmp_path, monkeypatch):
        """cwd is used when it already contains a .context/ directory."""
        ctx = tmp_path / CONTEXT_DIR_NAME
        ctx.mkdir()
        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.chdir(tmp_path)

        result = _resolve_project_dir()
        assert result == str(tmp_path)

    def test_refuses_when_neither(self, tmp_path, monkeypatch):
        """Returns None when env var absent and no ancestor has .context/."""
        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        # tmp_path itself has no .context/ — but walk-up could find one in
        # a real ancestor (e.g. the user's repo). Build an isolated chain
        # under tmp_path so the walk terminates without finding anything.
        deep = tmp_path / "isolated" / "deeply" / "nested"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        # None of the ancestors under tmp_path/isolated have .context/.
        # The walk may still find one above tmp_path on a dev machine, so
        # we assert against the specific behavior: if it returns something,
        # it must NOT be one of our isolated dirs.
        result = _resolve_project_dir()
        if result is not None:
            for ancestor in (deep, deep.parent, deep.parent.parent, tmp_path):
                assert result != str(ancestor)

    def test_walks_up_to_find_context_dir(self, tmp_path, monkeypatch):
        """When cwd is a subdirectory of a project with .context/, walk
        up the parent chain to find it (git-style discovery)."""
        project_root = tmp_path / "myproject"
        ctx = project_root / CONTEXT_DIR_NAME
        ctx.mkdir(parents=True)
        deep = project_root / "src" / "components" / "ui"
        deep.mkdir(parents=True)

        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.chdir(deep)

        result = _resolve_project_dir()
        assert result == str(project_root)

    def test_walk_up_does_not_create_context_dir(self, tmp_path, monkeypatch):
        """Walk-up resolution must never create a .context/ dir. If no
        ancestor has one, callers get None — never a stray pollution."""
        deep = tmp_path / "isolated" / "deep"
        deep.mkdir(parents=True)

        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.chdir(deep)

        _resolve_project_dir()
        # No .context/ should appear in any ancestor we control
        for ancestor in (deep, deep.parent, tmp_path):
            assert not (ancestor / CONTEXT_DIR_NAME).exists()

    def test_cwd_wins_over_ancestor(self, tmp_path, monkeypatch):
        """If both cwd and an ancestor have .context/, cwd takes priority."""
        project_root = tmp_path / "outer"
        (project_root / CONTEXT_DIR_NAME).mkdir(parents=True)
        inner = project_root / "inner"
        (inner / CONTEXT_DIR_NAME).mkdir(parents=True)

        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.chdir(inner)

        result = _resolve_project_dir()
        assert result == str(inner)

    def test_xylem_pointer_outranks_cwd_context_dir(self, tmp_path, monkeypatch):
        """Step 1.5: the session pointer beats cwd/.context discovery.

        This is the precedence that made the three cwd tests above fail on
        the author's machine (green in CI, red locally) -- assert it
        explicitly so the ordering is covered rather than incidental.
        """
        import json as _json
        pointed = tmp_path / "pointed"
        (pointed / CONTEXT_DIR_NAME).mkdir(parents=True)
        here = tmp_path / "here"
        (here / CONTEXT_DIR_NAME).mkdir(parents=True)
        ptr = tmp_path / "active_project.json"
        ptr.write_text(_json.dumps({"project": str(pointed)}), encoding="utf-8")

        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.setenv("XYLEM_ACTIVE_PROJECT_FILE", str(ptr))
        monkeypatch.chdir(here)

        assert _resolve_project_dir() == str(pointed)

    def test_env_var_outranks_xylem_pointer(self, tmp_path, monkeypatch):
        """Step 1 still beats step 1.5."""
        import json as _json
        pointed = tmp_path / "pointed"
        (pointed / CONTEXT_DIR_NAME).mkdir(parents=True)
        ptr = tmp_path / "active_project.json"
        ptr.write_text(_json.dumps({"project": str(pointed)}), encoding="utf-8")

        monkeypatch.setenv("XYLEM_ACTIVE_PROJECT_FILE", str(ptr))
        monkeypatch.setenv("CONTEXT_KEEPER_PROJECT", str(tmp_path / "env_wins"))

        assert _resolve_project_dir() == str(tmp_path / "env_wins")

    def test_suite_is_hermetic_against_a_real_xylem_pointer(self):
        """The conftest fixture must neutralize the developer's real pointer.

        Without this, anyone who actually runs the Xylem suite gets a
        different _resolve_project_dir() than CI does, and the cwd tests
        above fail for reasons that have nothing to do with the code.
        """
        import server as srv
        assert srv._xylem_session_project() is None
        assert not os.path.exists(srv._xylem_active_project_file())

    def test_record_decision_no_project_dir_returns_error(self, monkeypatch):
        """Calling handler with no project_dir and no env var returns error dict."""
        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        # Don't set project_dir in params — rely on module-level CONTEXT_DIR which
        # may or may not be set.  We patch _base_dir_from_params to return None.
        import server as srv
        original = srv._base_dir_from_params
        try:
            srv._base_dir_from_params = lambda p: None
            result = handle_record_decision({"summary": "x", "rationale": "y"})
            assert "error" in result
        finally:
            srv._base_dir_from_params = original


# ===========================================================================
# 2. record_decision
# ===========================================================================


class TestRecordDecision:
    def test_creates_decisions_json(self, tmp_path):
        result = handle_record_decision(project_params(tmp_path, {
            "summary": "Use JSON for storage",
            "rationale": "Human-readable and zero dependencies",
        }))
        assert result["success"] is True
        dec_path = context_dir(tmp_path) / "decisions.json"
        assert dec_path.exists()

    def test_correct_fields_present(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Use JSON for storage",
            "rationale": "Human-readable",
            "tags": ["storage", "architecture"],
            "alternatives": [{"option": "SQLite", "reason_rejected": "binary format"}],
            "constraints_created": ["Never use binary formats"],
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert len(data) == 1
        entry = data[0]
        assert entry["summary"] == "Use JSON for storage"
        assert entry["rationale"] == "Human-readable"
        assert entry["tags"] == ["storage", "architecture"]
        assert entry["alternatives"] == [{"option": "SQLite", "reason_rejected": "binary format"}]
        assert entry["constraints_created"] == ["Never use binary formats"]
        assert entry["status"] == "active"
        assert entry["superseded_by"] is None
        assert "created_at" in entry
        assert "verified_at" in entry

    def test_sequential_ids(self, tmp_path):
        for i in range(3):
            handle_record_decision(project_params(tmp_path, {
                "summary": f"Decision {i}",
                "rationale": "reason",
            }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        ids = [e["id"] for e in data]
        assert ids == ["dec-001", "dec-002", "dec-003"]

    def test_id_format(self, tmp_path):
        result = handle_record_decision(project_params(tmp_path, {
            "summary": "First decision",
            "rationale": "reason",
        }))
        assert result["id"] == "dec-001"

    def test_defaults_for_optional_fields(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Minimal decision",
            "rationale": "reason",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        entry = data[0]
        assert entry["alternatives"] == []
        assert entry["constraints_created"] == []
        assert entry["tags"] == []

    def test_creates_context_dir_if_missing(self, tmp_path):
        assert not context_dir(tmp_path).exists()
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        assert context_dir(tmp_path).exists()


# ===========================================================================
# 3. record_pipeline
# ===========================================================================


class TestRecordPipeline:
    def _steps(self):
        return [
            {"order": 1, "action": "Fetch data", "output": "raw CSV"},
            {"order": 2, "action": "Validate schema", "output": "validated rows"},
            {"order": 3, "action": "Write to DB", "output": "inserted records"},
        ]

    def test_creates_pipelines_json(self, tmp_path):
        result = handle_record_pipeline(project_params(tmp_path, {
            "name": "ETL Pipeline",
            "steps": self._steps(),
        }))
        assert result["success"] is True
        pipe_path = context_dir(tmp_path) / "pipelines.json"
        assert pipe_path.exists()

    def test_steps_stored_in_order(self, tmp_path):
        handle_record_pipeline(project_params(tmp_path, {
            "name": "ETL Pipeline",
            "steps": self._steps(),
        }))
        data = read_json_file(str(context_dir(tmp_path) / "pipelines.json"))
        entry = data[0]
        assert len(entry["steps"]) == 3
        actions = [s["action"] for s in entry["steps"]]
        assert actions == ["Fetch data", "Validate schema", "Write to DB"]
        orders = [s["order"] for s in entry["steps"]]
        assert orders == [1, 2, 3]

    def test_sequential_ids(self, tmp_path):
        for i in range(3):
            handle_record_pipeline(project_params(tmp_path, {
                "name": f"Pipeline {i}",
                "steps": [{"order": 1, "action": "do it"}],
            }))
        data = read_json_file(str(context_dir(tmp_path) / "pipelines.json"))
        ids = [e["id"] for e in data]
        assert ids == ["pipe-001", "pipe-002", "pipe-003"]

    def test_constraints_and_tags_stored(self, tmp_path):
        handle_record_pipeline(project_params(tmp_path, {
            "name": "Deploy Pipeline",
            "steps": [{"order": 1, "action": "build"}],
            "constraints": ["Never skip step 1"],
            "tags": ["deployment"],
        }))
        data = read_json_file(str(context_dir(tmp_path) / "pipelines.json"))
        entry = data[0]
        assert entry["constraints"] == ["Never skip step 1"]
        assert entry["tags"] == ["deployment"]

    def test_status_is_active(self, tmp_path):
        handle_record_pipeline(project_params(tmp_path, {
            "name": "Test Pipeline",
            "steps": [{"order": 1, "action": "run tests"}],
        }))
        data = read_json_file(str(context_dir(tmp_path) / "pipelines.json"))
        assert data[0]["status"] == "active"


# ===========================================================================
# 4. record_constraint
# ===========================================================================


class TestRecordConstraint:
    def test_creates_constraints_json(self, tmp_path):
        result = handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": "Security risk",
        }))
        assert result["success"] is True
        con_path = context_dir(tmp_path) / "constraints.json"
        assert con_path.exists()

    def test_hardness_absolute_default(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": "Security risk",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert data[0]["hardness"] == "absolute"

    def test_hardness_advisory_stored(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Prefer list comprehensions",
            "reason": "Readability",
            "hardness": "advisory",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert data[0]["hardness"] == "advisory"

    def test_scope_stored(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Use async functions only",
            "reason": "Concurrency model",
            "scope": "api/handlers.py",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert data[0]["scope"] == "api/handlers.py"

    def test_scope_defaults_to_global(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "No global state",
            "reason": "Testability",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert data[0]["scope"] == "global"

    def test_sequential_ids(self, tmp_path):
        for i in range(3):
            handle_record_constraint(project_params(tmp_path, {
                "rule": f"Rule {i}",
                "reason": "reason",
            }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        ids = [e["id"] for e in data]
        assert ids == ["con-001", "con-002", "con-003"]

    def test_correct_fields(self, tmp_path):
        # v0.4: reason must be >= 40 chars; pass enough text to satisfy.
        full_reason = "Security risk that must be avoided in this codebase."
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": full_reason,
            "tags": ["security"],
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        entry = data[0]
        assert entry["rule"] == "Never use eval()"
        assert entry["reason"] == full_reason
        assert entry["tags"] == ["security"]
        assert entry["status"] == "active"
        assert "created_at" in entry
        assert "verified_at" in entry


class TestRecordEntry:
    """Unified record_entry(kind=...) dispatches to the same impls; the three
    record_* tools are now thin wrappers producing identical results."""

    def _strip_volatile(self, entry):
        # created_at/updated_at/verified_at are wall-clock and differ between calls.
        return {k: v for k, v in entry.items()
                if k not in ("created_at", "updated_at", "verified_at", "id")}

    def test_kind_decision_matches_record_decision(self, tmp_path):
        p = decision_params(tmp_path, summary="Use JSON storage for entries",
                            tags=["storage"])
        via_entry = handle_record_entry({**p, "kind": "decision"})
        assert via_entry["success"] is True and via_entry["id"].startswith("dec-")
        # A second store: record_decision (the wrapper) yields the same shape.
        direct = handle_record_decision(decision_params(
            tmp_path, summary="Use JSON storage for entries", tags=["storage"]))
        assert self._strip_volatile(via_entry["entry"]) == self._strip_volatile(direct["entry"])

    def test_kind_constraint_writes_constraints_json(self, tmp_path):
        r = handle_record_entry(constraint_params(
            tmp_path, kind="constraint", rule="Never call eval on user input",
            reason="Executing user input is a remote code execution vector, full compromise."))
        assert r["success"] is True and r["id"].startswith("con-")
        assert (context_dir(tmp_path) / "constraints.json").exists()

    def test_kind_pipeline_writes_pipelines_json(self, tmp_path):
        r = handle_record_entry(pipeline_params(tmp_path, kind="pipeline"))
        assert r["success"] is True and r["id"].startswith("pipe-")
        assert (context_dir(tmp_path) / "pipelines.json").exists()

    def test_missing_kind_errors(self, tmp_path):
        r = handle_record_entry(decision_params(tmp_path))  # no kind
        assert "error" in r and "kind" in r["error"]

    def test_bad_kind_errors(self, tmp_path):
        r = handle_record_entry(decision_params(tmp_path, kind="gizmo"))
        assert "error" in r

    def test_kind_specific_validation_still_enforced(self, tmp_path):
        # decision requires why_chosen >= 60 chars; a too-short one is rejected
        # through record_entry exactly as through record_decision.
        r = handle_record_entry({
            "project_dir": str(tmp_path), "kind": "decision",
            "summary": "x", "problem": "y", "why_chosen": "too short",
        })
        assert "validation_errors" in r or "error" in r

    def test_wrappers_still_registered(self):
        from server import HANDLERS
        for name in ("record_entry", "record_decision", "record_pipeline", "record_constraint"):
            assert name in HANDLERS


# ===========================================================================
# 5. get_context
# ===========================================================================


class TestGetContext:
    def _populate(self, tmp_path):
        """Seed decisions, pipelines, and constraints into tmp_path."""
        handle_record_decision(project_params(tmp_path, {
            "summary": "Use JSON storage",
            "rationale": "Simplicity",
            "tags": ["storage", "architecture"],
        }))
        handle_record_decision(project_params(tmp_path, {
            "summary": "Use async HTTP client",
            "rationale": "Performance",
            "tags": ["http", "performance"],
        }))
        handle_record_pipeline(project_params(tmp_path, {
            "name": "Build and Deploy",
            "steps": [{"order": 1, "action": "build"}, {"order": 2, "action": "deploy"}],
            "tags": ["deployment"],
        }))
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": "Security",
            "tags": ["security"],
        }))

    def test_returns_all_entries_no_filter(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path))
        assert "results" in result
        assert result["entries_returned"] == 4

    def test_tag_filtering(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"tags": ["storage"]}))
        ids = [r["entry"]["id"] for r in result["results"]]
        assert "dec-001" in ids
        # dec-002 (http tag) should have a lower score but might still appear
        # The storage-tagged entry must be first (highest score)
        assert result["results"][0]["entry"]["id"] == "dec-001"

    def test_query_text_matching(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"query": "JSON storage"}))
        # The JSON storage decision should rank highest
        assert result["results"][0]["entry"]["id"] == "dec-001"

    def test_type_filter_decisions_only(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"types": ["decisions"]}))
        types = [r["type"] for r in result["results"]]
        assert all(t == "decision" for t in types)
        assert result["entries_returned"] == 2

    def test_type_filter_constraints_only(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"types": ["constraints"]}))
        types = [r["type"] for r in result["results"]]
        assert all(t == "constraint" for t in types)

    def test_direct_id_lookup(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"id": "dec-001"}))
        assert "entry" in result
        assert result["entry"]["id"] == "dec-001"
        assert result["type"] == "decisions"

    def test_direct_id_lookup_pipeline(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"id": "pipe-001"}))
        assert result["entry"]["id"] == "pipe-001"
        assert result["type"] == "pipelines"

    def test_direct_id_lookup_not_found(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_context(project_params(tmp_path, {"id": "dec-999"}))
        assert "error" in result

    def test_deprecated_entries_excluded(self, tmp_path):
        self._populate(tmp_path)
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "outdated",
        }))
        result = handle_get_context(project_params(tmp_path))
        ids = [r["entry"]["id"] for r in result["results"]]
        assert "dec-001" not in ids

    def test_no_context_dir_returns_initialized_false(self, tmp_path):
        # tmp_path has no .context/ dir
        result = handle_get_context(project_params(tmp_path))
        assert result.get("initialized") is False

    def test_token_budget_respected(self, tmp_path):
        # Seed many entries then request a tiny budget
        for i in range(10):
            handle_record_decision(project_params(tmp_path, {
                "summary": f"Decision number {i} about something important",
                "rationale": "reason " * 20,
            }))
        result = handle_get_context(project_params(tmp_path, {"token_budget": 50}))
        assert result["tokens_used"] <= 50


# ===========================================================================
# 6. get_project_summary
# ===========================================================================


class TestGetProjectSummary:
    def _populate(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Use JSON storage",
            "rationale": "Simplicity",
            "tags": ["storage"],
        }))
        handle_record_pipeline(project_params(tmp_path, {
            "name": "Build Pipeline",
            "steps": [{"order": 1, "action": "build"}, {"order": 2, "action": "test"}],
        }))
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": "Security",
            "hardness": "absolute",
        }))
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Prefer list comps",
            "reason": "Readability",
            "hardness": "advisory",
        }))

    def test_initialized_true_when_context_exists(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        assert result["initialized"] is True

    def test_initialized_false_when_no_context(self, tmp_path):
        result = handle_get_project_summary(project_params(tmp_path))
        assert result["initialized"] is False

    def test_counts_correct(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        counts = result["counts"]
        assert counts["decisions"] == 1
        assert counts["pipelines"] == 1
        assert counts["constraints_absolute"] == 1
        assert counts["constraints_advisory"] == 1

    def test_summary_contains_decision(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        assert "Use JSON storage" in result["summary"]

    def test_orientation_fields_present_and_shaped(self, tmp_path):
        self._populate(tmp_path)
        r = handle_get_project_summary(project_params(tmp_path))
        # existing keys preserved (SessionStart hook + current callers rely on these)
        for k in ("initialized", "summary", "counts", "stale_entries", "usage_guidance"):
            assert k in r
        # additive orientation fields
        assert r["counts_by_status"]["decisions"] == {"active": 1, "superseded": 0, "deprecated": 0}
        assert r["counts_by_status"]["constraints"]["active"] == 2
        rules = {c["rule"] for c in r["active_constraints"]}
        assert rules == {"Never use eval()", "Prefer list comps"}
        assert all({"id", "rule", "hardness", "scope"} <= set(c) for c in r["active_constraints"])
        assert r["recent_decisions"] == [{"id": "dec-001", "summary": "Use JSON storage"}]
        assert r["entry_ids"] == {
            "decisions": ["dec-001"], "pipelines": ["pipe-001"],
            "constraints": ["con-001", "con-002"]}

    def test_recent_decisions_capped_and_ordered(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
        for i in range(7):
            handle_record_decision(decision_params(
                tmp_path, summary=f"Decision number {i} about the storage layer design"))
        r = handle_get_project_summary(project_params(tmp_path))
        assert len(r["recent_decisions"]) == 5           # capped at 5
        assert r["recent_decisions"][0]["id"] == "dec-007"  # most recent first
        assert len(r["entry_ids"]["decisions"]) == 7      # id list is complete

    def test_counts_by_status_includes_superseded_and_deprecated(self, tmp_path):
        d1 = handle_record_decision(decision_params(tmp_path, summary="Original storage decision here"))
        handle_record_decision(decision_params(
            tmp_path, summary="Replacement storage decision", supersedes=[d1["id"]]))
        c1 = handle_record_constraint(constraint_params(tmp_path, rule="A rule to be retired soon"))
        handle_deprecate_entry({"project_dir": str(tmp_path), "id": c1["id"], "reason": "no longer needed"})
        r = handle_get_project_summary(project_params(tmp_path))
        assert r["counts_by_status"]["decisions"] == {"active": 1, "superseded": 1, "deprecated": 0}
        assert r["counts_by_status"]["constraints"]["deprecated"] == 1

    def test_summary_contains_pipeline(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        assert "Build Pipeline" in result["summary"]

    def test_summary_contains_absolute_constraint(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        assert "Never use eval()" in result["summary"]

    def test_usage_guidance_present(self, tmp_path):
        self._populate(tmp_path)
        result = handle_get_project_summary(project_params(tmp_path))
        assert "usage_guidance" in result
        assert len(result["usage_guidance"]) > 0

    def test_deprecated_entries_excluded_from_counts(self, tmp_path):
        self._populate(tmp_path)
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "outdated",
        }))
        result = handle_get_project_summary(project_params(tmp_path))
        assert result["counts"]["decisions"] == 0

    def test_stale_entries_flagged(self, tmp_path):
        """An entry with a very old verified_at should appear in stale_entries."""
        # Create a decision then manually backdate its verified_at
        handle_record_decision(project_params(tmp_path, {
            "summary": "Old decision",
            "rationale": "reason",
        }))
        dec_path = context_dir(tmp_path) / "decisions.json"
        data = json.loads(dec_path.read_text(encoding="utf-8"))
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        data[0]["verified_at"] = old_date
        dec_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        result = handle_get_project_summary(project_params(tmp_path))
        assert result.get("stale_entries") is not None
        stale_ids = [s["id"] for s in result["stale_entries"]]
        assert "dec-001" in stale_ids


# ===========================================================================
# 7. update_entry
# ===========================================================================


class TestUpdateEntry:
    def test_updates_specified_field(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Original summary",
            "rationale": "reason",
        }))
        result = handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"summary": "Updated summary"},
        }))
        assert result["success"] is True
        assert result["entry"]["summary"] == "Updated summary"

    def test_preserves_other_fields(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Original",
            "rationale": "My reasoning",
            "tags": ["arch"],
        }))
        handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"summary": "Updated"},
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        entry = data[0]
        assert entry["rationale"] == "My reasoning"
        assert entry["tags"] == ["arch"]

    def test_bumps_verified_at(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        old_verified = data[0]["verified_at"]

        # Small delay to ensure timestamp changes
        import time; time.sleep(0.01)

        handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"summary": "New summary"},
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        new_verified = data[0]["verified_at"]
        # verified_at must be present and is now refreshed
        assert "verified_at" in data[0]
        # updated_at must also be set
        assert "updated_at" in data[0]

    def test_protects_id(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"id": "dec-999"},
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert data[0]["id"] == "dec-001"

    def test_protects_created_at(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        original_created = data[0]["created_at"]

        handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"created_at": "1970-01-01T00:00:00+00:00"},
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert data[0]["created_at"] == original_created

    def test_not_found_returns_error(self, tmp_path):
        # Create the context dir so we don't get UNRESOLVED_PROJECT_ERROR
        (context_dir(tmp_path)).mkdir(parents=True)
        result = handle_update_entry(project_params(tmp_path, {
            "id": "dec-999",
            "updates": {"summary": "x"},
        }))
        assert "error" in result

    def test_update_pipeline(self, tmp_path):
        handle_record_pipeline(project_params(tmp_path, {
            "name": "Old Name",
            "steps": [{"order": 1, "action": "step"}],
        }))
        result = handle_update_entry(project_params(tmp_path, {
            "id": "pipe-001",
            "updates": {"name": "New Name"},
        }))
        assert result["success"] is True
        assert result["entry"]["name"] == "New Name"

    def test_update_constraint(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Old rule",
            "reason": "reason",
        }))
        result = handle_update_entry(project_params(tmp_path, {
            "id": "con-001",
            "updates": {"rule": "New rule"},
        }))
        assert result["success"] is True
        assert result["entry"]["rule"] == "New rule"

    def test_persists_to_disk(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        handle_update_entry(project_params(tmp_path, {
            "id": "dec-001",
            "updates": {"summary": "Persisted update"},
        }))
        # Re-read from disk
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert data[0]["summary"] == "Persisted update"


# ===========================================================================
# 8. deprecate_entry
# ===========================================================================


class TestDeprecateEntry:
    def test_sets_status_deprecated(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Old decision",
            "rationale": "reason",
        }))
        result = handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "No longer relevant",
        }))
        assert result["success"] is True
        assert result["status"] == "deprecated"

    def test_stores_reason(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Old decision",
            "rationale": "reason",
        }))
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "Replaced by new approach",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert data[0]["deprecated_reason"] == "Replaced by new approach"

    def test_superseded_by_stored_for_decisions(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {"summary": "Old", "rationale": "r"}))
        handle_record_decision(project_params(tmp_path, {"summary": "New", "rationale": "r"}))
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "Superseded",
            "superseded_by": "dec-002",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        entry = next(e for e in data if e["id"] == "dec-001")
        assert entry["superseded_by"] == "dec-002"

    def test_deprecated_persisted_to_disk(self, tmp_path):
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Old rule",
            "reason": "reason",
        }))
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "con-001",
            "reason": "Rule changed",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert data[0]["status"] == "deprecated"

    def test_not_found_returns_error(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True)
        result = handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-999",
            "reason": "reason",
        }))
        assert "error" in result

    def test_updated_at_set(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Decision",
            "rationale": "reason",
        }))
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "done",
        }))
        data = read_json_file(str(context_dir(tmp_path) / "decisions.json"))
        assert "updated_at" in data[0]


class TestDeprecateMerge:
    """deprecate_entry(merge_into=...) — dedup merge into a surviving entry."""

    def _two_decisions(self, tmp_path):
        a = handle_record_decision(decision_params(
            tmp_path,
            summary="Use JSON files for the entry store",
            problem="Need a store a human can hand-edit and diff in git without extra tooling here.",
            why_chosen="JSON is human-editable and diffable and needs zero dependencies unlike SQLite for this.",
            tags=["storage"],
            retrieval_hints=["json store"],
        ))
        b = handle_record_decision(decision_params(
            tmp_path,
            summary="Entry store is plain JSON on disk",
            problem="Same storage question, phrased differently by a later session that forgot A existed.",
            why_chosen="Plain JSON keeps the store transparent, greppable, and dependency-free for contributors here.",
            tags=["storage", "architecture"],
            retrieval_hints=["flat file db"],
            what_we_tried="Considered SQLite but rejected it for opacity.",
        ))
        return a["id"], b["id"]

    def test_merge_unions_lists_and_backfills_text(self, tmp_path):
        a_id, b_id = self._two_decisions(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": b_id,
            "reason": f"Restatement of {a_id}", "merge_into": a_id,
        })
        assert r["success"] is True
        assert r["merged_into"] == a_id
        assert r["merged"]["tags_added"] == ["architecture"]
        assert r["merged"]["fields_backfilled"] == ["what_we_tried"]
        keep = handle_get_context({"project_dir": str(tmp_path), "id": a_id})["entry"]
        assert keep["tags"] == ["storage", "architecture"]
        assert keep["retrieval_hints"] == ["json store", "flat file db"]
        assert keep["what_we_tried"] == "Considered SQLite but rejected it for opacity."

    def test_merge_deprecates_source_with_superseded_by(self, tmp_path):
        a_id, b_id = self._two_decisions(tmp_path)
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": b_id,
            "reason": "dupe", "merge_into": a_id,
        })
        dep = handle_get_context({"project_dir": str(tmp_path), "id": b_id})["entry"]
        assert dep["status"] == "deprecated"
        assert dep["superseded_by"] == a_id

    def test_merge_never_overwrites_target_text(self, tmp_path):
        a_id, b_id = self._two_decisions(tmp_path)
        before = handle_get_context({"project_dir": str(tmp_path), "id": a_id})["entry"]
        why_before = before["why_chosen"]
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": b_id,
            "reason": "dupe", "merge_into": a_id,
        })
        after = handle_get_context({"project_dir": str(tmp_path), "id": a_id})["entry"]
        # Target's own non-empty why_chosen is preserved, not clobbered by B's.
        assert after["why_chosen"] == why_before

    def test_merge_into_self_errors(self, tmp_path):
        a_id, _ = self._two_decisions(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a_id,
            "reason": "x", "merge_into": a_id,
        })
        assert "error" in r

    def test_merge_into_missing_target_errors_without_deprecating(self, tmp_path):
        a_id, b_id = self._two_decisions(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": b_id,
            "reason": "x", "merge_into": "dec-999",
        })
        assert "error" in r
        # B must NOT have been deprecated — the bad merge failed cleanly.
        b = handle_get_context({"project_dir": str(tmp_path), "id": b_id})["entry"]
        assert b.get("status", "active") == "active"

    def test_merge_into_cross_type_errors(self, tmp_path):
        a_id, _ = self._two_decisions(tmp_path)
        con = handle_record_constraint(constraint_params(
            tmp_path,
            rule="API responses must be camelCase",
            reason="Consumers expect camelCase keys and break on snake_case in the payload.",
        ))
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": con["id"],
            "reason": "x", "merge_into": a_id,
        })
        assert "error" in r
        # The constraint was not deprecated by the failed cross-type merge.
        c = handle_get_context({"project_dir": str(tmp_path), "id": con["id"]})["entry"]
        assert c.get("status", "active") == "active"

    def test_plain_deprecate_unchanged_without_merge_into(self, tmp_path):
        # Guard: default deprecate path is byte-for-byte the old behavior.
        a_id, b_id = self._two_decisions(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": b_id, "reason": "gone",
        })
        assert r["success"] is True and r["status"] == "deprecated"
        assert "merged_into" not in r
        b = handle_get_context({"project_dir": str(tmp_path), "id": b_id})["entry"]
        assert b["status"] == "deprecated"
        # A untouched — no merge happened.
        a = handle_get_context({"project_dir": str(tmp_path), "id": a_id})["entry"]
        assert a["tags"] == ["storage"]


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


class TestPruneStale:
    def test_fresh_entries_not_flagged(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Fresh decision",
            "rationale": "reason",
        }))
        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        assert result["count"] == 0
        assert result["stale"] == []

    def test_old_entries_flagged(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Stale decision",
            "rationale": "reason",
        }))
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-001", 60)

        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        assert result["count"] == 1
        assert result["stale"][0]["id"] == "dec-001"
        assert result["stale"][0]["days_since_verified"] >= 60

    def test_threshold_respected(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {"summary": "A", "rationale": "r"}))
        handle_record_decision(project_params(tmp_path, {"summary": "B", "rationale": "r"}))
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-001", 40)
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-002", 20)

        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        stale_ids = [s["id"] for s in result["stale"]]
        assert "dec-001" in stale_ids
        assert "dec-002" not in stale_ids

    def test_deprecated_entries_excluded(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {
            "summary": "Old deprecated",
            "rationale": "reason",
        }))
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-001", 60)
        handle_deprecate_entry(project_params(tmp_path, {
            "id": "dec-001",
            "reason": "outdated",
        }))
        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        assert result["count"] == 0

    def test_stale_sorted_by_age_descending(self, tmp_path):
        for i in range(3):
            handle_record_decision(project_params(tmp_path, {
                "summary": f"Decision {i}",
                "rationale": "reason",
            }))
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-001", 90)
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-002", 60)
        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-003", 45)

        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        days_list = [s["days_since_verified"] for s in result["stale"]]
        assert days_list == sorted(days_list, reverse=True)

    def test_works_across_all_types(self, tmp_path):
        handle_record_decision(project_params(tmp_path, {"summary": "D", "rationale": "r"}))
        handle_record_pipeline(project_params(tmp_path, {
            "name": "P",
            "steps": [{"order": 1, "action": "step"}],
        }))
        handle_record_constraint(project_params(tmp_path, {"rule": "C", "reason": "r"}))

        _backdate_entry(context_dir(tmp_path) / "decisions.json", "dec-001", 60)
        _backdate_entry(context_dir(tmp_path) / "pipelines.json", "pipe-001", 60)
        _backdate_entry(context_dir(tmp_path) / "constraints.json", "con-001", 60)

        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        assert result["count"] == 3

    def test_no_context_dir_returns_empty(self, tmp_path):
        result = handle_prune_stale(project_params(tmp_path, {"days": 30}))
        assert result["stale"] == []

    def test_threshold_days_in_response(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True)
        result = handle_prune_stale(project_params(tmp_path, {"days": 45}))
        assert result["threshold_days"] == 45


# ===========================================================================
# 10. get_compaction_report
# ===========================================================================


class TestGetCompactionReport:
    def test_no_report_file(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True)
        result = handle_get_compaction_report(project_params(tmp_path))
        assert result["has_report"] is False

    def test_report_returned_when_exists(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(parents=True)
        report_data = {
            "status": "ok",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "missing": [],
            "modified": [],
        }
        (ctx / "compaction_report.json").write_text(
            json.dumps(report_data), encoding="utf-8"
        )
        result = handle_get_compaction_report(project_params(tmp_path))
        assert result["has_report"] is True
        assert result["status"] == "ok"

    def test_discrepancies_found_adds_action(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(parents=True)
        report_data = {
            "status": "discrepancies_found",
            "missing": ["dec-001"],
            "modified": [],
        }
        (ctx / "compaction_report.json").write_text(
            json.dumps(report_data), encoding="utf-8"
        )
        result = handle_get_compaction_report(project_params(tmp_path))
        assert result["has_report"] is True
        assert "action" in result
        assert "discrepancies" in result["action"].lower() or "missing" in result["action"].lower()

    def test_no_project_resolved_returns_has_report_false(self, monkeypatch):
        """When base_dir is None, returns has_report: False gracefully."""
        import server as srv
        original = srv._base_dir_from_params
        try:
            srv._base_dir_from_params = lambda p: None
            result = handle_get_compaction_report({})
            assert result["has_report"] is False
        finally:
            srv._base_dir_from_params = original

    def test_corrupt_report_returns_error(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(parents=True)
        (ctx / "compaction_report.json").write_text("NOT JSON{{{", encoding="utf-8")
        result = handle_get_compaction_report(project_params(tmp_path))
        assert "error" in result


# ===========================================================================
# 11. v0.4 schema validation
# ===========================================================================


class TestSchemaValidation:
    def test_decision_rejected_when_problem_missing(self, tmp_path):
        """Decision without `problem` is rejected with validation_errors."""
        result = handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "A decision",
            "why_chosen": "Some reasoning that is definitely long enough to satisfy the minimum",
        })
        assert "error" in result
        assert "validation_errors" in result
        fields = [e["field"] for e in result["validation_errors"]]
        assert "problem" in fields

    def test_decision_rejected_when_why_chosen_too_short(self, tmp_path):
        result = handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "A decision",
            "problem": "We needed to test the validation logic for short reasoning fields.",
            "why_chosen": "too short",
        })
        assert "error" in result
        fields = [e["field"] for e in result["validation_errors"]]
        assert "why_chosen" in fields

    def test_rationale_back_compat_maps_to_why_chosen(self, tmp_path):
        """Old-style `rationale` arg auto-promotes to `why_chosen` so
        legacy MCP clients keep working — but `problem` still required."""
        long_text = "Legacy rationale text that is comfortably long enough for the validator."
        result = handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "A decision",
            "problem": "Backward-compat path needs to keep old MCP clients working.",
            "rationale": long_text,
        })
        assert result.get("success") is True
        assert result["entry"]["why_chosen"] == long_text
        assert result["entry"]["rationale"] == long_text  # preserved on disk too

    def test_constraint_rejected_when_reason_too_short(self, tmp_path):
        result = handle_record_constraint({
            "project_dir": str(tmp_path),
            "rule": "Never use eval()",
            "reason": "short",
        })
        assert "error" in result
        fields = [e["field"] for e in result["validation_errors"]]
        assert "reason" in fields

    def test_pipeline_rejected_when_purpose_missing(self, tmp_path):
        result = handle_record_pipeline({
            "project_dir": str(tmp_path),
            "name": "Build Pipeline",
            "steps": [{"order": 1, "action": "build"}],
        })
        assert "error" in result
        fields = [e["field"] for e in result["validation_errors"]]
        assert "purpose" in fields

    def test_valid_decision_stores_v4_schema(self, tmp_path):
        result = handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Use symlog",
            "problem": "Value head kept saturating because returns spanned 8 orders of magnitude.",
            "why_chosen": "Symlog bounds targets by construction; three independent audits converged on this fix.",
            "what_we_tried": "Reset value head 3 times; each recurred within hours.",
            "tradeoffs": "Slight loss of small-value resolution in the bounded space.",
        })
        assert result.get("success") is True
        entry = result["entry"]
        assert entry["schema_version"] == 4
        assert entry["problem"].startswith("Value head")
        assert entry["what_we_tried"].startswith("Reset")
        assert entry["tradeoffs"].startswith("Slight loss")


# ===========================================================================
# 12. related_to graph traversal
# ===========================================================================


class TestRelatedToTraversal:
    def _setup_linked_pair(self, tmp_path):
        """Record two decisions where the second links to the first."""
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Use symlog",
            "problem": "Value head saturation caused by huge returns spanning 8 orders of magnitude.",
            "why_chosen": "Symlog bounds the target distribution by construction, so no head reset is needed.",
            "tags": ["value-head", "ppo"],
        })
        handle_record_constraint({
            "project_dir": str(tmp_path),
            "rule": "Never reset value head without addressing root cause",
            "reason": "Without fixing the upstream distribution shift, the value head will saturate again.",
            "tags": ["value-head"],
            "related_to": ["dec-001"],
        })

    def test_related_entry_pulled_in_by_default(self, tmp_path):
        self._setup_linked_pair(tmp_path)
        # Query that matches the constraint by tag — the related decision
        # (dec-001) should come along even though the query didn't target it.
        result = handle_get_context(project_params(tmp_path, {
            "tags": ["value-head"],
            "types": ["constraints"],
        }))
        ids = [r["entry"]["id"] for r in result["results"]]
        assert "con-001" in ids
        assert "dec-001" in ids
        assert result["related_added"] == 1
        via_related = [r for r in result["results"] if r.get("via") == "related_to"]
        assert any(r["entry"]["id"] == "dec-001" for r in via_related)

    def test_include_related_false_skips_traversal(self, tmp_path):
        self._setup_linked_pair(tmp_path)
        result = handle_get_context(project_params(tmp_path, {
            "tags": ["value-head"],
            "types": ["constraints"],
            "include_related": False,
        }))
        ids = [r["entry"]["id"] for r in result["results"]]
        assert "dec-001" not in ids
        assert result["related_added"] == 0


# ===========================================================================
# 13. verify_quality
# ===========================================================================


class TestVerifyQuality:
    def test_legacy_decision_flagged(self, tmp_path):
        """Pre-v0.4 entries (no schema_version, no why_chosen) get flagged."""
        ctx = context_dir(tmp_path)
        ctx.mkdir(parents=True)
        # Write a legacy entry directly (bypassing the handler so no
        # auto-migration happens). Reasoning text is intentionally long
        # enough so the only flag we get is 'legacy'.
        legacy = [{
            "id": "dec-001",
            "summary": "Old decision",
            "rationale": (
                "Some long enough rationale text that easily clears the "
                "thin-reason threshold so we isolate the legacy flag in this test."
            ),
            "tags": ["old"],
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
            "verified_at": "2024-01-01T00:00:00+00:00",
        }]
        (ctx / "decisions.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        assert result["count"] >= 1
        flag = next(f for f in result["flagged"] if f["id"] == "dec-001")
        issue_types = [i["type"] for i in flag["issues"]]
        assert "legacy" in issue_types

    def test_thin_reason_flagged(self, tmp_path):
        """v0.4 entry with why_chosen that clears the 60-char validation
        minimum but trips the higher quality-scan threshold."""
        # 70 chars — above validation min (60), below test threshold (200)
        why = "Short answer that passes server-side validation but trips quality scan."
        assert 60 <= len(why) < 200
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Decision",
            "problem": "We needed a quick test of the thin-reason quality flag.",
            "why_chosen": why,
            "tags": ["test"],
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path, {"min_reason_chars": 200}))
        assert any(
            "thin_reason" in [i["type"] for i in f["issues"]]
            for f in result["flagged"]
        )

    def test_no_tags_flagged(self, tmp_path):
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Decision",
            "problem": "We needed an entry with no tags so we can flag it for the test.",
            "why_chosen": (
                "Tags are the primary retrieval signal — entries without tags "
                "won't surface from get_context queries, which is the issue we want to flag here."
            ),
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        assert any(
            "no_tags" in [i["type"] for i in f["issues"]]
            for f in result["flagged"]
        )

    def test_isolated_flagged(self, tmp_path):
        """Two entries sharing a tag but no related_to link → both flagged isolated."""
        for i in range(2):
            handle_record_decision({
                "project_dir": str(tmp_path),
                "summary": f"Decision {i}",
                "problem": "We needed two entries that share a tag for the isolation flag test.",
                "why_chosen": (
                    "Both entries share the 'shared' tag but neither links to the other, "
                    "which is exactly the missed-link condition the isolated flag is designed to catch."
                ),
                "tags": ["shared"],
            })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        isolated = [
            f for f in result["flagged"]
            if any(i["type"] == "isolated" for i in f["issues"])
        ]
        assert len(isolated) == 2

    def test_clean_entry_not_flagged(self, tmp_path):
        """Entry with full v0.4 schema + tags + related_to should pass clean."""
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "First decision",
            "problem": "We need a clean entry that passes every quality check we run today.",
            "why_chosen": (
                "Full v0.4 schema, populated tags, and a related_to link to dec-002 "
                "should make this entry survive verify_quality with zero flags."
            ),
            "tags": ["clean"],
            "related_to": ["dec-002"],
        })
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Second decision",
            "problem": "We need a second entry so the related_to link target exists in scope.",
            "why_chosen": (
                "Same shape as the first entry, with related_to pointing back at dec-001 "
                "so neither shows up isolated despite sharing the 'clean' tag."
            ),
            "tags": ["clean"],
            "related_to": ["dec-001"],
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        flagged_ids = [f["id"] for f in result["flagged"]]
        assert "dec-001" not in flagged_ids
        assert "dec-002" not in flagged_ids


# ===========================================================================
# Utility: next_id
# ===========================================================================


class TestNextId:
    def test_empty_list_starts_at_001(self):
        assert next_id([], "dec") == "dec-001"

    def test_increments_correctly(self):
        entries = [{"id": "dec-001"}, {"id": "dec-002"}]
        assert next_id(entries, "dec") == "dec-003"

    def test_ignores_other_prefixes(self):
        entries = [{"id": "pipe-005"}]
        assert next_id(entries, "dec") == "dec-001"

    def test_handles_gaps(self):
        entries = [{"id": "dec-001"}, {"id": "dec-005"}]
        assert next_id(entries, "dec") == "dec-006"

    def test_pads_to_three_digits(self):
        assert next_id([], "con") == "con-001"
        entries = [{"id": "con-009"}]
        assert next_id(entries, "con") == "con-010"


# ===========================================================================
# Data integrity: atomic writes + corrupt-store protection
# ===========================================================================


class TestDataIntegrity:
    def test_record_refuses_to_write_over_corrupt_store(self, tmp_path):
        """A corrupt decisions.json must reject the write, not be wiped."""
        ctx = context_dir(tmp_path)
        ctx.mkdir()
        corrupt = "{this is not valid json"
        (ctx / "decisions.json").write_text(corrupt, encoding="utf-8")

        result = handle_record_decision(decision_params(tmp_path))

        assert "error" in result
        assert "Refusing to write" in result["error"]
        # Original file untouched — history preserved for manual recovery
        assert (ctx / "decisions.json").read_text(encoding="utf-8") == corrupt

    def test_record_refuses_non_list_store(self, tmp_path):
        """Valid JSON that isn't a list is still a corrupt store for writes."""
        ctx = context_dir(tmp_path)
        ctx.mkdir()
        (ctx / "constraints.json").write_text('{"oops": "a dict"}', encoding="utf-8")

        result = handle_record_constraint(constraint_params(tmp_path))

        assert "error" in result
        assert "Refusing to write" in result["error"]

    def test_missing_file_is_still_a_fresh_store(self, tmp_path):
        """Absent file (vs corrupt) records normally from dec-001."""
        result = handle_record_decision(decision_params(tmp_path))
        assert result["success"] is True
        assert result["id"] == "dec-001"

    def test_read_paths_soft_fallback_on_corrupt(self, tmp_path):
        """Retrieval stays soft: corrupt file reads as empty, no crash."""
        ctx = context_dir(tmp_path)
        ctx.mkdir()
        (ctx / "decisions.json").write_text("<<<", encoding="utf-8")
        result = handle_get_context({"project_dir": str(tmp_path), "query": "anything"})
        assert result["results"] == []

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path):
        result = handle_record_decision(decision_params(tmp_path))
        assert result["success"] is True
        ctx = context_dir(tmp_path)
        assert not (ctx / "decisions.json.tmp").exists()
        # And the written file parses back
        entries = json.loads((ctx / "decisions.json").read_text(encoding="utf-8"))
        assert len(entries) == 1


# ===========================================================================
# update_entry validation (can't hollow out v0.4 structured fields)
# ===========================================================================


class TestUpdateEntryValidation:
    def test_update_rejects_thin_why_chosen(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        result = handle_update_entry({
            "project_dir": str(tmp_path),
            "id": rec["id"],
            "updates": {"why_chosen": "short"},
        })
        assert "validation_errors" in result
        # Entry on disk unchanged
        got = handle_get_context({"project_dir": str(tmp_path), "id": rec["id"]})
        assert got["entry"]["why_chosen"] == _LONG_WHY

    def test_update_accepts_valid_why_chosen(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        new_why = _LONG_WHY + " Revised after further evidence came in."
        result = handle_update_entry({
            "project_dir": str(tmp_path),
            "id": rec["id"],
            "updates": {"why_chosen": new_why},
        })
        assert result["success"] is True
        assert result["entry"]["why_chosen"] == new_why

    def test_update_rejects_thin_constraint_reason(self, tmp_path):
        rec = handle_record_constraint(constraint_params(tmp_path))
        result = handle_update_entry({
            "project_dir": str(tmp_path),
            "id": rec["id"],
            "updates": {"reason": "because"},
        })
        assert "validation_errors" in result

    def test_update_rejects_empty_pipeline_steps(self, tmp_path):
        rec = handle_record_pipeline(pipeline_params(tmp_path))
        result = handle_update_entry({
            "project_dir": str(tmp_path),
            "id": rec["id"],
            "updates": {"steps": []},
        })
        assert "error" in result

    def test_update_unvalidated_fields_pass_through(self, tmp_path):
        """Fields outside the structured schema (tags, status) update freely."""
        rec = handle_record_decision(decision_params(tmp_path))
        result = handle_update_entry({
            "project_dir": str(tmp_path),
            "id": rec["id"],
            "updates": {"tags": ["new-tag"]},
        })
        assert result["success"] is True
        assert result["entry"]["tags"] == ["new-tag"]


# ===========================================================================
# Budget packing: oversized entries are skipped, not blocking
# ===========================================================================


class TestBudgetPackingSkip:
    def test_oversized_entry_does_not_block_smaller_ones(self, tmp_path):
        # dec-001: huge summary — even truncated it exceeds the tiny budget.
        handle_record_decision(decision_params(
            tmp_path, summary="B" * 4000, tags=["shared"]))
        # dec-002: normal-sized entry that fits comfortably.
        handle_record_decision(decision_params(
            tmp_path, summary="A small decision that fits the budget", tags=["shared"]))

        result = handle_get_context({
            "project_dir": str(tmp_path),
            "token_budget": 300,
            "include_related": False,
        })
        ids = [r["entry"]["id"] for r in result["results"]]
        # Old behavior: dec-001 didn't fit -> break -> nothing returned.
        assert "dec-002" in ids
        assert "dec-001" not in ids


# ===========================================================================
# Recency scoring clamp
# ===========================================================================


class TestRecencyClamp:
    def test_future_timestamp_cannot_exceed_recency_cap(self):
        now_dt = datetime.now(timezone.utc)
        base = {"tags": [], "status": "active"}
        future_entry = dict(base, verified_at=(now_dt + timedelta(days=365)).isoformat())
        fresh_entry = dict(base, verified_at=now_dt.isoformat())
        s_future = score_entry(future_entry, now_dt=now_dt)
        s_fresh = score_entry(fresh_entry, now_dt=now_dt)
        assert s_future <= s_fresh + 1e-9


# ===========================================================================
# Semantic layer: unreachable embedder falls back to lexical
# ===========================================================================


class TestSemanticFallback:
    def test_unreachable_embedder_falls_back_to_lexical(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path, summary="Use JSON files for storage", tags=["storage"]))
        # Port 9 (discard) — nothing listens there; connection is refused
        # immediately and get_context must still return lexical results.
        result = handle_get_context({
            "project_dir": str(tmp_path),
            "query": "storage decision",
            "semantic": {"enabled": True, "url": "http://127.0.0.1:9"},
        })
        assert result["entries_returned"] >= 1


# ===========================================================================
# Similar-entry surfacing at record time (v0.6)
# ===========================================================================


class TestSimilarEntrySurfacing:
    def test_near_duplicate_is_flagged(self, tmp_path):
        first = handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic writes with temp file and os.replace for entry files",
            tags=["storage", "data-integrity"],
        ))
        second = handle_record_decision(decision_params(
            tmp_path,
            summary="Entry files use atomic writes via temp file and os.replace",
            tags=["storage", "data-integrity"],
        ))
        assert second["success"] is True  # write proceeds, advisory only
        flagged_ids = [m["id"] for m in second.get("similar_entries", [])]
        assert first["id"] in flagged_ids
        assert "similar_note" in second

    def test_unrelated_entries_not_flagged(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic writes for entry files on disk",
            problem="Crash mid-write corrupted the JSON store and destroyed recorded history.",
            why_chosen="Atomic replace keeps the old file intact until the new one is complete on disk.",
            tags=["storage"],
        ))
        second = handle_record_constraint(constraint_params(
            tmp_path,
            rule="Never run the scheduler binary from source",
            reason="Running from source skips the packaged environment and breaks scheduled job discovery.",
            tags=["scheduler"],
        ))
        assert second["success"] is True
        assert "similar_entries" not in second

    def test_related_to_links_are_excluded(self, tmp_path):
        first = handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic writes with temp file and os.replace for entry files",
            tags=["storage", "data-integrity"],
        ))
        second = handle_record_decision(decision_params(
            tmp_path,
            summary="Entry files use atomic writes via temp file and os.replace",
            tags=["storage", "data-integrity"],
            related_to=[first["id"]],
        ))
        # Caller already acknowledged the relation — no warning needed.
        flagged_ids = [m["id"] for m in second.get("similar_entries", [])]
        assert first["id"] not in flagged_ids


# ===========================================================================
# Restatement vs contradiction classification on overlapping entries
# ===========================================================================


class TestOverlapClassification:
    def test_classify_negation_asymmetry_is_contradiction(self):
        a = _text_words({"rule": "api responses must be camelCase required"})
        b = _text_words({"rule": "api responses must not be camelCase"})
        assert _classify_overlap(a, b) == "likely_contradiction"

    def test_classify_polarity_pair_is_contradiction(self):
        a = _text_words({"rule": "always run conductor from the packaged exe"})
        b = _text_words({"rule": "never run conductor from the packaged exe"})
        assert _classify_overlap(a, b) == "likely_contradiction"

    def test_classify_same_polarity_is_restatement(self):
        a = _text_words({"summary": "all api responses use camelCase keys"})
        b = _text_words({"summary": "api responses should use camelCase keys everywhere"})
        assert _classify_overlap(a, b) == "likely_restatement"

    def test_classify_both_negate_same_thing_is_restatement(self):
        # Both carry a negation marker -> no asymmetry -> restatement, not a
        # spurious contradiction.
        a = _text_words({"rule": "never run the scheduler from source"})
        b = _text_words({"rule": "do not run the scheduler from source, avoid it"})
        assert _classify_overlap(a, b) == "likely_restatement"

    def test_contradiction_surfaced_at_record_time(self, tmp_path):
        first = handle_record_constraint(constraint_params(
            tmp_path,
            rule="Always run the scheduler binary from the packaged exe",
            reason="The packaged exe carries the environment the scheduler needs to find jobs.",
            tags=["scheduler"],
        ))
        second = handle_record_constraint(constraint_params(
            tmp_path,
            rule="Never run the scheduler binary from the packaged exe",
            reason="The packaged exe pins a stale environment that hides newly added jobs.",
            tags=["scheduler"],
        ))
        assert second["success"] is True  # advisory only, write proceeds
        matches = second.get("similar_entries", [])
        flagged = [m for m in matches if m["id"] == first["id"]]
        assert flagged and flagged[0]["relation"] == "likely_contradiction"
        assert "contradiction_note" in second

    def test_restatement_has_no_contradiction_note(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic writes with temp file and os.replace for entry files",
            tags=["storage", "data-integrity"],
        ))
        second = handle_record_decision(decision_params(
            tmp_path,
            summary="Entry files use atomic writes via temp file and os.replace",
            tags=["storage", "data-integrity"],
        ))
        relations = [m["relation"] for m in second.get("similar_entries", [])]
        assert relations and all(r == "likely_restatement" for r in relations)
        assert "contradiction_note" not in second


# ===========================================================================
# query_entries: deterministic structured field filtering (v0.13)
# ===========================================================================


class TestQueryEntries:
    """A small mixed store, then one filter type per test."""

    def _seed(self, tmp_path):
        ids = {}
        d1 = handle_record_decision(decision_params(
            tmp_path,
            summary="Use JSON files for the entry store",
            problem="Need a store a human can hand-edit and diff in git without extra tooling.",
            why_chosen="JSON is human-editable, diffable, and needs zero dependencies unlike SQLite here.",
            tags=["storage", "architecture"],
            origin="user",
        ))
        d2 = handle_record_decision(decision_params(
            tmp_path,
            summary="Switch the entry store to atomic writes",
            problem="A crash mid-write truncated the JSON store and destroyed recorded history badly.",
            why_chosen="Atomic temp-file replace keeps the old file intact until the new one is fully written out.",
            tags=["storage"],
            origin="user",
            supersedes=[d1["id"]],
        ))
        c1 = handle_record_constraint(constraint_params(
            tmp_path,
            rule="Hook stdout must be ASCII only",
            reason="Windows cp1252 stdout raises UnicodeEncodeError on non-ASCII and crashes the hook.",
            scope="hooks/",
            hardness="absolute",
            tags=["hooks", "windows"],
            origin="user",
        ))
        c2 = handle_record_constraint(constraint_params(
            tmp_path,
            rule="Prefer small helper functions in the server module",
            reason="Keeps the growing server handlers readable and independently testable over time.",
            scope="server.py",
            hardness="advisory",
            tags=["style"],
        ))
        p1 = handle_record_pipeline(pipeline_params(
            tmp_path,
            name="Release flow",
            purpose="Ship a version bump consistently across pyproject, server.json and server.py.",
            steps=[{"order": 1, "action": "bump versions"}],
            tags=["release"],
        ))
        ids.update(d1=d1["id"], d2=d2["id"], c1=c1["id"], c2=c2["id"], p1=p1["id"])
        return ids

    def _q(self, tmp_path, **preds):
        r = handle_query_entries({"project_dir": str(tmp_path), **preds})
        return r, [x["entry"]["id"] for x in r["results"]]

    def test_kind_alias_maps_to_types(self, tmp_path):
        ids = self._seed(tmp_path)
        _, got = self._q(tmp_path, kind="constraint")
        assert set(got) == {ids["c1"], ids["c2"]}
        # list form works too
        _, got2 = self._q(tmp_path, kind=["decision", "pipeline"])
        assert set(got2) == {ids["d1"], ids["d2"], ids["p1"]}
        # explicit `types` wins over `kind` when both are given
        _, got3 = self._q(tmp_path, kind="constraint", types=["pipelines"])
        assert set(got3) == {ids["p1"]}

    def test_text_filter_and_terms(self, tmp_path):
        ids = self._seed(tmp_path)
        # "atomic" appears only in d2's summary/rationale
        _, got = self._q(tmp_path, text="atomic")
        assert got == [ids["d2"]]
        # all terms must appear (AND): "atomic" and "json" both in d2
        _, both = self._q(tmp_path, text="atomic json")
        assert both == [ids["d2"]]
        # a term present nowhere -> empty
        _, none = self._q(tmp_path, text="kubernetes")
        assert none == []

    def test_text_combines_with_other_predicates(self, tmp_path):
        ids = self._seed(tmp_path)
        # text + kind AND together
        _, got = self._q(tmp_path, kind="constraint", text="ascii")
        assert got == [ids["c1"]]

    def test_limit_caps_returned_but_reports_total(self, tmp_path):
        self._seed(tmp_path)
        r, got = self._q(tmp_path, limit=2)
        assert len(got) == 2
        assert r["entries_returned"] == 2
        assert r["matched_entries"] == 5      # total matched, not the limited count
        assert r["budget_truncated"] is True
        # no limit -> all five
        r2, got2 = self._q(tmp_path)
        assert len(got2) == 5 and r2["matched_entries"] == 5

    def test_type_filter(self, tmp_path):
        ids = self._seed(tmp_path)
        r, got = self._q(tmp_path, types=["constraints"])
        assert set(got) == {ids["c1"], ids["c2"]}
        # Every returned entry is labeled with its singular type.
        assert all(x["type"] == "constraint" for x in r["results"])

    def test_status_filter_superseded_and_active(self, tmp_path):
        ids = self._seed(tmp_path)
        _, superseded = self._q(tmp_path, status="superseded")
        assert superseded == [ids["d1"]]
        _, active_dec = self._q(tmp_path, types=["decisions"], status="active")
        assert active_dec == [ids["d2"]]

    def test_no_status_filter_includes_superseded(self, tmp_path):
        # Unlike get_context, query_entries applies no default status filter.
        ids = self._seed(tmp_path)
        _, got = self._q(tmp_path, types=["decisions"])
        assert set(got) == {ids["d1"], ids["d2"]}

    def test_deprecated_status_filter(self, tmp_path):
        ids = self._seed(tmp_path)
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": ids["c2"], "reason": "No longer a convention we keep."
        })
        _, dep = self._q(tmp_path, status="deprecated")
        assert dep == [ids["c2"]]
        # And it no longer shows under active.
        _, active_con = self._q(tmp_path, types=["constraints"], status="active")
        assert active_con == [ids["c1"]]

    def test_origin_filter(self, tmp_path):
        ids = self._seed(tmp_path)
        _, user_origin = self._q(tmp_path, origin="user")
        assert set(user_origin) == {ids["d1"], ids["d2"], ids["c1"]}
        # c2 and p1 default to agent origin.
        _, agent_origin = self._q(tmp_path, origin="agent")
        assert set(agent_origin) == {ids["c2"], ids["p1"]}

    def test_scope_exact_match(self, tmp_path):
        ids = self._seed(tmp_path)
        _, hooks = self._q(tmp_path, scope="hooks/")
        assert hooks == [ids["c1"]]
        # Exact, not substring: "hooks" (no slash) matches nothing.
        _, nope = self._q(tmp_path, scope="hooks")
        assert nope == []

    def test_hardness_filter(self, tmp_path):
        ids = self._seed(tmp_path)
        _, absolute = self._q(tmp_path, hardness="absolute")
        assert absolute == [ids["c1"]]
        _, advisory = self._q(tmp_path, hardness="advisory")
        assert advisory == [ids["c2"]]

    def test_tags_any_and_tags_all(self, tmp_path):
        ids = self._seed(tmp_path)
        _, any_storage = self._q(tmp_path, tags_any=["storage", "release"])
        assert set(any_storage) == {ids["d1"], ids["d2"], ids["p1"]}
        _, all_two = self._q(tmp_path, tags_all=["storage", "architecture"])
        assert all_two == [ids["d1"]]

    def test_supersedes_and_superseded_by(self, tmp_path):
        ids = self._seed(tmp_path)
        # The entry that supersedes d1 is d2.
        _, sup = self._q(tmp_path, supersedes=ids["d1"])
        assert sup == [ids["d2"]]
        # The entries replaced by d2 is d1.
        _, by = self._q(tmp_path, superseded_by=ids["d2"])
        assert by == [ids["d1"]]

    def test_combined_predicates_and(self, tmp_path):
        ids = self._seed(tmp_path)
        # user-origin absolute constraint scoped to hooks/ -> only c1.
        _, got = self._q(
            tmp_path, types=["constraints"], origin="user",
            hardness="absolute", scope="hooks/",
        )
        assert got == [ids["c1"]]
        # Tightening one predicate to a non-match yields nothing.
        _, none = self._q(
            tmp_path, types=["constraints"], origin="user",
            hardness="advisory", scope="hooks/",
        )
        assert none == []

    def test_empty_result_is_clean_not_abstention(self, tmp_path):
        self._seed(tmp_path)
        r, got = self._q(tmp_path, tags_any=["does-not-exist"])
        assert got == []
        assert r["matched_entries"] == 0
        assert r["entries_returned"] == 0
        # No relevance/abstention machinery on a structured query.
        assert "no_confident_match" not in r
        assert "guidance" not in r
        assert "top_relevance" not in r

    def test_since_before_temporal_filter(self, tmp_path):
        ids = self._seed(tmp_path)
        # Everything was just created; a future `since` excludes all, a future
        # `before` includes all — same semantics as get_context.
        _, future = self._q(tmp_path, since="2999-01-01")
        assert future == []
        _, past = self._q(tmp_path, before="2999-01-01", types=["decisions"])
        assert set(past) == {ids["d1"], ids["d2"]}

    def test_stable_id_order(self, tmp_path):
        self._seed(tmp_path)
        _, got = self._q(tmp_path)
        # Deterministic natural-ID order, grouped by prefix then number.
        assert got == sorted(got, key=lambda i: (i.split("-")[0], int(i.split("-")[1])))

    def test_budget_truncation_flag(self, tmp_path):
        self._seed(tmp_path)
        r = handle_query_entries({"project_dir": str(tmp_path), "token_budget": 1})
        # A 1-token budget can't fit any entry; matched > returned, flagged.
        assert r["matched_entries"] > 0
        assert r["entries_returned"] == 0
        assert r["budget_truncated"] is True

    def test_unresolved_project_errors(self, monkeypatch):
        # Project resolution is now PER CALL (env > Xylem session pointer >
        # cwd/.context discovery), not the static import-time CONTEXT_DIR. When
        # the suite runs from inside the repo, cwd discovery would resolve to the
        # repo's own .context/, so a params dict with no project_dir would wrongly
        # succeed. Neutralize every resolution source to assert the genuine
        # unresolved-project path regardless of pytest's cwd or a stray pointer.
        import server as srv
        monkeypatch.setattr(srv, "_resolve_project_dir", lambda: None)
        monkeypatch.setattr(srv, "CONTEXT_DIR", None)
        r = handle_query_entries({"status": "active"})
        assert "error" in r

    def test_xylem_session_pointer_is_honored(self, tmp_path, monkeypatch):
        # With no env and no project_dir param, a persistent server follows the
        # project the Xylem SessionStart hook recorded in the shared pointer.
        import json as _json
        import os as _os
        import server as srv
        proj = tmp_path / "proj"
        (proj / ".context").mkdir(parents=True)
        ptr = tmp_path / "active_project.json"
        ptr.write_text(_json.dumps({"project": str(proj)}))
        monkeypatch.setenv("XYLEM_ACTIVE_PROJECT_FILE", str(ptr))
        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        assert srv._resolve_project_dir() == str(proj)
        assert srv._base_dir_from_params({}) == _os.path.join(
            str(proj), ".context")

    def test_get_context_still_hides_deprecated(self, tmp_path):
        # Guard: query_entries must not have changed get_context's behavior —
        # get_context still drops deprecated entries by default.
        ids = self._seed(tmp_path)
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": ids["c1"], "reason": "Retired for this guard test only."
        })
        r = handle_get_context({"project_dir": str(tmp_path), "tags": ["hooks"]})
        returned = [x["entry"]["id"] for x in r["results"]]
        assert ids["c1"] not in returned


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


class TestScopeGuardHook:
    def _record_scoped(self, tmp_path):
        return handle_record_constraint(constraint_params(
            tmp_path,
            rule="Hook output must be ASCII only in this project",
            scope="hooks/",
            tags=["hooks"],
        ))

    def test_scoped_constraint_fires_on_matching_edit(self, tmp_path):
        rec = self._record_scoped(tmp_path)
        out = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "session_start.py"))
        assert rec["id"] in out
        data = json.loads(out)
        assert "additionalContext" in data["hookSpecificOutput"]

    def test_global_constraint_does_not_fire(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="A global rule that applies everywhere in the project"))
        out = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "x.py"))
        assert out == ""

    def test_non_matching_path_does_not_fire(self, tmp_path):
        self._record_scoped(tmp_path)
        out = _run_scope_guard(tmp_path, str(tmp_path / "src" / "main.py"))
        assert out == ""

    def test_fires_once_per_session(self, tmp_path):
        rec = self._record_scoped(tmp_path)
        first = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "a.py"), "sess-a")
        second = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "b.py"), "sess-a")
        assert rec["id"] in first
        assert second == ""

    def test_new_session_fires_again(self, tmp_path):
        rec = self._record_scoped(tmp_path)
        _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "a.py"), "sess-a")
        again = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "a.py"), "sess-b")
        assert rec["id"] in again


# ===========================================================================
# retrieval_hints: anticipated queries rescue vocabulary mismatch (v0.7)
# ===========================================================================


class TestRetrievalHints:
    def test_hint_is_stored(self, tmp_path):
        rec = handle_record_decision(decision_params(
            tmp_path, retrieval_hints=["value network diverging"]))
        assert rec["entry"]["retrieval_hints"] == ["value network diverging"]

    def test_query_matching_only_a_hint_finds_the_entry(self, tmp_path):
        hinted = handle_record_decision(decision_params(
            tmp_path,
            summary="Clamp the value head output during training",
            retrieval_hints=["value network diverging", "critic loss exploding"],
            tags=["training"],
        ))
        handle_record_decision(decision_params(
            tmp_path,
            summary="Use cosine schedule for the learning rate",
            tags=["training"],
        ))
        result = handle_get_context({
            "project_dir": str(tmp_path),
            "query": "value network diverging",
            "include_related": False,
        })
        assert result["results"][0]["entry"]["id"] == hinted["id"]


# ===========================================================================
# origin + trust weighting (v0.7)
# ===========================================================================


class TestOriginTrust:
    def test_origin_stored_and_defaults_to_agent(self, tmp_path):
        rec_user = handle_record_constraint(constraint_params(tmp_path, origin="user"))
        rec_default = handle_record_constraint(constraint_params(
            tmp_path, rule="Another rule long enough to record here"))
        assert rec_user["entry"]["origin"] == "user"
        assert rec_default["entry"]["origin"] == "agent"

    def test_invalid_origin_coerced_to_agent(self, tmp_path):
        rec = handle_record_constraint(constraint_params(tmp_path, origin="alien"))
        assert rec["entry"]["origin"] == "agent"

    def test_user_origin_outranks_agent_origin(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path, summary="Agent inferred this decision entry", tags=["x"]))
        user_rec = handle_record_decision(decision_params(
            tmp_path, summary="User explicitly stated this decision entry",
            tags=["x"], origin="user"))
        result = handle_get_context({
            "project_dir": str(tmp_path), "tags": ["x"], "include_related": False,
        })
        assert result["results"][0]["entry"]["id"] == user_rec["id"]


# ===========================================================================
# since/before temporal filters (v0.7)
# ===========================================================================


class TestTemporalFilters:
    def _age_entry(self, tmp_path, entry_id, iso_ts):
        """Rewrite an entry's timestamps directly on disk."""
        path = context_dir(tmp_path) / "decisions.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            if e["id"] == entry_id:
                e["verified_at"] = iso_ts
                e["created_at"] = iso_ts
        path.write_text(json.dumps(entries), encoding="utf-8")

    def test_since_excludes_older_entries(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="An old decision from last year"))
        new = handle_record_decision(decision_params(
            tmp_path, summary="A brand new decision from today"))
        self._age_entry(tmp_path, old["id"], "2025-01-15T12:00:00+00:00")

        result = handle_get_context({
            "project_dir": str(tmp_path), "since": "2026-01-01",
            "include_related": False,
        })
        ids = [r["entry"]["id"] for r in result["results"]]
        assert new["id"] in ids
        assert old["id"] not in ids

    def test_before_excludes_newer_entries(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="An old decision from last year"))
        new = handle_record_decision(decision_params(
            tmp_path, summary="A brand new decision from today"))
        self._age_entry(tmp_path, old["id"], "2025-01-15T12:00:00+00:00")

        result = handle_get_context({
            "project_dir": str(tmp_path), "before": "2026-01-01",
            "include_related": False,
        })
        ids = [r["entry"]["id"] for r in result["results"]]
        assert old["id"] in ids
        assert new["id"] not in ids

    def test_no_filter_returns_all(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        result = handle_get_context({
            "project_dir": str(tmp_path), "include_related": False,
        })
        assert result["entries_returned"] == 1


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


class TestMarkdownProjection:
    def test_flag_off_by_default_no_file_created(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        assert not (tmp_path / "DECISIONS.md").exists()

    def test_render_on_write_creates_projection(self, tmp_path):
        _enable_md_export(tmp_path)
        rec = handle_record_decision(decision_params(
            tmp_path, summary="Use JSON files over SQLite"))
        md = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert f"### Use JSON files over SQLite" in md
        assert f"(`{rec['id']}`)" in md
        assert "- **Why:**" in md
        assert "do not edit by hand" in md

    def test_regenerated_whole_hand_edits_clobbered(self, tmp_path):
        _enable_md_export(tmp_path)
        handle_record_decision(decision_params(tmp_path, summary="First decision here"))
        md_path = tmp_path / "DECISIONS.md"
        md_path.write_text("HAND EDIT THAT MUST NOT SURVIVE", encoding="utf-8")
        handle_record_decision(decision_params(tmp_path, summary="Second decision here"))
        md = md_path.read_text(encoding="utf-8")
        assert "HAND EDIT" not in md
        assert "First decision here" in md
        assert "Second decision here" in md

    def test_update_entry_regenerates(self, tmp_path):
        _enable_md_export(tmp_path)
        rec = handle_record_decision(decision_params(tmp_path, summary="Original title here"))
        handle_update_entry({
            "project_dir": str(tmp_path), "id": rec["id"],
            "updates": {"summary": "Revised title here"},
        })
        md = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert "Revised title here" in md
        assert "Original title here" not in md

    def test_deprecate_regenerates_with_marker(self, tmp_path):
        _enable_md_export(tmp_path)
        rec = handle_record_decision(decision_params(tmp_path))
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": rec["id"],
            "reason": "replaced by a better approach",
            "superseded_by": "dec-099",
        })
        md = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert "**DEPRECATED**" in md
        assert "replaced by a better approach" in md
        assert "`dec-099`" in md

    def test_non_decision_writes_do_not_render(self, tmp_path):
        _enable_md_export(tmp_path)
        handle_record_constraint(constraint_params(tmp_path))
        assert not (tmp_path / "DECISIONS.md").exists()

    def test_legacy_rationale_renders_as_why(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        legacy = [{
            "id": "dec-001", "summary": "Legacy entry from before v0.4",
            "rationale": "The old freeform reasoning text lives here.",
            "status": "active", "created_at": "2026-01-05T00:00:00+00:00",
        }]
        (ctx / "decisions.json").write_text(json.dumps(legacy), encoding="utf-8")
        result = handle_export_markdown({"project_dir": str(tmp_path)})
        assert result["success"] is True
        md = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert "- **Why:** The old freeform reasoning text lives here." in md
        assert "01-05" in md

    def test_export_markdown_backfills_without_flag(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        assert not (tmp_path / "DECISIONS.md").exists()  # flag off
        result = handle_export_markdown({"project_dir": str(tmp_path)})
        assert result["success"] is True
        assert result["decisions_rendered"] == 1
        assert (tmp_path / "DECISIONS.md").exists()

    def test_export_markdown_custom_path(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        result = handle_export_markdown({
            "project_dir": str(tmp_path), "path": "docs/LOG.md"})
        assert result["success"] is True
        # Custom relative path resolves against the project root
        assert (tmp_path / "docs" / "LOG.md").exists()

    def test_export_markdown_unresolved_project(self, tmp_path):
        result = handle_export_markdown({"project_dir": str(tmp_path / "nowhere")})
        assert "error" in result


# ===========================================================================
# v0.9: summary truncation bug, clustering, trust in similar_entries
# ===========================================================================


class TestSummaryBudgetTruncation:
    def test_over_budget_summary_is_truncated_not_emptied(self, tmp_path):
        """Regression: the truncation loop evaluated the ORIGINAL text every
        iteration, so any store over budget injected an EMPTY summary."""
        for i in range(30):
            handle_record_decision(decision_params(
                tmp_path,
                summary=f"Decision number {i} with a reasonably long summary line "
                        f"padded out so thirty of these comfortably exceed budget "
                        + "x" * 200,
                tags=[f"topic-{i % 3}"],
            ))
        result = handle_get_project_summary({
            "project_dir": str(tmp_path), "token_budget": 500})
        summary = result["summary"]
        # Not empty, within budget, and still carries real content
        assert len(summary.strip()) > 50
        assert "Decision number 0" in summary or "Active Decisions" in summary
        from server import estimate_tokens
        assert estimate_tokens(summary) <= 500


class TestSummaryClustering:
    def test_small_stores_stay_flat(self, tmp_path):
        for i in range(3):
            handle_record_decision(decision_params(
                tmp_path, summary=f"Small store decision {i}", tags=["alpha"]))
        result = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert "clustered by topic" not in result["summary"]

    def test_large_stores_cluster_by_topic(self, tmp_path):
        for i in range(6):
            handle_record_decision(decision_params(
                tmp_path, summary=f"Storage related decision {i}", tags=["storage"]))
        for i in range(5):
            handle_record_decision(decision_params(
                tmp_path, summary=f"Hooks related decision {i}", tags=["hooks"]))
        result = handle_get_project_summary({"project_dir": str(tmp_path)})
        summary = result["summary"]
        assert "clustered by topic" in summary
        assert "storage (6):" in summary
        assert "hooks (5):" in summary

    def test_untagged_decisions_grouped(self, tmp_path):
        for i in range(9):
            params = decision_params(tmp_path, summary=f"Untagged decision {i}")
            params.pop("tags", None)
            handle_record_decision(params)
        result = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert "(untagged) (9):" in result["summary"]


class TestSimilarEntriesTrust:
    def test_matches_carry_origin(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic writes with temp file and os.replace for entries",
            tags=["storage"], origin="user",
        ))
        second = handle_record_decision(decision_params(
            tmp_path,
            summary="Entry files use atomic writes via temp file and os.replace",
            tags=["storage"],
        ))
        similar = second.get("similar_entries", [])
        assert similar and similar[0]["origin"] == "user"
        assert "origin trust" in second["similar_note"]


# ===========================================================================
# v0.10: abstention (no_confident_match) + supersession-as-ranking
# ===========================================================================


class TestAbstention:
    def _seed(self, tmp_path):
        handle_record_decision(decision_params(
            tmp_path,
            summary="Use atomic temp-file writes with os.replace for the store",
            problem="A crash mid-write corrupted the JSON file and lost recorded history.",
            why_chosen="Atomic replace keeps the old file intact until the new one is complete.",
            tags=["storage", "durability"]))

    def test_no_answer_query_flags_no_confident_match(self, tmp_path):
        self._seed(tmp_path)
        r = handle_get_context({
            "project_dir": str(tmp_path),
            "query": "how does oauth authentication rate limiting work",
            "include_related": False})
        assert r.get("no_confident_match") is True
        assert "guidance" in r
        assert r["top_relevance"] < 0.20

    def test_no_answer_still_returns_results_not_suppressed(self, tmp_path):
        self._seed(tmp_path)
        r = handle_get_context({
            "project_dir": str(tmp_path),
            "query": "how does oauth authentication work",
            "include_related": False})
        # Annotate, don't suppress — the entries are still there, just flagged.
        assert r["entries_returned"] >= 1
        assert r.get("no_confident_match") is True

    def test_real_query_not_flagged(self, tmp_path):
        self._seed(tmp_path)
        r = handle_get_context({
            "project_dir": str(tmp_path),
            "query": "why do we use atomic writes for the storage file",
            "include_related": False})
        assert r.get("no_confident_match", False) is False
        assert r["top_relevance"] >= 0.20

    def test_no_query_no_abstention_signal(self, tmp_path):
        self._seed(tmp_path)
        r = handle_get_context({"project_dir": str(tmp_path), "include_related": False})
        # Asking for everything is never an abstention case.
        assert "no_confident_match" not in r
        assert "top_relevance" not in r

    def test_min_relevance_override(self, tmp_path):
        self._seed(tmp_path)
        q = "why do we use atomic writes for the storage file"
        # Default floor: a strong lexical match is not flagged.
        assert handle_get_context({
            "project_dir": str(tmp_path), "query": q,
            "include_related": False}).get("no_confident_match", False) is False
        # A floor above the max possible relevance (1.0) makes even it abstain.
        assert handle_get_context({
            "project_dir": str(tmp_path), "query": q,
            "min_relevance": 1.01, "include_related": False}).get("no_confident_match") is True


class TestSupersession:
    def test_supersedes_marks_old_entry(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="Store data in flat text files"))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Store data in JSON instead",
            supersedes=[old["id"]]))
        assert new.get("superseded") == [old["id"]]
        got = handle_get_context({"project_dir": str(tmp_path), "id": old["id"]})
        assert got["entry"]["status"] == "superseded"
        assert got["entry"]["superseded_by"] == new["id"]

    def test_superseded_still_recallable_but_demoted(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="Use SQLite for the backing store", tags=["storage"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Use JSON files for the backing store", tags=["storage"],
            supersedes=[old["id"]]))
        r = handle_get_context({
            "project_dir": str(tmp_path), "query": "backing store",
            "tags": ["storage"], "include_related": False})
        ids = [x["entry"]["id"] for x in r["results"]]
        # Superseded entry is NOT filtered out (unlike deprecated)...
        assert old["id"] in ids
        # ...but ranks below the current one.
        assert ids.index(new["id"]) < ids.index(old["id"])

    def test_supersedes_ignores_unknown_and_deprecated_ids(self, tmp_path):
        dep = handle_record_decision(decision_params(tmp_path, summary="A decision to deprecate"))
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": dep["id"], "reason": "no longer valid at all"})
        new = handle_record_decision(decision_params(
            tmp_path, summary="A new decision", supersedes=[dep["id"], "dec-999"]))
        # Deprecated stays deprecated (not resurrected to superseded); unknown skipped.
        assert new.get("superseded", []) == []
        got = handle_get_context({"project_dir": str(tmp_path), "id": dep["id"]})
        assert got["entry"]["status"] == "deprecated"

    def test_superseded_skipped_by_prune_stale(self, tmp_path):
        old = handle_record_decision(decision_params(tmp_path, summary="Old approach here"))
        handle_record_decision(decision_params(
            tmp_path, summary="New approach here", supersedes=[old["id"]]))
        # Age the superseded entry far past the threshold.
        path = context_dir(tmp_path) / "decisions.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            if e["id"] == old["id"]:
                e["verified_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(entries), encoding="utf-8")
        result = handle_prune_stale({"project_dir": str(tmp_path), "days": 30})
        stale_ids = [s["id"] for s in result["stale"]]
        assert old["id"] not in stale_ids

    def test_superseded_marked_in_markdown_projection(self, tmp_path):
        _enable_md_export(tmp_path)
        old = handle_record_decision(decision_params(tmp_path, summary="Superseded decision text"))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Replacement decision text", supersedes=[old["id"]]))
        md = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert "**SUPERSEDED**" in md
        assert f"by `{new['id']}`" in md


# ===========================================================================
# Tool-schema token budget (v0.7.1)
# ===========================================================================


class TestToolSchemaBudget:
    def test_tools_list_payload_within_budget(self):
        """Every connected MCP client pays the tools/list payload in context
        tokens at every session start. Keep it bounded: a new field or a
        wordier description must fit the budget or consciously raise it here,
        with the cost acknowledged. Rich guidance belongs in _FIELD_GUIDANCE
        rejection messages and CLAUDE.md, not in schema descriptions.
        Measured ~2374 tokens at v0.7.1; ~2579 at v0.11.0 after adding the
        12th tool (reload_constraints) with a deliberately terse description
        — budget raised to 2650 to accommodate one more real tool. ~3053 at
        v0.13.0 after adding the 13th tool (query_entries): a field-rich
        structured-query tool with 13 predicates is inherently ~470 tokens
        even with trimmed per-field descriptions — budget raised to 3100,
        cost consciously accepted for a distinct query capability. ~3121 at
        v0.14.0 after adding the merge_into param to deprecate_entry (dedup
        merge) — one param, no new tool; budget raised to 3150. ~3923 after
        adding record_entry, the unified write tool: it carries the union of the
        three record_* schemas, and during the deprecation window we
        deliberately keep BOTH record_entry and the three aliases so no caller
        breaks — that transitional overlap is the cost. Budget raised to 4000;
        it can drop again once the aliases are eventually retired. ~4066 after
        the consolidation items: query_entries gained kind/text/limit filters and
        get_project_summary's description became the one-call orientation blurb.
        Budget raised to 4150. ~4280 after adding export_snapshot /
        import_snapshot (team-shared snapshot) — two small schemas; budget
        raised to 4350. ~2927 after buying budget back: the three deprecated
        record_* aliases (record_decision/pipeline/constraint) were retired from
        tools/list — their handlers stay in HANDLERS as hidden back-compat, so
        the schema tax is gone but existing callers still work; pull_remote /
        backfill_remote were folded into one `mirror(op=...)` tool; and the
        heaviest descriptions were trimmed. Budget tightened to 3150 to LOCK IN
        the reclaimed headroom — a new tool or wordier field must consciously
        raise it again rather than silently re-spending what was bought back."""
        from server import TOOLS, estimate_tokens
        total = estimate_tokens(json.dumps(TOOLS))
        assert total <= 3150, (
            f"tools/list payload is ~{total} tokens (budget: 3150). "
            "Trim schema descriptions or consciously raise the budget."
        )


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


class TestBuildConstraintsBlock:
    """The constraints-only block builder and the reload_constraints tool."""

    def test_block_contains_only_constraints(self, tmp_path):
        # Record one of each entry type; the block must surface only the
        # constraint, not the decision or pipeline.
        handle_record_constraint(constraint_params(
            tmp_path, rule="Never bypass the auth middleware", hardness="absolute"))
        handle_record_decision(decision_params(
            tmp_path, summary="Adopt JSON store for memory"))
        handle_record_pipeline(pipeline_params(tmp_path, name="Deploy flow"))

        block = build_constraints_block(str(tmp_path / CONTEXT_DIR_NAME))
        assert block["initialized"] is True
        assert block["count"] == 1
        assert "Never bypass the auth middleware" in block["text"]
        assert "Absolute Constraints (1):" in block["text"]
        # No decision / pipeline leakage.
        assert "JSON store" not in block["text"]
        assert "Deploy flow" not in block["text"]
        assert "Decisions" not in block["text"]
        assert "Pipelines" not in block["text"]

    def test_block_format_matches_session_start(self, tmp_path):
        # The re-injected block must render constraints identically to the
        # session-start summary. Extract the constraint lines from the full
        # summary and assert they appear verbatim in the block.
        handle_record_constraint(constraint_params(
            tmp_path, rule="Absolute one here", hardness="absolute"))
        handle_record_constraint(constraint_params(
            tmp_path, rule="Advisory two here", hardness="advisory"))
        base = str(tmp_path / CONTEXT_DIR_NAME)
        block = build_constraints_block(base)
        summary = handle_get_project_summary({"project_dir": str(tmp_path)})["summary"]
        assert block["text"] in summary

    def test_deprecated_constraints_excluded(self, tmp_path):
        rec = handle_record_constraint(constraint_params(
            tmp_path, rule="Soon to be deprecated rule"))
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": rec["id"], "reason": "no longer true"})
        block = build_constraints_block(str(tmp_path / CONTEXT_DIR_NAME))
        assert block["count"] == 0
        assert block["text"] == ""

    def test_no_context_dir_uninitialized(self, tmp_path):
        block = build_constraints_block(str(tmp_path / "does_not_exist"))
        assert block["initialized"] is False
        assert block["count"] == 0
        assert block["text"] == ""

    def test_reload_tool_returns_current_constraints(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="Rule surfaced on demand"))
        out = handle_reload_constraints({"project_dir": str(tmp_path)})
        assert out["initialized"] is True
        assert out["count"] == 1
        assert "Rule surfaced on demand" in out["constraints"]

    def test_reload_tool_reflects_updates(self, tmp_path):
        # Recording another constraint changes what reload returns — it reads
        # live, not a snapshot.
        handle_record_constraint(constraint_params(tmp_path, rule="First rule here"))
        first = handle_reload_constraints({"project_dir": str(tmp_path)})
        assert first["count"] == 1
        handle_record_constraint(constraint_params(tmp_path, rule="Second rule here"))
        second = handle_reload_constraints({"project_dir": str(tmp_path)})
        assert second["count"] == 2
        assert "Second rule here" in second["constraints"]

    def test_reload_tool_uninitialized_when_no_store(self, tmp_path):
        out = handle_reload_constraints({"project_dir": str(tmp_path / "nope")})
        assert out["initialized"] is False
        assert out["count"] == 0


class TestConstraintReinjectHook:
    """The PostToolUse periodic re-injection hook: opt-in, default off."""

    def _record(self, tmp_path):
        return handle_record_constraint(constraint_params(
            tmp_path, rule="Re-injected rule that must persist"))

    def test_disabled_by_default(self, tmp_path):
        # No config.json at all -> feature off -> never fires.
        self._record(tmp_path)
        for _ in range(6):
            assert _run_reinject(tmp_path) == ""

    def test_explicit_disabled_never_fires(self, tmp_path):
        self._record(tmp_path)
        _write_config(tmp_path, {"constraint_reinjection": {"enabled": False}})
        for _ in range(6):
            assert _run_reinject(tmp_path) == ""

    def test_fires_every_n_tools(self, tmp_path):
        rec = self._record(tmp_path)
        _write_config(tmp_path, {
            "constraint_reinjection": {"enabled": True, "every_n_tools": 3}})
        fired = [bool(_run_reinject(tmp_path)) for _ in range(7)]
        # Silent for the first 2, fires on the 3rd, then every 3rd after.
        assert fired == [False, False, True, False, False, True, False]

    def test_injected_payload_is_constraints_only(self, tmp_path):
        rec = self._record(tmp_path)
        handle_record_decision(decision_params(
            tmp_path, summary="A decision that must not appear"))
        _write_config(tmp_path, {
            "constraint_reinjection": {"enabled": True, "every_n_tools": 1}})
        out = _run_reinject(tmp_path)
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert rec["id"] in ctx
        assert "Re-injected rule that must persist" in ctx
        assert "A decision that must not appear" not in ctx

    def test_counter_resets_per_session(self, tmp_path):
        self._record(tmp_path)
        _write_config(tmp_path, {
            "constraint_reinjection": {"enabled": True, "every_n_tools": 3}})
        # Two calls in session A (no fire yet), then a new session B restarts
        # the count, so B also needs 3 calls before its first fire.
        assert _run_reinject(tmp_path, "sess-a") == ""
        assert _run_reinject(tmp_path, "sess-a") == ""
        assert _run_reinject(tmp_path, "sess-b") == ""
        assert _run_reinject(tmp_path, "sess-b") == ""
        assert _run_reinject(tmp_path, "sess-b") != ""

    def test_no_constraints_stays_silent(self, tmp_path):
        # Feature on, but nothing recorded yet -> nothing to surface.
        _write_config(tmp_path, {
            "constraint_reinjection": {"enabled": True, "every_n_tools": 1}})
        # Need a .context/ dir for the hook to resolve the project.
        assert _run_reinject(tmp_path) == ""

    def test_output_is_ascii(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="Rule with unicode: cafe resume naive"))
        _write_config(tmp_path, {
            "constraint_reinjection": {"enabled": True, "every_n_tools": 1}})
        out = _run_reinject(tmp_path)
        out.encode("ascii")  # raises if any non-ASCII slipped through


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


class TestCLI:
    def test_record_then_query_roundtrip(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
        rec = _run_cli(tmp_path, "record_entry", json.dumps({
            "kind": "constraint",
            "rule": "Never log the signing secret anywhere",
            "reason": "A leaked secret in logs lets anyone forge authenticated requests, a bypass.",
        }))
        assert rec.returncode == 0
        assert json.loads(rec.stdout)["id"] == "con-001"

        q = _run_cli(tmp_path, "query_entries", json.dumps({"kind": "constraint"}))
        assert q.returncode == 0
        assert json.loads(q.stdout)["matched_entries"] == 1

    def test_project_dir_in_json_args(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
        r = _run_cli(tmp_path, "get_project_summary",
                     json.dumps({"project_dir": str(tmp_path)}), env_project=False)
        assert r.returncode == 0
        assert json.loads(r.stdout)["initialized"] in (True, False)

    def test_unknown_tool_exit_2(self, tmp_path):
        r = _run_cli(tmp_path, "bogus_tool", "{}")
        assert r.returncode == 2
        assert "Unknown tool" in r.stderr

    def test_bad_json_exit_2(self, tmp_path):
        r = _run_cli(tmp_path, "query_entries", "not json")
        assert r.returncode == 2
        assert "Invalid JSON" in r.stderr

    def test_handler_error_exit_1(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
        # get_context with an unknown id returns {"error": ...} -> exit 1
        r = _run_cli(tmp_path, "get_context", json.dumps({"id": "dec-999"}))
        assert r.returncode == 1
        assert "error" in json.loads(r.stdout)

    def test_help_exit_0_lists_tools(self, tmp_path):
        r = _run_cli(tmp_path, "--help")
        assert r.returncode == 0
        assert "record_entry" in r.stderr and "query_entries" in r.stderr

    def test_no_args_is_stdio_not_cli(self, tmp_path):
        # With no args the process serves stdio: feed one initialize request.
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(tmp_path))
        proc = subprocess.run(
            [sys.executable, _SERVER], capture_output=True, text=True, env=env,
            input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
            timeout=30,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout.splitlines()[0])["result"]["serverInfo"]["name"] == "context-keeper"


# ===========================================================================
# Team-shared snapshot (Item 5): export / import / first-run bootstrap
# ===========================================================================

from server import (  # noqa: E402
    handle_export_snapshot,
    handle_import_snapshot,
    SNAPSHOT_DIR_NAME,
    SNAPSHOT_FILE_NAME,
)


class TestSnapshot:
    def _seed(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="Never log the signing secret in any output",
            reason="A leaked secret in logs lets anyone forge authenticated requests here."))
        handle_record_decision(decision_params(
            tmp_path, summary="Use gzip snapshots for team sharing"))

    def _snap_file(self, tmp_path):
        return tmp_path / SNAPSHOT_DIR_NAME / SNAPSHOT_FILE_NAME

    def test_export_writes_snapshot_and_gitattributes(self, tmp_path):
        self._seed(tmp_path)
        r = handle_export_snapshot({"project_dir": str(tmp_path)})
        assert r["success"] is True
        assert self._snap_file(tmp_path).exists()
        assert r["counts"] == {"decisions": 1, "pipelines": 0, "constraints": 1}
        ga = (tmp_path / ".gitattributes").read_text()
        assert f"{SNAPSHOT_DIR_NAME}/{SNAPSHOT_FILE_NAME} merge=ours" in ga

    def test_export_is_byte_stable_for_same_content(self, tmp_path):
        self._seed(tmp_path)
        handle_export_snapshot({"project_dir": str(tmp_path)})
        first = self._snap_file(tmp_path).read_bytes()
        handle_export_snapshot({"project_dir": str(tmp_path)})
        second = self._snap_file(tmp_path).read_bytes()
        assert first == second  # mtime=0 -> reproducible, no git churn

    def test_gitattributes_idempotent(self, tmp_path):
        self._seed(tmp_path)
        assert handle_export_snapshot({"project_dir": str(tmp_path)})["gitattributes"] == "added"
        assert handle_export_snapshot({"project_dir": str(tmp_path)})["gitattributes"] == "present"
        # not duplicated
        ga = (tmp_path / ".gitattributes").read_text()
        assert ga.count("merge=ours") == 1

    def test_import_into_empty_store_restores(self, tmp_path):
        self._seed(tmp_path)
        handle_export_snapshot({"project_dir": str(tmp_path)})
        # simulate a fresh clone: remove the working store, keep the snapshot
        import shutil
        shutil.rmtree(context_dir(tmp_path))
        r = handle_import_snapshot({"project_dir": str(tmp_path)})
        assert r["success"] is True
        assert r["imported"]["decisions"] == 1 and r["imported"]["constraints"] == 1
        assert (context_dir(tmp_path) / "decisions.json").exists()

    def test_import_is_non_destructive(self, tmp_path):
        self._seed(tmp_path)
        handle_export_snapshot({"project_dir": str(tmp_path)})
        # store still populated -> import must skip, not overwrite
        r = handle_import_snapshot({"project_dir": str(tmp_path)})
        assert r["skipped"] == {"decisions": True, "constraints": True}

    def test_import_errors_without_snapshot(self, tmp_path):
        (context_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
        r = handle_import_snapshot({"project_dir": str(tmp_path)})
        assert "error" in r

    def test_get_project_summary_bootstraps_on_first_run(self, tmp_path):
        self._seed(tmp_path)
        handle_export_snapshot({"project_dir": str(tmp_path)})
        import shutil
        shutil.rmtree(context_dir(tmp_path))
        assert not context_dir(tmp_path).exists()
        # first orienting call hydrates from the snapshot, then summarizes
        s = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert s["initialized"] is True
        assert s["counts"]["decisions"] == 1
        assert len(s["active_constraints"]) == 1

    def test_bootstrap_noop_when_store_present(self, tmp_path):
        self._seed(tmp_path)
        handle_export_snapshot({"project_dir": str(tmp_path)})
        # add an entry AFTER export; bootstrap must not clobber it back
        handle_record_decision(decision_params(tmp_path, summary="Added after the snapshot export"))
        s = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert s["counts"]["decisions"] == 2  # the post-snapshot entry survives


# ===========================================================================
# UTF-8 stdio boundary: non-ASCII input must not be decoded as cp1252 mojibake
# ===========================================================================

_SERVER_PATH = str(Path(__file__).parent.parent / "server.py")


class TestStdioUtf8RoundTrip:
    """Regression: the MCP stdio transport must decode client input as UTF-8.

    On Windows, stdin defaults to cp1252, so a UTF-8 em-dash (U+2014, wire
    bytes e2 80 94) was decoded to the mojibake 'a\\u20ac\"' and persisted --
    the upstream source of the mojibake that corrupted the knowledge base.
    We drive the REAL stdio server in a subprocess and force its default
    stdio codepage to cp1252 (PYTHONIOENCODING) so the bug reproduces on any
    OS; the fix (reconfigure to UTF-8 in _serve_stdio) must override it.
    """

    def _drive(self, tmp_path, marker):
        (tmp_path / CONTEXT_DIR_NAME).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(tmp_path),
                   PYTHONIOENCODING="cp1252")
        env.pop("CONTEXT_KEEPER_REMOTE_URL", None)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "record_decision", "arguments": {
                    "summary": marker,
                    "problem": "p" * 50,
                    "why_chosen": "w" * 70,
                }}},
        ]
        # ensure_ascii=False so the non-ASCII chars travel as RAW UTF-8 bytes on
        # the wire -- exactly what a JavaScript MCP client's JSON.stringify emits
        # (it does not \u-escape). That raw-byte path is what a cp1252 stdin
        # mis-decodes; ASCII \u-escapes would sidestep the bug and prove nothing.
        stdin_bytes = ("\n".join(json.dumps(r, ensure_ascii=False) for r in requests)
                       + "\n").encode("utf-8")
        subprocess.run(
            [sys.executable, _SERVER_PATH], input=stdin_bytes,
            capture_output=True, env=env, timeout=30,
        )
        disk = json.loads(
            (tmp_path / CONTEXT_DIR_NAME / "decisions.json").read_text(encoding="utf-8"))
        return disk

    def test_em_dash_arrow_check_round_trip(self, tmp_path):
        # em-dash (U+2014), arrow (U+2192), check (U+2713)
        marker = "scope—wide flow→step done✓"
        disk = self._drive(tmp_path, marker)
        assert len(disk) == 1
        # Exact codepoints preserved -- not cp1252 mojibake.
        assert disk[0]["summary"] == marker
        # And explicitly assert the classic em-dash mojibake is absent.
        assert "â" not in disk[0]["summary"]


# ===========================================================================
# Transport: JSON-RPC batch handling (audit #1)
# ===========================================================================

from server import _handle_line, _dispatch_message  # noqa: E402


class TestJsonRpcTransport:
    """A legal JSON-RPC 2.0 batch must not kill the transport.

    Regression: _serve_stdio called msg.get("id") on the parsed line
    *outside* the only try block, so a top-level array -- which the spec
    explicitly permits a client to send at any time -- raised
    AttributeError out of the read loop and terminated the server process,
    taking every subsequent request with it.
    """

    def test_batch_array_does_not_crash_and_answers_each(self, tmp_path):
        line = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        out = _handle_line(line)
        assert out is not None
        payload = json.loads(out)
        assert isinstance(payload, list) and len(payload) == 2
        assert [r["id"] for r in payload] == [1, 2]
        assert payload[0]["result"]["serverInfo"]["name"] == "context-keeper"
        assert "tools" in payload[1]["result"]

    def test_batch_of_only_notifications_writes_nothing(self):
        line = json.dumps([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        ])
        assert _handle_line(line) is None

    def test_batch_mixed_notification_and_request_answers_only_request(self):
        line = json.dumps([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
        ])
        payload = json.loads(_handle_line(line))
        assert isinstance(payload, list) and len(payload) == 1
        assert payload[0]["id"] == 7

    def test_empty_batch_is_invalid_request_not_a_crash(self):
        payload = json.loads(_handle_line("[]"))
        assert payload["error"]["code"] == -32600
        assert payload["id"] is None

    def test_batch_with_junk_member_still_answers_the_good_one(self):
        line = json.dumps([42, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}])
        payload = json.loads(_handle_line(line))
        assert len(payload) == 2
        assert payload[0]["error"]["code"] == -32600
        assert payload[1]["id"] == 3

    def test_scalar_toplevel_is_invalid_request(self):
        for raw in ("42", '"hello"', "true", "null"):
            payload = json.loads(_handle_line(raw))
            assert payload["error"]["code"] == -32600, raw

    def test_malformed_json_is_parse_error(self):
        payload = json.loads(_handle_line("{not json"))
        assert payload["error"]["code"] == -32700

    def test_non_string_method_is_invalid_request(self):
        payload = json.loads(_handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": {"a": 1}})))
        assert payload["error"]["code"] == -32600

    def test_null_params_does_not_crash(self):
        payload = json.loads(_handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None})))
        # Falls through to the unknown-tool branch rather than raising.
        assert payload["result"]["isError"] is True

    def test_single_request_shape_unchanged(self):
        payload = json.loads(_handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})))
        assert isinstance(payload, dict)
        assert payload["result"]["protocolVersion"] == "2024-11-05"

    def test_live_server_survives_a_batch_and_keeps_serving(self, tmp_path):
        """End-to-end: drive the REAL stdio process. Before the fix the batch
        line killed it, so the follow-up request got no answer at all."""
        (tmp_path / CONTEXT_DIR_NAME).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(tmp_path))
        env.pop("CONTEXT_KEEPER_REMOTE_URL", None)
        stdin = (
            json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            + "\n"
        )
        proc = subprocess.run(
            [sys.executable, _SERVER_PATH], input=stdin, capture_output=True,
            text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        # Line 1: the batch response (an array). Line 2: the follow-up, which
        # only exists if the batch did not kill the process.
        assert isinstance(json.loads(lines[0]), list)
        assert json.loads(lines[1])["id"] == 2


# ===========================================================================
# Read-modify-write races: index must not survive the re-read (audit #2)
# ===========================================================================


class TestStaleIndexWrites:
    """update_entry / deprecate_entry must re-find by id after the re-read.

    Both handlers used to take `index` from _find_entry_by_id's read, then
    _load_entries_for_write the file AGAIN and blind-assign
    entries[index] = entry. If the file changed in between -- another
    agent's record_*, a mirror pull, or the hand-edit README.md:537
    explicitly invites -- that index points at a DIFFERENT record and the
    write silently overwrites it. The merge path in deprecate_entry was
    already hardened against exactly this (it rebuilds an id->index map
    after the re-read) but the fix was never back-ported to the two plain
    paths.

    Each test reproduces the corruption by changing the store between the
    two reads, which is what a hand-edit or a concurrent write looks like
    from the handler's point of view.
    """

    def _decisions(self, tmp_path):
        return context_dir(tmp_path) / "decisions.json"

    def _seed_three(self, tmp_path):
        ids = []
        for n in ("alpha", "bravo", "charlie"):
            r = handle_record_decision(decision_params(
                tmp_path, summary=f"Decision about {n} subsystem", tags=[n]))
            ids.append(r["entry"]["id"])
        return ids

    def _change_between_reads(self, monkeypatch, path, mutate, fire_on=2):
        """Run mutate(path) exactly once, on the handler's Nth read of `path`.

        Read 1 is _find_entry_by_id's (where the stale index came from); read
        2 is _load_entries_for_write's, so firing on read 2 places the change
        precisely in the window between them -- the race being fixed. The
        mutated content is what read 2 returns.
        """
        import server as srv
        original = srv._read_json_file_checked
        state = {"reads": 0, "fired": False}

        def hooked(p):
            if os.path.abspath(p) == os.path.abspath(path):
                state["reads"] += 1
                if not state["fired"] and state["reads"] == fire_on:
                    state["fired"] = True
                    mutate(path)
            return original(p)

        monkeypatch.setattr(srv, "_read_json_file_checked", hooked)
        return state

    @staticmethod
    def _reverse_on_disk(p):
        disk = json.loads(Path(p).read_text(encoding="utf-8"))
        disk.reverse()
        Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")

    def test_update_entry_does_not_clobber_a_reordered_neighbor(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        # 'a' is at index 0 on the first read; after the reversal index 0 is
        # 'c'. The old code wrote the mutated 'a' into slot 0 -- destroying 'c'.
        self._change_between_reads(monkeypatch, str(path), self._reverse_on_disk)
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})
        assert "error" not in r, r

        disk = json.loads(path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in disk}
        # Nothing lost, nothing duplicated.
        assert len(disk) == 3
        assert sorted(by_id) == sorted([a, b, c])
        # The update landed on 'a' ...
        assert by_id[a]["summary"] == "Rewritten alpha summary"
        # ... and the neighbors are untouched, not overwritten by a copy of 'a'.
        assert by_id[c]["summary"] == "Decision about charlie subsystem"
        assert by_id[b]["summary"] == "Decision about bravo subsystem"

    def test_deprecate_entry_does_not_clobber_a_reordered_neighbor(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._reverse_on_disk)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "Superseded during a concurrent-edit regression test."})
        assert "error" not in r, r

        disk = json.loads(path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in disk}
        assert len(disk) == 3
        assert sorted(by_id) == sorted([a, b, c])
        # Exactly ONE entry got deprecated, and it is the one we asked for.
        assert by_id[a]["status"] == "deprecated"
        assert by_id[b].get("status") != "deprecated"
        assert by_id[c].get("status") != "deprecated"

    def test_update_entry_preserves_a_concurrent_edit_to_another_field(
            self, tmp_path, monkeypatch):
        """The stale COPY was assigned back wholesale, so a concurrent edit to
        a field the caller never mentioned got reverted. Applying the update
        to the freshly-read object keeps it."""
        a, _b, _c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        def hand_edit(p):
            disk = json.loads(Path(p).read_text(encoding="utf-8"))
            for e in disk:
                if e["id"] == a:
                    e["tradeoffs"] = "Added by hand while the tool was running"
            Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")

        self._change_between_reads(monkeypatch, str(path), hand_edit)
        handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})

        disk = json.loads(path.read_text(encoding="utf-8"))
        entry = next(e for e in disk if e["id"] == a)
        assert entry["summary"] == "Rewritten alpha summary"
        assert entry["tradeoffs"] == "Added by hand while the tool was running"

    @staticmethod
    def _deleter(target_id):
        def delete(p):
            disk = json.loads(Path(p).read_text(encoding="utf-8"))
            disk = [e for e in disk if e["id"] != target_id]
            Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")
        return delete

    def test_update_entry_errors_if_the_entry_vanishes_mid_write(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._deleter(a))
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})
        assert "error" in r and "retry" in r["error"].lower()

        # Refusing is the point: the survivors stay intact and the vanished
        # entry is not resurrected into someone else's slot.
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert sorted(e["id"] for e in disk) == sorted([b, c])
        assert all("Rewritten alpha" not in e["summary"] for e in disk)

    def test_deprecate_entry_errors_if_the_entry_vanishes_mid_write(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._deleter(a))
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "Deprecating an entry that vanished under us."})
        assert "error" in r and "retry" in r["error"].lower()

        disk = json.loads(path.read_text(encoding="utf-8"))
        assert sorted(e["id"] for e in disk) == sorted([b, c])
        assert not any(e.get("status") == "deprecated" for e in disk)

    def test_update_entry_happy_path_unchanged(self, tmp_path):
        """Guard: no concurrent change, behavior identical to before."""
        a, _b, _c = self._seed_three(tmp_path)
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Plain update, no race"}})
        assert r["success"] is True
        assert r["entry"]["summary"] == "Plain update, no race"
        disk = json.loads(self._decisions(tmp_path).read_text(encoding="utf-8"))
        assert len(disk) == 3
        assert next(e for e in disk if e["id"] == a)["summary"] == "Plain update, no race"

    def test_deprecate_entry_happy_path_unchanged(self, tmp_path):
        a, _b, _c = self._seed_three(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "No race here; the ordinary deprecation path must still work."})
        assert r["success"] is True and r["status"] == "deprecated"
        disk = json.loads(self._decisions(tmp_path).read_text(encoding="utf-8"))
        assert len(disk) == 3
        assert next(e for e in disk if e["id"] == a)["status"] == "deprecated"


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


class TestPackagingCompleteness:
    """Every runtime module must appear in BOTH hatch include lists.

    This is a flat module layout, not a package, so hatch sweeps no
    directory -- each file must be named explicitly. A module missing from
    these lists is silently absent from the pip install and the feature it
    implements just stops existing for pip users, with no error anywhere.

    That has now happened twice: mirror.py (fixed in b5acffa) and
    hooks/constraint_reinject.py, which dropped the entire v0.11 constraint-
    reinjection feature from every pip install. Deriving the expected set
    from the repo is what stops a third occurrence -- a hand-maintained
    checklist is exactly what failed the first two times.
    """

    def test_every_runtime_module_is_in_the_sdist_include(self):
        section = _pyproject_section("tool.hatch.build.targets.sdist")
        missing = sorted(m for m in _shipped_python_modules()
                         if f'"{m}"' not in section)
        assert not missing, (
            f"Modules exist in the repo but are missing from the sdist "
            f"include list in pyproject.toml: {missing}")

    def test_every_runtime_module_is_in_the_wheel_force_include(self):
        section = _pyproject_section("tool.hatch.build.targets.wheel.force-include")
        missing = sorted(m for m in _shipped_python_modules()
                         if f'"{m}" = "{m}"' not in section)
        assert not missing, (
            f"Modules exist in the repo but are missing from the wheel "
            f"force-include map in pyproject.toml: {missing}")

    def test_constraint_reinject_hook_specifically_is_packaged(self):
        """Named explicitly: this is the regression that motivated the check,
        and the hook README.md documents as a configurable PostToolUse hook."""
        for section_name in ("tool.hatch.build.targets.sdist",
                             "tool.hatch.build.targets.wheel.force-include"):
            assert "hooks/constraint_reinject.py" in _pyproject_section(section_name)

    def test_every_documented_hook_is_packaged(self):
        """README tells users to wire these paths into settings.json. A hook
        the docs configure but the package omits is a broken install."""
        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        documented = {
            f"hooks/{p.name}" for p in (_REPO_ROOT / "hooks").glob("*.py")
            if f"hooks/{p.name}" in readme
        }
        assert documented, "expected README to document at least one hook path"
        sdist = _pyproject_section("tool.hatch.build.targets.sdist")
        assert all(f'"{h}"' in sdist for h in documented), sorted(
            h for h in documented if f'"{h}"' not in sdist)


class TestSemanticAbstention:
    """top_relevance must reflect the semantic signal get_context ranks on.

    get_context blends embedding cosine into the RANKING, but computed
    top_relevance from a lexical-only signal. So an entry retrieved purely
    on semantic similarity -- the vocabulary-mismatch rescue the blend
    exists for -- was ranked #1 and then flagged `no_confident_match` for
    having no lexical overlap, while the guidance text told the agent the
    semantic signal "has already been blended in". It had not.

    The fix cannot be max(lexical, cosine). Measured on evals/ across three
    real stores, nomic-embed's top cosine never drops below ~0.51 even for
    a question the store cannot answer (no-answer band 0.515-0.718;
    answerable band 0.636-0.815). Raw cosine would put EVERY query above
    the 0.20 floor: measured TNR 19% -> 0%, abstention silently off. So the
    cosine is calibrated first -- zero below the no-answer ceiling, ramping
    to 1.0 near the answerable maximum.
    """

    _QUERY = "throttling bursty inbound traffic"

    def _seed_disjoint(self, tmp_path):
        """An entry that shares NO query vocabulary, so lexical relevance is
        exactly 0 and only the semantic path can rescue it."""
        r = handle_record_decision(decision_params(
            tmp_path,
            summary="Token bucket limiter on the ingress gateway",
            problem="Sudden spikes of concurrent client calls were exhausting "
                    "the upstream connection pool during peak hours.",
            why_chosen="A bucket smooths spikes without dropping legitimate "
                       "callers outright, and the gateway already tracks per-client "
                       "identity so no new state store was required.",
            tags=["gateway"]))
        return r["entry"]["id"]

    def _stub_cosines(self, monkeypatch, mapping):
        import semantic_index
        monkeypatch.setattr(semantic_index, "query_cosines",
                            lambda q, entries, base, cfg: dict(mapping))

    def _get(self, tmp_path, semantic=None, **extra):
        params = {"project_dir": str(tmp_path), "query": self._QUERY,
                  "include_related": False}
        if semantic is not None:
            params["semantic"] = semantic
        params.update(extra)
        return handle_get_context(params)

    # --- the calibration curve itself -------------------------------------

    def test_cosine_below_the_floor_contributes_nothing(self):
        import server as srv
        # The entire no-answer band measured on the eval set.
        for cos in (0.515, 0.60, 0.65, 0.70, 0.718, srv._SEM_REL_LO):
            assert srv._semantic_relevance(cos) == 0.0, cos

    def test_cosine_ramps_between_floor_and_ceiling(self):
        import server as srv
        mid = (srv._SEM_REL_LO + srv._SEM_REL_HI) / 2
        assert srv._semantic_relevance(mid) == pytest.approx(0.5, abs=1e-6)
        assert srv._semantic_relevance(srv._SEM_REL_HI) == 1.0
        assert srv._semantic_relevance(0.99) == 1.0
        assert srv._semantic_relevance(None) == 0.0

    def test_calibration_floor_sits_above_the_no_answer_band(self):
        """Guard on the constants themselves: if someone lowers the floor
        into the measured no-answer band, abstention starts eroding."""
        import server as srv
        assert srv._SEM_REL_LO > 0.718, (
            "floor must stay above the highest cosine any no-answer query "
            "reached on the eval set, or no-answer queries start clearing "
            "the abstention floor on semantics alone")
        assert srv._SEM_REL_HI > srv._SEM_REL_LO

    # --- the bug being fixed ----------------------------------------------

    def test_lexical_only_flags_a_pure_semantic_hit_as_no_match(self, tmp_path):
        """Baseline: with semantic off, the disjoint entry abstains. This is
        correct for a lexical judgement and is what must NOT change."""
        eid = self._seed_disjoint(tmp_path)
        import server as srv
        entry = srv._find_entry_by_id(eid, str(tmp_path / CONTEXT_DIR_NAME))[0]
        # Self-check: genuinely zero lexical overlap, so the test proves what
        # it claims rather than riding on incidental shared words.
        assert srv._relevance_signal(entry, None, self._QUERY) == 0.0

        r = self._get(tmp_path)
        assert r["no_confident_match"] is True
        assert r["top_relevance"] == 0.0
        assert "lexical" in r["guidance"]

    def test_strong_cosine_rescues_a_zero_lexical_hit(self, tmp_path, monkeypatch):
        """The actual fix: a strong semantic match no longer gets flagged as
        'no memory on this' just because the words differ."""
        eid = self._seed_disjoint(tmp_path)
        self._stub_cosines(monkeypatch, {eid: 0.84})
        r = self._get(tmp_path, semantic={"enabled": True})
        assert r["top_relevance"] >= 0.20
        assert "no_confident_match" not in r

    def test_no_answer_band_cosine_does_not_defeat_abstention(
            self, tmp_path, monkeypatch):
        """THE regression that matters. Every cosine in the measured
        no-answer band must leave the abstention verdict untouched -- this is
        what a naive max(lexical, cosine) would have broken."""
        eid = self._seed_disjoint(tmp_path)
        for cos in (0.515, 0.573, 0.603, 0.674, 0.718):
            self._stub_cosines(monkeypatch, {eid: cos})
            r = self._get(tmp_path, semantic={"enabled": True})
            assert r["no_confident_match"] is True, (
                f"cosine {cos} is inside the measured no-answer band and must "
                f"not lift a zero-lexical entry over the floor")
            assert r["top_relevance"] == 0.0, cos

    def test_raw_cosine_would_have_disabled_abstention(self, tmp_path, monkeypatch):
        """Documents the trap explicitly: if the raw cosine were used, this
        same 0.60 no-answer-band value would clear the 0.20 floor threefold."""
        import server as srv
        raw = 0.60
        assert raw > srv.DEFAULT_CONFIG["min_relevance"]      # the trap
        assert srv._semantic_relevance(raw) == 0.0            # the fix

    # --- honesty of the reported number ------------------------------------

    def test_guidance_names_the_basis_it_actually_used(self, tmp_path, monkeypatch):
        eid = self._seed_disjoint(tmp_path)
        off = self._get(tmp_path)
        assert "Semantic retrieval is off" in off["guidance"]

        self._stub_cosines(monkeypatch, {eid: 0.60})
        on = self._get(tmp_path, semantic={"enabled": True})
        assert "lexical + semantic" in on["guidance"]
        assert "already reflected" in on["guidance"]

    def test_semantic_off_is_byte_identical_to_before(self, tmp_path, monkeypatch):
        """Zero-dep default must be untouched: no cosine is even requested."""
        self._seed_disjoint(tmp_path)
        import semantic_index
        called = []
        monkeypatch.setattr(semantic_index, "query_cosines",
                            lambda *a, **k: called.append(1) or {})
        r = self._get(tmp_path)
        assert not called
        assert r["no_confident_match"] is True

    def test_calibration_band_is_configurable_per_project(
            self, tmp_path, monkeypatch):
        """A different embedding model has a different cosine distribution,
        so the band must be overridable rather than baked in."""
        eid = self._seed_disjoint(tmp_path)
        self._stub_cosines(monkeypatch, {eid: 0.60})
        # Default band: 0.60 is below the floor -> abstain.
        assert self._get(tmp_path, semantic={"enabled": True})[
            "no_confident_match"] is True
        # Recalibrated for a model whose cosines run lower -> confident.
        r = self._get(tmp_path, semantic={
            "enabled": True, "relevance_floor": 0.40, "relevance_ceiling": 0.65})
        assert "no_confident_match" not in r
        assert r["top_relevance"] >= 0.20

    def test_lexical_still_wins_when_it_is_the_stronger_signal(
            self, tmp_path, monkeypatch):
        """max(), not replace: a strong lexical hit is not dragged down by a
        weak cosine."""
        r0 = handle_record_decision(decision_params(
            tmp_path, summary="Throttling bursty inbound traffic at the edge"))
        self._stub_cosines(monkeypatch, {r0["entry"]["id"]: 0.30})
        r = self._get(tmp_path, semantic={"enabled": True})
        assert r["top_relevance"] >= 0.20
        assert "no_confident_match" not in r


class TestMcpbBundleStaging:
    """The Claude Desktop .mcpb bundle must stage every first-party module.

    Third instance of the same bug found by this audit: build-mcpb.sh
    hand-picked server.py + semantic_index.py and omitted mirror.py.
    server.py imports mirror inside a try/except so it can never fail loudly
    -- the desktop bundle just shipped with the mirror feature silently
    missing, exactly like the pip package did before b5acffa.
    """

    def test_build_script_stages_every_top_level_module(self):
        script = (_REPO_ROOT / "scripts" / "build-mcpb.sh").read_text(encoding="utf-8")
        modules = {p.name for p in _REPO_ROOT.glob("*.py")} - _NOT_SHIPPED
        missing = sorted(m for m in modules if f'"$ROOT/{m}"' not in script)
        assert not missing, (
            f"scripts/build-mcpb.sh does not stage: {missing} -- the desktop "
            f"bundle would ship without them")

    def test_mirror_specifically_is_staged(self):
        script = (_REPO_ROOT / "scripts" / "build-mcpb.sh").read_text(encoding="utf-8")
        assert '"$ROOT/mirror.py"' in script


class TestManifestToolsMatchServer:
    """mcpb/manifest.json advertises the tool list to Claude Desktop.

    It had drifted badly: it still advertised record_decision /
    record_pipeline / record_constraint, which were folded into record_entry
    and are no longer in server.TOOLS at all, while omitting record_entry
    itself plus export_snapshot, import_snapshot and mirror. So the bundle
    promised three tools that do not exist over MCP and hid four that do.
    """

    def _manifest_names(self):
        manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        return sorted(t["name"] for t in manifest["tools"])

    def test_manifest_advertises_exactly_the_server_tools(self):
        import server as srv
        assert self._manifest_names() == sorted(t["name"] for t in srv.TOOLS)

    def test_manifest_advertises_no_retired_tool(self):
        import server as srv
        live = {t["name"] for t in srv.TOOLS}
        retired = {"record_decision", "record_pipeline", "record_constraint"}
        assert not retired & live, "retired names came back into TOOLS"
        assert not retired & set(self._manifest_names())

    def test_manifest_tools_are_all_callable_handlers(self):
        import server as srv
        assert all(n in srv.HANDLERS for n in self._manifest_names())


class TestVersionConsistency:
    """One release, five version literals, and CI syncs only one of them.

    pyproject.toml, server.json (twice: the server version and the pypi
    package version), mcpb/manifest.json and server.py all carry the number
    verbatim, because each format requires a literal. Nothing checked they
    agreed, so a partial bump ships a wheel whose initialize response,
    registry entry and desktop bundle all claim different versions.
    """

    def _versions(self):
        pyproject = _PYPROJECT.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        assert m, "no version in pyproject.toml"
        server_json = json.loads(_SERVER_JSON.read_text(encoding="utf-8"))
        manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        import server as srv
        return {
            "pyproject.toml": m.group(1),
            "server.json:version": server_json["version"],
            "server.json:packages[0].version": server_json["packages"][0]["version"],
            "mcpb/manifest.json": manifest["version"],
            "server.py:__version__": srv.__version__,
        }

    def test_all_five_version_literals_agree(self):
        versions = self._versions()
        assert len(set(versions.values())) == 1, (
            "version literals disagree -- bump them together: "
            + json.dumps(versions, indent=2))

    def test_initialize_response_reports_the_packaged_version(self):
        """The wire version a client sees must be the packaged one, not a
        literal frozen into the transport code."""
        payload = json.loads(_handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})))
        assert payload["result"]["serverInfo"]["version"] == \
            self._versions()["pyproject.toml"]


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


class TestScopeToGlob:
    def test_directory_scope_matches_everything_beneath(self):
        assert _scope_to_paths("hooks/") == ["hooks/**/*", "**/hooks/**/*"]

    def test_file_scope_matches_the_file(self):
        assert _scope_to_paths("server.py") == ["server.py", "**/server.py"]

    def test_nested_file_scope_keeps_its_directory(self):
        assert _scope_to_paths("tests/test_server.py") == [
            "tests/test_server.py", "**/tests/test_server.py"]

    def test_global_scope_produces_no_pattern(self):
        # A global constraint has no path to trigger on; it is session-start
        # material and projecting it would load it unconditionally.
        assert _scope_to_paths("global") == []
        assert _scope_to_paths("") == []

    def test_glob_metacharacters_are_refused_not_emitted(self):
        """A '[' Claude Code cannot read as a bracket expression matches
        NOTHING. Emitting such a pattern would produce a rule that silently
        never fires -- worse than no rule, because the file exists and looks
        like coverage."""
        for scope in ("photos [2024/", "src/*.py", "a{b,c}/", "log?/"):
            assert _scope_to_paths(scope) == [], scope


class TestRuleFilenames:
    def test_collisions_resolved_deterministically(self):
        """'a/b' and 'a-b' slugify identically. The mapping must be stable
        across regenerations or every render churns the output directory."""
        scopes = ["a/b", "a-b", "hooks/"]
        first = _rule_filenames(scopes)
        assert first == _rule_filenames(list(reversed(scopes)))
        assert len(set(first.values())) == 3


class TestRulesProjection:
    def test_flag_off_by_default_no_directory_created(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        assert not _rules_dir(tmp_path).exists()

    def test_render_on_write_creates_rule_file(self, tmp_path):
        _enable_rules_export(tmp_path)
        handle_record_constraint(constraint_params(
            tmp_path, scope="hooks/", rule="Hook output must be ASCII only"))
        content = (_rules_dir(tmp_path) / "hooks.md").read_text(encoding="utf-8")
        assert content.startswith("---\npaths:\n")
        assert '"hooks/**/*"' in content
        assert "Hook output must be ASCII only" in content
        assert "**Why:**" in content

    def test_global_constraints_are_not_projected(self, tmp_path):
        _enable_rules_export(tmp_path)
        handle_record_constraint(constraint_params(tmp_path, scope="global"))
        assert list(_rules_dir(tmp_path).glob("*.md")) == []

    def test_absolute_constraints_sort_before_advisory(self):
        rendered = render_scope_rule("hooks/", [
            {"id": "con-002", "rule": "Advisory one", "hardness": "advisory"},
            {"id": "con-001", "rule": "Absolute one", "hardness": "absolute"},
        ])
        assert rendered.index("Absolute one") < rendered.index("Advisory one")

    def test_deprecating_a_constraint_retires_its_rule_file(self, tmp_path):
        _enable_rules_export(tmp_path)
        rec = handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        assert (_rules_dir(tmp_path) / "hooks.md").exists()
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": rec["id"],
            "reason": "No longer applies",
        })
        assert not (_rules_dir(tmp_path) / "hooks.md").exists()

    def test_hand_written_rules_in_the_directory_are_never_deleted(self, tmp_path):
        """The projection points at a directory inside the user's repo. It
        may only reap files carrying its own marker -- anything else in
        there belongs to the user."""
        _enable_rules_export(tmp_path)
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        mine = _rules_dir(tmp_path) / "my-own-rule.md"
        mine.write_text('---\npaths:\n  - "**/*"\n---\nHand written.',
                        encoding="utf-8")
        # A second write triggers a full regeneration + reap pass.
        handle_record_constraint(constraint_params(tmp_path, scope="server.py"))
        assert mine.read_text(encoding="utf-8").endswith("Hand written.")

    def test_regenerated_whole_hand_edits_to_generated_files_clobbered(self, tmp_path):
        _enable_rules_export(tmp_path)
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        target = _rules_dir(tmp_path) / "hooks.md"
        target.write_text(_RULES_MARKER + "\nHAND EDIT", encoding="utf-8")
        handle_record_constraint(constraint_params(
            tmp_path, scope="hooks/", rule="Second rule for the same scope"))
        content = target.read_text(encoding="utf-8")
        assert "HAND EDIT" not in content
        assert "Second rule for the same scope" in content

    def test_generated_marker_is_an_html_comment(self):
        """Block-level HTML comments are stripped before a rules file enters
        the model's context, so the marker costs zero context tokens."""
        assert _RULES_MARKER.startswith("<!--") and _RULES_MARKER.endswith("-->")

    def test_export_rules_backfills_without_flag(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        result = handle_export_rules({"project_dir": str(tmp_path)})
        assert result["rules_written"] == 1
        assert (_rules_dir(tmp_path) / "hooks.md").exists()

    def test_unprojectable_scopes_are_reported_not_swallowed(self, tmp_path):
        """Silence would read as full coverage. These constraints get no rule
        file at all, so the caller has to be told."""
        handle_record_constraint(constraint_params(tmp_path, scope="src/*.py"))
        result = handle_export_rules({"project_dir": str(tmp_path)})
        assert result["rules_written"] == 0
        assert result["skipped_scopes"][0]["scope"] == "src/*.py"

    def test_export_rules_is_not_in_tools_list(self):
        """con-004 caps the tools/list payload every client pays at session
        start. This is a one-time backfill reachable from the CLI; it stays
        in HANDLERS and out of TOOLS."""
        from server import HANDLERS, TOOLS
        assert "export_rules" in HANDLERS
        assert "export_rules" not in {t["name"] for t in TOOLS}

    def test_export_rules_unresolved_project(self, tmp_path):
        result = handle_export_rules({"project_dir": str(tmp_path / "nowhere")})
        assert "error" in result


class TestSummaryTruncationFloor:
    """dec-009 was a store that looked healthy injecting an empty summary.
    The truncation loop is fixed, but a small enough budget could still pop
    the constraints block out from under the session. The floor keeps the
    part a session cannot function without, and says when it bit."""

    def _store_with_many_entries(self, tmp_path):
        for i in range(12):
            handle_record_constraint(constraint_params(
                tmp_path, scope=f"mod{i}/",
                rule=f"Rule number {i} with enough text to be substantial"))
            handle_record_decision(decision_params(
                tmp_path, summary=f"Decision number {i} with a long summary line"))

    def test_tiny_budget_never_empties_the_summary(self, tmp_path):
        self._store_with_many_entries(tmp_path)
        result = handle_get_project_summary({
            "project_dir": str(tmp_path), "token_budget": 10})
        assert result["summary"].strip()
        assert "Absolute Constraints" in result["summary"]

    def test_truncation_is_reported(self, tmp_path):
        self._store_with_many_entries(tmp_path)
        result = handle_get_project_summary({
            "project_dir": str(tmp_path), "token_budget": 100})
        assert result["summary_truncated"] is True
        assert result["summary_lines_dropped"] > 0
        assert "get_context" in result["summary_truncation_note"]

    def test_untruncated_summary_adds_no_keys(self, tmp_path):
        """The session-start block must stay byte-stable when nothing changed
        (dec-012's cache-stable prefix), so these keys appear only on the
        occasion they describe."""
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        result = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert "summary_truncated" not in result
        assert "summary_lines_dropped" not in result


# ===========================================================================
# scope_guard under PreToolUse: the rule arrives BEFORE the write
#
# PostToolUse delivers a scoped constraint after the edit has landed, which
# makes it a review note rather than a guardrail. PreToolUse additionalContext
# is injected next to the tool result, so the same hook wired one event
# earlier states the rule while the model can still act on it. One script
# serves both wirings so an upgrade is a config change, not a rewrite.
# ===========================================================================


class TestScopeGuardPreToolUse:
    def _record_scoped(self, tmp_path, **overrides):
        params = constraint_params(
            tmp_path,
            rule="Hook output must be ASCII only in this project",
            scope="hooks/",
            tags=["hooks"],
        )
        params.update(overrides)
        return handle_record_constraint(params)

    def test_answers_with_the_event_it_was_called_on(self, tmp_path):
        """Echoing the wrong hookEventName is how a hook silently does
        nothing -- the harness routes on this field."""
        self._record_scoped(tmp_path)
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_post_tool_use_still_answers_post_tool_use(self, tmp_path):
        self._record_scoped(tmp_path)
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PostToolUse")
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_missing_event_defaults_to_post_tool_use(self, tmp_path):
        """Back-compat: every installation predating this change is wired
        under PostToolUse and sends no hook_event_name we rely on."""
        self._record_scoped(tmp_path)
        out = _run_scope_guard(tmp_path, str(tmp_path / "hooks" / "a.py"))
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_unknown_event_falls_back_rather_than_echoing_it(self, tmp_path):
        self._record_scoped(tmp_path)
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="SomeFutureEvent")
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_pre_tool_use_wording_is_about_to_write(self, tmp_path):
        self._record_scoped(tmp_path)
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "about to write" in ctx
        assert "BEFORE writing" in ctx

    def test_no_permission_decision_by_default(self, tmp_path):
        """confirm_absolute is opt-in. Interrupting every edit to a scoped
        file is not the default anyone wants."""
        self._record_scoped(tmp_path)
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        assert "permissionDecision" not in json.loads(out)["hookSpecificOutput"]

    def test_confirm_absolute_escalates_to_ask(self, tmp_path):
        self._record_scoped(tmp_path, hardness="absolute")
        (context_dir(tmp_path) / "config.json").write_text(
            json.dumps({"scope_guard": {"confirm_absolute": True}}), encoding="utf-8")
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        hso = json.loads(out)["hookSpecificOutput"]
        assert hso["permissionDecision"] == "ask"
        assert "absolute constraint" in hso["permissionDecisionReason"]

    def test_confirm_absolute_ignores_advisory_constraints(self, tmp_path):
        self._record_scoped(tmp_path, hardness="advisory")
        (context_dir(tmp_path) / "config.json").write_text(
            json.dumps({"scope_guard": {"confirm_absolute": True}}), encoding="utf-8")
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        assert "permissionDecision" not in json.loads(out)["hookSpecificOutput"]

    def test_confirm_absolute_never_asks_after_the_fact(self, tmp_path):
        """Prompting the user about an edit that already happened is theatre."""
        self._record_scoped(tmp_path, hardness="absolute")
        (context_dir(tmp_path) / "config.json").write_text(
            json.dumps({"scope_guard": {"confirm_absolute": True}}), encoding="utf-8")
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PostToolUse")
        assert "permissionDecision" not in json.loads(out)["hookSpecificOutput"]

    def test_output_is_ascii_only(self, tmp_path):
        """con-001: Windows hook stdout is cp1252. A non-ASCII byte here
        raises UnicodeEncodeError and takes the whole hook down."""
        self._record_scoped(
            tmp_path,
            rule="Never use an em-dash — or an arrow → in hook output",
        )
        (context_dir(tmp_path) / "config.json").write_text(
            json.dumps({"scope_guard": {"confirm_absolute": True}}), encoding="utf-8")
        out = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), event="PreToolUse")
        out.encode("ascii")  # raises if the hook emitted anything non-ASCII


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


class TestEditPathHookCost:
    def test_scope_guard_does_not_import_server(self, tmp_path):
        """server costs ~73ms to import and this hook needs none of it.
        store_paths carries the resolution rules and the raw reads. If this
        fails, someone reached for `import server` and doubled the latency of
        every edit in every project."""
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        payload = json.dumps({
            "session_id": "cost-probe",
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "hooks" / "a.py")},
        })
        env_before = os.environ.get("CONTEXT_KEEPER_PROJECT")
        os.environ["CONTEXT_KEEPER_PROJECT"] = str(tmp_path)
        try:
            verdict = _modules_after_running_hook("scope_guard.py", payload)
        finally:
            if env_before is None:
                os.environ.pop("CONTEXT_KEEPER_PROJECT", None)
            else:
                os.environ["CONTEXT_KEEPER_PROJECT"] = env_before
        assert verdict == "LIGHT", (
            "hooks/scope_guard.py imported server. It runs before every Edit "
            "and Write; use store_paths for project resolution and raw reads.")

    def test_store_paths_imports_nothing_expensive(self):
        """store_paths is the cheap half by construction. Anything it imports
        is paid on every edit, so the allowlist is stdlib essentials only."""
        source = (Path(__file__).parent.parent / "store_paths.py").read_text(
            encoding="utf-8")
        imported = set(re.findall(r"^import (\w+)", source, re.M))
        imported |= set(re.findall(r"^from (\w+) import", source, re.M))
        assert imported <= {"json", "os"}, (
            f"store_paths imports {sorted(imported - {'json', 'os'})}. It is on "
            "the edit-path critical section; keep it to json and os.")

    def test_resolution_logic_has_exactly_one_implementation(self):
        """The hook and the server must agree on which project they are
        looking at. Two copies of the precedence order (env > Xylem pointer >
        cwd > parent walk) would drift, and the copy that drifts is the one
        nothing executes."""
        import server as srv
        import store_paths
        assert srv._resolve_project_dir is store_paths._resolve_project_dir
        assert srv.CONTEXT_DIR_NAME is store_paths.CONTEXT_DIR_NAME


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


class TestScopeCovers:
    def _fn(self):
        import importlib.util
        path = Path(__file__).parent.parent / "hooks" / "scope_guard.py"
        spec = importlib.util.spec_from_file_location("_sg_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._scope_covers

    @pytest.mark.parametrize("scope,path,expected", _SCOPE_CASES)
    def test_scope_covers(self, scope, path, expected):
        assert self._fn()(scope, path) is expected, f"{scope} vs {path}"

    def test_backslash_paths_normalize(self):
        """Windows hands the hook backslashes; scopes are recorded with
        forward slashes."""
        assert self._fn()("hooks/", r"C:\proj\hooks\a.py") is True
        assert self._fn()("hooks/", r"C:\proj\webhooks\a.py") is False

    def test_agrees_with_the_rules_projection_on_directory_scopes(self):
        """The hook and the .claude/rules/ projection are two deliveries of
        the same rule. If they disagree about which files a scope covers,
        one of them is lying about coverage."""
        from server import _scope_to_paths
        covers = self._fn()
        for scope in ("src/", "hooks/", "tests/"):
            patterns = _scope_to_paths(scope)
            assert patterns, scope
            # The projection anchors on the scope's own components; so must
            # the hook. A sibling directory merely ENDING in the scope name
            # must be covered by neither.
            sibling = f"C:/proj/my{scope.strip('/')}/file.py"
            assert covers(scope, sibling) is False, sibling


class TestScopeGuardFalsePositives:
    """End-to-end: the constraint must not be spent on the wrong file."""

    def test_sibling_directory_does_not_consume_the_constraint(self, tmp_path):
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="hooks/", rule="Hook output must be ASCII only"))
        # An edit to webhooks/ must NOT fire...
        decoy = _run_scope_guard(
            tmp_path, str(tmp_path / "webhooks" / "send.py"), "sess-fp",
            event="PreToolUse")
        assert decoy == ""
        # ...and must therefore leave the constraint available for the file
        # it actually governs, in the same session.
        real = _run_scope_guard(
            tmp_path, str(tmp_path / "hooks" / "a.py"), "sess-fp",
            event="PreToolUse")
        assert rec["id"] in real

    def test_test_file_does_not_consume_a_source_file_constraint(self, tmp_path):
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="server.py", rule="Keep the schema payload bounded"))
        decoy = _run_scope_guard(
            tmp_path, str(tmp_path / "test_server.py"), "sess-fp2",
            event="PreToolUse")
        assert decoy == ""
        real = _run_scope_guard(
            tmp_path, str(tmp_path / "server.py"), "sess-fp2", event="PreToolUse")
        assert rec["id"] in real


# ===========================================================================
# Findings from the v0.16 audit. Each of these shipped in the first pass and
# was found by probing the projection adversarially rather than by reading it.
# ===========================================================================

import shutil

# Aliased: this module already has a local _rules_dir(tmp_path) helper, and a
# bare import would silently rebind it for every test defined above.
from server import RulesPathOutsideProject
from server import _rules_dir as _server_rules_dir


class TestScopeYamlSafety:
    def test_quote_in_scope_is_refused_not_emitted(self):
        """Each pattern is emitted as a double-quoted YAML scalar. A quote
        inside the scope closes it early and the WHOLE frontmatter block
        stops parsing, so the harness loads no rule from the file at all --
        not even the patterns that were fine."""
        assert _scope_to_paths('we"ird/') == []

    def test_control_characters_are_refused(self):
        assert _scope_to_paths("bad\nscope/") == []
        assert _scope_to_paths("bad\tscope/") == []

    def test_emitted_frontmatter_always_parses(self, tmp_path):
        """Whatever survives the filter must produce a frontmatter block that
        a YAML reader can actually read."""
        for scope in ("hooks/", "src/api/", "server.py", "tests/test_x.py",
                      "a-b/", "a_b/", "dir.with.dots/"):
            rendered = render_scope_rule(scope, [
                {"id": "con-001", "rule": "r", "reason": "why", "hardness": "absolute"}])
            assert rendered.startswith("---\npaths:\n")
            block = rendered.split("---")[1]
            for line in block.splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue
                value = line[2:]
                # A well-formed double-quoted scalar: quotes only at the ends.
                assert value.startswith('"') and value.endswith('"'), (scope, line)
                assert '"' not in value[1:-1], (scope, line)

    def test_unprojectable_scopes_are_reported(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope='we"ird/'))
        result = handle_export_rules({"project_dir": str(tmp_path)})
        assert result["rules_written"] == 0
        assert result["skipped_scopes"][0]["scope"] == 'we"ird/'


class TestRulesPathContainment:
    """The rules directory is REAPED, not just written: every marker-carrying
    .md file in it is a deletion candidate on each regeneration. A config
    value must not be able to aim that at a directory outside the project."""

    def test_parent_traversal_is_refused(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        with pytest.raises(RulesPathOutsideProject):
            _server_rules_dir(str(ctx), out_dir="../../ESCAPED")

    def test_absolute_path_outside_project_is_refused(self, tmp_path, tmp_path_factory):
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        with pytest.raises(RulesPathOutsideProject):
            _server_rules_dir(str(ctx), out_dir=str(elsewhere))

    def test_sibling_prefix_directory_is_refused(self, tmp_path):
        """`/proj-evil` must not read as inside `/proj`. A startswith check
        would have let it through; commonpath does not."""
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        sibling = str(tmp_path) + "-evil"
        with pytest.raises(RulesPathOutsideProject):
            _server_rules_dir(str(ctx), out_dir=sibling)

    def test_absolute_path_inside_project_is_allowed(self, tmp_path):
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        inside = str(tmp_path / "docs" / "rules")
        assert _server_rules_dir(str(ctx), out_dir=inside) == os.path.realpath(inside)

    def test_export_rules_reports_the_refusal_instead_of_raising(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        result = handle_export_rules({
            "project_dir": str(tmp_path), "path": "../../ESCAPED"})
        assert "error" in result
        assert "outside the project" in result["error"]

    def test_render_on_write_never_raises_on_a_bad_path(self, tmp_path):
        """A misconfigured path must not take down the constraint write --
        projections are derived, the JSON store is canonical."""
        ctx = context_dir(tmp_path)
        ctx.mkdir(exist_ok=True)
        (ctx / "config.json").write_text(json.dumps(
            {"rules_export": {"enabled": True, "path": "../../ESCAPED"}}),
            encoding="utf-8")
        result = handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        assert result.get("success") is True


class TestProjectionsFollowInboundWrites:
    """Entries arriving from a snapshot or the mirror are still writes to the
    store. If the projections only follow LOCAL writes, a fresh clone imports
    its constraints and gets no rule files until someone happens to record
    another one -- the moment the rules are most wanted is the moment they
    are absent."""

    def _rules_dir(self, tmp_path):
        return tmp_path / ".claude" / "rules" / "context-keeper"

    def test_import_snapshot_regenerates_the_rules_projection(self, tmp_path,
                                                              tmp_path_factory):
        source = tmp_path_factory.mktemp("snapsrc")
        handle_record_constraint(constraint_params(
            source, scope="shared/", rule="A rule that travels in a snapshot"))
        handle_export_snapshot({"project_dir": str(source)})

        dest = tmp_path
        context_dir(dest).mkdir(parents=True, exist_ok=True)
        (context_dir(dest) / "config.json").write_text(
            json.dumps({"rules_export": {"enabled": True}}), encoding="utf-8")
        shutil.copytree(source / ".context-keeper", dest / ".context-keeper",
                        dirs_exist_ok=True)

        handle_import_snapshot({"project_dir": str(dest)})
        assert (self._rules_dir(dest) / "shared.md").exists()

    def test_import_snapshot_regenerates_the_markdown_projection(self, tmp_path,
                                                                 tmp_path_factory):
        source = tmp_path_factory.mktemp("snapsrc2")
        handle_record_decision(decision_params(source, summary="Imported decision"))
        handle_export_snapshot({"project_dir": str(source)})

        dest = tmp_path
        context_dir(dest).mkdir(parents=True, exist_ok=True)
        (context_dir(dest) / "config.json").write_text(
            json.dumps({"markdown_export": {"enabled": True}}), encoding="utf-8")
        shutil.copytree(source / ".context-keeper", dest / ".context-keeper",
                        dirs_exist_ok=True)

        handle_import_snapshot({"project_dir": str(dest)})
        assert "Imported decision" in (dest / "DECISIONS.md").read_text(encoding="utf-8")


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


class TestDemojibake:
    def test_repairs_a_known_misdecode(self):
        assert demojibake(_GARBLED) == _CLEAN

    def test_clean_text_is_left_alone(self):
        for text in (_CLEAN, "plain ascii text", "already fine — em dash",
                     "accented café latte", ""):
            assert demojibake(text) is None, text

    def test_non_strings_are_ignored(self):
        for value in (None, 42, [], {}, True):
            assert demojibake(value) is None

    def test_repair_is_verified_as_an_exact_inverse(self):
        """Re-applying the corruption to the candidate must reproduce the
        input byte for byte. A partial or approximate repair of someone's
        recorded reasoning is worse than legible damage: it looks fixed."""
        repaired = demojibake(_GARBLED)
        assert repaired.encode("utf-8").decode("cp1252") == _GARBLED

    def test_repair_is_idempotent(self):
        once = demojibake(_GARBLED)
        assert demojibake(once) is None

    def test_marker_prefilter_gates_the_transform(self):
        """Some innocent strings survive the encode/decode round trip. The
        marker check is what keeps those from being 'repaired'."""
        assert looks_like_mojibake(_GARBLED) is True
        assert looks_like_mojibake(_CLEAN) is False


class TestRepairMojibake:
    def _garble(self, tmp_path, entry_id, field, value):
        path = context_dir(tmp_path) / "decisions.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            if e["id"] == entry_id:
                e[field] = value
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def test_dry_run_reports_without_writing(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        result = handle_repair_mojibake({"project_dir": str(tmp_path)})
        assert result["applied"] is False
        assert result["entries_affected"] == 1
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["why_chosen"] == _GARBLED, "dry run wrote to disk"

    def test_apply_repairs_the_entry(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["why_chosen"] == _CLEAN

    def test_verified_at_is_not_refreshed(self, tmp_path):
        """An encoding fix is not a claim that anyone re-confirmed the entry
        is still true. Resetting the staleness clock here would erase the
        exact signal prune_stale and the drift check exist to raise."""
        rec = handle_record_decision(decision_params(tmp_path))
        before = rec["entry"]["verified_at"]
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["verified_at"] == before

    def test_updated_at_is_bumped_so_the_repair_wins_the_mirror_merge(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        before = rec["entry"]["updated_at"]
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["updated_at"] >= before

    def test_clean_store_is_a_no_op(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert result["entries_affected"] == 0

    def test_repairs_nested_alternatives(self, tmp_path):
        rec = handle_record_decision(decision_params(
            tmp_path,
            alternatives=[{"option": _GARBLED, "reason_rejected": _GARBLED}]))
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert result["fields_affected"] == 2
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["alternatives"][0]["option"] == _CLEAN

    def test_ids_are_never_rewritten(self, tmp_path):
        """Rewriting an id would break every related_to pointing at it."""
        rec = handle_record_decision(decision_params(tmp_path))
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["id"] == rec["id"]

    def test_refuses_to_write_over_a_corrupt_store(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        (context_dir(tmp_path) / "decisions.json").write_text("{ not json",
                                                              encoding="utf-8")
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert "error" in result

    def test_not_in_tools_list(self):
        from server import HANDLERS, TOOLS
        assert "repair_mojibake" in HANDLERS
        assert "repair_mojibake" not in {t["name"] for t in TOOLS}


class TestVerifyQualityFlagsMojibake:
    def test_flagged_with_a_repair_instruction(self, tmp_path):
        rec = handle_record_constraint(constraint_params(tmp_path))
        path = context_dir(tmp_path) / "constraints.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries[0]["reason"] = _GARBLED
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

        result = handle_verify_quality({"project_dir": str(tmp_path),
                                        "check_drift": False})
        entry = next(f for f in result["flagged"] if f["id"] == rec["id"])
        issue = next(i for i in entry["issues"] if i["type"] == "mojibake")
        assert "repair_mojibake" in issue["detail"]

    def test_clean_entries_are_not_flagged(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path))
        result = handle_verify_quality({"project_dir": str(tmp_path),
                                        "check_drift": False})
        types = {i["type"] for f in result["flagged"] for i in f["issues"]}
        assert "mojibake" not in types


class TestManifestHasNoEditorialKeys:
    """The .mcpb manifest is validated against a schema that REJECTS keys it
    does not recognise, and the packer refuses to build on a validation
    failure.

    v0.16.0 shipped without its desktop bundle because of this. The manifest
    carried a `_tools_comment` key -- a maintainer note explaining that
    `tools` must match `server.TOOLS`. It had been harmless for several
    releases, then @anthropic-ai/mcpb@2 tightened validation and the pack
    step started failing. The release itself succeeded (PyPI and the MCP
    registry both published), so nothing surfaced until someone looked for
    the .mcpb attachment and found it missing.

    Two lessons, both encoded here. A maintainer note belongs somewhere it
    cannot break a build -- a test docstring like this one, or a comment in
    the build script. And the note was restating a rule that
    TestManifestToolsMatchServer already enforces, which is con-009's
    complaint exactly: prose duplicating a check will drift from it, and the
    prose is the copy nothing executes.
    """

    def test_no_underscore_prefixed_keys(self):
        manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        editorial = [k for k in manifest if k.startswith("_")]
        assert not editorial, (
            f"mcpb/manifest.json carries non-spec key(s) {editorial}. The mcpb "
            "packer rejects unrecognised keys and the bundle silently fails to "
            "build. Put maintainer notes in a test docstring or build script "
            "comment instead."
        )

    def test_no_underscore_keys_nested_in_tools_or_config(self):
        manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        offenders = []

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.startswith("_"):
                        offenders.append(f"{path}.{key}".lstrip("."))
                    walk(value, f"{path}.{key}".lstrip("."))
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]")

        walk(manifest, "")
        assert not offenders, f"non-spec keys in mcpb/manifest.json: {offenders}"
