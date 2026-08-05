"""Derived projections: DECISIONS.md and the .claude/rules/ files.

Split out of tests/test_server.py; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




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
