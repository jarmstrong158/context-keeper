# context-keeper retrieval evals

Measures the one thing the test suite doesn't: **given a natural-language query a
future session would actually ask, does `get_context` surface the entry that
answers it?** Built on [llm-evals](../../llm-evals) as the engine.

## Run

```bash
cd context-keeper/evals
python run_eval.py                         # combined dataset, all splits, lexical
python run_eval.py --semantic              # blend nomic-embed cosine into ranking
python run_eval.py --split dev --semantic --sem-weight 150   # tune on dev
python run_eval.py --split test --set-baseline               # held-out baseline
python run_eval.py --include-related       # ablation: let related_to traversal help
```

The combined dataset spans three stores (Clark, Conductor, context-keeper); each
case carries its own `store` and a `dev`/`test` `split`. Tune weights on `dev`,
report on the held-out `test` split.

Requires the `llm-evals` repo checked out as a sibling of `context-keeper`.
No Ollama / network needed — pure lexical-vs-gold measurement.

## Metrics

- **hit@k** = `RunResult.pass_rate` — was a gold entry in the top-k returned?
- **MRR** = `RunResult.avg_score` — mean reciprocal rank of the first gold hit.

Mapping retrieval metrics onto `passed`/`score` means the llm-evals
baseline/regression tooling (`runs/baseline_clark_retrieval.json`) works
unchanged: every retriever change is now measured against the baseline.

## Files

- `datasets/clark_retrieval.json` — labeled `(query -> gold entry ids)` cases
  drawn from Clark's real decision log. Several are deliberate
  *vocabulary-mismatch* traps (the query and the entry share no keywords).
- `retrieval_eval.py` — `ContextKeeperRetriever` (executor) + `RetrievalScorer`.
- `run_eval.py` — runner, report, baseline compare.

## Results (2026-06-17)

**Lexical baseline:** `hit@5 = 70%, MRR = 0.477`. All six misses are
vocabulary-mismatch cases (query phrased differently than the entry). Arc
traversal (`--include-related`) recovers none of them.

**Semantic blend** (`--semantic`, nomic-embed cosine added to `score_entry`):

| sem_weight | hit@5 | MRR |
|---|---|---|
| 0 (lexical) | 70% | 0.477 |
| 50 | 80% | 0.650 |
| 100 | 85% | 0.696 |
| 150 | 85% | **0.752** |
| 400 | **90%** | 0.704 |

MRR peaks near weight ~150; very high weight buys raw coverage at the cost of
demoting exact-match hits (the MRR dip). Recommended integration default:
~100–150, opt-in, lexical fallback when the embedder is unavailable.

**Held-out result** (`combined_retrieval.json`, 31 cases / 3 stores, weight tuned
on `dev` only, reported on the `test` split it never saw):

| test split | hit@5 | MRR |
|---|---|---|
| lexical baseline | 80.0% | 0.633 |
| semantic w=150 (tuned on dev) | **93.3%** | **0.880** |

The gain generalizes across stores (Clark RL, Conductor process-locking,
context-keeper meta), not just Clark's vocabulary, and is robust to weight
(w=150 vs w=300 differ by <0.01 MRR on test). This justified shipping the
opt-in semantic blend in `server.py` (`semantic.enabled` in config).
