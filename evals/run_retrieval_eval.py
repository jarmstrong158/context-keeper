#!/usr/bin/env python3
"""Retrieval-quality eval for context-keeper's get_context.

The suite proves the store is CORRECT -- entries round-trip, budgets hold,
projections regenerate. None of it proves the store is USEFUL, which is a
different question with a different answer: given a natural-language question a
future session would actually ask, does get_context return the entry that
answers it? Without a number for that, the opt-in embedding path cannot be shown
to earn its complexity over the lexical fallback, and a ranking change cannot be
shown not to have made things worse.

Run:
    python evals/run_retrieval_eval.py                # both arms
    python evals/run_retrieval_eval.py --arm lexical  # no network at all
    python evals/run_retrieval_eval.py --all-arms     # adds embedding-heavy
    python evals/run_retrieval_eval.py --json out.json

READ-ONLY against real data. Every store is snapshotted into a scratch
directory and the eval runs against the COPY, never the live `.context`. That is
not paranoia -- get_context is not actually a read-only operation, twice over:

  * it records retrieval telemetry to `usage.json` on every call (usage.py),
  * and with the semantic arm on, query_cosines writes an `embeddings.json`
    cache next to the entries it embeds.

So a naive eval loop would write to all 23 stores as a side effect of measuring
them, and the usage signal -- which feeds verify_quality's `unused` check -- would
be polluted by thousands of synthetic queries that no human ever asked. Mirror
env vars are cleared for the same reason. A measurement that mutates its subject
is not a measurement.

METRICS
  recall@k  |gold and retrieved@k| / |gold|  -- the fraction of the entries that
            SHOULD have come back that did. Strict: a case with 5 gold entries
            cannot exceed 0.2 at k=1, and that is the honest reading.
  hit@k     did at least ONE gold entry land in the top k. Looser, and what the
            2026-06-17 measurement in evals/README.md reported, so it is carried
            here for comparability.
  MRR       mean reciprocal rank of the FIRST gold hit (0 when none).
  FPR       negatives only. A negative case has no correct answer, so the
            failure is presenting one anyway. get_context ANNOTATES rather than
            suppresses (dec-010), so returning entries is not itself wrong --
            returning them WITHOUT no_confident_match is. Both are reported:
            fpr_confident (the real failure) and returned_anything (context).
"""

import argparse
import json
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
REPOS_ROOT = os.path.dirname(REPO)

sys.path.insert(0, REPO)

# A measurement must not mirror. Cleared BEFORE server is imported so the module
# never sees a remote configured.
for _var in ("CONTEXT_KEEPER_REMOTE_URL", "CONTEXT_KEEPER_REMOTE_TIMEOUT"):
    os.environ.pop(_var, None)

import server  # noqa: E402

GOLDEN = os.path.join(HERE, "retrieval_golden.json")

# Retrieval is deterministic given a store and a query -- no sampling anywhere in
# score_entry. The seed is set so that stays true if any future ranking change
# introduces a tiebreak that is not.
SEED = 20260805

STORE_FILES = ("decisions.json", "pipelines.json", "constraints.json", "config.json")

# k values reported. 5 is the pinned one (see tests/test_retrieval_eval.py).
KS = (1, 3, 5)

ARMS = {
    # name: (description, params-patch)
    "lexical": (
        "lexical only, no network",
        {"semantic": {"enabled": False}},
    ),
    "embedding": (
        "lexical + embedding blend at the shipped default weight 150",
        {"semantic": {"enabled": True, "weight": 150}},
    ),
    "embedding_heavy": (
        "lexical + embedding blend at weight 400 (embedding-dominant)",
        {"semantic": {"enabled": True, "weight": 400}},
    ),
}
DEFAULT_ARMS = ("lexical", "embedding")


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


def resolve_store_dir(store):
    """A case's `store` -> the real directory holding its .context/."""
    if store.startswith("fixtures/"):
        return os.path.join(HERE, "fixtures", store.split("/", 1)[1])
    return os.path.join(REPOS_ROOT, store)


