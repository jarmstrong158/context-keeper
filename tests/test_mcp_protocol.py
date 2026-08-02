"""Conformance tests for MCP protocol revision 2026-07-28.

This server is hand-rolled and DUAL-ERA: zero dependencies is an advertised
property (README, and the .mcpb manifest, whose Desktop install path has no pip
step), so adopting the official SDK was not an option. That makes these tests
load-bearing in a way they are not for an SDK-backed server: nothing else
checks the wire shape.

The legacy assertions matter as much as the modern ones. The bundle ships to
Claude Desktop installs we do not control, so a legacy client must keep seeing
byte-identical responses.
"""

import json

import pytest

import mirror
import server


def _dispatch(method, params=None, *, modern=True, version=server.PROTOCOL_VERSION):
    payload = dict(params or {})
    if modern:
        meta = dict(payload.get("_meta") or {})
        meta["io.modelcontextprotocol/protocolVersion"] = version
        payload["_meta"] = meta
    return server._dispatch_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}
    )


# --- modern era -------------------------------------------------------------

def test_server_discover_is_implemented():
    """Servers MUST implement server/discover as of 2026-07-28."""
    result = _dispatch("server/discover")["result"]

    assert server.PROTOCOL_VERSION in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": server.SERVER_NAME,
        "version": server.__version__,
    }


def test_server_discover_answers_a_legacy_probe():
    """A dual-era client may probe with server/discover before it knows the
    server's era, so it must answer even with no `_meta` on the request."""
    result = _dispatch("server/discover", modern=False)["result"]
    assert server.PROTOCOL_VERSION in result["supportedVersions"]


@pytest.mark.parametrize("method", ["tools/list", "server/discover"])
def test_cacheable_results_carry_cache_hints(method):
    """SEP-2549. TOOLS is module-level constant data identical for every
    caller, hence public."""
    result = _dispatch(method)["result"]

    assert result["ttlMs"] == server.CACHE_TTL_MS
    assert result["cacheScope"] == "public"


def test_results_carry_result_type_and_server_info():
    """There is no handshake in which to identify once, so identity travels on
    every result."""
    result = _dispatch("tools/list")["result"]

    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == server.SERVER_NAME


def test_unsupported_version_is_rejected_with_the_supported_list():
    """A version mismatch returns UnsupportedProtocolVersionError, renumbered
    to -32022 by the spec's error-code allocation policy."""
    error = _dispatch("tools/list", version="1999-01-01")["error"]

    assert error["code"] == server.UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"]["supported"] == server.SUPPORTED_PROTOCOL_VERSIONS
    assert error["data"]["requested"] == "1999-01-01"


def test_tool_order_is_deterministic():
    """Deterministic ordering keeps client-side and prompt caches hitting."""
    first = [t["name"] for t in _dispatch("tools/list")["result"]["tools"]]
    second = [t["name"] for t in _dispatch("tools/list")["result"]["tools"]]

    assert first == [t["name"] for t in server.TOOLS]
    assert first == second


def test_tool_errors_still_flag_is_error():
    result = _dispatch("tools/call", {"name": "does_not_exist", "arguments": {}})["result"]

    assert result["isError"] is True
    assert "Unknown tool" in result["content"][0]["text"]


# --- legacy era -------------------------------------------------------------

def test_legacy_initialize_still_answers():
    """The .mcpb bundle ships to Desktop installs we do not control; a
    modern-only server would strand anyone on an older build, and legacy
    clients have no fall-forward mechanism."""
    result = _dispatch("initialize", modern=False)["result"]

    assert result["protocolVersion"] == server.LEGACY_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == server.SERVER_NAME


def test_legacy_responses_are_byte_identical_to_pre_migration():
    """A legacy client must see exactly what it saw before the migration: no
    resultType, no _meta, no cache hints leaking into its responses."""
    result = _dispatch("tools/list", modern=False)["result"]
    assert set(result) == {"tools"}

    called = _dispatch(
        "tools/call", {"name": "does_not_exist", "arguments": {}}, modern=False
    )["result"]
    assert set(called) == {"content", "isError"}


# --- mirror compatibility ---------------------------------------------------

def test_mirror_speaks_the_same_revision_as_the_server():
    """The mirror is an MCP *client* against context-keeper-remote. If the two
    constants drift, the mirror starts declaring a version this project does
    not implement."""
    assert mirror.MCP_PROTOCOL_VERSION == server.PROTOCOL_VERSION


def test_mirror_sends_the_metadata_a_modern_server_requires(monkeypatch):
    """A migrated remote MUST reject a POST with no MCP-Protocol-Version
    header. The mirror sends it (and the matching `_meta`) BEFORE the remote is
    migrated, so it keeps working whichever side deploys first.

    Header and body version must agree, or a conforming server answers -32020
    HeaderMismatch.
    """
    captured = {}

    class _FakeResponse:
        headers = {"content-type": "application/json"}

        def read(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(mirror.urllib.request, "urlopen", _fake_urlopen)
    mirror._rpc_call({"url": "https://example.invalid/mcp/tok", "timeout": 5}, "query_entries", {})

    headers = captured["headers"]
    meta = captured["body"]["params"]["_meta"]

    assert headers["mcp-protocol-version"] == mirror.MCP_PROTOCOL_VERSION
    assert headers["mcp-method"] == "tools/call"
    assert headers["mcp-name"] == "query_entries"
    assert meta["io.modelcontextprotocol/protocolVersion"] == mirror.MCP_PROTOCOL_VERSION
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "context-keeper-mirror"
    # Header/body agreement is what a conforming server validates.
    assert headers["mcp-protocol-version"] == meta["io.modelcontextprotocol/protocolVersion"]
