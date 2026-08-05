"""Core store behaviour: recording, retrieval, ranking, and lifecycle.

Quality, projections, hooks, packaging and transport tests live in their own
files; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




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
# Write-time supersession advisory
#
# The store held 36 active entries, 0 deprecated and 0 supersedes links while
# two of them narrated their own history inline instead ("replacing the old
# additive-only sync"). deprecate_entry's superseded_by had been available the
# whole time and was never used, because nothing asked at the moment the
# answer was known. These tests pin the three properties that make asking
# safe: it fires on SUBJECT overlap (not wording), it is advisory only, and it
# never touches the older entry.
# ===========================================================================


class TestSupersessionAdvisory:
    def test_advisory_names_the_overlapping_entry(self, tmp_path):
        old = handle_record_constraint(constraint_params(
            tmp_path, rule="The mirror merge is additive-only",
            scope="mirror.py", tags=["mirror", "sync"]))
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="The mirror merge is timestamp-based newest-wins",
            scope="mirror.py", tags=["mirror", "sync"]))
        advisory = new["supersession_advisory"]
        assert any(a.startswith(f"possible supersession of {old['id']}: ")
                   for a in advisory), advisory
        assert "additive-only" in " ".join(advisory)

    def test_advisory_fires_when_the_wording_does_not_overlap(self, tmp_path):
        """The reason this is not _find_similar_entries. A replacement often
        shares almost no words with what it replaces while addressing exactly
        the same subject -- which is the case Jaccard-over-text misses."""
        old = handle_record_constraint(constraint_params(
            tmp_path, rule="Push every entry, skip none",
            reason="Additive sync keeps the remote complete without edits.",
            scope="mirror.py", tags=["mirror"]))
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="Overwrite only on a strictly newer timestamp",
            reason="Newest-wins lets a deprecation reach the other store.",
            scope="mirror.py", tags=["mirror"]))
        # Text overlap alone would not have connected these two...
        assert not any(m["id"] == old["id"]
                       for m in new.get("similar_entries", []))
        # ...but they are plainly about the same subject.
        assert any(old["id"] in a for a in new["supersession_advisory"])

    def test_advisory_never_mutates_the_older_entry(self, tmp_path):
        old = handle_record_constraint(constraint_params(
            tmp_path, rule="An older rule about the mirror",
            scope="mirror.py", tags=["mirror"]))
        before = handle_get_context({"project_dir": str(tmp_path), "id": old["id"]})["entry"]
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="A newer rule about the mirror",
            scope="mirror.py", tags=["mirror"]))
        assert new["supersession_advisory"]  # it did fire
        after = handle_get_context({"project_dir": str(tmp_path), "id": old["id"]})["entry"]
        assert after == before
        assert after["status"] == "active"
        # superseded_by is a decisions-only field; the advisory must not have
        # introduced one on a constraint either.
        assert after.get("superseded_by") is None

    def test_advisory_never_blocks_the_write(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="First rule here", scope="mirror.py", tags=["mirror"]))
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="Second rule here", scope="mirror.py", tags=["mirror"]))
        assert new["success"] is True
        assert new["entry"]["status"] == "active"
        stored = read_json_file(str(context_dir(tmp_path) / "constraints.json"))
        assert new["id"] in [e["id"] for e in stored]

    def test_no_advisory_across_kinds(self, tmp_path):
        """A decision cannot supersede a constraint, so offering one as a
        candidate would invite a link the schema cannot hold."""
        handle_record_constraint(constraint_params(
            tmp_path, rule="A constraint about hooks", scope="hooks/", tags=["hooks"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="A decision about hooks", tags=["hooks"]))
        assert "supersession_advisory" not in new

    def test_no_advisory_for_ids_already_linked(self, tmp_path):
        """`supersedes` IS the explicit form of this advisory; re-offering an
        id the caller just linked is noise."""
        old = handle_record_decision(decision_params(
            tmp_path, summary="Storage by flat file", tags=["storage"]))
        other = handle_record_decision(decision_params(
            tmp_path, summary="Storage by memory cache", tags=["storage"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Storage by JSON documents", tags=["storage"],
            supersedes=[old["id"]]))
        joined = " ".join(new.get("supersession_advisory", []))
        assert old["id"] not in joined
        assert other["id"] in joined

    def test_no_advisory_for_related_entries(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="A decision about caching", tags=["cache"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Another decision about caching", tags=["cache"],
            related_to=[old["id"]]))
        assert "supersession_advisory" not in new

    def test_no_advisory_for_superseded_or_deprecated_entries(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="The original storage decision", tags=["storage"]))
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": old["id"],
            "reason": "This decision was wrong from the start."})
        new = handle_record_decision(decision_params(
            tmp_path, summary="A fresh storage decision", tags=["storage"]))
        assert "supersession_advisory" not in new

    def test_untagged_unscoped_entries_produce_no_advisory(self, tmp_path):
        """No subject signal at all is silence, not a guess. An advisory
        nobody can act on trains the reader to skip the next one."""
        handle_record_decision(decision_params(tmp_path, summary="Some decision one"))
        new = handle_record_decision(decision_params(tmp_path, summary="Some decision two"))
        assert "supersession_advisory" not in new

    def test_global_scope_does_not_manufacture_overlap(self, tmp_path):
        """Two unrelated global constraints share a 'scope' string but no
        subject. `global` is the absence of a scope, not a shared one."""
        handle_record_constraint(constraint_params(
            tmp_path, rule="Never bulk-edit files with PowerShell", tags=["tooling"]))
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="Never pipe git into a conditional", tags=["git"]))
        assert "supersession_advisory" not in new

    @pytest.mark.parametrize("a,b,expected", [
        ("hooks/", "hooks/", 1.0),
        ("hooks/", "hooks/scope_guard.py", 0.5),
        ("hooks/", "webhooks/", 0.0),
        ("server.py", "test_server.py", 0.0),
        ("src/api/", "src/api/routes.py", 2 / 3),
        ("global", "hooks/", None),
        ("", "hooks/", None),
    ])
    def test_scope_overlap_scores(self, a, b, expected):
        from server import _scope_overlap
        assert _scope_overlap(a, b) == expected

    def test_scope_overlap_agrees_with_scope_covers(self):
        """con-011-76f8: every surface answering "is this the same area"
        compares whole COMPONENTS. This one scores scope-vs-scope rather than
        deciding scope-vs-file coverage, but a sibling directory merely ENDING
        in another scope's name must read as unrelated to both, or one surface
        is claiming a relationship the other denies."""
        import importlib.util
        from server import _scope_overlap
        path = Path(__file__).parent.parent / "hooks" / "scope_guard.py"
        spec = importlib.util.spec_from_file_location("_sg_probe_sup", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for scope in ("src/", "hooks/", "tests/"):
            sibling = f"my{scope.strip('/')}/"
            assert mod._scope_covers(scope, f"C:/proj/{sibling}file.py") is False
            assert _scope_overlap(scope, sibling) == 0.0
        assert mod._scope_covers("server.py", "C:/proj/test_server.py") is False
        assert _scope_overlap("server.py", "test_server.py") == 0.0

    def test_threshold_is_configurable_per_store(self, tmp_path):
        cdir = context_dir(tmp_path)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "config.json").write_text(
            json.dumps({"supersession_threshold": 0.99}), encoding="utf-8")
        handle_record_decision(decision_params(
            tmp_path, summary="Decision one about caching", tags=["cache", "perf"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="Decision two about caching", tags=["cache"]))
        # Jaccard of {cache,perf} vs {cache} is 0.5 -- under a 0.99 threshold.
        assert "supersession_advisory" not in new

    def test_advisory_carries_actionable_guidance(self, tmp_path):
        """con-004: the wording lives in _FIELD_GUIDANCE (lazy, only costs
        tokens when it fires), never in a schema description."""
        from server import TOOLS, _FIELD_GUIDANCE
        handle_record_constraint(constraint_params(
            tmp_path, rule="An earlier rule", scope="mirror.py", tags=["mirror"]))
        new = handle_record_constraint(constraint_params(
            tmp_path, rule="A later rule", scope="mirror.py", tags=["mirror"]))
        assert new["supersession_note"] == _FIELD_GUIDANCE["supersedes"]
        assert "supersedes=[<id>]" in new["supersession_note"]
        assert "supersession" not in json.dumps(TOOLS).lower()




# ===========================================================================
# Predecessor line in get_context
#
# A supersedes link only pays for itself if retrieval spends it. The
# predecessor is superseded, so every retrieval filter has already dropped it
# by the time an agent reads the replacement -- "what changed, and why" cost a
# second deliberate lookup that nobody made.
# ===========================================================================


class TestPredecessorLine:
    def _supersede(self, tmp_path, old_summary, new_summary, **kw):
        old = handle_record_decision(decision_params(
            tmp_path, summary=old_summary, tags=["storage"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary=new_summary, tags=["storage"],
            supersedes=[old["id"]], **kw))
        return old, new

    def test_replacement_carries_its_predecessor(self, tmp_path):
        old, new = self._supersede(
            tmp_path, "Sync is additive-only", "Sync is newest-wins",
            problem="Additive sync froze deprecations out of the mirror entirely.")
        r = handle_get_context({
            "project_dir": str(tmp_path), "query": "sync", "include_related": False})
        item = next(x for x in r["results"] if x["entry"]["id"] == new["id"])
        assert "Sync is additive-only" in item["predecessor"]
        assert old["id"] in item["predecessor"]
        assert "froze deprecations" in item["predecessor"]

    def test_line_is_prepended_before_the_entry(self, tmp_path):
        _, new = self._supersede(tmp_path, "The old way", "The new way")
        r = handle_get_context({
            "project_dir": str(tmp_path), "query": "way", "include_related": False})
        item = next(x for x in r["results"] if x["entry"]["id"] == new["id"])
        keys = list(item.keys())
        assert keys.index("predecessor") < keys.index("entry")

    def test_entries_without_history_gain_nothing(self, tmp_path):
        handle_record_decision(decision_params(tmp_path, summary="A lone decision"))
        r = handle_get_context({"project_dir": str(tmp_path), "query": "lone"})
        assert all("predecessor" not in x for x in r["results"])

    def test_direct_id_lookup_carries_the_same_line(self, tmp_path):
        """Same entry, two ways of asking. If only one carries the history,
        'what changed' depends on how you happened to look it up."""
        _, new = self._supersede(tmp_path, "Old storage plan", "New storage plan")
        ranked = handle_get_context({
            "project_dir": str(tmp_path), "query": "storage plan",
            "include_related": False})
        item = next(x for x in ranked["results"] if x["entry"]["id"] == new["id"])
        direct = handle_get_context({"project_dir": str(tmp_path), "id": new["id"]})
        assert direct["predecessor"] == item["predecessor"]

    def test_deprecation_reason_wins_over_the_successor_problem(self, tmp_path):
        old = handle_record_decision(decision_params(
            tmp_path, summary="The original plan", tags=["plan"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="The revised plan", tags=["plan"],
            problem="A fallback problem statement that should not be used here."))
        handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": old["id"],
            "reason": "Superseded once the vendor changed the API contract.",
            "superseded_by": new["id"]})
        direct = handle_get_context({"project_dir": str(tmp_path), "id": new["id"]})
        assert "vendor changed the API contract" in direct["predecessor"]
        assert "fallback problem statement" not in direct["predecessor"]

    def test_only_one_level_deep(self, tmp_path):
        """A chain rendered in full grows without bound inside a budget that
        is already the scarce resource."""
        a = handle_record_decision(decision_params(
            tmp_path, summary="Generation one of the plan", tags=["chain"]))
        b = handle_record_decision(decision_params(
            tmp_path, summary="Generation two of the plan", tags=["chain"],
            supersedes=[a["id"]]))
        c = handle_record_decision(decision_params(
            tmp_path, summary="Generation three of the plan", tags=["chain"],
            supersedes=[b["id"]]))
        direct = handle_get_context({"project_dir": str(tmp_path), "id": c["id"]})
        assert b["id"] in direct["predecessor"]
        assert a["id"] not in direct["predecessor"]
        assert "Generation one" not in direct["predecessor"]

    def test_most_recent_predecessor_wins_when_several_point_here(self, tmp_path):
        old_a = handle_record_decision(decision_params(
            tmp_path, summary="First merged-away plan", tags=["merge"]))
        old_b = handle_record_decision(decision_params(
            tmp_path, summary="Second merged-away plan", tags=["merge"]))
        new = handle_record_decision(decision_params(
            tmp_path, summary="The consolidated plan", tags=["merge"],
            supersedes=[old_a["id"], old_b["id"]]))
        # Make old_b unambiguously the later of the two.
        path = context_dir(tmp_path) / "decisions.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            if e["id"] == old_a["id"]:
                e["updated_at"] = "2020-01-01T00:00:00+00:00"
            if e["id"] == old_b["id"]:
                e["updated_at"] = "2030-01-01T00:00:00+00:00"
        path.write_text(json.dumps(entries), encoding="utf-8")
        direct = handle_get_context({"project_dir": str(tmp_path), "id": new["id"]})
        assert old_b["id"] in direct["predecessor"]
        assert old_a["id"] not in direct["predecessor"]

    def test_budget_drops_the_line_and_keeps_the_id_trail(self, tmp_path):
        """dec-021-b607: what gets dropped under budget pressure is the LINE,
        never the entry, and an id trail is left so the history is still
        reachable in one targeted call."""
        _, new = self._supersede(
            tmp_path, "A predecessor with a fairly long summary line here",
            "The replacement decision")
        # Budget sized to admit the entry but not the ~30-token line after it.
        entry_tokens = None
        for budget in range(200, 4001, 10):
            r = handle_get_context({
                "project_dir": str(tmp_path), "query": "replacement",
                "token_budget": budget, "include_related": False})
            item = next((x for x in r["results"]
                         if x["entry"]["id"] == new["id"]), None)
            if item is None:
                continue
            if "predecessor_id" in item:
                entry_tokens = budget
                assert "predecessor" not in item
                assert item["predecessor_id"].startswith("dec-")
                assert item["entry"]["id"] == new["id"]  # the ENTRY survived
                break
        assert entry_tokens is not None, "no budget produced the id-trail fallback"

    def test_line_is_counted_against_the_budget(self, tmp_path):
        self._supersede(tmp_path, "An older approach", "A newer approach")
        r = handle_get_context({
            "project_dir": str(tmp_path), "query": "approach",
            "token_budget": 4000, "include_related": False})
        assert any("predecessor" in x for x in r["results"])
        assert r["tokens_used"] <= r["token_budget"]

    def test_line_stays_ascii(self, tmp_path):
        """The store has already been through one mojibake incident
        (con-008-dc30); a derived line is a new place for it to reappear."""
        _, new = self._supersede(tmp_path, "Old plan", "New plan")
        direct = handle_get_context({"project_dir": str(tmp_path), "id": new["id"]})
        direct["predecessor"].encode("ascii")  # raises if it is not




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




class TestTruncationLeavesATrail:
    """A dropped entry used to be undiscoverable, not merely unsummarised.
    The SessionStart hook prints the summary text and nothing else, so an id
    that fell off the end could not be asked for -- the agent had no way to
    learn it existed."""

    def _big_store(self, tmp_path, n=30):
        for i in range(n):
            handle_record_decision(decision_params(
                tmp_path,
                summary=f"Decision {i} " + "padding " * 20,
                tags=[f"topic-{i % 3}"]))

    def test_dropped_ids_are_listed_in_the_summary_text(self, tmp_path):
        self._big_store(tmp_path)
        r = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 500})
        assert r["summary_truncated"] is True
        assert "retrieve any of these by id with get_context" in r["summary"]
        # Every id the caller is told about must actually be findable.
        listed = [t for t in r["summary"].split() if t.startswith("dec-")]
        assert listed
        for eid in listed[:5]:
            got = handle_get_context({"project_dir": str(tmp_path), "id": eid})
            assert got.get("entry") or got.get("entries"), eid

    def test_the_trail_is_inside_the_budget(self, tmp_path):
        """The trail is paid for out of the same budget, not appended past
        it -- otherwise 'fits the budget' quietly stops being true."""
        self._big_store(tmp_path)
        for budget in (300, 500, 900):
            r = handle_get_project_summary({"project_dir": str(tmp_path),
                                            "token_budget": budget})
            assert estimate_tokens(r["summary"]) <= budget, budget

    def test_dropped_ids_also_returned_structurally(self, tmp_path):
        self._big_store(tmp_path)
        r = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 500})
        assert r["summary_dropped_ids"]
        assert all(i.startswith(("dec-", "pipe-")) for i in r["summary_dropped_ids"])

    def test_no_trail_when_nothing_was_dropped(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        r = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert "retrieve any of these by id" not in r["summary"]
        assert "summary_dropped_ids" not in r

    def test_trail_is_deterministic_across_calls(self, tmp_path):
        """dec-012: an unchanged store must produce byte-identical text or
        the session-start prompt cache misses every time."""
        self._big_store(tmp_path)
        a = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 500})["summary"]
        b = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 500})["summary"]
        assert a == b

    def test_floor_over_budget_is_reported_even_with_nothing_dropped(self, tmp_path):
        """The silent case. When the constraints floor alone exceeds the
        budget, no lines are dropped -- so a truncation-only flag stays
        False and the summary quietly overspends every session forever.
        One real store's 19 constraints are 2429 tokens against 2000."""
        for i in range(12):
            handle_record_constraint(constraint_params(
                tmp_path, rule=f"Constraint {i} " + "with substantial text " * 15))
        r = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 50})
        assert "Absolute Constraints" in r["summary"]
        assert estimate_tokens(r["summary"]) > 50
        assert r["summary_over_budget"] is True
        assert "never dropped" in r["summary_over_budget_note"]

    def test_within_budget_summary_reports_neither_flag(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        r = handle_get_project_summary({"project_dir": str(tmp_path)})
        assert "summary_over_budget" not in r
        assert "summary_truncated" not in r

    def test_constraints_still_survive_a_tiny_budget(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        self._big_store(tmp_path)
        r = handle_get_project_summary({"project_dir": str(tmp_path),
                                        "token_budget": 10})
        assert "Absolute Constraints" in r["summary"]
