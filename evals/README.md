# context-keeper retrieval evals

## Token reduction (`token_reduction.py`)

Measures what session-start injection costs vs. the naive baseline of dumping
the full store into context: `python token_reduction.py <project_dir> ...`.
Results across four real stores (2026-07-03):

| store | active entries | full store (tokens) | injected at session start | reduction |
|---|---|---|---|---|
| balatron | 78 | ~75,277 | ~2,057 | 97.3% |
| clark | 55 | ~35,445 | ~2,102 | 94.1% |
| context-keeper | 13 | ~5,692 | ~828 | 85.5% |
| conductor | 9 | ~1,538 | ~411 | 73.3% |

Caveats stated in the script: token counts are the server's chars/4 estimate
(same estimator on both sides, so the ratio is meaningful), and the summary is
budget-capped, so large-store reduction is partly by construction — the real
property is injected cost staying flat as stores grow. Running this
measurement found a real bug: the truncation loop never recomputed its
estimate, so over-budget stores injected an *empty* summary (fixed in v0.9).

## Abstention (`abstention.py`)

Measures the question hit@k can't: when **nothing relevant** is stored, does
`get_context` correctly signal `no_confident_match`, or confabulate a
confident-looking top result? `python abstention.py` sweeps the relevance
floor and reports the trade-off.

**Finding (2026-07-03):** with no floor, confabulation is **100%** — every
no-answer query returns a top result that looks like an answer, because the
composite score banks ~55 points from recency/status/origin regardless of
relevance. The fix keys the honesty signal on the *relevance signal*
(tag/text overlap only) and **annotates rather than suppresses** (weak
matches are still returned, just flagged — so vocab-mismatch recall
survives).

| floor | TNR (abstain on no-answer) | false-abstention (miss real) |
|---|---|---|
| 0.15 | 38% | 0% |
| **0.20** (default) | **38%** | **0%** |
| 0.25 | 56% | 3% |
| 0.30 | 75% | 16% |

0.20 is the highest floor with zero false-abstention (no positive query
falls below it). Honest limit: hard-negatives that share real topic
vocabulary ("compaction snapshots" present, "encrypted" absent) score ~0.50
and still slip through — a lexical signal cannot separate them; the opt-in
semantic blend is what helps there, and even dense retrieval struggles on
this (a known-hard problem across the field).

## Retrieval quality

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
