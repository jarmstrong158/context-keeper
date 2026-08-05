# Context Keeper — Metrics Report

**Date:** 2026-07-08 · **Commit evaluated:** `941770a` · **Python:** 3.11 (Linux container)

This report records what the repo's eval suite measures, what actually ran in
this environment, and the numbers produced. Every value here is **measured**, not
estimated. Where an eval needed a corpus or service that isn't present in this
container, that is stated explicitly and the metric was reproduced against a
**synthetic corpus** (clearly labeled), never faked.

## Environment & honesty notes (read before citing)

- **Synthetic corpus.** The shipped evals point at real project stores
  (`Clark`, `Conductor`, `context-keeper`) and, for the retrieval runner, a
  sibling `llm-evals` repo. None are present in a fresh clone. So a realistic
  synthetic store was built in Context Keeper's **actual schema** via the real
  `record_*` handlers — a fictional task-queue service, "Orion": **15 decisions,
  7 constraints, 3 pipelines** (26 curated entries), with rationale fields, tags,
  `related_to` links, origins, and deliberate vocabulary-mismatch query cases. A
  larger 70-entry variant (curated + templated filler) is used only for the
  token-scaling measurement. All results below are on this synthetic corpus and
  are labeled as such.
- **Token counts use the server's estimator** (`len(text) // 4`), not a real
  BPE tokenizer. Both sides of every ratio use the same estimator, so ratios are
  meaningful; absolute token counts are approximate (±~15% vs a real tokenizer).
- **Retrieval numbers here are lexical-only.** The optional semantic blend could
  not run (no embedder — see "Not runnable"). On real data the repo documents
  semantic raising hit@5 from 80% → 93%; that uplift is **not reproduced here**.
- **Synthetic content is cleaner than real notes.** Hand-written entries have
  crisper wording than messy field notes, so retrieval numbers here may be
  optimistic relative to a real, sprawling store.

## Summary

| Metric | Value (synthetic corpus) | Plain English | How measured |
|---|---|---|---|
| **Session-start token reduction** | **88.3%** (26-entry store) → **92.0%** (70-entry) | The memory briefing injected at session start costs ~1/10th of pasting the whole store. | `get_project_summary` tokens vs full active-store JSON dump (`token_reduction.py`). |
| **Injected-cost growth** | 2.7× more entries → **1.8× injected tokens** (638 → 1,172) | The store can grow a lot without the per-session context cost growing proportionally (capped at the summary budget). | Same, across the 26- and 70-entry stores. |
| **Per-question cold vs injection** | **−82%** (5,470 → ~990 tokens) | Answering one project question pulls ~1k tokens of relevant memory instead of the whole 5.5k-token store. | `get_context` at a 1,000-token budget vs full-store dump, 3 questions. |
| **Retrieval hit@5** | **86.7%** (13/15) | For ~87% of natural-language questions, the entry that answers it is in the top 5 returned. | `get_context` top-5 vs a labeled gold set (`synthetic_corpus.py` scorer). Superseded on real data by `evals/run_retrieval_eval.py` -- see `evals/README.md`. |
| **Retrieval MRR** | **0.733** | The right entry is usually at or near rank 1. | Mean reciprocal rank of the first gold hit. |
| **Abstention (no floor)** | **100% confabulation** | Without the honesty floor, every no-answer query gets a confident-looking top result. | Baseline, by construction. |
| **Abstention (floor 0.20, shipped default)** | **50% caught, 0% false-abstention** | The floor flags half the no-answer queries as "nothing relevant" while never wrongly abstaining on a real one. | `_relevance_signal` sweep (`abstention.py`). |
| **Abstention (floor 0.25)** | **83% caught, 0% false-abstention** | A slightly higher floor catches 5/6 no-answer queries, still zero false abstentions on this corpus. | Same sweep. |
| **MMR redundancy@5** | **0.088 → 0.076 (−14%), hit@5 flat** | Diversity reordering trims near-duplicate results without dropping the right answer. | Pairwise Jaccard of top-5, MMR off vs on (`mmr_check.py`). |

## What's impressive to a general audience (ranked) + suggested phrasings

