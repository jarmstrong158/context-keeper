"""Transport and entry points: JSON-RPC, stdio encoding, the CLI, snapshots.

Split out of tests/test_server.py; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




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
