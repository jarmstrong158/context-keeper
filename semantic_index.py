"""Optional semantic retrieval layer for context-keeper.

Stdlib-only and opt-in. When `semantic.enabled` is set in a project's config,
`query_cosines` returns a {entry_id: cosine} map that the retriever blends into
its lexical score, rescuing vocabulary-mismatch queries (asking about a "value
network diverging" finds an entry about a "value head saturating", though they
share no keywords).

Embeddings come from a local Ollama server (the same nomic-embed-text the rest of
the toolchain uses). If Ollama is unreachable or the model is missing,
`query_cosines` returns None and the caller falls back to pure lexical ranking —
so there is no hard dependency and zero-dep stays the default.

Per-store embeddings are cached in `.context/embeddings.json`, keyed by entry id
plus a hash of the entry text, so an edited entry is re-embedded automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request

_TEXT_FIELDS = (
    "summary", "name", "rule", "problem", "why_chosen", "rationale",
    "reason", "purpose", "what_we_tried", "tradeoffs", "when_to_invoke",
    "triggering_incident",
)


def entry_text(entry):
    parts = [str(entry[k]) for k in _TEXT_FIELDS if entry.get(k)]
    parts += [str(t) for t in entry.get("tags", [])]
    for step in entry.get("steps", []):
        if step.get("action"):
            parts.append(str(step["action"]))
    return "  ".join(parts)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    ma = sum(x * x for x in a) ** 0.5
    mb = sum(x * x for x in b) ** 0.5
    return dot / (ma * mb) if ma and mb else 0.0


# How many documents to embed per /api/embed request when back-filling the
# cache. Bounds request payload size on large stores.
_EMBED_BATCH_SIZE = 64


class _Embedder:
    def __init__(self, model, url):
        self.model = model
        self.url = url.rstrip("/")

    def _post(self, texts, kind, timeout):
        """POST a batch to /api/embed. Ollama accepts a list input and
        returns one embedding per text. Raises on any failure — callers
        treat that as 'semantic unavailable' and fall back to lexical."""
        prefix = "search_query: " if kind == "query" else "search_document: "
        payload = json.dumps({
            "model": self.model,
            "input": [prefix + t for t in texts],
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["embeddings"]

    def embed(self, text, kind):
        # The query embed doubles as the availability check: if Ollama is
        # down or the model is missing this raises, and query_cosines
        # returns None. Saves the /api/tags round-trip per query that the
        # old available() pre-check cost.
        return self._post([text], kind, timeout=10)[0]

    def embed_batch(self, texts):
        return self._post(texts, "document", timeout=120)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def query_cosines(query, entries, base_dir, sem_cfg):
    """Return {entry_id: cosine(query, entry)} or None if semantic is unavailable.

    Returning None (not {}) tells the caller to fall back to lexical-only ranking.
    """
    if not query:
        return None
    emb = _Embedder(sem_cfg.get("model", "nomic-embed-text"),
                    sem_cfg.get("url", "http://localhost:11434"))
    try:
        q_vec = emb.embed(query, "query")
    except Exception:
        return None

    cache_path = os.path.join(base_dir, "embeddings.json")
    cache = _load(cache_path)

    # Split entries into cache hits and misses, then embed all misses in
    # batched requests instead of one HTTP round-trip per entry (a cold
    # store embeds in a handful of calls rather than N).
    vecs = {}
    misses = []  # (eid, text, hash)
    for e in entries:
        eid = e.get("id")
        if not eid:
            continue
        text = entry_text(e)
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        rec = cache.get(eid)
        if rec and rec.get("hash") == h:
            vecs[eid] = rec["vec"]
        else:
            misses.append((eid, text, h))

    dirty = False
    for i in range(0, len(misses), _EMBED_BATCH_SIZE):
        batch = misses[i:i + _EMBED_BATCH_SIZE]
        try:
            batch_vecs = emb.embed_batch([text for _eid, text, _h in batch])
        except Exception:
            continue  # skip this batch; cached/other entries still score
        for (eid, _text, h), vec in zip(batch, batch_vecs):
            cache[eid] = {"hash": h, "vec": vec}
            vecs[eid] = vec
            dirty = True

    if dirty:
        _save(cache_path, cache)
    return {eid: _cosine(q_vec, vec) for eid, vec in vecs.items()}