1. **Session-start token reduction (88–92%).** Most citable.
   > *"Context Keeper injects your project's decisions and constraints at session
   > start for ~90% fewer tokens than re-pasting the project — so the assistant
   > starts oriented without eating your context window."*
   ⚠️ **Caveat required:** the summary is capped by a token budget, so on large
   stores part of the percentage is by construction. The honest, durable claim is
   the **growth** number (#2), not a single headline percentage.

2. **Per-question cold vs injection (−82%).** Concrete and relatable.
   > *"Ask a question about the project and it pulls ~1,000 tokens of the
   > relevant past decisions, versus ~5,500 to paste the whole store — 80%+ less
   > context to answer the same question."*
   Clean because it's a like-for-like, per-question measurement at a stated budget.

3. **Retrieval hit@5 = 86.7% (MRR 0.733).**
   > *"For ~87% of natural-language questions — including ones worded nothing like
   > the original note — the decision that answers it lands in the top 5 results."*
   ⚠️ **Caveat:** lexical-only here; a real deployment with semantic retrieval on
   would differ (repo documents higher on real data). Synthetic wording is clean.

## ⚠️ Claims to avoid / correct

- **"100% → 0% confabulation" is NOT supported by measurement.** With no floor,
  confabulation is 100% by construction; with the shipped 0.20 floor the measured
  catch is **50%** (and **83%** at 0.25) — not 100%. The honest framing is
  *"eliminates the always-confident baseline, catching 50–83% of no-answer
  queries at zero false-abstention,"* with the known limit that hard-negatives
  sharing vocabulary (e.g. asking how "job records" are *encrypted* when only the
  records exist) score ~0.50 and still slip through. A lexical signal cannot fully
  separate those; that's a field-wide hard problem.
- **Don't quote a single token-reduction percentage as a fixed property** — it
  scales with store size and budget. Pair it with the setup.
- **Don't present any number as real-world** — it is a synthetic corpus.

## Per-eval detail & exact commands

All commands run from the repo root.

### Reproduce everything (synthetic corpus)
```bash
python evals/synthetic_corpus.py --out /tmp/ck-synth
```
Builds the synthetic store(s) via the real `record_*` handlers and reproduces the
token-reduction, retrieval, abstention, MMR, and cold-vs-injection methodologies
using the same `server` functions the shipped evals use. Deterministic (idempotent
build).

### 1. Token reduction — `token_reduction.py` (shipped script, ran directly)
```bash
python evals/synthetic_corpus.py --out /tmp/ck-synth      # build the stores
python evals/token_reduction.py /tmp/ck-synth             # curated (26 entries)
```
Measured: `| ck-synth | 26 | ~5,470 | ~638 | 88.3% |`; 70-entry variant → 92.0%
(`~14,719` → `~1,172`). **Runnable as shipped**, given a store.

### 2. Retrieval hit@k / MRR — synthetic-corpus methodology
The real measurement now lives in `evals/run_retrieval_eval.py`, which runs
anywhere against a frozen corpus. This section is the synthetic-store
reproduction for containers with no stores at all. The
scorer logic (rank of first gold in top-k; RR) is reproduced in
`synthetic_corpus.py` against 15 labeled NL→entry queries (incl. 3
vocabulary-mismatch). Measured: **hit@5 = 86.7% (13/15), MRR = 0.733**. The two
misses are a pipeline and a decision out-ranked by many deployment-tagged entries
for a generic "deploy"/"release" query — an honest characteristic, not a bug.

### 3. Abstention — `abstention.py` methodology
Not runnable as shipped (its baked-in stores/queries reference absent corpora).
Reproduced via `_relevance_signal` over 15 answerable + 6 no-answer queries:

| floor | TNR (abstain on no-answer) | false-abstention (miss real) |
|---|---|---|
| 0.20 (default) | 50% | 0% |
| 0.25 | 83% | 0% |
| 0.30 | 83% | 0% |

Positive relevance min/med/max = 0.50 / 0.67 / 1.00; no-answer = 0.00 / 0.20 / 0.50.

### 4. MMR diversity — `mmr_check.py` methodology
Reproduced: redundancy@5 **0.088 → 0.076** with MMR on; **hit@5 unchanged (86.7%)**.
Effect is small because the curated store has little near-duplication — MMR earns
its keep as a store accumulates restatements, which this fixture doesn't have.

### 5. Cold vs `get_context` injection (bonus)
Measured at a 1,000-token budget, `include_related=True`:

| Question | Cold (full dump) | Injected | Reduction |
|---|---|---|---|
| what stops the same job from running twice | 5,470 | 999 (4 entries) | −81.7% |
| how do we handle a job that keeps failing | 5,470 | 990 (4 entries) | −81.9% |
| what is the deploy procedure | 5,470 | 971 (4 entries) | −82.2% |

## Not runnable in this container (and exactly why)

| Eval | Blocker | Consequence |
|---|---|---|
| `run_eval.py`, `retrieval_eval.py` | **Removed 2026-08-05.** Needed the sibling `../llm-evals` repo, so they could never run in CI or a fresh clone. | Replaced by `evals/run_retrieval_eval.py`, which is self-contained and ships a frozen corpus. |
| `abstention.py`, `mmr_check.py` (as shipped) | Real `.context` stores referenced by `combined_retrieval.json` absent. | Baked-in corpora missing; methodology reproduced on synthetic corpus. |
| `semantic.py` / `run_retrieval_eval.py --arm embedding` | No local embedder — Ollama + `nomic-embed-text` not reachable (`localhost:11434` down). | Semantic blend cannot be exercised; the documented lexical→semantic hit@5 uplift (80%→93% on real data) is **not reproduced here** and is not claimed. |

## Reproducibility

```bash
# from repo root, commit 941770a
python evals/synthetic_corpus.py --out /tmp/ck-synth   # full synthetic report
python evals/token_reduction.py  /tmp/ck-synth         # shipped script, cross-check
```
The synthetic corpus is generated deterministically by `evals/synthetic_corpus.py`
(no network, no embedder, stdlib only), so anyone can reproduce these exact numbers.
To run the shipped retrieval/abstention/semantic evals with real numbers, provide
the `../llm-evals` repo, real `.context` stores, and a local Ollama embedder.
