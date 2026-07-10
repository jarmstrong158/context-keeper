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
  `upsert_entries` tool. upsert_entries preserves incoming ids and REPLACES
  an existing id only when the pushed entry's updated_at is newer
  (last-writer-wins by timestamp), so an edit or deprecation actually
  propagates instead of being skipped as an already-present id. If the
  remote is unreachable the entry is appended to a local queue
  (.context/.mirror_queue.json) and flushed on the next successful push. A
  push failure NEVER propagates -- the local write already succeeded and
  must not be undone by a network problem.

  MIRROR IN (remote -> local): `pull_remote(base_dir)` calls the remote
  `query_entries` tool, keeps entries newer than a local watermark
  (.context/.mirror_watermark), and merges them by TIMESTAMP -- a remote
  entry whose id exists locally overwrites the local copy only when the
  remote's updated_at is newer; a newer local copy is kept and pushes back
  on its next write. Wired into the SessionStart hook and exposed as the
  `pull_remote` MCP tool.

  CONFLICTS: when either direction overwrites a copy that differed in
  substance (not just timestamps), the losing version is appended to
  .context/.mirror_conflicts.json. Last-writer-wins already resolved it;
  this just preserves what was overwritten. No resolution UI.

Design constraints (see dec-012 / con-006 and CLAUDE.md):
  - stdlib only (urllib.request for HTTP) -- zero new dependencies.
  - no secrets in code -- the remote URL (which contains the token) comes
    from CONTEXT_KEEPER_REMOTE_URL only.
  - fail soft always -- a mirror problem must never break local operation.
  - timestamp-based newest-wins on both the local merge and the remote
    upsert; neither side ever deletes (deprecation is a status change).

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
CONFLICTS_NAME = ".mirror_conflicts.json"
LOG_NAME = "mirror.log"

_DEFAULT_TIMEOUT = 5.0

# Timestamp fields are bookkeeping, not substance: two entries that differ only
# in these are the same content written at different times, not a real conflict.
_TS_KEYS = frozenset({"updated_at", "verified_at", "created_at"})


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _entry_ts(entry):
    """The entry's newest timestamp for last-writer-wins comparison.

    Prefer updated_at; fall back to created_at so a legacy entry that predates
    updated_at still compares sanely. ISO-8601 strings order correctly
    lexically.
    """
    if not isinstance(entry, dict):
        return ""
    return entry.get("updated_at") or entry.get("created_at") or ""


def _with_ts(entry):
    """Return the entry guaranteed to carry an updated_at for the push.

    Entries written by the current server always have one, but entries read
    straight off disk from an older store may not; the remote upsert keys its
    newest-wins decision on updated_at, so synthesize one from created_at (else
    now) without mutating the caller's dict.
    """
    if entry.get("updated_at"):
        return entry
    healed = dict(entry)
    healed["updated_at"] = entry.get("created_at") or _now_iso()
    return healed


def _strip_ts(entry):
    """The entry's substantive fields only (timestamps removed)."""
    return {k: v for k, v in entry.items() if k not in _TS_KEYS}


def _differs_in_substance(a, b):
    """True if a and b differ in something other than their timestamps."""
    return _strip_ts(a) != _strip_ts(b)


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
# Conflict log (losing versions preserved when an upsert overwrites)
# ------------------------------------------------------------------


def _conflicts_path(base_dir):
    return os.path.join(base_dir, CONFLICTS_NAME)


def _append_conflicts(base_dir, conflicts):
    """Append losing entry versions to .mirror_conflicts.json, fail-soft.

    We do NOT build resolution UI -- last-writer-wins already resolved the
    conflict. This just preserves the overwritten version (with the winner's
    timestamp) so nothing substantive is silently lost. Never raises.
    """
    if not conflicts:
        return
    try:
        existing = []
        path = _conflicts_path(base_dir)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = data
        existing.extend(conflicts)
        os.makedirs(base_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, path)
        _log(base_dir, f"logged {len(conflicts)} mirror conflict(s) to {CONFLICTS_NAME}")
    except Exception:
        pass


def _record_push_conflicts(base_dir, kind, pushed_entries, res):
    """Log any remote rows an outbound upsert overwrote with different content.

    The remote's upsert_entries returns per-id results; on an "updated" action
    it hands back `previous` -- the row it replaced. If that loser differed
    from what we pushed in more than timestamps, preserve it locally.
    """
    results = res.get("results") if isinstance(res, dict) else None
    if not isinstance(results, list):
        return
    by_id = {e.get("id"): e for e in pushed_entries}
    losers = []
    for r in results:
        if not isinstance(r, dict) or r.get("action") != "updated":
            continue
        prev = r.get("previous")
        winner = by_id.get(r.get("id"))
        if not isinstance(prev, dict) or not isinstance(winner, dict):
            continue
        loser = _row_to_entry(prev)
        if _differs_in_substance(loser, winner):
            losers.append({
                "logged_at": _now_iso(),
                "direction": "mirror_out",
                "kind": kind,
                "id": r.get("id"),
                "loser": loser,
                "winner": winner,
            })
    _append_conflicts(base_dir, losers)


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
    """upsert_entries one call per kind (the tool takes a single kind).

    Upsert (not import) so an edit or deprecation actually overwrites the
    remote's stale copy instead of being skipped as an existing id. Raises on
    the first failing call so the caller can persist the queue. Partial success
    is safe: upsert is idempotent (an equal-or-older re-push is skipped
    server-side), so re-pushing an already-synced kind is a harmless no-op.
    """
    project = _project_name(base_dir)
    by_kind = {}
    for rec in _dedupe_records(records):
        kind = rec.get("kind")
        entry = rec.get("entry")
        if kind in _KIND_TO_TYPE and isinstance(entry, dict) and entry.get("id"):
            by_kind.setdefault(kind, []).append(_with_ts(entry))
    pushed = 0
    for kind, entries in by_kind.items():
        res = _rpc_call(cfg, "upsert_entries", {"project": project, "kind": kind, "entries": entries})
        _record_push_conflicts(base_dir, kind, entries, res)
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
    # The columns are authoritative for status/timestamps and OVERRIDE any copy
    # embedded in the payload blob. A remote-side update_entry/deprecate_entry
    # bumps the updated_at COLUMN but leaves the payload's stale copy behind; if
    # we let that stale copy win, the pulled entry would look older than it is
    # and the newest-wins merge would wrongly skip it.
    if row.get("created_at"):
        entry["created_at"] = row["created_at"]
    if row.get("updated_at"):
        entry["updated_at"] = row["updated_at"]
    return entry


