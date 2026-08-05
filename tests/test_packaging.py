"""Shipping guards: schema budget, module manifests, bundle staging, version parity.

Split out of tests/test_server.py; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




# ===========================================================================
# Tool-schema token budget (v0.7.1)
# ===========================================================================


class TestHandlerVisibility:
    """A handler with no schema is unreachable over MCP, and nothing said so.

    Seven of twenty-one handlers are absent from tools/list, every one of them
    on purpose (deprecated aliases, ops folded into `mirror`, CLI-only
    migrations). Being hidden saves every connected client the schema tokens
    (con-004), so it is a real decision -- but "hidden on purpose" and "somebody
    forgot the schema" looked identical until this test made the intent explicit.
    """

    def test_every_handler_is_a_tool_or_explicitly_hidden(self):
        from server import HANDLERS, TOOLS, _HIDDEN_HANDLERS
        exposed = {t["name"] for t in TOOLS}
        unexplained = sorted(set(HANDLERS) - exposed - set(_HIDDEN_HANDLERS))
        assert not unexplained, (
            f"handlers with neither a tool schema nor an entry in "
            f"_HIDDEN_HANDLERS: {unexplained}. Either give it a schema or say "
            f"why it is hidden.")

    def test_no_tool_is_missing_its_handler(self):
        from server import HANDLERS, TOOLS
        assert not sorted({t["name"] for t in TOOLS} - set(HANDLERS))

    def test_hidden_table_has_no_stale_entries(self):
        """A name that is neither a handler nor a tool is a leftover."""
        from server import HANDLERS, _HIDDEN_HANDLERS
        assert not sorted(set(_HIDDEN_HANDLERS) - set(HANDLERS))




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
