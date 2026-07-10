#!/usr/bin/env python3
"""Context Keeper -- two-way mirror (local <-> remote).

The local `.context/` JSON store is CANONICAL. This module adds an
optional, fail-soft mirror to the `context-keeper-remote` Cloudflare
Worker so a second device -- e.g. a phone -- can both receive the
desktop's memory and contribute its own.

Transport: the remote is a stateless Streamable-HTTP MCP server. Its URL
IS the credential -- the auth token is the final path segment
(`/mcp/<token>`), so there is no separate token env var and no auth
header. We POST plain JSON-RPC `tools/call` requests to that one URL; the
server answers with a single `application/json` body (no SSE, no session
handshake). See context-keeper-remote/src/{index,mcp}.ts.

Two halves:

  MIRROR OUT (local -> remote): after every local write the server calls
  `mirror_out(entry, type_name, base_dir)`, which calls the remote
  `import_entries` tool. import_entries preserves incoming ids and SKIPS
  (never overwrites) any id already present -- exactly the additive
  semantics we want. If the remote is unreachable the entry is appended to
  a local queue (.context/.mirror_queue.json) and flushed on the next
  successful push. A push failure NEVER propagates -- the local write
  already succeeded and must not be undone by a network problem.

  MIRROR IN (remote -> local): `pull_remote(base_dir)` calls the remote
  `query_entries` tool, keeps entries newer than a local watermark
  (.context/.mirror_watermark), and merges them ADDITIVELY -- a remote
  entry whose id already exists locally is never allowed to overwrite the
  local copy. Wired into the SessionStart hook and exposed as the
  `pull_remote` MCP tool.

Design constraints (see dec-012 / con-006 and CLAUDE.md):
  - stdlib only (urllib.request for HTTP) -- zero new dependencies.
  - no secrets in code -- the remote URL (which contains the token) comes
    from CONTEXT_KEEPER_REMOTE_URL only.
  - fail soft always -- a mirror problem must never break local operation.
  - additive-only on the local merge; import/upsert-skip on the remote.

The remote stores each entry as {id, kind, status, created_at, updated_at,
superseded_by, payload}, where `payload` holds every other local field.
On pull we reconstruct the local entry = payload + {id, status,
superseded_by}. `kind` is singular on the wire ("decision") vs the local
plural store name ("decisions").
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Local store name <-> remote singular kind.
_TYPE_TO_KIND = {
    "decisions": "decision",
    "pipelines": "pipeline",
    "constraints": "constraint",
}
_KIND_TO_TYPE = {v: k for k, v in _TYPE_TO_KIND.items()}

# Store name -> on-disk filename. Kept local (not imported from server) so
# this module has no import-time dependency on server.py, avoiding a
# circular import (server.py imports this module at top level).
_TYPE_FILES = {
    "decisions": "decisions.json",
    "pipelines": "pipelines.json",
    "constraints": "constraints.json",
}

QUEUE_NAME = ".mirror_queue.json"
WATERMARK_NAME = ".mirror_watermark"
LOG_NAME = "mirror.log"

_DEFAULT_TIMEOUT = 5.0


# ------------------------------------------------------------------
# Configuration (env only -- no secrets in code)
# ------------------------------------------------------------------


def mirror_config():
    """Return the remote config from env, or None if mirroring is off.

    CONTEXT_KEEPER_REMOTE_URL -- full Streamable-HTTP MCP URL INCLUDING the
        secret path segment, e.g.
        https://context-keeper-remote.example.workers.dev/mcp/<token>
        (the token is the credential; there is no separate token env var).
    CONTEXT_KEEPER_REMOTE_TIMEOUT -- per-request timeout seconds (optional).
    """
    url = os.environ.get("CONTEXT_KEEPER_REMOTE_URL")
    if not url or not url.strip():
        return None
    try:
        timeout = float(os.environ.get("CONTEXT_KEEPER_REMOTE_TIMEOUT", ""))
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    return {
        "url": url.strip(),
        "timeout": timeout if timeout > 0 else _DEFAULT_TIMEOUT,
    }


def mirror_enabled():
    return mirror_config() is not None


# ------------------------------------------------------------------
# Small file helpers (paths live inside the project's .context/)
# ------------------------------------------------------------------


def _queue_path(base_dir):
    return os.path.join(base_dir, QUEUE_NAME)


def _watermark_path(base_dir):
    return os.path.join(base_dir, WATERMARK_NAME)


def _log(base_dir, message):
    """One-line, best-effort log. Never raises."""
    try:
        os.makedirs(base_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(base_dir, LOG_NAME), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _project_name(base_dir):
    """A stable scoping key so the remote can separate projects.

    Prefer the configured project_name; fall back to the project directory
    basename (the parent of .context/).
    """
    try:
        import server
        name = (server.read_config(base_dir) or {}).get("project_name")
        if name:
            return name
    except Exception:
        pass
    return os.path.basename(os.path.dirname(os.path.abspath(base_dir)))


# ------------------------------------------------------------------
# Queue (pending pushes when the remote was unreachable)
# ------------------------------------------------------------------


def _queue_load(base_dir):
    path = _queue_path(base_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _dedupe_records(records):
    """Collapse records to one-per-(kind,id), keeping the LAST occurrence.

    The queue can accumulate several writes to the same entry (record then
    update then deprecate). Only the latest state needs to reach the
    remote, so we dedupe to keep the queue -- and each push -- bounded.
    """
    out = []
    index = {}
    for rec in records:
        entry = rec.get("entry") or {}
        key = (rec.get("kind"), entry.get("id"))
        if key[1] is None:
            out.append(rec)
            continue
        if key in index:
            out[index[key]] = rec
        else:
            index[key] = len(out)
            out.append(rec)
    return out


def _queue_save(base_dir, records):
    records = _dedupe_records(records)
    try:
        os.makedirs(base_dir, exist_ok=True)
        path = _queue_path(base_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _queue_clear(base_dir):
    try:
        path = _queue_path(base_dir)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ------------------------------------------------------------------
# JSON-RPC MCP transport (stdlib urllib only)
# ------------------------------------------------------------------

# Monotonic-ish request id; value is irrelevant to a stateless server.
_rpc_id = 0


def _next_rpc_id():
    global _rpc_id
    _rpc_id += 1
    return _rpc_id


def _parse_body(raw, content_type):
    """Parse the HTTP body as JSON, tolerating an SSE-framed response.

    The reference Worker always returns application/json, but Streamable
    HTTP permits text/event-stream; if we ever get SSE, pull the JSON out
    of the last `data:` line so the client still works.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "event-stream" in (content_type or "") or raw.startswith("event:") or raw.startswith("data:"):
        payload = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
        return json.loads(payload) if payload else {}
    return json.loads(raw)


