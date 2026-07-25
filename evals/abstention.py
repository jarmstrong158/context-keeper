"""Abstention measurement: does get_context confabulate on no-answer queries?

The retrieval eval (run_eval.py) only asks "when an answer exists, is it in
the top-k?" It never asks the harder question: when NOTHING relevant is
stored, does get_context correctly signal "no confident match" — or does it
hand back its top-scored entry as if it were an answer?

That second failure (confabulation) is invisible to hit@k because there is no
gold id to miss. This script measures it directly, the way a memory tool
should be measured:

  TNR (true-negative rate) — on no-answer queries, how often does the top
      relevance signal fall below the floor (correct abstention)?
  false-abstention rate    — on real answerable queries, how often would the
      floor make us wrongly abstain (the cost of the floor)?

The floor keys on server._relevance_signal (tag/text overlap only), NOT the
composite score — the composite is inflated by recency/status/origin and
cannot separate "relevant" from "merely present."

Usage:
    python abstention.py                 # sweep floors, print the trade-off table
    python abstention.py --floor 0.18    # report TNR / false-abstention at one floor
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CK_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _CK_ROOT)
import server  # noqa: E402

_REPOS_ROOT = os.path.dirname(_CK_ROOT)


def _store_dir(name):
    return os.path.join(_REPOS_ROOT, name)


# No-answer queries per store: topics the store does NOT contain. The
# hard-negatives deliberately share vocabulary with a real entry but ask
# about something absent — the case most likely to bait a confident wrong
# answer out of a token-overlap ranker.
NO_ANSWER = {
    "context-keeper": [
        "how does the server authenticate incoming API requests",
        "what rate limiting is applied to the record tools",
        "which cloud database stores the decisions",          # hard-neg: decisions present, DB absent
        "how are the compaction snapshots encrypted at rest",  # hard-neg: compaction present, crypto absent
        "what CSS framework does the dashboard use",
        "how do i configure OAuth for the MCP server",
    ],
    "Clark": [
        "what color theme does the web dashboard use",
        "how is end-user authentication handled",
        "how does the value network process image inputs",     # hard-neg: value network present, images absent
        "what is the monthly cloud hosting bill",
        "which payment processor handles subscriptions",
        "how does the agent send email notifications",         # hard-neg: notifications maybe, email absent
    ],
    "Conductor": [
        "what is the database backup schedule",
        "how does the UI render the settings page",
        "which font is used in the report export",
        "how are worker processes billed to customers",        # hard-neg: workers present, billing absent
    ],
}


SEMANTIC_CFG = {"enabled": True, "model": "nomic-embed-text",
                "url": "http://localhost:11434"}


# The four arms below vary TWO independent things, and conflating them
# produces a misleading number:
#   (a) whether semantic cosine steers the RANKING (a shipped, opt-in
#       feature that changes WHICH entries come back), and
#   (b) which signal top_relevance is computed FROM.
# Turning on semantic ranking changes the returned set, so the max lexical
# overlap in that set moves on its own -- with the abstention signal
# untouched. To judge a change to (b), hold (a) fixed:
#   "semantic-lexsignal" vs "semantic" is the correct A/B for the signal.
#   "lexical"            is the zero-dependency default and the safety net.
MODES = ("lexical", "semantic-lexsignal", "semantic", "raw-cosine")

MODE_LABELS = {
    "lexical": "semantic OFF, lexical signal (zero-dep default)",
    "semantic-lexsignal": "semantic ranking ON, lexical-only signal (the bug)",
    "semantic": "semantic ranking ON, calibrated cosine signal (shipped)",
    "raw-cosine": "semantic ranking ON, RAW cosine signal (the trap)",
}


def _top_relevance(store_dir, query, mode="lexical"):
    """Best relevance signal among the entries get_context actually returns.

    mode:
      "lexical"            — semantic off entirely; tag/text overlap only.
      "semantic-lexsignal" — semantic ranking on but top_relevance still
                             lexical-only. This is the DEFECT: cosine steers
                             which entries come back, then a purely lexical
                             judgement decides whether to trust them, so an
                             entry surfaced on semantics alone gets flagged
                             `no_confident_match`.
      "semantic"           — semantic ranking on + the CALIBRATED cosine
                             folded into the signal. What ships.
      "raw-cosine"         — semantic ranking on + the RAW cosine. The naive
                             implementation: it looks obviously correct and
                             is catastrophically wrong, because nomic's
                             cosines never fall below ~0.51 even for an
                             unrelated question. Drives TNR to 0% —
                             abstention silently off. Measuring it is why
                             this arm exists.
    """
    params = {"project_dir": store_dir, "query": query, "include_related": False}
    if mode != "lexical":
        params["semantic"] = dict(SEMANTIC_CFG)
    result = server.handle_get_context(params)
    if not isinstance(result, dict) or "results" not in result:
        return None, result

    sem_map = {}
    if mode != "lexical":
        base = os.path.join(store_dir, server.CONTEXT_DIR_NAME)
        try:
            import semantic_index
            sem_map = semantic_index.query_cosines(
                query, [r.get("entry", {}) for r in result["results"]],
                base, SEMANTIC_CFG) or {}
        except Exception:
            sem_map = {}

    best = 0.0
    for r in result["results"]:
        entry = r.get("entry", {})
        cos = sem_map.get(entry.get("id"))
        if mode in ("lexical", "semantic-lexsignal"):
            sig = server._relevance_signal(entry, None, query)
        elif mode == "semantic":
            sig = server._relevance_signal(entry, None, query, cosine=cos)
        else:  # raw-cosine
            sig = server._relevance_signal(entry, None, query)
            if cos is not None:
                sig = max(sig or 0.0, cos)
        if sig is not None:
            best = max(best, sig)
    return best, None


def gather(mode="lexical"):
    """Return (positives, negatives) as lists of (store, query, top_relevance)."""
    dataset = json.load(open(os.path.join(_HERE, "datasets", "combined_retrieval.json"),
                             encoding="utf-8"))
    positives, negatives = [], []
    for c in dataset:
        inp = c.get("input") or {}
        store = inp.get("store")
        query = inp.get("query")
        if not store or not query:
            continue
        rel, err = _top_relevance(_store_dir(store), query, mode)
        if err is None:
            positives.append((store, query, rel))
    for store, queries in NO_ANSWER.items():
        for query in queries:
            rel, err = _top_relevance(_store_dir(store), query, mode)
            if err is None:
                negatives.append((store, query, rel))
    return positives, negatives


def report_at(floor, positives, negatives, verbose=False):
    tn = sum(1 for _s, _q, rel in negatives if rel < floor)   # correctly abstained
    fa = sum(1 for _s, _q, rel in positives if rel < floor)   # wrongly abstained
    tnr = tn / len(negatives) if negatives else 0.0
    far = fa / len(positives) if positives else 0.0
    if verbose:
        print(f"\nFloor {floor}:  TNR {tn}/{len(negatives)}={tnr:.0%}   "
              f"false-abstention {fa}/{len(positives)}={far:.0%}")
        print("  confabulated (no-answer, relevance >= floor):")
        for s, q, rel in sorted(negatives, key=lambda x: -x[2]):
            if rel >= floor:
                print(f"    [{rel:.2f}] ({s}) {q}")
        print("  wrongly abstained (answerable, relevance < floor):")
        for s, q, rel in sorted(positives, key=lambda x: x[2]):
            if rel < floor:
                print(f"    [{rel:.2f}] ({s}) {q}")
    return tnr, far


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--mode", choices=MODES, default="lexical",
                    help="which relevance signal to sweep (see _top_relevance)")
    ap.add_argument("--compare", action="store_true",
                    help="sweep all four arms side by side at the default floor")
    args = ap.parse_args()

    if args.compare:
        floor = args.floor if args.floor is not None else 0.20
        print(f"\nAbstention at floor {floor:.2f}, by configuration:\n")
        print("| configuration | TNR (abstain on no-answer) | "
              "false-abstention (miss real) |")
        print("|---|---|---|")
        for mode in MODES:
            pos, neg = gather(mode)
            tnr, far = report_at(floor, pos, neg)
            print(f"| {MODE_LABELS[mode]} | {tnr:.0%} | {far:.0%} |")
        print("\nHold semantic RANKING fixed to judge the SIGNAL: compare "
              "'semantic ranking ON, lexical-only signal' against "
              "'semantic ranking ON, calibrated cosine signal'.")
        return

    positives, negatives = gather(args.mode)
    print(f"mode: {args.mode}")
    print(f"positives (answerable): {len(positives)}    negatives (no-answer): {len(negatives)}")

    pr = sorted(rel for _s, _q, rel in positives)
    nr = sorted(rel for _s, _q, rel in negatives)
    print(f"positive relevance  min={pr[0]:.2f} median={pr[len(pr)//2]:.2f} max={pr[-1]:.2f}")
    print(f"no-answer relevance min={nr[0]:.2f} median={nr[len(nr)//2]:.2f} max={nr[-1]:.2f}")

    # Baseline confabulation with NO floor: every no-answer query returns a
    # top result, so confabulation is 100% by construction.
    print(f"\nNo floor (current behavior): confabulation {len(negatives)}/{len(negatives)}=100% "
          f"— every no-answer query gets a confident-looking top result.")

    if args.floor is not None:
        report_at(args.floor, positives, negatives, verbose=True)
        return

    print("\n| floor | TNR (abstain on no-answer) | false-abstention (miss real) |")
    print("|---|---|---|")
    for floor in (0.10, 0.15, 0.18, 0.20, 0.25, 0.30, 0.35):
        tnr, far = report_at(floor, positives, negatives)
        print(f"| {floor:.2f} | {tnr:.0%} | {far:.0%} |")


if __name__ == "__main__":
    main()
