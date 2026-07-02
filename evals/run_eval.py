"""
Run the context-keeper retrieval eval.

  python run_eval.py                    # Clark store, top-5, scorer-only (no arc traversal)
  python run_eval.py --set-baseline     # save this run as the regression baseline
  python run_eval.py --include-related  # ablation: let related_to traversal help retrieval
  python run_eval.py -k 3 --budget 8000 # tighter top-k, larger token budget
  python run_eval.py --store ../vanguard # point at a different .context store

hit@k == RunResult.pass_rate, MRR == RunResult.avg_score, so the llm-evals
baseline/regression tooling works on retrieval metrics for free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_CK_ROOT = _THIS.parent.parent
_LLM_EVALS_ROOT = _CK_ROOT.parent / "llm-evals"
sys.path.insert(0, str(_LLM_EVALS_ROOT))
sys.path.insert(0, str(_THIS.parent))

from core import runner as ev_runner       # noqa: E402
from core import baseline as ev_baseline    # noqa: E402
from retrieval_eval import ContextKeeperRetriever, RetrievalScorer  # noqa: E402
from semantic import SemanticBlendedRetriever  # noqa: E402

_DATASET = _THIS.parent / "datasets" / "combined_retrieval.json"
_RUNS_DIR = _THIS.parent / "runs"


def main():
    ap = argparse.ArgumentParser(description="context-keeper retrieval eval")
    ap.add_argument("--store", default=str(_CK_ROOT.parent / "Clark"),
                    help="project dir containing the .context store to query")
    ap.add_argument("--dataset", default=str(_DATASET))
    ap.add_argument("-k", type=int, default=5, help="top-k for hit@k / MRR")
    ap.add_argument("--budget", type=int, default=None, help="override token_budget")
    ap.add_argument("--include-related", action="store_true",
                    help="enable related_to arc traversal (ablation)")
    ap.add_argument("--semantic", action="store_true",
                    help="blend semantic (nomic-embed) similarity into ranking")
    ap.add_argument("--sem-weight", type=float, default=50.0,
                    help="weight on the cosine term when --semantic (default 50)")
    ap.add_argument("--split", choices=["dev", "test", "all"], default="all",
                    help="filter cases by their split tag")
    ap.add_argument("--mmr", action="store_true", help="enable MMR diversity reorder")
    ap.add_argument("--mmr-lambda", type=float, default=0.7)
    ap.add_argument("--set-baseline", action="store_true")
    args = ap.parse_args()

    cases = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.split != "all":
        cases = [c for c in cases if c.get("split") == args.split]
    stem = Path(args.dataset).stem
    dataset_name = stem if args.split == "all" else f"{stem}_{args.split}"

    if args.semantic:
        executor = SemanticBlendedRetriever(project_dir=args.store, sem_weight=args.sem_weight)
        if executor.fell_back:
            print("WARNING: embedder unavailable -- semantic disabled, ranking lexical-only")
        else:
            print(f"semantic blend ON (nomic-embed, sem_weight={args.sem_weight})")
    else:
        executor = ContextKeeperRetriever(
            project_dir=args.store,
            token_budget=args.budget,
            include_related=args.include_related,
            mmr={"enabled": True, "lambda": args.mmr_lambda} if args.mmr else None,
        )
    scorer = RetrievalScorer(k=args.k)
    run = ev_runner.run(executor, cases, scorer, dataset_name=dataset_name)

    print()
    for c in run.cases:
        mark = "PASS" if c.passed else "FAIL"
        q = c.input.get("query") if isinstance(c.input, dict) else c.input
        print(f"[{mark}] {c.case_id}  RR={c.score:<6} {q}")
        if not c.passed:
            print(f"         gold={c.expected}  ::  {c.reason or c.error.splitlines()[-1:]} ")
    print()
    print(f"hit@{args.k}: {run.pass_rate:.1%}    MRR: {run.avg_score:.3f}    "
          f"({run.passed}/{run.total} cases)    include_related={args.include_related}")

    report = ev_baseline.compare(run, runs_dir=_RUNS_DIR)
    if report:
        tag = "REGRESSION" if report.is_regression else "ok"
        print(f"vs baseline: hit@k {report.baseline_pass_rate:.1%} -> "
              f"{report.current_pass_rate:.1%}  (delta {report.delta:+.1%})  [{tag}]")
        if report.newly_failing:
            print(f"  newly failing: {report.newly_failing}")
        if report.newly_passing:
            print(f"  newly passing: {report.newly_passing}")
    else:
        print("no baseline yet -- run with --set-baseline to set one")

    ev_baseline.save_run(run, runs_dir=_RUNS_DIR)
    if args.set_baseline:
        path = ev_baseline.save_baseline(run, runs_dir=_RUNS_DIR)
        print(f"baseline saved: {path}")


if __name__ == "__main__":
    main()
