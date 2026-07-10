"""End-to-end tests for the two-way mirror (mirror.py + server wiring).

Strategy: stand up a tiny in-process HTTP server that speaks the same
JSON-RPC / Streamable-HTTP MCP dialect as context-keeper-remote
(import_entries upsert-skip, query_entries), point
CONTEXT_KEEPER_REMOTE_URL at its /mcp/<token> URL, and exercise the real
code paths -- no network, no mocking of urllib. Everything is stdlib.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import mirror  # noqa: E402
import server  # noqa: E402
from server import (  # noqa: E402
    _leading_int,
    handle_backfill_remote,
    handle_deprecate_entry,
    handle_pull_remote,
    handle_record_constraint,
    handle_record_decision,
    handle_record_pipeline,
    handle_update_entry,
    next_id,
    read_json_file,
)

TOKEN = "s3cr3t-token"


# ---------------------------------------------------------------------------
# Fake remote: a stateless MCP server implementing the two tools we use.
# Mirrors context-keeper-remote: composite (project, id) key, import skips
# existing ids, query_entries returns hydrated rows with a `payload` blob.
# ---------------------------------------------------------------------------


class FakeRemote:
    def __init__(self, token=TOKEN):
        self.token = token
        self.rows = {}  # (project, id) -> row dict
        self.tool_calls = []

    # --- tool implementations ---
    def import_entries(self, args):
        project, kind, entries = args["project"], args["kind"], args["entries"]
        imported, skipped = [], []
        for e in entries:
            eid = e.get("id")
            key = (project, eid)
            if key in self.rows:
                skipped.append({"id": eid, "reason": "id already exists"})
                continue
            payload = {k: v for k, v in e.items()
                       if k not in ("project", "status", "id", "kind", "superseded_by")}
            self.rows[key] = {
                "id": eid, "kind": kind, "project": project,
                "status": e.get("status", "active"),
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at") or e.get("created_at"),
                "superseded_by": e.get("superseded_by"),
                "payload": payload,
            }
            imported.append(eid)
        return {"project": project, "kind": kind, "total": len(entries),
                "imported_count": len(imported), "skipped_count": len(skipped),
                "imported": imported, "skipped": skipped}

    def query_entries(self, args):
        project = args["project"]
        results = [r for (p, _i), r in self.rows.items() if p == project]
        return {"project": project, "count": len(results),
                "matched": len(results), "results": results}

    def dispatch(self, name, args):
        self.tool_calls.append(name)
        if name == "import_entries":
            return self.import_entries(args)
        if name == "query_entries":
            return self.query_entries(args)
        raise ValueError(f"unknown tool {name}")

    # convenience for assertions
    def ids(self, project):
        return {i for (p, i) in self.rows if p == project}


def make_handler(remote):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _rpc(self, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # The token is the last path segment: /mcp/<token>. Bad token -> 404.
            m = urlparse(self.path).path.rstrip("/").rsplit("/", 1)
            if len(m) != 2 or m[0] != "/mcp" or m[1] != remote.token:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            rid = req.get("id")
            params = req.get("params") or {}
            try:
                result = remote.dispatch(params.get("name"), params.get("arguments") or {})
                self._rpc({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "structuredContent": result, "isError": False}})
            except Exception as e:
                self._rpc({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True}})

    return Handler


@pytest.fixture
def remote(monkeypatch):
    store = FakeRemote()
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(store))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", f"http://127.0.0.1:{port}/mcp/{TOKEN}")
    store._port = port
    try:
        yield store
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, name: str) -> str:
    ctx = tmp_path / ".context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "config.json").write_text(json.dumps({"project_name": name}), encoding="utf-8")
    return str(tmp_path)


_LONG = "x" * 70  # clears every v0.4 min-length field


def _record_all(project_dir):
    d1 = handle_record_decision({
        "project_dir": project_dir, "summary": "Use JSON store",
        "problem": _LONG, "why_chosen": _LONG, "tags": ["arch"]})
    d2 = handle_record_decision({
        "project_dir": project_dir, "summary": "Fail soft on mirror",
        "problem": _LONG, "why_chosen": _LONG, "tags": ["mirror"]})
    c1 = handle_record_constraint({
        "project_dir": project_dir, "rule": "Never block local", "reason": _LONG})
    p1 = handle_record_pipeline({
        "project_dir": project_dir, "name": "Deploy flow",
        "purpose": _LONG, "steps": [{"order": 1, "action": "build"}]})
    return [d1["id"], d2["id"], c1["id"], p1["id"]]


# ===========================================================================
# Mirror OUT
# ===========================================================================


class TestMirrorOut:
    def test_writes_reach_remote(self, tmp_path, remote):
        proj = _project(tmp_path, "roundtrip")
        ids = _record_all(proj)
        assert remote.ids("roundtrip") == set(ids)
        assert len(ids) == 4

    def test_local_write_succeeds_even_if_remote_down(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", "http://127.0.0.1:1/mcp/x")
        proj = _project(tmp_path, "offline")
        res = handle_record_decision({
            "project_dir": proj, "summary": "Works offline",
            "problem": _LONG, "why_chosen": _LONG})
        assert res["success"] is True
        disk = read_json_file(str(tmp_path / ".context" / "decisions.json"))
        assert len(disk) == 1
        queue = json.loads((tmp_path / ".context" / mirror.QUEUE_NAME).read_text())
        assert any(r["entry"]["id"] == res["id"] for r in queue)

    def test_queue_flushes_on_next_success(self, tmp_path, remote):
        proj = _project(tmp_path, "flushproj")
        stale = {"kind": "decision", "entry": {
            "id": "dec-099", "summary": "queued earlier",
            "created_at": server.now_iso(), "verified_at": server.now_iso(),
            "status": "active"}}
        (tmp_path / ".context" / mirror.QUEUE_NAME).write_text(
            json.dumps([stale]), encoding="utf-8")
        res = handle_record_decision({
            "project_dir": proj, "summary": "new entry",
            "problem": _LONG, "why_chosen": _LONG})
        assert "dec-099" in remote.ids("flushproj")
        assert res["id"] in remote.ids("flushproj")
        assert not (tmp_path / ".context" / mirror.QUEUE_NAME).exists()

    def test_wrong_token_fails_soft_and_queues(self, tmp_path, remote, monkeypatch):
        # Point at the right server but a wrong token path -> 404 -> queue.
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL",
                           f"http://127.0.0.1:{remote._port}/mcp/WRONG")
        proj = _project(tmp_path, "badtoken")
        res = handle_record_decision({
            "project_dir": proj, "summary": "will 404",
            "problem": _LONG, "why_chosen": _LONG})
        assert res["success"] is True
        assert remote.ids("badtoken") == set()
        queue = json.loads((tmp_path / ".context" / mirror.QUEUE_NAME).read_text())
        assert len(queue) == 1

    def test_update_before_first_sync_propagates(self, tmp_path, remote, monkeypatch):
        # Create + update while OFFLINE, then flush: the remote sees the entry
        # once, with the UPDATED content (queue dedupes to latest state).
        proj = _project(tmp_path, "offedit")
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", "http://127.0.0.1:1/mcp/x")
        d = handle_record_decision({
            "project_dir": proj, "summary": "first",
            "problem": _LONG, "why_chosen": _LONG})
        handle_update_entry({"project_dir": proj, "id": d["id"],
                             "updates": {"summary": "edited while offline"}})
        # Back online: flush the queue.
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL",
                           f"http://127.0.0.1:{remote._port}/mcp/{TOKEN}")
        mirror.flush_queue(str(tmp_path / ".context"))
        row = remote.rows[("offedit", d["id"])]
        assert row["payload"]["summary"] == "edited while offline"

    def test_post_sync_edits_are_additive_only(self, tmp_path, remote):
        # Once an id is on the remote, import_entries SKIPS it, so an edit or
        # deprecation does not overwrite the remote copy (additive-only, con-006).
        proj = _project(tmp_path, "additive")
        d = handle_record_decision({
            "project_dir": proj, "summary": "original",
            "problem": _LONG, "why_chosen": _LONG})
        handle_deprecate_entry({"project_dir": proj, "id": d["id"], "reason": "obsolete now"})
        row = remote.rows[("additive", d["id"])]
        assert row["status"] == "active"  # deprecation did NOT overwrite remote
        assert row["payload"]["summary"] == "original"


# ===========================================================================
# Mirror IN (pull) + full round-trip count verification
# ===========================================================================


class TestPullRoundTrip:
    def test_counts_match_after_pull(self, tmp_path, remote):
        src = _project(tmp_path / "src", "shared")
        ids = _record_all(src)
        assert remote.ids("shared") == set(ids)

        dst = _project(tmp_path / "dst", "shared")
        res = handle_pull_remote({"project_dir": dst})
        assert res["pulled"] == 4

        dst_ctx = tmp_path / "dst" / ".context"
        got = (read_json_file(str(dst_ctx / "decisions.json"))
               + read_json_file(str(dst_ctx / "constraints.json"))
               + read_json_file(str(dst_ctx / "pipelines.json")))
        assert {e["id"] for e in got} == set(ids)

    def test_pulled_entry_reconstructs_fields(self, tmp_path, remote):
        src = _project(tmp_path / "src", "fields")
        d = handle_record_decision({
            "project_dir": src, "summary": "rich entry",
            "problem": _LONG, "why_chosen": _LONG, "tags": ["a", "b"]})
        dst = _project(tmp_path / "dst", "fields")
        handle_pull_remote({"project_dir": dst})
        got = read_json_file(str(tmp_path / "dst" / ".context" / "decisions.json"))[0]
        assert got["id"] == d["id"]
        assert got["summary"] == "rich entry"
        assert got["tags"] == ["a", "b"]
        assert got["status"] == "active"

    def test_pull_is_additive_never_overwrites(self, tmp_path, remote):
        src = _project(tmp_path / "src", "noover")
        d = handle_record_decision({
            "project_dir": src, "summary": "remote version",
            "problem": _LONG, "why_chosen": _LONG})
        dst = _project(tmp_path / "dst", "noover")
        local_entry = dict(d["entry"])
        local_entry["summary"] = "LOCAL WINS"
        (tmp_path / "dst" / ".context" / "decisions.json").write_text(
            json.dumps([local_entry]), encoding="utf-8")
        res = handle_pull_remote({"project_dir": dst})
        assert res["pulled"] == 0
        assert res["skipped"] == 1
        disk = read_json_file(str(tmp_path / "dst" / ".context" / "decisions.json"))
        assert len(disk) == 1
        assert disk[0]["summary"] == "LOCAL WINS"

    def test_second_pull_is_noop_via_watermark(self, tmp_path, remote):
        src = _project(tmp_path / "src", "wm")
        _record_all(src)
        dst = _project(tmp_path / "dst", "wm")
        assert handle_pull_remote({"project_dir": dst})["pulled"] == 4
        assert handle_pull_remote({"project_dir": dst})["pulled"] == 0

    def test_backfill_pushes_everything(self, tmp_path, remote, monkeypatch):
        proj = _project(tmp_path, "backfillproj")
        # Record while the remote is unreachable so nothing auto-mirrors.
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", "http://127.0.0.1:1/mcp/x")
        ids = _record_all(proj)
        assert remote.ids("backfillproj") == set()
        # Point back at the live remote and backfill.
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL",
                           f"http://127.0.0.1:{remote._port}/mcp/{TOKEN}")
        res = handle_backfill_remote({"project_dir": proj})
        assert res["backfilled"] == 4
        assert remote.ids("backfillproj") == set(ids)


# ===========================================================================
# Disabled state (no env var) -- pure no-op
# ===========================================================================


class TestDisabled:
    def test_no_url_is_noop(self, tmp_path):
        proj = _project(tmp_path, "nomirror")
        res = handle_record_decision({
            "project_dir": proj, "summary": "no mirror",
            "problem": _LONG, "why_chosen": _LONG})
        assert res["success"] is True
        assert res["id"] == "dec-001"  # bare id, no suffix when mirror off
        assert not (tmp_path / ".context" / mirror.QUEUE_NAME).exists()
        assert handle_pull_remote({"project_dir": proj})["reason"] == "disabled"
        assert handle_backfill_remote({"project_dir": proj})["reason"] == "disabled"


# ===========================================================================
# ID collision-safety (Option B: random suffix, only when mirroring)
# ===========================================================================


class TestIdSuffix:
    def test_leading_int_parses_suffixed_ids(self):
        assert _leading_int("dec-012", "dec") == 12
        assert _leading_int("dec-012-a7f3", "dec") == 12
        assert _leading_int("con-005", "con") == 5
        assert _leading_int("pipe-003-ffff", "pipe") == 3
        assert _leading_int("dec-012", "con") is None

    def test_bare_ids_when_mirror_off(self, tmp_path):
        # conftest clears the remote env, so mirror is off here.
        assert next_id([], "dec") == "dec-001"
        assert next_id([{"id": "dec-001"}, {"id": "dec-002"}], "dec") == "dec-003"

    def test_suffixed_ids_when_mirror_on(self, tmp_path, remote):
        nid = next_id([{"id": "dec-012"}], "dec")
        assert nid.startswith("dec-013-")
        assert len(nid) == len("dec-013-") + 4  # 4 hex chars

    def test_suffix_increments_over_suffixed_ids(self, tmp_path, remote):
        nid = next_id([{"id": "dec-013-a7f3"}], "dec")
        assert nid.startswith("dec-014-")

    def test_suffixed_ids_are_unique(self, tmp_path, remote):
        seen = {next_id([{"id": "dec-001"}], "dec") for _ in range(50)}
        assert len(seen) > 1  # random suffix varies across mints
