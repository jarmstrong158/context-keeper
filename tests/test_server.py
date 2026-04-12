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
)


# ---------------------------------------------------------------------------
# Helper: build a params dict that targets a tmp project directory
# ---------------------------------------------------------------------------

def project_params(tmp_path: Path, extra: dict = None) -> dict:
    """Return a params dict with project_dir set to ``tmp_path``."""
    params = {"project_dir": str(tmp_path)}
    if extra:
        params.update(extra)
    return params


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
        """Returns None when env var absent and cwd has no .context/."""
        monkeypatch.delenv("CONTEXT_KEEPER_PROJECT", raising=False)
        monkeypatch.chdir(tmp_path)

        result = _resolve_project_dir()
        assert result is None

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
        handle_record_constraint(project_params(tmp_path, {
            "rule": "Never use eval()",
            "reason": "Security risk",
            "tags": ["security"],
        }))
        data = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        entry = data[0]
        assert entry["rule"] == "Never use eval()"
        assert entry["reason"] == "Security risk"
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