def _rpc_call(cfg, tool_name, arguments):
    """Invoke a remote MCP tool and return its structured result dict.

    Raises on transport error, JSON-RPC error, or tool-level isError so
    callers can fail soft around it.
    """
    body = {
        "jsonrpc": "2.0",
        "id": _next_rpc_id(),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("User-Agent", "context-keeper-mirror/0.15")
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        raw = resp.read().decode("utf-8")
        ctype = resp.headers.get("content-type", "")
    parsed = _parse_body(raw, ctype)

    if isinstance(parsed, list):  # batch echo -- take the first response
        parsed = parsed[0] if parsed else {}
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response type: {type(parsed).__name__}")
    if parsed.get("error"):
        raise RuntimeError(f"rpc error: {parsed['error']}")

    result = parsed.get("result") or {}
    if result.get("isError"):
        text = ""
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
        raise RuntimeError(f"tool error: {text or 'unknown'}")
    # Prefer structuredContent; fall back to parsing the text block.
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except Exception:
                return {}
    return {}


def _push_records(cfg, base_dir, records):
    """import_entries one call per kind (the tool takes a single kind).

    Raises on the first failing call so the caller can persist the queue.
    Partial success is safe: import_entries skips ids already present, so a
    re-push of an already-imported kind is a harmless no-op.
    """
    project = _project_name(base_dir)
    by_kind = {}
    for rec in _dedupe_records(records):
        kind = rec.get("kind")
        entry = rec.get("entry")
        if kind in _KIND_TO_TYPE and isinstance(entry, dict) and entry.get("id"):
            by_kind.setdefault(kind, []).append(entry)
    pushed = 0
    for kind, entries in by_kind.items():
        _rpc_call(cfg, "import_entries", {"project": project, "kind": kind, "entries": entries})
        pushed += len(entries)
    return pushed


# ------------------------------------------------------------------
# MIRROR OUT
# ------------------------------------------------------------------


def mirror_out(entry, type_name, base_dir):
    """Push a single just-written entry to the remote, fail-soft.

    Flushes any previously queued entries in the same push. On any failure
    the current entry (plus the existing queue) is persisted to the queue
    for a later retry, and the failure is logged but never raised -- the
    local write has already committed.
    """
    cfg = mirror_config()
    if cfg is None:
        return {"mirrored": False, "reason": "disabled"}
    kind = _TYPE_TO_KIND.get(type_name)
    if kind is None or not entry or not entry.get("id"):
        return {"mirrored": False, "reason": "not-mirrorable"}

    record = {"kind": kind, "entry": entry}
    pending = _queue_load(base_dir) + [record]
    try:
        pushed = _push_records(cfg, base_dir, pending)
        _queue_clear(base_dir)
        return {"mirrored": True, "pushed": pushed}
    except Exception as e:
        _queue_save(base_dir, pending)
        _log(base_dir, f"mirror_out queued {entry.get('id')} ({kind}): {e}")
        return {"mirrored": False, "queued": True, "reason": str(e)}


def flush_queue(base_dir):
    """Attempt to push any queued entries. Fail-soft. Returns a status dict."""
    cfg = mirror_config()
    if cfg is None:
        return {"flushed": 0, "reason": "disabled"}
    pending = _queue_load(base_dir)
    if not pending:
        return {"flushed": 0}
    try:
        pushed = _push_records(cfg, base_dir, pending)
        _queue_clear(base_dir)
        return {"flushed": pushed}
    except Exception as e:
        _log(base_dir, f"flush_queue failed ({len(pending)} pending): {e}")
        return {"flushed": 0, "reason": str(e)}


# ------------------------------------------------------------------
# MIRROR IN
# ------------------------------------------------------------------


def _watermark_read(base_dir):
    path = _watermark_path(base_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            wm = f.read().strip()
        return wm or None
    except Exception:
        return None


def _watermark_write(base_dir, value):
    if not value:
        return
    try:
        os.makedirs(base_dir, exist_ok=True)
        path = _watermark_path(base_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, path)
    except Exception:
        pass


def _row_timestamp(row):
    """The remote row's newest timestamp, for watermark comparison.

    Uses the remote's own updated_at/created_at columns so comparisons are
    against a single clock. ISO-8601 strings compare correctly lexically.
    """
    return row.get("updated_at") or row.get("created_at") or ""


def _row_to_entry(row):
    """Reconstruct a local entry dict from a remote query_entries row."""
    payload = row.get("payload")
    entry = dict(payload) if isinstance(payload, dict) else {}
    entry["id"] = row.get("id")
    entry["status"] = row.get("status", entry.get("status", "active"))
    if row.get("superseded_by") is not None:
        entry["superseded_by"] = row["superseded_by"]
    # payload normally already carries these; backfill from columns just in case.
    if row.get("created_at"):
        entry.setdefault("created_at", row["created_at"])
    if row.get("updated_at"):
        entry.setdefault("updated_at", row["updated_at"])
    return entry


def _merge_rows(base_dir, rows):
    """Merge remote rows into the local store, ADDITIVELY.

    A row whose id already exists locally is skipped -- the local copy is
    canonical and is never overwritten by a pulled entry. Returns
    (added, skipped).
    """
    import server  # lazy: avoids import-time circularity

    by_type = {}
    for row in rows:
        kind = row.get("kind")
        type_name = _KIND_TO_TYPE.get(kind)
        if type_name is None or not row.get("id"):
            continue
        by_type.setdefault(type_name, []).append(_row_to_entry(row))

    added = 0
    skipped = 0
    for type_name, incoming in by_type.items():
        path = os.path.join(base_dir, _TYPE_FILES[type_name])
        existing = server.read_json_file(path)
        existing_ids = {e.get("id") for e in existing}
        changed = False
        for entry in incoming:
            if entry.get("id") in existing_ids:
                skipped += 1
                continue
            existing.append(entry)
            existing_ids.add(entry.get("id"))
            added += 1
            changed = True
        if changed:
            server.write_json_file(path, existing)
    return added, skipped


def pull_remote(base_dir=None):
    """Fetch remote entries newer than the watermark and merge them in.

    query_entries has no server-side `since`, so we fetch the project's
    entries (all statuses, so deprecations propagate as new ids) and filter
    by the remote timestamp locally. Fail-soft: any error returns a status
    dict without raising, so wiring this into SessionStart can never break a
    session.
    """
    cfg = mirror_config()
    if cfg is None:
        return {"pulled": 0, "reason": "disabled"}

    if base_dir is None:
        try:
            import server
            base_dir = server.CONTEXT_DIR
        except Exception:
            base_dir = None
    if not base_dir:
        return {"pulled": 0, "reason": "no-project"}

    watermark = _watermark_read(base_dir)
    try:
        result = _rpc_call(cfg, "query_entries", {
            "project": _project_name(base_dir), "status": "all"})
    except Exception as e:
        _log(base_dir, f"pull_remote query failed: {e}")
        return {"pulled": 0, "error": str(e)}

    rows = result.get("results") if isinstance(result, dict) else None
    if not rows:
        return {"pulled": 0, "skipped": 0}

    # Advance the watermark to the newest timestamp we saw regardless of how
    # many were new, so the next pull skips this batch. Filter to strictly
    # newer rows before merging (merge is additive/idempotent anyway).
    max_ts = watermark
    to_merge = []
    for row in rows:
        ts = _row_timestamp(row)
        if max_ts is None or (ts and ts > max_ts):
            max_ts = ts if (max_ts is None or ts > max_ts) else max_ts
        if watermark and ts and ts <= watermark:
            continue
        to_merge.append(row)

    try:
        added, skipped = _merge_rows(base_dir, to_merge)
    except Exception as e:
        _log(base_dir, f"pull_remote merge failed: {e}")
        return {"pulled": 0, "error": str(e)}

    if max_ts and (watermark is None or max_ts > watermark):
        _watermark_write(base_dir, max_ts)

    if added:
        _log(base_dir, f"pull_remote merged {added} new entries ({skipped} already present)")
    return {"pulled": added, "skipped": skipped, "watermark": max_ts or watermark}


# ------------------------------------------------------------------
# BACKFILL (push the entire local store to the remote in one batch)
# ------------------------------------------------------------------


def backfill_remote(base_dir=None):
    """Push ALL local entries for the project to the remote.

    One import_entries call per kind. Idempotent (import skips ids already
    present). Fail-soft: a failing kind is logged and the others still run.
    """
    cfg = mirror_config()
    if cfg is None:
        return {"backfilled": 0, "reason": "disabled"}

    if base_dir is None:
        try:
            import server
            base_dir = server.CONTEXT_DIR
        except Exception:
            base_dir = None
    if not base_dir or not os.path.exists(base_dir):
        return {"backfilled": 0, "reason": "no-project"}

    import server
    project = _project_name(base_dir)
    counts = {}
    imported = 0
    skipped = 0
    errors = {}
    for type_name, kind in _TYPE_TO_KIND.items():
        entries = [e for e in server.read_json_file(
            os.path.join(base_dir, _TYPE_FILES[type_name])) if e.get("id")]
        counts[type_name] = len(entries)
        if not entries:
            continue
        try:
            res = _rpc_call(cfg, "import_entries", {
                "project": project, "kind": kind, "entries": entries})
            imported += int(res.get("imported_count") or 0)
            skipped += int(res.get("skipped_count") or 0)
        except Exception as e:
            errors[kind] = str(e)
            _log(base_dir, f"backfill_remote {kind} failed ({len(entries)} entries): {e}")

    out = {"backfilled": imported, "skipped": skipped, "counts": counts}
    if errors:
        out["errors"] = errors
    _log(base_dir, f"backfill_remote imported {imported}, skipped {skipped}")
    return out
