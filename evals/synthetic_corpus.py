"""Synthetic-corpus eval driver for containers without the real project stores.

The shipped evals (run_eval.py, abstention.py, mmr_check.py, semantic.py) point
at real .context stores (Clark / Conductor / context-keeper) and, for the
retrieval runner, the sibling `llm-evals` repo. In a fresh clone none of that is
present. This driver builds a REALISTIC SYNTHETIC store in context-keeper's
actual schema (via the real record_* handlers, so validation and field shapes
are authentic) and reproduces each eval's methodology against it using the SAME
server functions the shipped evals use:

  - token reduction  -> server.handle_get_project_summary + estimate_tokens
                        (identical math to token_reduction.py)
  - retrieval hit@k / MRR -> server.handle_get_context + the rank/RR logic from
                        retrieval_eval.RetrievalScorer
  - abstention TNR / false-abstention -> server._relevance_signal, the exact
                        signal abstention.py sweeps
  - MMR redundancy@k -> server.handle_get_context with mmr off/on + the Jaccard
                        redundancy from mmr_check.py
  - cold vs get_context injection token cost (bonus)

Every number this prints is MEASURED against the synthetic corpus, not estimated.
The semantic blend is NOT exercised here (it needs a local embedder); that eval
is reported as "not runnable in this container" in the metrics report.

Usage:
    python evals/synthetic_corpus.py [--out DIR]   # build + report (DIR persists
                                                   # so token_reduction.py can run on it)
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_CK_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _CK_ROOT)
import server  # noqa: E402


# ---------------------------------------------------------------------------
# The synthetic project: "Orion", a task-queue / job API service. Content is
# hand-written to be realistic and to include vocabulary-mismatch retrieval
# cases (query wording != entry wording), the hard case a memory tool exists
# to handle.
# ---------------------------------------------------------------------------

DECISIONS = [
    dict(id="storage", summary="Store job records in Postgres, not Redis",
         problem="Jobs must survive a broker restart and be queryable by status and owner after the fact; an in-memory broker loses them and can't answer analytical queries.",
         why_chosen="Postgres gives durable storage, transactional status updates, and rich queries in one system we already operate, avoiding a second datastore's operational burden for little benefit.",
         tags=["storage", "architecture", "database"], origin="user",
         retrieval_hints=["where are jobs persisted", "durable task store"]),
    dict(id="queue", summary="Use Postgres SKIP LOCKED as the job queue, not a dedicated broker",
         problem="We need at-least-once delivery and worker fan-out, but adding RabbitMQ/SQS means a second system to run, monitor, and keep consistent with the job records in Postgres.",
         why_chosen="SELECT ... FOR UPDATE SKIP LOCKED gives safe concurrent dequeue directly against the records table, so the queue and the source of truth can never drift apart, at the cost of some throughput ceiling.",
         tags=["queue", "architecture", "database"], origin="user",
         related_to=["dec-001"], retrieval_hints=["message broker choice", "how workers pull jobs"]),
    dict(id="idempotency", summary="Dequeue is exactly-once per attempt via a claimed_at + attempt token",
         problem="A worker that crashes mid-job must not cause the job to run twice with visible side effects, and two workers must never process the same job concurrently.",
         why_chosen="Each claim stamps claimed_at and a monotonic attempt token; side-effecting steps check the token so a duplicate delivery is detected and skipped, making retries safe to fire freely.",
         tags=["queue", "reliability", "correctness"], origin="agent",
         related_to=["dec-002"], retrieval_hints=["prevent running a job twice", "duplicate delivery", "exactly once"]),
    dict(id="retries", summary="Exponential backoff with jitter, capped at 6 attempts",
         problem="Transient downstream failures should be retried, but a tight retry loop amplifies an outage into a thundering herd and a poison job can retry forever.",
         why_chosen="Backoff 2^n seconds plus random jitter spreads load, and a hard cap of 6 attempts sends persistent failures to the dead-letter table instead of looping, trading a little latency for stability.",
         tags=["reliability", "queue"], origin="user",
         retrieval_hints=["retry policy", "backoff strategy", "what happens when a job keeps failing"]),
    dict(id="deadletter", summary="Failed-past-cap jobs move to a dead_letter table, not deleted",
         problem="Jobs that exhaust retries can't just vanish — operators need to inspect why they failed and optionally replay them after a fix.",
         why_chosen="A dead_letter table preserves the full job payload and last error so failures are auditable and replayable, which a delete or a log line would not allow.",
         tags=["reliability", "operations"], origin="agent", related_to=["dec-004"]),
    dict(id="auth", summary="Authenticate API clients with signed service tokens (HMAC), not sessions",
         problem="Callers are other backend services, not browsers, so cookie sessions don't fit and we need stateless verification that survives horizontal scaling.",
         why_chosen="Short-lived HMAC-signed tokens verify locally with a shared secret, needing no session store and no per-request database lookup, which keeps the auth path cheap under load.",
         tags=["auth", "security", "api"], origin="user",
         retrieval_hints=["how services log in", "request authentication scheme"]),
    dict(id="ratelimit", summary="Per-token rate limiting via a sliding-window counter in Postgres",
         problem="A misbehaving client can flood the enqueue endpoint and starve everyone else, and we have no API gateway in front to absorb it.",
         why_chosen="A sliding-window counter keyed by service token, stored in Postgres, enforces fair per-client limits without adding Redis just for counters, accepting a small write cost per request.",
         tags=["api", "reliability", "security"], origin="agent", related_to=["dec-006"]),
    dict(id="apiversion", summary="Version the API by URL prefix (/v1/), not header negotiation",
         problem="We will need to make breaking changes to request shapes, and clients update on their own schedule, so old and new must coexist.",
         why_chosen="A /v1/ URL prefix makes the version explicit, cacheable, and trivial to route, which opaque header negotiation makes harder to debug and to reason about at a glance.",
         tags=["api", "architecture"], origin="user"),
    dict(id="config", summary="Configuration via environment variables, validated at boot",
         problem="Misconfiguration should fail loudly at startup, not surface as a mysterious runtime error hours later in production.",
         why_chosen="Twelve-factor env vars parsed and validated in one boot-time schema means a bad config crashes immediately with a clear message, which scattered lazy reads would not guarantee.",
         tags=["operations", "configuration"], origin="agent"),
    dict(id="deploy", summary="Deploy as a single container image running both API and worker roles",
         problem="Splitting API and worker into separate images doubles the build and release surface for a service whose code is 90% shared.",
         why_chosen="One image with a ROLE env var selecting api-vs-worker keeps builds and versions in lockstep and simplifies rollback, trading a little image bloat for release simplicity.",
         tags=["deployment", "operations", "architecture"], origin="user",
         retrieval_hints=["how is the service packaged", "container image layout"]),
    dict(id="migrations", summary="Schema migrations run as a separate job before rollout, gated on success",
         problem="Running migrations inside app startup races multiple booting replicas and can half-apply a schema during a deploy.",
         why_chosen="A dedicated migration job that must exit zero before new app replicas roll out serializes schema changes and gives a clean gate, unlike in-process migration on boot.",
         tags=["deployment", "database", "operations"], origin="agent", related_to=["dec-010"]),
    dict(id="observability", summary="Structured JSON logs plus per-job trace ids, no external APM yet",
         problem="When a job misbehaves we need to follow it across enqueue, claim, and execution, but we don't want to pay for a full APM vendor at this stage.",
         why_chosen="Structured logs carrying a trace id per job let us reconstruct a job's life with grep and a log aggregator we already run, deferring APM cost until scale actually demands it.",
         tags=["observability", "operations"], origin="user",
         retrieval_hints=["how do we debug a stuck job", "logging approach", "tracing"]),
    dict(id="timeouts", summary="Every downstream call has an explicit timeout; no unbounded waits",
         problem="A hung downstream dependency can pin a worker forever, silently draining the pool until throughput collapses.",
         why_chosen="Mandatory per-call timeouts convert a hang into a fast, retryable error, protecting the worker pool, which relying on default socket behavior does not reliably do.",
         tags=["reliability", "correctness"], origin="agent", related_to=["dec-004"]),
    dict(id="payload", summary="Cap job payloads at 256 KB; larger inputs go to object storage by reference",
         problem="Large inline payloads bloat the records table, slow every dequeue scan, and blow up memory when many are claimed at once.",
         why_chosen="A 256 KB cap keeps the hot table lean; oversized inputs are written to object storage and referenced by key, trading an extra fetch for a predictable table size.",
         tags=["storage", "performance"], origin="user", related_to=["dec-001"]),
    dict(id="testing", summary="Integration tests run against a real ephemeral Postgres, not mocks",
         problem="Mocked database behavior hid two SKIP LOCKED concurrency bugs that only appear against a real engine, so mocks gave false confidence.",
         why_chosen="Spinning an ephemeral Postgres per test run exercises the real locking semantics the queue depends on, catching concurrency bugs mocks structurally cannot.",
         tags=["testing", "database", "correctness"], origin="user", related_to=["dec-002", "dec-003"]),
    dict(id="shutdown", summary="Workers drain in-flight jobs on SIGTERM before exiting",
         problem="A deploy that kills workers mid-job forces every in-flight job through the retry path, causing a visible latency spike on every release.",
         why_chosen="Catching SIGTERM and finishing claimed jobs before exit makes deploys graceful, avoiding the retry storm that an immediate kill would trigger, at the cost of a slightly longer drain window.",
         tags=["deployment", "reliability", "operations"], origin="agent", related_to=["dec-004", "dec-010"]),
]

CONSTRAINTS = [
    dict(id="c_secret", rule="Never log service tokens or the HMAC secret",
         reason="Tokens and the signing secret in logs would let anyone with log access forge authenticated requests, a full auth bypass.",
         scope="server.py", hardness="absolute", tags=["security", "auth"], origin="user",
         retrieval_hints=["can we log the auth token", "secret handling in logs"]),
    dict(id="c_skiplocked", rule="All dequeue queries must use FOR UPDATE SKIP LOCKED",
         reason="Dropping SKIP LOCKED reintroduces the double-processing race the queue design exists to prevent, silently running jobs twice.",
         scope="queue/", hardness="absolute", tags=["queue", "correctness"], origin="user", related_to=["dec-003"]),
    dict(id="c_timeout", rule="Every outbound network call must set an explicit timeout",
         reason="One unbounded call can pin a worker indefinitely and cascade into pool exhaustion under load.",
         scope="workers/", hardness="absolute", tags=["reliability"], origin="agent", related_to=["dec-013"]),
    dict(id="c_migrate", rule="Migrations must be backward-compatible for one release",
         reason="Because old and new app replicas run simultaneously during a rollout, a non-backward-compatible schema change crashes the still-running old replicas.",
         scope="migrations/", hardness="absolute", tags=["database", "deployment"], origin="user", related_to=["dec-011"]),
    dict(id="c_camel", rule="All API responses use snake_case JSON keys",
         reason="Mixed key casing across endpoints breaks clients that deserialize into fixed structs and creates needless churn.",
         scope="api/", hardness="advisory", tags=["api", "conventions"], origin="agent"),
    dict(id="c_py", rule="Support Python 3.10 and newer only",
         reason="The codebase relies on structural pattern matching and modern typing that predate 3.10 do not provide.",
         scope="global", hardness="advisory", tags=["tooling"], origin="user"),
    dict(id="c_payload", rule="Reject enqueue requests with payloads over 256 KB",
         reason="Oversized inline payloads bloat the hot records table and degrade every worker's dequeue scan.",
         scope="api/", hardness="absolute", tags=["storage", "performance"], origin="user", related_to=["dec-014"]),
]

PIPELINES = [
    dict(id="p_deploy", name="Production deploy",
         purpose="Ship a new version safely with schema changes, without a retry storm or a half-applied migration.",
         when_to_invoke="Any release to production that may include a database migration.",
         steps=[{"order": 1, "action": "run migration job, gate rollout on exit zero"},
                {"order": 2, "action": "roll out new image to workers with SIGTERM drain"},
                {"order": 3, "action": "roll out new image to API replicas"},
                {"order": 4, "action": "watch dead_letter rate and trace-id error logs for 15 min"}],
         tags=["deployment", "operations"], origin="user", related_to=["dec-011", "dec-016"]),
    dict(id="p_incident", name="Stuck-queue incident response",
         purpose="Diagnose and clear a backlog when jobs stop draining, in a fixed order that avoids masking the root cause.",
         when_to_invoke="When queue depth climbs and jobs are not completing.",
         steps=[{"order": 1, "action": "check worker liveness and claimed_at ages"},
                {"order": 2, "action": "inspect dead_letter for a common failing dependency"},
                {"order": 3, "action": "verify downstream timeouts are firing, not hanging"},
                {"order": 4, "action": "scale workers only after ruling out a poison job"}],
         tags=["operations", "reliability"], origin="user", related_to=["dec-005", "dec-013"]),
    dict(id="p_onboard", name="New downstream integration",
         purpose="Add a new downstream dependency without violating the reliability invariants the service depends on.",
         when_to_invoke="Whenever a worker step starts calling a new external service.",
         steps=[{"order": 1, "action": "set an explicit timeout on the new call"},
                {"order": 2, "action": "classify its failures as retryable or dead-letter"},
                {"order": 3, "action": "add a trace-id log line around the call"}],
         tags=["reliability", "operations"], origin="agent", related_to=["dec-012"]),
]

# NL query -> gold entry key(s). Includes vocabulary-mismatch cases where the
# query shares few/no keywords with the entry it should surface.
LABELED = [
    ("where do we keep the job records", ["storage"]),
    ("why postgres instead of a real message broker for the queue", ["queue"]),
    ("what stops the same job from running twice", ["idempotency"]),        # vocab mismatch
    ("how do we handle a job that keeps failing", ["retries", "deadletter"]),
    ("how do backend services authenticate to the API", ["auth"]),
    ("how do we stop one client flooding the enqueue endpoint", ["ratelimit"]),
    ("how is the service packaged and shipped", ["deploy"]),
    ("how do we avoid a half-applied schema during release", ["migrations"]),
    ("how do we trace a misbehaving job", ["observability"]),
    ("what protects a worker from a hung dependency", ["timeouts", "c_timeout"]),  # vocab mismatch
    ("what happens to oversized job inputs", ["payload", "c_payload"]),
    ("rule about logging the signing secret", ["c_secret"]),
    ("what is the deploy procedure", ["p_deploy"]),
    ("queue is backed up and nothing is draining, what do we do", ["p_incident"]),  # vocab mismatch
    ("do deploys interrupt running jobs", ["shutdown"]),
]

# Topics deliberately ABSENT from the store (no-answer). Hard-negatives share
# vocabulary with a real entry but ask about something not recorded.
NO_ANSWER = [
    "how is customer billing calculated",
    "which css framework does the admin dashboard use",
    "how do we send SMS notifications to users",
    "how are the job records encrypted at rest",          # hard-neg: records present, crypto absent
    "how does the queue autoscale onto GPU nodes",         # hard-neg: queue present, GPU absent
    "what is the oauth login flow for end users",           # hard-neg: auth present, oauth/end-users absent
]


def _prefix_map():
    return {"decisions": "dec", "constraints": "con", "pipelines": "pipe"}


def build_store(base_dir, scale_to=0):
    """Record the curated corpus (and optional templated filler) into base_dir.

    Returns a dict mapping our stable keys ('storage', 'c_secret', ...) to the
    real assigned ids ('dec-001', 'con-001', ...), so labels resolve to ids.
    """
    ctx = os.path.join(base_dir, ".context")
    os.makedirs(ctx, exist_ok=True)
    # Idempotent: wipe any prior entry files so a re-run yields exactly the
    # curated corpus (record_* appends, so without this a second run doubles it).
    for name in ("decisions", "pipelines", "constraints"):
        fp = os.path.join(ctx, name + ".json")
        if os.path.exists(fp):
            os.remove(fp)
    key_to_id = {}

    # record decisions in order; resolve related_to keys after we know ids
    pending_related = {}
    for d in DECISIONS:
        params = {k: v for k, v in d.items() if k not in ("id", "related_to")}
        params["project_dir"] = base_dir
        r = server.handle_record_decision(params)
        key_to_id[d["id"]] = r["id"]
        if d.get("related_to"):
            pending_related[r["id"]] = d["related_to"]
    for c in CONSTRAINTS:
        params = {k: v for k, v in c.items() if k not in ("id", "related_to")}
        params["project_dir"] = base_dir
        r = server.handle_record_constraint(params)
        key_to_id[c["id"]] = r["id"]
        if c.get("related_to"):
            pending_related[r["id"]] = c["related_to"]
    for p in PIPELINES:
        params = {k: v for k, v in p.items() if k not in ("id", "related_to")}
        params["project_dir"] = base_dir
        r = server.handle_record_pipeline(params)
        key_to_id[p["id"]] = r["id"]

    # related_to in DECISIONS was authored as literal dec-00N ids assuming
    # insertion order; since we recorded in order those already line up, but
    # normalize through key_to_id where a key was used.
    # (Our related_to values are literal ids like "dec-002" that match the
    # recording order, so no rewrite needed. Filler below is independent.)

    # Optional templated filler to grow the store for the token-scaling metric.
    for i in range(scale_to):
        topic = f"module {chr(65 + (i % 20))}{i // 20}"
        server.handle_record_decision({
            "project_dir": base_dir,
            "summary": f"Adopt convention {i+1} for {topic}",
            "problem": (f"The team kept re-litigating how to handle {topic}; "
                        "an unwritten convention led to inconsistent implementations across the codebase."),
            "why_chosen": (f"Writing the convention for {topic} down once removes the repeated debate and "
                           "gives reviewers a concrete rule to point at, at the cost of a little upfront documentation."),
            "tags": ["conventions", f"area-{i % 8}"],
            "origin": "import",
        })
    return key_to_id


def estimate_tokens(text):
    return server.estimate_tokens(text)


def metric_token_reduction(base_dir, label):
    """Mirror of token_reduction.py: full active-store dump vs injected summary."""
    ctx = os.path.join(base_dir, ".context")
    parts, total = [], 0
    for name in ("decisions", "pipelines", "constraints"):
        entries = server.read_json_file(os.path.join(ctx, name + ".json"))
        active = [e for e in entries if e.get("status", "active") != "deprecated"]
        total += len(active)
        parts.append(json.dumps(active, indent=2))
    full = estimate_tokens("\n".join(parts))
    summary = server.handle_get_project_summary({"project_dir": base_dir})
    injected = estimate_tokens((summary.get("summary") or "") + "\n" + (summary.get("usage_guidance") or ""))
    red = 100.0 * (1 - injected / full) if full else 0.0
    return dict(label=label, entries=total, full_tokens=full, injected_tokens=injected,
               reduction_pct=round(red, 1))


def _get_ids(base_dir, query, **extra):
    params = {"project_dir": base_dir, "query": query, "include_related": False}
    params.update(extra)
    r = server.handle_get_context(params)
    return [x["entry"]["id"] for x in r.get("results", [])], r


def metric_retrieval(base_dir, key_to_id, k=5):
    """Mirror of retrieval_eval.RetrievalScorer: hit@k + MRR over labeled queries."""
    hits, rr_sum, rows = 0, 0.0, []
    for query, gold_keys in LABELED:
        gold = {key_to_id[g] for g in gold_keys}
        ids, _ = _get_ids(base_dir, query)
        rank = next((i + 1 for i, rid in enumerate(ids) if rid in gold), None)
        hit = rank is not None and rank <= k
        rr = (1.0 / rank) if hit else 0.0
        hits += 1 if hit else 0
        rr_sum += rr
        rows.append((query, rank, hit))
    n = len(LABELED)
    return dict(k=k, n=n, hit_at_k=hits / n, mrr=rr_sum / n, rows=rows)


def metric_abstention(base_dir, floors=(0.10, 0.15, 0.18, 0.20, 0.25, 0.30, 0.35)):
    """Mirror of abstention.py: TNR (abstain on no-answer) vs false-abstention."""
    def top_rel(query):
        _, r = _get_ids(base_dir, query)
        best = 0.0
        for x in r.get("results", []):
            sig = server._relevance_signal(x.get("entry", {}), None, query)
            if sig is not None:
                best = max(best, sig)
        return best
    pos = [top_rel(q) for q, _ in LABELED]
    neg = [top_rel(q) for q in NO_ANSWER]
    table = []
    for f in floors:
        tn = sum(1 for r in neg if r < f)
        fa = sum(1 for r in pos if r < f)
        table.append((f, tn / len(neg), fa / len(pos)))
    return dict(positives=len(pos), negatives=len(neg), table=table,
               pos=sorted(pos), neg=sorted(neg))


def metric_mmr(base_dir, key_to_id, k=5):
    """Mirror of mmr_check.py: redundancy@k off vs on, hit@k must not drop."""
    byid = {}
    base = server._base_dir_from_params({"project_dir": base_dir})
    for _t, p in server._resolve_paths(base).items():
        for e in server.read_json_file(p):
            byid[e.get("id")] = e

    def redundancy(ids):
        ws = [server._text_words(byid[i]) for i in ids if i in byid]
        if len(ws) < 2:
            return 0.0
        pairs = [server._jaccard(ws[a], ws[b]) for a, b in itertools.combinations(range(len(ws)), 2)]
        return sum(pairs) / len(pairs)

    ro = rn = ho = hn = 0.0
    for query, gold_keys in LABELED:
        gold = {key_to_id[g] for g in gold_keys}
        off, _ = _get_ids(base_dir, query)
        on, _ = _get_ids(base_dir, query, mmr={"enabled": True, "lambda": 0.7})
        off, on = off[:k], on[:k]
        ro += redundancy(off); rn += redundancy(on)
        ho += 1.0 if gold & set(off) else 0.0
        hn += 1.0 if gold & set(on) else 0.0
    n = len(LABELED)
    return dict(k=k, n=n, red_off=ro / n, red_on=rn / n, hit_off=ho / n, hit_on=hn / n)


def metric_cold_vs_injection(base_dir):
    """Bonus: tokens to answer a project question COLD (dump whole active store)
    vs WITH get_context injection (only the entries it returns). Measured."""
    ctx = os.path.join(base_dir, ".context")
    parts = []
    for name in ("decisions", "pipelines", "constraints"):
        entries = server.read_json_file(os.path.join(ctx, name + ".json"))
        parts.append(json.dumps([e for e in entries if e.get("status", "active") != "deprecated"], indent=2))
    cold = estimate_tokens("\n".join(parts))
    questions = [
        "what stops the same job from running twice",
        "how do we handle a job that keeps failing",
        "what is the deploy procedure",
    ]
    # Targeted injection: a realistic per-question budget returns only the top
    # relevant entries, not the whole store. Budget stated so it's reproducible.
    budget = 1000
    rows = []
    for q in questions:
        r = server.handle_get_context({"project_dir": base_dir, "query": q,
                                       "include_related": True, "token_budget": budget})
        inj = estimate_tokens(json.dumps([x["entry"] for x in r.get("results", [])], indent=2))
        rows.append((q, inj, len(r.get("results", [])),
                    round(100.0 * (1 - inj / cold), 1) if cold else 0.0))
    return dict(cold_tokens=cold, budget=budget, rows=rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="dir to build the store in (persists for token_reduction.py)")
    args = ap.parse_args()

    out = args.out or tempfile.mkdtemp(prefix="ck-synth-")
    key_to_id = build_store(out, scale_to=0)

    # A second, larger store (curated core + templated filler) for token scaling.
    big = tempfile.mkdtemp(prefix="ck-synth-big-")
    build_store(big, scale_to=44)  # ~26 curated + 44 filler = ~70 entries

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"# Synthetic-corpus eval run ({now})")
    print(f"store: {out}  (curated {len(DECISIONS)+len(CONSTRAINTS)+len(PIPELINES)} entries)")
    print(f"large store: {big}\n")

    tr_small = metric_token_reduction(out, "synthetic-curated")
    tr_big = metric_token_reduction(big, "synthetic-scaled")
    print("== token reduction ==")
    for r in (tr_small, tr_big):
        print(f"  {r['label']}: entries={r['entries']} full={r['full_tokens']} "
              f"injected={r['injected_tokens']} reduction={r['reduction_pct']}%")

    ret = metric_retrieval(out, key_to_id)
    print(f"\n== retrieval (lexical only) ==")
    print(f"  hit@{ret['k']}={ret['hit_at_k']:.1%}  MRR={ret['mrr']:.3f}  ({ret['n']} labeled queries)")
    for q, rank, hit in ret["rows"]:
        print(f"    [{'HIT ' if hit else 'MISS'}] rank={rank}  {q}")

    ab = metric_abstention(out)
    print(f"\n== abstention ({ab['positives']} answerable, {ab['negatives']} no-answer) ==")
    print(f"  positive relevance: min={ab['pos'][0]:.2f} med={ab['pos'][len(ab['pos'])//2]:.2f} max={ab['pos'][-1]:.2f}")
    print(f"  no-answer relevance: min={ab['neg'][0]:.2f} med={ab['neg'][len(ab['neg'])//2]:.2f} max={ab['neg'][-1]:.2f}")
    print("  floor | TNR | false-abstention")
    for f, tnr, far in ab["table"]:
        print(f"   {f:.2f} | {tnr:.0%} | {far:.0%}")

    mmr = metric_mmr(out, key_to_id)
    print(f"\n== MMR redundancy (k={mmr['k']}) ==")
    print(f"  redundancy@{mmr['k']}: off {mmr['red_off']:.3f} -> on {mmr['red_on']:.3f}")
    print(f"  hit@{mmr['k']}: off {mmr['hit_off']:.1%} -> on {mmr['hit_on']:.1%}")

    cvi = metric_cold_vs_injection(out)
    print(f"\n== cold vs get_context injection (budget={cvi['budget']}) ==")
    print(f"  cold (full active store dump): {cvi['cold_tokens']} tokens")
    for q, inj, n, red in cvi["rows"]:
        print(f"    injected {inj} tokens ({n} entries, -{red}%)  <-  {q}")

    print(f"\n(run the shipped script too:  python evals/token_reduction.py {out} {big} )")


if __name__ == "__main__":
    main()