def snapshot(store, workdir):
    """Copy one store's .context into workdir. Returns the copy's project dir.

    The eval never touches the original. Copies are keyed by store name so the
    embedding cache written into a copy survives between runs in the same
    workdir, which is what makes the embedding arm tolerable to re-run.
    """
    src = os.path.join(resolve_store_dir(store), ".context")
    dst_project = os.path.join(workdir, store.replace("/", "__"))
    dst = os.path.join(dst_project, ".context")
    os.makedirs(dst, exist_ok=True)
    for name in STORE_FILES:
        s = os.path.join(src, name)
        if os.path.exists(s):
            d = os.path.join(dst, name)
            # Only re-copy when the source changed, so a warm embedding cache is
            # not invalidated on every run.
            if not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d):
                shutil.copy2(s, d)
    return dst_project


def load_golden(path=GOLDEN):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


def validate(cases):
    """Every gold id must exist in its store, and be superseded for history cases.

    A golden set that quietly references a renamed or deleted id measures
    nothing and reports a clean number while doing it.
    """
    problems = []
    seen_ids = set()
    for case in cases:
        if case["id"] in seen_ids:
            problems.append(f"{case['id']}: duplicate case id")
        seen_ids.add(case["id"])
        base = os.path.join(resolve_store_dir(case["store"]), ".context")
        if not os.path.isdir(base):
            problems.append(f"{case['id']}: store not found: {case['store']}")
            continue
        by_id = {}
        for name in ("decisions.json", "pipelines.json", "constraints.json"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    for e in json.load(f):
                        by_id[e.get("id")] = e
        for gid in case["expected"]:
            entry = by_id.get(gid)
            if entry is None:
                problems.append(f"{case['id']}: gold id {gid} absent from {case['store']}")
            elif case["case_type"] == "history" and entry.get("status") != "superseded":
                problems.append(
                    f"{case['id']}: gold id {gid} is '{entry.get('status')}', "
                    "history cases require 'superseded'")
        if case["case_type"] == "negative" and case["expected"]:
            problems.append(f"{case['id']}: negative case must have empty expected")
        if case["case_type"] != "negative" and not case["expected"]:
            problems.append(f"{case['id']}: non-negative case needs at least one gold id")
    return problems


def run_case(case, project_dir, arm_params, token_budget):
    """One get_context call. Returns (ordered ids, no_confident_match)."""
    params = {
        "project_dir": project_dir,
        "query": case["query"],
        # Arc traversal is OFF so the number measures the RANKING, not
        # related_to's ability to drag a neighbour in behind a correct hit.
        "include_related": False,
        "token_budget": token_budget,
    }
    params.update(arm_params)
    result = server.handle_get_context(params)
    if not isinstance(result, dict) or "results" not in result:
        raise RuntimeError(f"get_context returned: {result}")
    ids = [r.get("entry", {}).get("id") for r in result["results"]]
    return [i for i in ids if i], bool(result.get("no_confident_match"))


def score_case(case, retrieved):
    """recall@k / hit@k / RR for one positive-or-history case."""
    gold = list(case["expected"])
    out = {}
    for k in KS:
        topk = retrieved[:k]
        found = sum(1 for g in gold if g in topk)
        out[f"recall@{k}"] = found / len(gold)
        out[f"hit@{k}"] = 1.0 if found else 0.0
    rank = next((i + 1 for i, rid in enumerate(retrieved) if rid in gold), None)
    out["rr"] = (1.0 / rank) if rank else 0.0
    out["first_gold_rank"] = rank
    return out


def _mean(values):
    return (sum(values) / len(values)) if values else 0.0


def aggregate(rows):
    """rows -> metric dict. Positives+history feed recall/MRR, negatives FPR."""
    scored = [r for r in rows if r["case_type"] in ("positive", "history")]
    negatives = [r for r in rows if r["case_type"] == "negative"]
    agg = {"cases": len(rows), "scored_cases": len(scored), "negative_cases": len(negatives)}
    for k in KS:
        agg[f"recall@{k}"] = _mean([r[f"recall@{k}"] for r in scored])
        agg[f"hit@{k}"] = _mean([r[f"hit@{k}"] for r in scored])
    agg["mrr"] = _mean([r["rr"] for r in scored])
    if negatives:
        agg["fpr_confident"] = _mean([0.0 if r["no_confident_match"] else 1.0
                                      for r in negatives])
        agg["returned_anything"] = _mean([1.0 if r["retrieved"] else 0.0
                                          for r in negatives])
    return agg


def run_arm(arm, cases, workdir, token_budget):
    desc, patch = ARMS[arm]
    rows = []
    for case in cases:
        project_dir = snapshot(case["store"], workdir)
        retrieved, no_conf = run_case(case, project_dir, patch, token_budget)
        row = {
            "case": case["id"],
            "store": case["store"],
            "case_type": case["case_type"],
            "retrieved": retrieved[:10],
            "no_confident_match": no_conf,
        }
        if case["case_type"] != "negative":
            row.update(score_case(case, retrieved))
        rows.append(row)
    return rows


def embedding_available(cases, workdir, token_budget, attempts=3):
    """Did the embedding arm actually embed anything, or silently fall back?

    query_cosines returns None on any failure and the caller drops to lexical --
    correct for production, fatal for a measurement, because the embedding arm
    would then report the lexical number under a different name.

    Retried, because the first probe against a cold Ollama is exactly the case
    that fails: the query embed carries a 10s timeout and loading nomic-embed
    into memory can exceed it. That happened on the first real run here and
    silently dropped the arm -- the precise silent downgrade this probe exists
    to catch, arriving through the probe itself.
    """
    probe = cases[0]
    project_dir = snapshot(probe["store"], workdir)
    base = os.path.join(project_dir, ".context")
    for attempt in range(attempts):
        try:
            import semantic_index
            entries = server.read_json_file(os.path.join(base, "decisions.json"))
            cos = semantic_index.query_cosines(
                "probe query", entries[:2], base, {"enabled": True})
            if cos:
                return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            print("embedder probe %d/%d failed (cold model?), retrying"
                  % (attempt + 1, attempts))
    return False


def report(results, arms):
    lines = []
    lines.append("=" * 78)
    lines.append("context-keeper retrieval eval")
    lines.append("=" * 78)
    first = results[arms[0]]["rows"]
    n_pos = sum(1 for r in first if r["case_type"] == "positive")
    n_hist = sum(1 for r in first if r["case_type"] == "history")
    n_neg = sum(1 for r in first if r["case_type"] == "negative")
    stores = sorted({r["store"] for r in first})
    lines.append("cases: %d (%d positive, %d history, %d negative) across %d stores"
                 % (len(first), n_pos, n_hist, n_neg, len(stores)))
    lines.append("seed: %d   arc traversal (include_related): off" % SEED)
    lines.append("")

    header = "%-17s %8s %8s %8s %8s %8s %8s" % (
        "arm", "rec@1", "rec@3", "rec@5", "hit@5", "MRR", "FPR")
    lines.append(header)
    lines.append("-" * len(header))
    for arm in arms:
        a = results[arm]["overall"]
        lines.append("%-17s %8.3f %8.3f %8.3f %8.3f %8.3f %8s" % (
            arm, a["recall@1"], a["recall@3"], a["recall@5"], a["hit@5"], a["mrr"],
            ("%.3f" % a["fpr_confident"]) if "fpr_confident" in a else "-"))
    lines.append("")

    lines.append("By case type")
    lines.append("-" * 60)
    for arm in arms:
        for ctype in ("positive", "history"):
            rows = [r for r in results[arm]["rows"] if r["case_type"] == ctype]
            if not rows:
                continue
            a = aggregate(rows)
            lines.append("%-17s %-9s n=%-3d rec@5=%.3f hit@5=%.3f MRR=%.3f" % (
                arm, ctype, len(rows), a["recall@5"], a["hit@5"], a["mrr"]))
        negs = [r for r in results[arm]["rows"] if r["case_type"] == "negative"]
        if negs:
            a = aggregate(negs)
            lines.append("%-17s %-9s n=%-3d confident-answer rate=%.3f "
                         "(returned anything=%.3f)" % (
                             arm, "negative", len(negs),
                             a["fpr_confident"], a["returned_anything"]))
    lines.append("")

    lines.append("By project (rec@5 / hit@5)")
    lines.append("-" * 60)
    lines.append("%-24s %s" % ("store", "  ".join("%-17s" % a for a in arms)))
    for store in stores:
        cells = []
        for arm in arms:
            rows = [r for r in results[arm]["rows"]
                    if r["store"] == store and r["case_type"] != "negative"]
            if rows:
                a = aggregate(rows)
                cells.append("%-17s" % ("%.3f / %.3f" % (a["recall@5"], a["hit@5"])))
            else:
                cells.append("%-17s" % "-")
        lines.append("%-24s %s" % (store[:24], "  ".join(cells)))
    lines.append("")

    lines.append("Misses (no gold entry in top 5)")
    lines.append("-" * 60)
    for arm in arms:
        misses = [r for r in results[arm]["rows"]
                  if r["case_type"] != "negative" and r["hit@5"] == 0.0]
        lines.append("%s: %d" % (arm, len(misses)))
        for r in misses:
            lines.append("  %-10s %-22s rank of first gold: %s" % (
                r["case"], r["store"][:22],
                r["first_gold_rank"] if r["first_gold_rank"] else "not returned"))
    return "\n".join(_ascii(x) for x in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="context-keeper retrieval eval")
    parser.add_argument("--arm", action="append", choices=sorted(ARMS),
                        help="Arm to run. Repeatable. Default: lexical + embedding.")
    parser.add_argument("--all-arms", action="store_true",
                        help="Run every arm including embedding_heavy.")
    parser.add_argument("--golden", default=GOLDEN)
    parser.add_argument("--token-budget", type=int, default=4000,
                        help="get_context token_budget per call (default 4000).")
    parser.add_argument("--workdir", default=os.path.join(HERE, ".snapshots"),
                        help="Where store COPIES live. Never a real store.")
    parser.add_argument("--json", dest="json_out",
                        help="Also write the full per-case results here.")
    args = parser.parse_args(argv)

    random.seed(SEED)
    cases = load_golden(args.golden)

    problems = validate(cases)
    if problems:
        print("golden set is invalid:")
        for p in problems:
            print("  " + _ascii(p))
        return 2

    arms = list(args.arm) if args.arm else (
        sorted(ARMS) if args.all_arms else list(DEFAULT_ARMS))

    workdir = args.workdir
    ephemeral = False
    if not workdir:
        workdir = tempfile.mkdtemp(prefix="ck_eval_")
        ephemeral = True
    os.makedirs(workdir, exist_ok=True)

    try:
        if any(a.startswith("embedding") for a in arms):
            if not embedding_available(cases, workdir, args.token_budget):
                print("embedding arm unavailable (no embedder reachable) -- "
                      "it would silently report the lexical number. Dropping it.")
                arms = [a for a in arms if not a.startswith("embedding")]
                if not arms:
                    return 3

        results = {}
        for arm in arms:
            rows = run_arm(arm, cases, workdir, args.token_budget)
            results[arm] = {"rows": rows, "overall": aggregate(rows)}

        print(report(results, arms))

        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump({
                    "seed": SEED,
                    "token_budget": args.token_budget,
                    "arms": {a: {"description": ARMS[a][0], **results[a]} for a in arms},
                }, f, indent=2)
            print("\nwrote %s" % _ascii(args.json_out))
    finally:
        if ephemeral:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
