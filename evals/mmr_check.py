"""Measure MMR's effect on retrieval, since the single-gold eval can't.

For every dataset query, compares the top-k returned with MMR off vs on:
  - redundancy@k : mean pairwise lexical (Jaccard) similarity of the returned
                   entries. LOWER = more diverse = the point of MMR.
  - hit@k        : did a gold entry still surface? MUST NOT drop.

A good result is redundancy down with hit@k flat.
"""
import json
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_CK = _THIS.parent.parent
sys.path.insert(0, str(_CK))
import server  # noqa: E402

_REPOS = _CK.parent
_DATA = _THIS.parent / "datasets" / "combined_retrieval.json"
K = 5


def entries_by_id(store_dir):
    base = server._base_dir_from_params({"project_dir": store_dir})
    paths = server._resolve_paths(base)
    out = {}
    for _t, p in paths.items():
        for e in server.read_json_file(p):
            out[e.get("id")] = e
    return out


def top(query, store_dir, mmr):
    params = {"query": query, "project_dir": store_dir, "include_related": False}
    if mmr:
        params["mmr"] = {"enabled": True, "lambda": 0.7}
    r = server.handle_get_context(params)
    return [x["entry"]["id"] for x in r["results"]][:K]


def redundancy(ids, byid):
    ws = [server._text_words(byid[i]) for i in ids if i in byid]
    if len(ws) < 2:
        return 0.0
    pairs = [server._jaccard(ws[a], ws[b])
             for a in range(len(ws)) for b in range(a + 1, len(ws))]
    return sum(pairs) / len(pairs)


def main():
    cases = json.loads(_DATA.read_text(encoding="utf-8"))
    store_cache = {}
    red_off = red_on = hit_off = hit_on = 0.0
    rows = []
    for c in cases:
        q = c["input"]["query"]
        store = str(_REPOS / c["input"]["store"])
        gold = set(c["expected"].replace(",", " ").split())
        byid = store_cache.setdefault(store, entries_by_id(store))
        off, on = top(q, store, False), top(q, store, True)
        ro, rn = redundancy(off, byid), redundancy(on, byid)
        red_off += ro
        red_on += rn
        hit_off += 1.0 if gold & set(off) else 0.0
        hit_on += 1.0 if gold & set(on) else 0.0
        rows.append((ro - rn, c["id"], q, ro, rn))

    n = len(cases)
    pct = (red_on - red_off) / (red_off or 1) * 100
    print(f"cases: {n}   (k={K})")
    print(f"mean redundancy@{K}:  off {red_off/n:.3f}  ->  on {red_on/n:.3f}  ({pct:+.0f}%)")
    print(f"hit@{K}:              off {hit_off/n:.1%}  ->  on {hit_on/n:.1%}")
    print("\ntop redundancy reductions (off -> on):")
    for d, cid, q, ro, rn in sorted(rows, reverse=True)[:6]:
        print(f"  -{d:.3f}  {cid}  {ro:.2f}->{rn:.2f}  {q[:58]}")


if __name__ == "__main__":
    main()
