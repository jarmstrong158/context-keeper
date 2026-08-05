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

## Retrieval quality (v2: `run_retrieval_eval.py`, 2026-08-05)

Self-contained successor to the llm-evals-based harness below. No sibling repo,
no network in the lexical arm, and it runs against **copies** of every store —
the semantic path writes an `embeddings.json` cache next to the entries it
embeds, so pointing it at real stores would mutate 23 project stores as a side
effect of measuring them.

```bash
python evals/run_retrieval_eval.py                 # lexical + embedding
python evals/run_retrieval_eval.py --arm lexical   # no network at all
python evals/run_retrieval_eval.py --all-arms      # adds embedding-heavy (w=400)
```

**Golden set:** `retrieval_golden.json` — 59 cases across 9 stores (44 positive,
9 negative, 6 history). Every question is written from the **problem the entry
solves** — the symptom you'd be staring at before you knew the entry existed —
never by paraphrasing its summary. That rule is the whole point: a query derived
from the summary shares surface tokens with its target, so lexical scoring hits
it for free and recall comes out inflated.

Cases may only be drawn from stores whose project repo is **public** — a case
quotes the entry it targets, and this repo is public (`con-015-12da`).

**Results (2026-08-05, 59 cases, `include_related=False`):**

| arm | recall@1 | recall@3 | recall@5 | hit@5 | MRR | FPR |
|---|---|---|---|---|---|---|
| lexical | 0.220 | 0.370 | 0.483 | 0.560 | 0.383 | 0.667 |
| embedding (w=150) | **0.390** | **0.554** | **0.691** | **0.760** | **0.565** | 0.667 |

`recall@k` is strict — `|gold ∩ top-k| / |gold|`, so a case with 5 gold entries
cannot exceed 0.2 at k=1. `hit@5` (any gold in top 5) is carried for
comparability with the 2026-06-17 numbers below.

**The embedding path earns its complexity.** +21 points recall@5 and +18 points
MRR over lexical, on a set specifically built to deny lexical its free tokens.
The margin is wider here than in the 2026-06-17 measurement precisely because
that older set was partly paraphrase-derived.

Per-project, the gap is entirely in the two big, prose-heavy stores: Clark
0.333 → 0.744 and context-keeper 0.398 → 0.741 recall@5. Small stores
(agentsync, meristem) are already at 1.000 lexically and embeddings add nothing
— which is the honest shape of the result. The blend earns its keep as a store
grows, not from the first entry.

Three findings the number surfaced:

1. **History is unreachable: recall@5 = 0.000 in *both* arms.** All six history
   cases ask explicitly for a prior state ("what was the original sync design
   before the newest-wins merge") and no superseded entry is returned in the top
   5 — usually not in the returned set at all. `score_entry` demotes superseded
   by 15 points *and* those entries are old, so recency compounds the demotion.
   Supersession is now first-class at write time and read time (`dec-022-730e`),
   and retrieval still cannot find the predecessor when you ask for it directly.
2. **Abstention misses in-domain negatives: 7 of 10** plausible-but-unanswerable
   questions come back with no `no_confident_match` flag, in both arms. Consistent
   with `abstention.py`'s TNR of 38% at the 0.20 floor — the honest limit noted
   there (hard negatives sharing real topic vocabulary) is what these are.
3. **The embedding arm can fail silently.** `query_cosines` returns `None` on any
   error and the caller drops to lexical, so a cold Ollama made the arm report
   the *lexical* number under a different name. The harness now probes with
   retries and refuses to run an arm it cannot prove is live.

`tests/test_retrieval_eval.py` pins the lexical arm's positive-case recall@5 so a
ranking change cannot quietly regress it, and pins the history gap at zero so a
fix to it cannot land unnoticed either. **That test owns those thresholds and
this README deliberately does not repeat them** (`con-009-6bdc`) — the table
above is a dated measurement, not the enforced value. Lexical is bit-for-bit
deterministic run to run; the test asserts that too.

## Retrieval quality (v1, llm-evals based)

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
