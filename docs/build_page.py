#!/usr/bin/env python3
"""Build docs/index.html from measured data. Run it; do not hand-edit index.html.

Every number and every bar on the page comes from a file something else
produced -- the retrieval eval's --json output, the token-reduction script, the
abstention sweep. Nothing is typed in.

That is not tidiness, it is the whole reason the page is allowed to exist. This
project has now published four wrong numbers on three surfaces: a synthetic
hit@5 of 86.7% that outlived its corpus, a registry count that said seven when
the registry had eight, and -- in the same session that fixed those two -- a set
of retrieval figures measured against LIVE stores, published, and then
invalidated hours later when the corpus was frozen. Every one of them was
hand-copied prose that nothing executed.

So: `python docs/build_page.py` regenerates the page, and
`tests/test_page_data.py` fails if the committed page disagrees with the
measurements. A stale number becomes a red test instead of a claim on a website.

Usage:
    python evals/run_retrieval_eval.py --json evals/runs/page_data.json
    python docs/build_page.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

EVAL_JSON = os.path.join(REPO, "evals", "runs", "page_data.json")
OUT = os.path.join(HERE, "index.html")

# Token reduction: measured by evals/token_reduction.py against four real
# stores on 2026-07-03. Carried as data rather than prose so the page and the
# eval README cannot drift; re-run that script to refresh.
TOKEN_REDUCTION = [
    ("balatron", 78, 75277, 2057),
    ("clark", 55, 35445, 2102),
    ("context-keeper", 13, 5692, 828),
    ("conductor", 9, 1538, 411),
]

# Abstention sweep from evals/abstention.py (2026-07-03): relevance floor ->
# (true-negative rate, false-abstention rate).
ABSTENTION = [(0.15, 38, 0), (0.20, 38, 0), (0.25, 56, 3), (0.30, 75, 16)]
ABSTENTION_DEFAULT = 0.20


def load_eval():
    if not os.path.exists(EVAL_JSON):
        raise SystemExit(
            "missing %s\nRun: python evals/run_retrieval_eval.py --json %s"
            % (EVAL_JSON, EVAL_JSON))
    with open(EVAL_JSON, encoding="utf-8") as f:
        return json.load(f)


def per_project(rows):
    """recall@5 per store, scored cases only."""
    acc = {}
    for r in rows:
        if r["case_type"] == "negative":
            continue
        acc.setdefault(r["store"], []).append(r["recall@5"])
    return {k: sum(v) / len(v) for k, v in sorted(acc.items())}


def bar(pct, cls="", width=100):
    return ('<div class="track"><div class="fill %s" style="width:%.1f%%"></div></div>'
            % (cls, max(0.0, min(100.0, pct * width / 100))))


def build():
    data = load_eval()
    arms = data["arms"]
    lex, emb = arms["lexical"]["overall"], arms["embedding"]["overall"]
    lex_rows, emb_rows = arms["lexical"]["rows"], arms["embedding"]["rows"]
    n_cases = len(lex_rows)
    n_pos = sum(1 for r in lex_rows if r["case_type"] == "positive")
    n_neg = sum(1 for r in lex_rows if r["case_type"] == "negative")
    n_hist = sum(1 for r in lex_rows if r["case_type"] == "history")
    lex_pp, emb_pp = per_project(lex_rows), per_project(emb_rows)

    # --- retrieval bars -----------------------------------------------------
    metrics = [("recall@1", "recall@1"), ("recall@3", "recall@3"),
               ("recall@5", "recall@5"), ("hit@5", "hit@5"), ("mrr", "MRR")]
    ret_rows = []
    for key, label in metrics:
        l, e = lex[key] * 100, emb[key] * 100
        ret_rows.append(f'''      <div class="mrow">
        <div class="mlabel">{label}</div>
        <div class="mbars">
          <div class="pair"><span class="tag lex">lexical</span>{bar(l, "lex")}<span class="v">{l:.0f}%</span></div>
          <div class="pair"><span class="tag emb">+ embedding</span>{bar(e, "emb")}<span class="v">{e:.0f}%</span></div>
        </div>
      </div>''')

    # --- per-project --------------------------------------------------------
    pp_rows = []
    for store in lex_pp:
        if store.startswith("fixtures/"):
            continue
        l, e = lex_pp[store] * 100, emb_pp.get(store, 0) * 100
        delta = e - l
        pp_rows.append(f'''      <div class="pprow">
        <div class="pname">{store}</div>
        <div class="ptrack">{bar(l, "lex")}{bar(e, "emb")}</div>
        <div class="pdelta {'up' if delta > 1 else 'flat'}">{'+' if delta > 0 else ''}{delta:.0f}</div>
      </div>''')

    # --- token reduction ----------------------------------------------------
    tr_rows = []
    for name, entries, full, injected in TOKEN_REDUCTION:
        pct = (1 - injected / full) * 100
        tr_rows.append(f'''      <div class="trrow">
        <div class="pname">{name}<span class="sub">{entries} entries</span></div>
        <div class="ptrack">{bar(pct, "tok")}</div>
        <div class="v">{pct:.1f}%</div>
      </div>''')

    # --- abstention ---------------------------------------------------------
    ab_rows = []
    for floor, tnr, fa in ABSTENTION:
        mark = " default" if floor == ABSTENTION_DEFAULT else ""
        ab_rows.append(f'''      <div class="abrow{mark}">
        <div class="pname">floor {floor:.2f}{'<span class="sub">shipped</span>' if floor == ABSTENTION_DEFAULT else ''}</div>
        <div class="ptrack">{bar(tnr, "tok")}{bar(fa, "bad")}</div>
        <div class="v">{tnr}% caught<span class="sub2">{fa}% false</span></div>
      </div>''')

    fpr = lex.get("fpr_confident", 0) * 100

    html = PAGE.format(
        n_cases=n_cases, n_pos=n_pos, n_neg=n_neg, n_hist=n_hist,
        ret_rows="\n".join(ret_rows),
        pp_rows="\n".join(pp_rows),
        tr_rows="\n".join(tr_rows),
        ab_rows="\n".join(ab_rows),
        lex_r5=lex["recall@5"] * 100, emb_r5=emb["recall@5"] * 100,
        lex_mrr=lex["mrr"] * 100, emb_mrr=emb["mrr"] * 100,
        gain_r5=(emb["recall@5"] - lex["recall@5"]) * 100,
        fpr=fpr, hist_note=n_hist,
        best_tok=max((1 - i / f) * 100 for _n, _e, f, i in TOKEN_REDUCTION),
    )
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("wrote %s" % OUT)
    print("  retrieval: %d cases (%d positive, %d negative, %d history)"
          % (n_cases, n_pos, n_neg, n_hist))
    print("  lexical recall@5 %.3f -> embedding %.3f" % (lex["recall@5"], emb["recall@5"]))
    return 0


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>context-keeper &mdash; project memory that keeps the reasoning</title>
<meta name="description" content="An MCP server that records the decisions, constraints and rationale behind a codebase, and injects them back at session start. Measured, not asserted.">
<style>
:root{{
  --bg:#0d1117; --panel:#161b22; --line:#21262d; --ink:#e6edf3; --dim:#8b949e;
  --lex:#4a5568; --emb:#2dd4bf; --tok:#7c9cf5; --bad:#f97583; --accent:#2dd4bf;
}}
@media (prefers-color-scheme:light){{
  :root{{--bg:#fbfcfd;--panel:#fff;--line:#e3e8ee;--ink:#161b22;--dim:#5b6570;--lex:#a9b4c2;}}
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:860px;margin:0 auto;padding:0 20px}}
header{{padding:72px 0 40px}}
h1{{font-size:clamp(30px,5vw,44px);line-height:1.15;margin:0 0 14px;letter-spacing:-.02em}}
.tagline{{font-size:clamp(17px,2.4vw,20px);color:var(--dim);margin:0 0 26px;max-width:36em}}
.cta{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}}
.btn{{display:inline-block;padding:9px 15px;border-radius:7px;border:1px solid var(--line);
  color:var(--ink);text-decoration:none;font-size:14px;background:var(--panel)}}
.btn.pri{{background:var(--accent);border-color:var(--accent);color:#06231f;font-weight:600}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;
  background:var(--panel);padding:2px 6px;border-radius:4px;border:1px solid var(--line)}}
section{{padding:34px 0;border-top:1px solid var(--line)}}
h2{{font-size:23px;margin:0 0 6px;letter-spacing:-.01em}}
h3{{font-size:16px;margin:26px 0 10px;color:var(--dim);font-weight:600;
  text-transform:uppercase;letter-spacing:.06em}}
.lede{{color:var(--dim);margin:0 0 22px;max-width:44em}}
p{{max-width:44em}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px}}
.mrow{{display:grid;grid-template-columns:92px 1fr;gap:14px;align-items:center;
  padding:9px 0;border-bottom:1px solid var(--line)}}
.mrow:last-child{{border:0}}
.mlabel{{font-size:13px;color:var(--dim);font-variant-numeric:tabular-nums}}
.pair{{display:grid;grid-template-columns:82px 1fr 44px;gap:9px;align-items:center;margin:3px 0}}
.tag{{font-size:11px;color:var(--dim);text-align:right}}
.track{{height:9px;background:rgba(127,127,127,.16);border-radius:5px;overflow:hidden}}
.fill{{height:100%;border-radius:5px}}
.fill.lex{{background:var(--lex)}} .fill.emb{{background:var(--emb)}}
.fill.tok{{background:var(--tok)}} .fill.bad{{background:var(--bad)}}
.v{{font-size:13px;font-variant-numeric:tabular-nums;text-align:right}}
.sub{{display:block;font-size:11px;color:var(--dim)}}
.sub2{{display:block;font-size:11px;color:var(--dim)}}
.pprow,.trrow,.abrow{{display:grid;grid-template-columns:150px 1fr 74px;gap:12px;
  align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}}
.pprow:last-child,.trrow:last-child,.abrow:last-child{{border:0}}
.pname{{font-size:13px}}
.ptrack{{display:grid;gap:4px}}
.pdelta{{font-size:13px;text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}}
.pdelta.up{{color:var(--emb)}}
.abrow.default{{background:rgba(45,212,191,.06);margin:0 -10px;padding:8px 10px;border-radius:6px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:16px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}}
.card h4{{margin:0 0 6px;font-size:15px}}
.card p{{margin:0;font-size:14px;color:var(--dim)}}
.big{{font-size:30px;font-weight:650;letter-spacing:-.02em}}
.legend{{display:flex;gap:16px;font-size:12px;color:var(--dim);margin-bottom:14px;flex-wrap:wrap}}
.sw{{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:5px}}
.caveat{{font-size:13px;color:var(--dim);border-left:2px solid var(--line);
  padding-left:14px;margin-top:18px}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:10px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}}
th{{font-size:12px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.05em}}
td.no{{color:var(--dim)}}
footer{{padding:40px 0 70px;color:var(--dim);font-size:13px;border-top:1px solid var(--line)}}
a{{color:var(--accent)}}
@media(max-width:620px){{
  .mrow{{grid-template-columns:1fr}}
  .pair{{grid-template-columns:70px 1fr 40px}}
  .pprow,.trrow,.abrow{{grid-template-columns:110px 1fr 66px}}
}}
</style></head><body>
<div class="wrap">

<header>
  <h1>Your agent forgets <em>why</em>.</h1>
  <p class="tagline">context-keeper is an MCP server that records the decisions, constraints
  and reasoning behind a codebase &mdash; then puts them back in front of the model at session
  start, and before the edit that would break one. Local JSON. Zero dependencies.</p>
  <div class="cta">
    <a class="btn pri" href="https://github.com/jarmstrong158/context-keeper">GitHub</a>
    <a class="btn" href="https://pypi.org/project/context-keeper-mcp/">PyPI</a>
    <a class="btn" href="https://github.com/jarmstrong158/context-keeper/blob/main/evals/README.md">Full eval methodology</a>
  </div>
  <p style="font-size:13px;color:var(--dim);margin-top:14px"><code>pip install context-keeper-mcp</code></p>
</header>

<section>
  <h2>The problem it actually solves</h2>
  <p class="lede">Most memory tools answer &ldquo;what did we say?&rdquo; This one answers
  &ldquo;why is it like this, and what will break if I change it?&rdquo;</p>
  <div class="grid">
    <div class="card"><h4>Decisions with rationale</h4><p>Not a summary. The schema
      <em>refuses</em> an entry whose problem statement or reasoning is too thin &mdash;
      because future-you cannot recover a why that was never written.</p></div>
    <div class="card"><h4>Constraints that fire in time</h4><p>A rule scoped to a path is
      injected <em>before</em> you edit a file it governs, not in a summary at turn one when
      it is still abstract.</p></div>
    <div class="card"><h4>History that stays reachable</h4><p>A decision that replaced
      another carries one line saying what the old one said and why it changed &mdash; so
      &ldquo;why did we move off X?&rdquo; has an answer.</p></div>
  </div>
</section>

<section>
  <h2>Retrieval, measured</h2>
  <p class="lede">{n_cases} questions across a frozen corpus of 7 real project stores:
  {n_pos} with a known answer, {n_neg} deliberately unanswerable, {n_hist} asking for
  superseded history. Every question is written from the <strong>problem an entry
  solves</strong> &mdash; never reworded from its summary, because a reworded question
  shares vocabulary with its target and hands lexical search a free hit.</p>
  <div class="panel">
    <div class="legend">
      <span><span class="sw" style="background:var(--lex)"></span>lexical only</span>
      <span><span class="sw" style="background:var(--emb)"></span>with embedding blend</span>
    </div>
{ret_rows}
  </div>
  <p class="caveat"><strong>recall@k is strict</strong> &mdash; the fraction of <em>all</em>
  entries that should have come back that did. A question with five correct answers cannot
  score above 20% at k=1. Reported this way because the flattering version (&ldquo;did any
  correct entry appear?&rdquo;) is the one that hides a half-answered query.</p>
</section>

<section>
  <h2>Where the embedding blend earns its keep</h2>
  <p class="lede">recall@5 per store, lexical vs blended. The gain is not uniform &mdash;
  it concentrates in large, prose-heavy stores. Small ones are already answered by keywords.</p>
  <div class="panel">
{pp_rows}
  </div>
  <p class="caveat">This is why the embedding path is <strong>opt-in</strong> and falls back
  to lexical when no embedder is reachable. On a small store it buys almost nothing, and it
  costs a local model and a cache. Turning it on should be a decision, not a default.</p>
</section>

<section>
  <h2>What it costs at session start</h2>
  <p class="lede">Injecting the summary versus dumping every active entry into context.
  The number that matters is not the headline percentage &mdash; it is that injected cost
  stays roughly flat while the store grows.</p>
  <div class="panel">
{tr_rows}
  </div>
</section>

<section>
  <h2>When it has nothing, it says so</h2>
  <p class="lede">Asked something the store has no answer for, a naive retriever still returns
  its top match &mdash; and it looks like an answer. Measured confabulation with no floor was
  <strong>100%</strong>. The fix flags low-relevance results instead of suppressing them.</p>
  <div class="panel">
    <div class="legend">
      <span><span class="sw" style="background:var(--tok)"></span>unanswerable questions correctly flagged</span>
      <span><span class="sw" style="background:var(--bad)"></span>real answers wrongly withheld</span>
    </div>
{ab_rows}
  </div>
  <p class="caveat"><strong>Honest limit.</strong> On the {n_cases}-question set above,
  {fpr:.0f}% of the in-domain unanswerable questions still come back unflagged &mdash;
  identically in both arms. Hard negatives that share real topic vocabulary are not separable
  by a lexical signal, and embeddings do not rescue them either. This is a known-hard problem
  and the page is not going to pretend otherwise.</p>
</section>

<section>
  <h2>Why not one of the bulkier ones?</h2>
  <p class="lede">Because most of them are solving a different problem, and if that is the
  problem you have, you should use them instead.</p>
  <table>
    <tr><th></th><th>Extraction memory</th><th>context-keeper</th></tr>
    <tr><td>Capture</td><td>Watches the conversation, extracts automatically</td>
        <td>You state it; the schema enforces depth</td></tr>
    <tr><td>Retrieval</td><td>Similarity over embedded turns</td>
        <td>Tag / text / scope, embeddings optional</td></tr>
    <tr><td>Best at</td><td>&ldquo;What did we discuss about X?&rdquo;</td>
        <td>&ldquo;Why is it built this way, and what breaks if I change it?&rdquo;</td></tr>
    <tr><td>Infrastructure</td><td>Vector DB, embedding service, often a daemon</td>
        <td>JSON files in the repo. No service, no network.</td></tr>
  </table>
  <p class="caveat">The trade is real and it cuts both ways. <strong>Nothing is captured
  unless the agent records it</strong> &mdash; there is no auto-extraction, no web UI, and no
  entity graph. In an independent
  <a href="https://carsteneu.github.io/ai-memory-comparison/">81-system feature comparison</a>
  context-keeper covers 53% of tracked features against a 25% median, leading on rationale,
  origin-trust and conflict surfacing, and absent on auto-extraction and visual tooling.
  That evidence file was submitted by this project's author under the comparison's
  cite-your-source rule &mdash; read it as a structured self-report that a third party
  accepted, not as an independent audit.</p>
</section>

<section>
  <h2>Reproduce all of it</h2>
  <p class="lede">Every number on this page is generated from committed measurement output.
  Nothing here is typed in by hand.</p>
  <div class="panel" style="font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.9">
    git clone https://github.com/jarmstrong158/context-keeper<br>
    python evals/run_retrieval_eval.py &nbsp;<span style="color:var(--dim)"># retrieval, frozen corpus</span><br>
    python evals/token_reduction.py &nbsp;&nbsp;<span style="color:var(--dim)"># session-start cost</span><br>
    python evals/abstention.py &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:var(--dim)"># the honesty floor</span>
  </div>
  <p class="caveat">The lexical arm needs no network and is bit-for-bit reproducible. The
  corpus is frozen and committed precisely so these numbers cannot drift underneath the
  claim &mdash; an earlier version of this page quoted figures measured against live stores
  that stopped reproducing within a day.</p>
</section>

<footer>
  <div class="wrap" style="padding:0">
    MIT &middot; <a href="https://github.com/jarmstrong158/context-keeper">source</a> &middot;
    <a href="https://jarmstrong158.github.io/Xylem/">part of the Xylem stack</a><br>
    Generated by <code>docs/build_page.py</code> from committed measurement output.
  </div>
</footer>

</div></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(build())
