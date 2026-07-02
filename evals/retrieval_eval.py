"""
Retrieval-quality eval for context-keeper, built on llm-evals.

The question this measures: given a natural-language query a *future session*
would actually ask, does `get_context` surface the entry that genuinely answers
it? That is the one thing the tool exists to do, and nothing in the test suite
measures it today.

Pieces:
  ContextKeeperRetriever — executor. Runs the real `handle_get_context` against a
      target `.context` store and returns the ordered list of retrieved entry ids.
  RetrievalScorer        — scorer. hit@k (passed) + reciprocal rank (score)
      against a gold set of acceptable entry ids.

Mapping onto llm-evals so the baseline/regression machinery works unchanged:
  passed = hit@k   -> RunResult.pass_rate == hit@k rate
  score  = recip.  -> RunResult.avg_score == MRR

Default config is deliberately `include_related=False`, so the score isolates the
*scorer's* relevance ranking. Re-run with `--include-related` to measure how much
the `related_to` arc traversal contributes on top — a clean ablation.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_CK_ROOT = _THIS.parent.parent              # context-keeper/
_LLM_EVALS_ROOT = _CK_ROOT.parent / "llm-evals"

# context-keeper's server is a stdlib-only module (MCP loop is under __main__),
# so importing it just gives us the pure retrieval handlers.
sys.path.insert(0, str(_CK_ROOT))
import server  # noqa: E402

# llm-evals provides the ScorerResult contract.
sys.path.insert(0, str(_LLM_EVALS_ROOT))
from core.scorers import ScorerResult  # noqa: E402

_REPOS_ROOT = _CK_ROOT.parent


def resolve_store(input, default):
    """A case may name its own store (repo-relative); else use the executor default."""
    if isinstance(input, dict) and input.get("store"):
        return str(_REPOS_ROOT / input["store"])
    return str(default)


class ContextKeeperRetriever:
    """Executor: query -> comma-separated ordered entry ids actually returned."""

    def __init__(self, project_dir, token_budget=None, include_related=False,
                 types=None, mmr=None):
        self.project_dir = str(project_dir)
        self.token_budget = token_budget
        self.include_related = include_related
        self.types = types
        self.mmr = mmr  # dict like {"enabled": True, "lambda": 0.7} or None
        self.name = "context_keeper_get_context"

    def __call__(self, input):
        params = {"query": input["query"]} if isinstance(input, dict) else {"query": str(input)}
        params["project_dir"] = resolve_store(input, self.project_dir)
        params["include_related"] = self.include_related
        if self.token_budget is not None:
            params.setdefault("token_budget", self.token_budget)
        if self.types is not None:
            params.setdefault("types", self.types)
        if self.mmr is not None:
            params["mmr"] = self.mmr

        result = server.handle_get_context(params)
        if not isinstance(result, dict) or "results" not in result:
            # surfaces unresolved-project / uninitialized errors as a visible miss
            raise RuntimeError(f"get_context returned: {result}")
        ids = [r.get("entry", {}).get("id", "") for r in result["results"]]
        return ",".join(i for i in ids if i)


def _parse_ids(s):
    return [t for t in str(s).replace(",", " ").split() if t]


class RetrievalScorer:
    """Scorer: hit@k (passed) + reciprocal rank (score) against a gold id set."""

    def __init__(self, k=5):
        self.k = k
        self.name = f"retrieval@{k}"

    def __call__(self, actual, expected):
        retrieved = _parse_ids(actual)
        gold = set(_parse_ids(expected))

        rank = next((i + 1 for i, rid in enumerate(retrieved) if rid in gold), None)
        hit = rank is not None and rank <= self.k
        rr = (1.0 / rank) if hit else 0.0

        if not retrieved:
            reason = "no entries returned"
        elif hit:
            reason = f"gold at rank {rank} (within top-{self.k}); RR={rr:.3f}"
        elif rank is not None:
            reason = f"gold at rank {rank}, OUTSIDE top-{self.k}"
        else:
            reason = f"gold {sorted(gold)} absent from {len(retrieved)} returned; top: {retrieved[:self.k]}"

        return ScorerResult(passed=hit, score=round(rr, 4), reason=reason)
