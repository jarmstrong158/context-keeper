"""End-to-end tests for the two-way mirror (mirror.py + server wiring).

Strategy: stand up a tiny in-process HTTP server that implements the
remote API contract (POST /import upsert, GET /entries?since=), point
CONTEXT_KEEPER_REMOTE_URL at it, and exercise the real code paths --
no network, no mocking of urllib. Everything is stdlib.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import mirror  # noqa: E402
import server  # noqa: E402
from server import (  # noqa: E402
    handle_backfill_remote,
    handle_pull_remote,
    handle_record_constraint,
    handle_record_decision,
    handle_record_pipeline,
    next_id,
    read_json_file,
)


# ---------------------------------------------------------------------------
# Fake remote: an in-memory store implementing the mirror API contract.
# ---------------------------------------------------------------------------


class FakeRemote:
    """Records keyed by (project, type, id). Upsert on import; since-filter
    on read. Mirrors what the Cloudflare Worker is expected to do."""

    def __init__(self):
        self.records = {}  # (project, type, id) -> entry
        self.import_calls = 0
        self.auth_headers = []

    def do_import(self, project, records):
        self.import_calls += 1
        n = 0
        for rec in records:
            entry = rec.get("entry") or {}
            key = (project, rec.get("type"), entry.get("id"))
            self.records[key] = entry  # upsert by id
            n += 1
        return {"imported": n}

    def do_entries(self, project, since):
        out = []
        for (proj, tname, _id), entry in self.records.items():
            if proj != project:
                continue
            ts = server._entry_timestamp(entry)
            ts_iso = ts.isoformat() if ts else None
            if since and ts_iso and ts_iso <= since:
                continue
            out.append({"type": tname, "entry": entry})
        return {"records": out}


def make_handler(remote, token=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            remote.auth_headers.append(self.headers.get("Authorization"))
            if token is None:
                return True
            return self.headers.get("Authorization") == f"Bearer {token}"

        def do_POST(self):
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/import":
                res = remote.do_import(payload.get("project"), payload.get("records") or [])
                return self._send(res)
            return self._send({"error": "not found"}, 404)

        def do_GET(self):
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            if parsed.path == "/entries":
                project = (q.get("project") or [None])[0]
                since = (q.get("since") or [None])[0]
                return self._send(remote.do_entries(project, since))
            return self._send({"error": "not found"}, 404)

    return Handler


@pytest.fixture
def remote(monkeypatch):
    """Start a fake remote, set the env var, yield the store, tear down."""
    store = FakeRemote()
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(store))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("CONTEXT_KEEPER_ID_NAMESPACE", raising=False)
    store.httpd = httpd
    try:
        yield store
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# Helpers to build a project with a fixed name (so remote scoping matches).
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, name: str) -> str:
    ctx = tmp_path / ".context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "config.json").write_text(json.dumps({"project_name": name}), encoding="utf-8")
    return str(tmp_path)


_LONG = "x" * 70  # clears every v0.4 min-length field


def _record_all(project_dir):
    """Record one of each type. Returns their ids."""
    d1 = handle_record_decision({
        "project_dir": project_dir, "summary": "Use JSON store",
        "problem": _LONG, "why_chosen": _LONG, "tags": ["arch"],
    })
    d2 = handle_record_decision({
        "project_dir": project_dir, "summary": "Fail soft on mirror",
        "problem": _LONG, "why_chosen": _LONG, "tags": ["mirror"],
    })
    c1 = handle_record_constraint({
        "project_dir": project_dir, "rule": "Never block local",
        "reason": _LONG, "tags": ["mirror"],
    })
    p1 = handle_record_pipeline({
        "project_dir": project_dir, "name": "Deploy flow",
        "purpose": _LONG, "steps": [{"order": 1, "action": "build"}],
    })
    return [d1["id"], d2["id"], c1["id"], p1["id"]]


# ===========================================================================
# Mirror OUT
# ===========================================================================


class TestMirrorOut:
    def test_writes_reach_remote(self, tmp_path, remote):
        proj = _project(tmp_path, "roundtrip")
        ids = _record_all(proj)
        # All four entries should now live on the fake remote, scoped to the project.
        remote_ids = {k[2] for k in remote.records if k[0] == "roundtrip"}
        assert set(ids) == remote_ids
        assert len(remote_ids) == 4

    def test_local_write_succeeds_even_if_remote_down(self, tmp_path, monkeypatch):
        # Point at a dead port; the local record must still succeed and queue.
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", "http://127.0.0.1:1")
        proj = _project(tmp_path, "offline")
        res = handle_record_decision({
            "project_dir": proj, "summary": "Works offline",
            "problem": _LONG, "why_chosen": _LONG,
        })
        assert res["success"] is True
        # Entry is on disk locally...
        disk = read_json_file(str(tmp_path / ".context" / "decisions.json"))
        assert len(disk) == 1
        # ...and queued for a later push.
        queue = json.loads((tmp_path / ".context" / mirror.QUEUE_NAME).read_text())
        assert any(r["entry"]["id"] == res["id"] for r in queue)

    def test_queue_flushes_on_next_success(self, tmp_path, monkeypatch, remote):
        proj = _project(tmp_path, "flushproj")
        # Pre-seed a queue as if an earlier push had failed.
        stale = {"type": "decisions", "entry": {
            "id": "dec-099", "summary": "queued earlier", "created_at": server.now_iso(),
            "verified_at": server.now_iso(), "status": "active"}}
        (tmp_path / ".context" / mirror.QUEUE_NAME).write_text(json.dumps([stale]), encoding="utf-8")
        # A fresh successful record should push BOTH the queued and new entry.
        res = handle_record_decision({
            "project_dir": proj, "summary": "new entry",
            "problem": _LONG, "why_chosen": _LONG,
        })
        remote_ids = {k[2] for k in remote.records if k[0] == "flushproj"}
        assert "dec-099" in remote_ids
        assert res["id"] in remote_ids
        # Queue is cleared after the successful flush.
        assert not (tmp_path / ".context" / mirror.QUEUE_NAME).exists()

    def test_update_and_deprecate_mirror(self, tmp_path, remote):
        from server import handle_deprecate_entry, handle_update_entry
        proj = _project(tmp_path, "lifecycle")
        d = handle_record_decision({
            "project_dir": proj, "summary": "original decision",
            "problem": _LONG, "why_chosen": _LONG,
        })
        handle_update_entry({"project_dir": proj, "id": d["id"],
                             "updates": {"summary": "updated summary"}})
        assert remote.records[("lifecycle", "decisions", d["id"])]["summary"] == "updated summary"
        handle_deprecate_entry({"project_dir": proj, "id": d["id"], "reason": "obsolete now"})
        assert remote.records[("lifecycle", "decisions", d["id"])]["status"] == "deprecated"


# ===========================================================================
# Mirror IN (pull) + full round-trip count verification
# ===========================================================================


class TestPullRoundTrip:
    def test_counts_match_after_pull(self, tmp_path, remote):
        # Source project mirrors 4 entries out.
        src = _project(tmp_path / "src", "shared")
        ids = _record_all(src)
        assert len({k for k in remote.records if k[0] == "shared"}) == 4

        # Fresh destination project pulls them in.
        dst = _project(tmp_path / "dst", "shared")
        res = handle_pull_remote({"project_dir": dst})
        assert res["pulled"] == 4

        # Counts match across the three stores.
        dst_ctx = tmp_path / "dst" / ".context"
        got = (read_json_file(str(dst_ctx / "decisions.json"))
               + read_json_file(str(dst_ctx / "constraints.json"))
               + read_json_file(str(dst_ctx / "pipelines.json")))
        assert {e["id"] for e in got} == set(ids)

    def test_pull_is_additive_never_overwrites(self, tmp_path, remote):
        src = _project(tmp_path / "src", "additive")
        d = handle_record_decision({
            "project_dir": src, "summary": "remote version",
            "problem": _LONG, "why_chosen": _LONG,
        })
        dst = _project(tmp_path / "dst", "additive")
        # Local already has an entry with the SAME id but different content.
        local_entry = dict(d["entry"])
        local_entry["summary"] = "LOCAL WINS"
        (tmp_path / "dst" / ".context" / "decisions.json").write_text(
            json.dumps([local_entry]), encoding="utf-8")
        res = handle_pull_remote({"project_dir": dst})
        assert res["pulled"] == 0
        assert res["skipped"] == 1
        disk = read_json_file(str(tmp_path / "dst" / ".context" / "decisions.json"))
        assert len(disk) == 1
        assert disk[0]["summary"] == "LOCAL WINS"  # local preserved

    def test_second_pull_is_noop_via_watermark(self, tmp_path, remote):
        src = _project(tmp_path / "src", "wm")
        _record_all(src)
        dst = _project(tmp_path / "dst", "wm")
        first = handle_pull_remote({"project_dir": dst})
        assert first["pulled"] == 4
        second = handle_pull_remote({"project_dir": dst})
        assert second["pulled"] == 0

    def test_backfill_pushes_everything(self, tmp_path, remote):
        # Record while remote is unreachable, then backfill once it's up.
        proj = _project(tmp_path, "backfillproj")
        # Temporarily break the URL so records don't auto-mirror.
        real = os.environ["CONTEXT_KEEPER_REMOTE_URL"]
        os.environ["CONTEXT_KEEPER_REMOTE_URL"] = "http://127.0.0.1:1"
        try:
            ids = _record_all(proj)
        finally:
            os.environ["CONTEXT_KEEPER_REMOTE_URL"] = real
        # Nothing on remote yet.
        assert len({k for k in remote.records if k[0] == "backfillproj"}) == 0
        res = handle_backfill_remote({"project_dir": proj})
        assert res["backfilled"] == 4
        remote_ids = {k[2] for k in remote.records if k[0] == "backfillproj"}
        assert set(ids) == remote_ids


# ===========================================================================
# Auth
# ===========================================================================


class TestAuth:
    def test_token_sent_as_bearer(self, tmp_path, monkeypatch):
        store = FakeRemote()
        httpd = HTTPServer(("127.0.0.1", 0), make_handler(store, token="s3cret"))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_TOKEN", "s3cret")
        try:
            proj = _project(tmp_path, "authproj")
            handle_record_decision({
                "project_dir": proj, "summary": "authed",
                "problem": _LONG, "why_chosen": _LONG,
            })
            assert ("authproj", "decisions") in {(k[0], k[1]) for k in store.records}
            assert "Bearer s3cret" in store.auth_headers
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_wrong_token_fails_soft_and_queues(self, tmp_path, monkeypatch):
        store = FakeRemote()
        httpd = HTTPServer(("127.0.0.1", 0), make_handler(store, token="right"))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CONTEXT_KEEPER_REMOTE_TOKEN", "wrong")
        try:
            proj = _project(tmp_path, "badauth")
            res = handle_record_decision({
                "project_dir": proj, "summary": "will 401",
                "problem": _LONG, "why_chosen": _LONG,
            })
            assert res["success"] is True  # local unaffected
            assert ("badauth", "decisions") not in {(k[0], k[1]) for k in store.records}
            queue = json.loads((tmp_path / ".context" / mirror.QUEUE_NAME).read_text())
            assert len(queue) == 1
        finally:
            httpd.shutdown()
            httpd.server_close()


# ===========================================================================
# Disabled state (no env var) -- pure no-op
# ===========================================================================


class TestDisabled:
    def test_no_url_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_URL", raising=False)
        proj = _project(tmp_path, "nomirror")
        res = handle_record_decision({
            "project_dir": proj, "summary": "no mirror",
            "problem": _LONG, "why_chosen": _LONG,
        })
        assert res["success"] is True
        assert not (tmp_path / ".context" / mirror.QUEUE_NAME).exists()
        pull = handle_pull_remote({"project_dir": proj})
        assert pull["reason"] == "disabled"
        back = handle_backfill_remote({"project_dir": proj})
        assert back["reason"] == "disabled"


# ===========================================================================
# ID namespace collision-safety
# ===========================================================================


class TestIdNamespace:
    def test_local_ids_stay_bare(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT_KEEPER_ID_NAMESPACE", raising=False)
        assert next_id([], "dec") == "dec-001"
        assert next_id([{"id": "dec-001"}, {"id": "dec-002"}], "dec") == "dec-003"

    def test_remote_namespace_disjoint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXT_KEEPER_ID_NAMESPACE", "r")
        # A store in the 'r' namespace ignores bare local ids when numbering.
        entries = [{"id": "dec-005"}, {"id": "dec-r001"}, {"id": "dec-r002"}]
        assert next_id(entries, "dec") == "dec-r003"

    def test_bare_store_ignores_remote_ids(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT_KEEPER_ID_NAMESPACE", raising=False)
        # Pulled-in remote ids must not inflate the local counter.
        entries = [{"id": "dec-005"}, {"id": "dec-r099"}]
        assert next_id(entries, "dec") == "dec-006"