def _merge_rows(base_dir, rows):
    """Merge remote rows into the local store by TIMESTAMP (newest wins).

    For each remote row:
      - id absent locally               -> add it.
      - remote updated_at newer          -> replace the local copy (and, if the
                                            two differed in substance, preserve
                                            the overwritten local version to the
                                            conflict log).
      - local equal-or-newer             -> skip; the local edit is newer and
                                            will push back on its next write.

    This replaces the old additive-only merge, which left a locally-stale entry
    frozen even after the remote deprecated or edited it. Returns
    (added, updated, skipped).
    """
    import server  # lazy: avoids import-time circularity

    by_type = {}
    for row in rows:
        kind = row.get("kind")
        type_name = _KIND_TO_TYPE.get(kind)
        if type_name is None or not row.get("id"):
            continue
        by_type.setdefault(type_name, []).append(row)

    added = 0
    updated = 0
    skipped = 0
    conflicts = []
    for type_name, incoming_rows in by_type.items():
        kind = _TYPE_TO_KIND[type_name]
        path = os.path.join(base_dir, _TYPE_FILES[type_name])
        existing = server.read_json_file(path)
        index = {e.get("id"): i for i, e in enumerate(existing)}
        changed = False
        for row in incoming_rows:
            entry = _row_to_entry(row)
            rid = entry.get("id")
            if rid not in index:
                existing.append(entry)
                index[rid] = len(existing) - 1
                added += 1
                changed = True
                continue
            local = existing[index[rid]]
            remote_ts = _row_timestamp(row)  # authoritative column, not payload
            local_ts = _entry_ts(local)
            if remote_ts and remote_ts > local_ts:
                if _differs_in_substance(local, entry):
                    conflicts.append({
                        "logged_at": _now_iso(),
                        "direction": "mirror_in",
                        "kind": kind,
                        "id": rid,
                        "loser": local,
                        "winner": entry,
                    })
                existing[index[rid]] = entry
                updated += 1
                changed = True
            else:
                # Local is equal-or-newer: keep it; it pushes back on next write.
                skipped += 1
        if changed:
            server.write_json_file(path, existing)
    if conflicts:
        _append_conflicts(base_dir, conflicts)
    return added, updated, skipped


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
        added, updated, skipped = _merge_rows(base_dir, to_merge)
    except Exception as e:
        _log(base_dir, f"pull_remote merge failed: {e}")
        return {"pulled": 0, "error": str(e)}

    if max_ts and (watermark is None or max_ts > watermark):
        _watermark_write(base_dir, max_ts)

    if added or updated:
        _log(base_dir, f"pull_remote merged {added} new, {updated} updated ({skipped} kept local)")
    return {"pulled": added, "updated": updated, "skipped": skipped, "watermark": max_ts or watermark}


# ------------------------------------------------------------------
# BACKFILL (push the entire local store to the remote in one batch)
# ------------------------------------------------------------------


def backfill_remote(base_dir=None):
    """Push ALL local entries for the project to the remote.

    One upsert_entries call per kind. Idempotent (upsert skips an
    equal-or-older id, updates a newer one). Fail-soft: a failing kind is
    logged and the others still run.
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
    created = 0
    updated = 0
    skipped = 0
    errors = {}
    for type_name, kind in _TYPE_TO_KIND.items():
        entries = [_with_ts(e) for e in server.read_json_file(
            os.path.join(base_dir, _TYPE_FILES[type_name])) if e.get("id")]
        counts[type_name] = len(entries)
        if not entries:
            continue
        try:
            res = _rpc_call(cfg, "upsert_entries", {
                "project": project, "kind": kind, "entries": entries})
            created += int(res.get("created_count") or 0)
            updated += int(res.get("updated_count") or 0)
            skipped += int(res.get("skipped_count") or 0)
            _record_push_conflicts(base_dir, kind, entries, res)
        except Exception as e:
            errors[kind] = str(e)
            _log(base_dir, f"backfill_remote {kind} failed ({len(entries)} entries): {e}")

    out = {"backfilled": created, "updated": updated, "skipped": skipped, "counts": counts}
    if errors:
        out["errors"] = errors
    _log(base_dir, f"backfill_remote created {created}, updated {updated}, skipped {skipped}")
    return out
