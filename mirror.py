#!/usr/bin/env python3
"""Context Keeper -- two-way mirror (local <-> remote).

The local `.context/` JSON store is CANONICAL. This module adds an
optional, fail-soft mirror to a remote endpoint (a Cloudflare Worker in
the reference deployment) so a second device -- e.g. a phone -- can both
receive the desktop's memory and contribute its own.

Two halves:

  MIRROR OUT (local -> remote): after every local write the server calls
  `mirror_out(entry, type_name, base_dir)`, which POSTs the entry to the
  remote. If the remote is unreachable the entry is appended to a local
  queue (.context/.mirror_queue.json) and flushed on the next successful
  push. A push failure NEVER propagates -- the local write already
  succeeded and must not be undone by a network problem.

  MIRROR IN (remote -> local): `pull_remote(base_dir)` fetches entries the
  remote has that are newer than a locally stored watermark
  (.context/.mirror_watermark) and merges them ADDITIVELY -- a remote
  entry whose id already exists locally is never allowed to overwrite the
  local copy. Wired into the SessionStart hook and exposed as the
  `pull_remote` MCP tool.

Design constraints (see the task brief and CLAUDE.md):
  - stdlib only (urllib.request for HTTP) -- zero new dependencies.
  - no secrets in code -- remote URL/token come from env vars only.
  - fail soft always -- a mirror problem must never break local operation.
  - additive/upsert on the wire, additive-only on local merge.

Remote API contract (what the Worker must implement):
  POST {url}/import
      headers: Authorization: Bearer <token>   (only if a token is set)
      body:    {"project": "<name>", "records": [{"type": "<store>",
                "entry": {<full entry>}}, ...]}
      semantics: upsert each record by entry id (replace if present, insert
                 if new). Returns e.g. {"imported": <n>}.
  GET  {url}/entries?project=<name>&since=<iso-8601>
      headers: Authorization: Bearer <token>   (only if a token is set)
      returns: {"records": [{"type": "<store>", "entry": {<full entry>}}, ...]}
               -- every entry whose timestamp is > `since` (or all, if
               `since` is omitted), scoped to the given project.

`type` is carried in the wire wrapper, never stored inside the entry, so
the on-disk JSON entries stay exactly the shape the rest of the server
expects.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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
    """Return the remote config from env vars, or None if mirroring is off.

    CONTEXT_KEEPER_REMOTE_URL   -- base URL of the remote (required to enable)
    CONTEXT_KEEPER_REMOTE_TOKEN -- bearer token for auth (optional)
    CONTEXT_KEEPER_REMOTE_TIMEOUT -- per-request timeout seconds (optional)
    """
    url = os.environ.get("CONTEXT_KEEPER_REMOTE_URL")
    if not url or not url.strip():
        return None
    try:
        timeout = float(os.environ.get("CONTEXT_KEEPER_REMOTE_TIMEOUT", ""))
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    return {
        "url": url.strip().rstrip("/"),
        "token": (os.environ.get("CONTEXT_KEEPER_REMOTE_TOKEN") or "").strip(),
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
    """Collapse records to one-per-(type,id), keeping the LAST occurrence.

    The queue can accumulate several writes to the same entry (record then
    update then deprecate). Only the latest state needs to reach the
    remote, so we dedupe to keep the queue -- and each push -- bounded.
    Records without an id are kept as-is (can't be keyed).
    """
    out = []
    index = {}
    for rec in records:
        entry = rec.get("entry") or {}
        key = (rec.get("type"), entry.get("id"))
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
# HTTP (stdlib urllib only)
# ------------------------------------------------------------------


def _http(method, url, cfg, payload=None):
    """Perform an HTTP request and return the parsed JSON body (or {}).

    Raises on transport/HTTP error so callers can fail soft around it.
    """
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if cfg.get("token"):
        req.add_header("Authorization", "Bearer " + cfg["token"])
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _push(cfg, base_dir, records):
    """POST records to the remote /import endpoint. Raises on failure."""
    payload = {"project": _project_name(base_dir), "records": records}
    return _http("POST", cfg["url"] + "/import", cfg, payload)


# ------------------------------------------------------------------
# MIRROR OUT
# ------------------------------------------------------------------


def mirror_out(entry, type_name, base_dir):
    """Push a single just-written entry to the remote, fail-soft.

    Flushes any previously queued entries in the same push. On any failure
    the current entry (plus the existing queue) is persisted to the queue
    for a later retry, and the failure is logged but never raised -- the
    local write has already committed.

    Returns a small status dict for callers/tests; the server ignores it.
    """
    cfg = mirror_config()
    if cfg is None:
        return {"mirrored": False, "reason": "disabled"}
    if type_name not in _TYPE_FILES or not entry or not entry.get("id"):
        return {"mirrored": False, "reason": "not-mirrorable"}

    record = {"type": type_name, "entry": entry}
    pending = _queue_load(base_dir) + [record]
    try:
        _push(cfg, base_dir, _dedupe_records(pending))
        _queue_clear(base_dir)
        return {"mirrored": True, "pushed": len(_dedupe_records(pending))}
    except Exception as e:
        _queue_save(base_dir, pending)
        _log(base_dir, f"mirror_out queued {entry.get('id')} ({type_name}): {e}")
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
        deduped = _dedupe_records(pending)
        _push(cfg, base_dir, deduped)
        _queue_clear(base_dir)
        return {"flushed": len(deduped)}
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


def _record_timestamp(entry):
    """The entry's newest timestamp, for watermark advancement. Import server
    lazily so this module stays free of an import-time server dependency."""
    try:
        import server
        dt = server._entry_timestamp(entry)
        return dt.isoformat() if dt else None
    except Exception:
        return (entry.get("verified_at") or entry.get("updated_at")
                or entry.get("created_at"))


def _merge_records(base_dir, records):
    """Merge remote records into the local store, ADDITIVELY.

    A record whose entry id already exists locally is skipped -- the local
    copy is canonical and is never overwritten by a pulled entry. Returns
    (added, skipped, max_timestamp_seen).
    """
    import server  # lazy: avoids import-time circularity

    # Bucket incoming records by store type.
    by_type = {}
    max_ts = None
    for rec in records:
        tname = rec.get("type")
        entry = rec.get("entry")
        if tname not in _TYPE_FILES or not isinstance(entry, dict) or not entry.get("id"):
            continue
        by_type.setdefault(tname, []).append(entry)
        ts = _record_timestamp(entry)
        if ts and (max_ts is None or ts > max_ts):
            max_ts = ts

    added = 0
    skipped = 0
    for tname, incoming in by_type.items():
        path = os.path.join(base_dir, _TYPE_FILES[tname])
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

    return added, skipped, max_ts


def pull_remote(base_dir=None):
    """Fetch entries newer than the local watermark and merge them in.

    Fail-soft: any transport/parse error returns an error status without
    raising, so wiring this into SessionStart can never break a session.
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
    query = {"project": _project_name(base_dir)}
    if watermark:
        query["since"] = watermark
    url = cfg["url"] + "/entries?" + urllib.parse.urlencode(query)

    try:
        resp = _http("GET", url, cfg)
    except Exception as e:
        _log(base_dir, f"pull_remote failed: {e}")
        return {"pulled": 0, "error": str(e)}

    records = resp.get("records") if isinstance(resp, dict) else None
    if not records:
        return {"pulled": 0, "skipped": 0}

    try:
        added, skipped, max_ts = _merge_records(base_dir, records)
    except Exception as e:
        _log(base_dir, f"pull_remote merge failed: {e}")
        return {"pulled": 0, "error": str(e)}

    # Advance the watermark to the newest timestamp we saw so the next pull
    # only asks for entries after it. Merge is idempotent (additive by id),
    # so a boundary re-fetch is harmless if the remote uses >= semantics.
    if max_ts and (watermark is None or max_ts > watermark):
        _watermark_write(base_dir, max_ts)

    if added:
        _log(base_dir, f"pull_remote merged {added} new entries ({skipped} already present)")
    return {"pulled": added, "skipped": skipped, "watermark": max_ts or watermark}


# ------------------------------------------------------------------
# BACKFILL (push the entire local store to the remote in one batch)
# ------------------------------------------------------------------


def backfill_remote(base_dir=None):
    """Push ALL local entries for the project to the remote in one batch.

    Idempotent on the remote (import is upsert-by-id). Fail-soft. Returns a
    status dict with per-store counts.
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
    records = []
    counts = {}
    for tname, fname in _TYPE_FILES.items():
        entries = server.read_json_file(os.path.join(base_dir, fname))
        counts[tname] = len(entries)
        for e in entries:
            if e.get("id"):
                records.append({"type": tname, "entry": e})

    if not records:
        return {"backfilled": 0, "counts": counts}

    try:
        resp = _http("POST", cfg["url"] + "/import", cfg,
                     {"project": _project_name(base_dir), "records": records})
    except Exception as e:
        _log(base_dir, f"backfill_remote failed ({len(records)} records): {e}")
        return {"backfilled": 0, "error": str(e), "counts": counts}

    _log(base_dir, f"backfill_remote pushed {len(records)} records")
    return {
        "backfilled": len(records),
        "counts": counts,
        "remote_response": resp if isinstance(resp, dict) else None,
    }
