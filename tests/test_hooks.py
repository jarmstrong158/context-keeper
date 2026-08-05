"""The hook surfaces: scope_guard, constraint_reinject, subagent_start, and their cost.

Split out of tests/test_server.py; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




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

    def test_every_surface_gives_the_same_answer(self):
        """con-011-76f8's actual claim, finally executed.

        The constraint says every surface that decides "does this scope cover
        this file" must agree. Nothing tested that, and they did not: for the
        whole case table below, `work_focus._covers` disagreed with the hook on
        `server.py` vs `pkg/server.py` (hook yes, focus no) and on `hooks/` vs a
        bare `hooks` path (hook no, focus yes). Both surfaces claimed to enforce
        the same rule and one of them was wrong on any given case.

        They now share `scope_rules.covers`, so this is a regression test
        against re-forking it rather than a claim about two implementations
        happening to match.
        """
        import scope_rules
        import work_focus
        hook_covers = self._fn()
        for scope, path, expected in _SCOPE_CASES:
            # work_focus is fed repo-relative paths by git; strip the fake root
            # the hook cases carry so each surface sees its own real input.
            rel = path.replace("C:/proj/", "")
            assert hook_covers(scope, path) is expected, f"hook: {scope} vs {path}"
            assert work_focus._covers(scope, rel) is expected, f"work_focus: {scope} vs {rel}"
            assert scope_rules.covers(scope, rel) is expected, f"scope_rules: {scope} vs {rel}"

    def test_ranking_does_not_reintroduce_substring_matching(self):
        """score_entry compares scope-to-scope rather than scope-to-file, which
        is why the original substring test survived there long after the hook
        was fixed: querying scope='hooks/' gave a `webhooks/`-scoped entry the
        identical full boost as the real one."""
        from server import score_entry
        hooks_entry = {"scope": "hooks/", "tags": [], "status": "active"}
        webhooks_entry = {"scope": "webhooks/", "tags": [], "status": "active"}
        assert score_entry(hooks_entry, None, None, "hooks/") > \
            score_entry(webhooks_entry, None, None, "hooks/")

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




class TestSubagentStartHook:
    """SessionStart does not fire for subagents and they do not inherit the
    parent's injected context, so every subagent started with no project
    memory -- on a fan-out, a dozen contributors who never read the rules."""

    def _run(self, project, agent_type="general-purpose"):
        payload = json.dumps({
            "session_id": "s1",
            "hook_event_name": "SubagentStart",
            "agent_type": agent_type,
            "agent_id": "a1",
        })
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(project))
        proc = subprocess.run([sys.executable, _SUBAGENT_HOOK], input=payload,
                              capture_output=True, text=True, env=env, timeout=60)
        return proc.stdout.strip(), proc.returncode

    def test_injects_the_constraints(self, tmp_path):
        rec = handle_record_constraint(constraint_params(
            tmp_path, rule="Hook output must be ASCII only"))
        out, rc = self._run(tmp_path)
        assert rc == 0 and out
        hso = json.loads(out)["hookSpecificOutput"]
        assert hso["hookEventName"] == "SubagentStart"
        assert rec["id"] in hso["additionalContext"]
        assert "Hook output must be ASCII only" in hso["additionalContext"]

    def test_points_at_the_tools_for_everything_else(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path))
        out, _ = self._run(tmp_path)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "get_context" in ctx and "get_project_summary" in ctx

    def test_silent_when_no_constraints_recorded(self, tmp_path):
        """A header with nothing under it is worse than saying nothing."""
        context_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        out, rc = self._run(tmp_path)
        assert rc == 0 and out == ""

    def test_silent_on_an_unresolved_project(self, tmp_path):
        out, rc = self._run(tmp_path / "nowhere")
        assert rc == 0 and out == ""

    def test_malformed_stdin_never_interferes_with_the_spawn(self, tmp_path):
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(tmp_path))
        proc = subprocess.run([sys.executable, _SUBAGENT_HOOK], input="not json",
                              capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 0 and proc.stdout.strip() == ""

    def test_caps_the_constraint_count(self, tmp_path):
        """This fires once per spawned subagent, so a fan-out multiplies it.
        Absolute rules are kept first when the cap bites."""
        for i in range(20):
            handle_record_constraint(constraint_params(
                tmp_path, rule=f"Advisory rule number {i} about things",
                hardness="advisory"))
        keeper = handle_record_constraint(constraint_params(
            tmp_path, rule="An absolute rule that must survive the cap",
            hardness="absolute"))
        out, _ = self._run(tmp_path)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert keeper["id"] in ctx
        assert "not shown" in ctx

    def test_output_is_ascii_only(self, tmp_path):
        handle_record_constraint(constraint_params(
            tmp_path, rule="Never use an em-dash — or an arrow → here"))
        out, _ = self._run(tmp_path)
        out.encode("ascii")




class TestHotPathHooksStayLight:
    """con-010 generalised. scope_guard runs before every edit;
    constraint_reinject's matcher is "" so it runs after EVERY tool call;
    subagent_start runs once per spawned agent and a fan-out multiplies it.
    None of them may import server."""

    @pytest.mark.parametrize("hook_name", [
        "scope_guard.py", "constraint_reinject.py", "subagent_start.py"])
    def test_hook_does_not_import_server(self, hook_name, tmp_path):
        handle_record_constraint(constraint_params(tmp_path, scope="hooks/"))
        payload = json.dumps({
            "session_id": "probe",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "hooks" / "a.py")},
        })
        repo = str(Path(__file__).parent.parent)
        hook = str(Path(repo) / "hooks" / hook_name)
        probe = (
            "import sys, io, json, runpy\n"
            f"sys.path.insert(0, {repo!r})\n"
            f"sys.stdin = io.StringIO({payload!r})\n"
            "try:\n"
            f"    runpy.run_path({hook!r}, run_name='__main__')\n"
            "except SystemExit:\n    pass\n"
            "sys.stderr.write('HEAVY' if 'server' in sys.modules else 'LIGHT')\n"
        )
        env = dict(os.environ, CONTEXT_KEEPER_PROJECT=str(tmp_path))
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              text=True, env=env, timeout=60)
        assert proc.stderr.strip()[-5:] == "LIGHT", (
            f"hooks/{hook_name} imported server; use store_paths (con-010)")

    def test_constraint_formatting_has_one_implementation(self):
        import server as srv
        import store_paths
        assert srv._constraint_lines is store_paths.constraint_lines
