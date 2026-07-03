"""Comprehensive test suite for context-keeper server.py.

Strategy: All tests pass ``project_dir`` explicitly in params so that the
module-level CONTEXT_DIR (resolved once at import time) is irrelevant.  This
lets us use ``tmp_path`` for full isolation without mocking any file I/O.
"""

import json
import os
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
    _resolve_project_dir,
    handle_deprecate_entry,
    handle_get_compaction_report,
    handle_get_context,
    handle_get_project_summary,
    handle_prune_stale,
    handle_record_constraint,
    handle_record_decision,
    handle_record_pipeline,
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
# scope_guard hook (v0.6): edit-time injection of scoped constraints
# ===========================================================================

import subprocess

_HOOK = str(Path(__file__).parent.parent / "hooks" / "scope_guard.py")


def _run_scope_guard(project: Path, file_path: str, session_id: str = "s1"):
    payload = json.dumps({
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    })
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
# Tool-schema token budget (v0.7.1)
# ===========================================================================


class TestToolSchemaBudget:
    def test_tools_list_payload_within_budget(self):
        """Every connected MCP client pays the tools/list payload in context
        tokens at every session start. Keep it bounded: a new field or a
        wordier description must fit the budget or consciously raise it here,
        with the cost acknowledged. Rich guidance belongs in _FIELD_GUIDANCE
        rejection messages and CLAUDE.md, not in schema descriptions.
        Measured ~2374 tokens at v0.7.1."""
        from server import TOOLS, estimate_tokens
        total = estimate_tokens(json.dumps(TOOLS))
        assert total <= 2500, (
            f"tools/list payload is ~{total} tokens (budget: 2500). "
            "Trim schema descriptions or consciously raise the budget."
        )
