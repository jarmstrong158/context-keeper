"""
Semantic-blend retriever for context-keeper — opt-in, lexical fallback.

Reuses context-keeper's existing `score_entry` for the lexical signal and ADDS a
semantic term: cosine(query_embedding, entry_embedding) * sem_weight. The point
is to rescue the vocabulary-mismatch cases the lexical matcher misses (a query
about the "value network diverging" should find the entry about the "value head
saturating", though they share no keywords).

Embeddings come from Ollama's nomic-embed-text — the same model llm-evals'
SemanticSimilarity scorer assumes — using nomic's recommended task prefixes
(search_query: / search_document:) and an on-disk cache so reruns are instant.

If Ollama or the model is unavailable, the retriever falls back to pure lexical
ranking and sets `.fell_back = True`. Zero-dep stays the default; this is the
opt-in ceiling-raise.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_CK_ROOT = _THIS.parent.parent
sys.path.insert(0, str(_CK_ROOT))
import server  # noqa: E402

_CACHE_PATH = _THIS.parent / ".emb_cache.json"

_TEXT_FIELDS = (
    "summary", "name", "rule", "problem", "why_chosen", "rationale",
    "reason", "purpose", "what_we_tried", "tradeoffs", "when_to_invoke",
    "triggering_incident",
)


def entry_text(entry):
    """Natural-language blob for embedding an entry (mirrors score_entry's fields)."""
    parts = [str(entry[k]) for k in _TEXT_FIELDS if entry.get(k)]
    parts += [str(t) for t in entry.get("tags", [])]
    for step in entry.get("steps", []):
        if step.get("action"):
            parts.append(str(step["action"]))
    return "  ".join(parts)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    ma = sum(x * x for x in a) ** 0.5
    mb = sum(x * x for x in b) ** 0.5
    return dot / (ma * mb) if ma and mb else 0.0


class OllamaEmbedder:
    def __init__(self, model="nomic-embed-text", host="localhost", port=11434):
        self.model = model
        self.url = f"http://{host}:{port}"
        self._available = None
        self._cache = {}
        if _CACHE_PATH.exists():
            try:
                self._cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def available(self):
        if self._available is not None:
            return self._available
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=4) as r:
                models = json.loads(r.read()).get("models", [])
            self._available = any(self.model.split(":")[0] in m.get("name", "") for m in models)
        except Exception:
            self._available = False
        return self._available

    def _key(self, text):
        return hashlib.md5(f"{self.model}\x00{text}".encode("utf-8")).hexdigest()

    def embed(self, text, kind="document"):
        prefix = "search_query: " if kind == "query" else "search_document: "
        full = prefix + text
        key = self._key(full)
        if key in self._cache:
            return self._cache[key]
        payload = json.dumps({"model": self.model, "input": full}).encode()
        req = urllib.request.Request(
            f"{self.url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            vec = json.loads(resp.read())["embeddings"][0]
        self._cache[key] = vec
        return vec

    def flush(self):
        try:
            _CACHE_PATH.write_text(json.dumps(self._cache), encoding="utf-8")
        except Exception:
            pass


class SemanticBlendedRetriever:
    """Executor: blended (lexical + semantic) ranking -> top-k entry ids."""

    def __init__(self, project_dir, sem_weight=50.0, k_return=15,
                 embedder=None, types=None):
        self.project_dir = str(project_dir)
        self.sem_weight = sem_weight
        self.k_return = k_return
        self.embedder = embedder or OllamaEmbedder()
        self.types = types or ["decisions", "pipelines", "constraints"]
        self.name = f"semantic_blend(w={sem_weight})"
        self.fell_back = not self.embedder.available()
        self._entry_cache = {}

    def _entries(self, store_dir):
        if store_dir in self._entry_cache:
            return self._entry_cache[store_dir]
        base = server._base_dir_from_params({"project_dir": store_dir})
        paths = server._resolve_paths(base)
        out = []
        for tname in self.types:
            if tname in paths:
                for e in server.read_json_file(paths[tname]):
                    if e.get("status", "active") != "deprecated":
                        out.append(e)
        self._entry_cache[store_dir] = out
        return out

    def __call__(self, input):
        from retrieval_eval import resolve_store
        query = input.get("query") if isinstance(input, dict) else str(input)
        now = datetime.now(timezone.utc)
        entries = self._entries(resolve_store(input, self.project_dir))

        use_sem = not self.fell_back
        q_emb = self.embedder.embed(query, "query") if use_sem else None

        scored = []
        for e in entries:
            lex = server.score_entry(e, None, query, None, now)
            sem = 0.0
            if use_sem:
                sem = cosine(q_emb, self.embedder.embed(entry_text(e), "document"))
            scored.append((lex + self.sem_weight * sem, e.get("id", "")))

        scored.sort(key=lambda x: x[0], reverse=True)
        if use_sem:
            self.embedder.flush()
        return ",".join(eid for _s, eid in scored[: self.k_return] if eid)
