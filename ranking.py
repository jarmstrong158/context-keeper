"""Relevance ranking: how get_context decides what comes back, and in what order.

Pure functions over entry dicts. No I/O, no store access, no config reads -- the
caller resolves those and passes the answers in. That is what makes this
testable in isolation and what keeps it out of the storage and protocol layers
it used to be interleaved with.

The pieces, roughly in the order get_context uses them:

  estimate_tokens / _truncate_entry   the budget arithmetic
  score_entry                          the composite rank (tags, text, scope,
                                       recency, status, origin)
  _relevance_signal                    the HONESTY signal, deliberately NOT the
                                       composite: the composite banks ~55 points
                                       from recency/status/origin regardless of
                                       whether the entry matches, so keying
                                       abstention on it would call every query a
                                       confident hit (dec-010)
  _mmr_reorder                         optional diversity pass so near-duplicates
                                       do not crowd the budget

Scope comparison goes through scope_rules, shared with the hook, work_focus and
the .claude/rules projection, so no two surfaces can disagree (con-011-76f8).
"""

import json
from datetime import datetime

import scope_rules

def estimate_tokens(text):
    return max(1, len(text) // 4)


def _text_words(entry):
    """Extract searchable words from an entry's text fields.

    Pulls from both v0.4 structured fields (problem, why_chosen,
    what_we_tried, tradeoffs, purpose, when_to_invoke,
    triggering_incident) and the legacy `rationale` field so old
    entries remain searchable.
    """
    parts = []
    text_keys = (
        "summary", "rationale", "rule", "reason", "name",
        "problem", "why_chosen", "what_we_tried", "tradeoffs",
        "purpose", "when_to_invoke", "triggering_incident",
    )
    for key in text_keys:
        val = entry.get(key, "")
        if val:
            parts.append(val.lower())
    for tag in entry.get("tags", []):
        parts.append(tag.lower())
    # Anticipated-query hints: alternate phrasings supplied at record time,
    # indexed so vocabulary-mismatch queries hit without embeddings.
    for hint in entry.get("retrieval_hints", []) or []:
        parts.append(str(hint).lower())
    # Include step actions for pipelines
    for step in entry.get("steps", []):
        action = step.get("action", "")
        if action:
            parts.append(action.lower())
    return set(" ".join(parts).split())


def score_entry(entry, query_tags=None, query_text=None, scope=None, now_dt=None):
    score = 0.0
    entry_tags = set(t.lower() for t in entry.get("tags", []))
    entry_words = _text_words(entry)

    # Tag matching (0-40)
    if query_tags:
        q_tags = set(t.lower() for t in query_tags)
        overlap = len(entry_tags & q_tags)
        score += 40 * (overlap / max(len(q_tags), 1))

    # Free-text word matching against tags + text fields (0-40)
    if query_text:
        q_words = set(query_text.lower().split())
        overlap = len(q_words & entry_words)
        score += 40 * (overlap / max(len(q_words), 1))

    # If neither tags nor text query, give base score so all entries are considered
    if not query_tags and not query_text:
        score += 20

    # Scope matching (0-20)
    #
    # Whole components, via the shared rule. This was `scope.lower() in
    # entry_scope.lower()` -- the exact substring test con-011-76f8 exists to
    # forbid, surviving in the ranking path because the constraint's wording
    # names coverage decisions and this is a relevance boost. It behaved
    # accordingly: querying scope="hooks/" handed a `webhooks/`-scoped entry the
    # identical full +20 as the real `hooks/` entry. Related-area scopes still
    # score partially, which substring matching could never express -- `hooks/`
    # against `hooks/scope_guard.py` is a real 0.5 overlap, not a binary miss.
    entry_scope = entry.get("scope", "global")
    entry_is_domain = scope_rules.is_domain(entry_scope)
    if scope:
        rel = None if entry_is_domain else scope_rules.overlap(scope, entry_scope)
        if rel:
            score += 20 * rel
        elif entry_is_domain:
            score += 10
        else:
            score += 5
    elif entry_is_domain:
        score += 10
    else:
        score += 5

    # Recency (0-20)
    verified = entry.get("verified_at") or entry.get("updated_at") or entry.get("created_at")
    if verified and now_dt:
        try:
            v_dt = datetime.fromisoformat(verified.replace("Z", "+00:00"))
            days_ago = (now_dt - v_dt).days
            # Clamp to [0, 1]: a future timestamp (clock skew, manual edit)
            # must not push recency past its 20-point cap.
            recency = max(0.0, min(1.0, 1 - (days_ago / 90)))
            score += 20 * recency
        except Exception:
            score += 10  # can't parse, give middle score

    # Status (0-20)
    status = entry.get("status", "active")
    if status == "active":
        score += 20
    elif status == "superseded":
        score += 5

    # Origin trust (0-10): user-stated entries outrank agent-inferred,
    # which outrank imported/backfilled. Entries without an origin
    # (pre-v0.7) score as agent — a uniform shift that preserves their
    # relative order.
    origin = entry.get("origin", "agent")
    score += {"user": 10, "agent": 5, "import": 2}.get(origin, 5)

    return score


# Common English + query-filler words that carry no retrieval signal. Small
# and dependency-free. Used ONLY for the abstention relevance signal below,
# never for the main ranking (which stays backward-compatible).
_STOPWORDS = frozenset("""
a an the this that these those of in on at to for from by with about into over
and or but if is are was were be been being do does did doing done have has had
how what why when where which who whom whose it its we our us you your they them
their i my me can could should would will shall may might must not no nor as so
than then there here out up down off again once only just also very more most
""".split())


# Cosine calibration for the abstention signal. These map a RAW embedding
# cosine onto the same 0-1 scale the lexical signal already lives on, and
# they are model-specific — do not treat them as universal.
#
# Why any mapping at all: a cosine from nomic-embed-text is not a
# probability and shares no scale with lexical overlap. Measured on this
# repo's eval set (evals/, 31 answerable + 16 no-answer queries across three
# real stores), nomic's cosines are compressed into a high, narrow band:
#
#     answerable queries   top cosine  min 0.636  median 0.719  max 0.815
#     NO-ANSWER queries    top cosine  min 0.515  median 0.603  max 0.718
#
# The bands overlap heavily, and the no-answer FLOOR is 0.515 — far above
# the 0.20 abstention floor. So the obvious implementation,
# max(lexical, cosine), puts every query on earth above the floor and
# silently disables abstention completely: measured TNR falls 19% -> 0%.
# ("what is the capital of France" scores ~0.49 against this store.)
#
# _SEM_REL_LO is therefore set just above the highest cosine any no-answer
# query reached (0.718), so the semantic term contributes exactly nothing
# until a match is stronger than anything a no-answer query produced, and
# lexical alone decides. Above that it ramps linearly to _SEM_REL_HI (near
# the observed answerable maximum), where it saturates at 1.0.
#
# Overridable per project via config semantic.relevance_floor /
# .relevance_ceiling, because a different embedding model has a different
# cosine distribution and these numbers would be wrong for it.
_SEM_REL_LO = 0.72
_SEM_REL_HI = 0.85


def _semantic_relevance(cosine, lo=_SEM_REL_LO, hi=_SEM_REL_HI):
    """Map a raw embedding cosine onto the lexical signal's 0-1 scale.

    Deliberately returns 0.0 for anything at or below `lo` rather than a
    small positive number: below the calibration floor the cosine carries
    no evidence that this entry answers the query, and letting it leak a
    fraction of a point is what would erode the abstention floor.
    """
    if cosine is None or hi <= lo:
        return 0.0
    if cosine <= lo:
        return 0.0
    return min(1.0, (cosine - lo) / (hi - lo))


def _relevance_signal(entry, query_tags, query_text, cosine=None,
                      sem_lo=_SEM_REL_LO, sem_hi=_SEM_REL_HI):
    """0-1 estimate of how well this entry matches the QUERY specifically —
    tag/text overlap, plus the calibrated semantic cosine when one is given.

    This is the signal the abstention floor keys on. The composite
    score_entry value is inflated by non-relevance terms (an active,
    recent, global entry banks ~55 points with zero query overlap), so a
    totally irrelevant entry can masquerade as a confident hit. This
    isolates the part that actually reflects "does this answer the query."

    `cosine` is the entry's embedding cosine when the opt-in semantic blend
    is active, and None otherwise. It matters because get_context already
    blends cosine into the RANKING: without it here, an entry retrieved
    purely on semantic similarity — the vocabulary-mismatch rescue the
    blend exists for — was ranked first and then flagged
    `no_confident_match` for having no lexical overlap, which is precisely
    backwards. The cosine is run through _semantic_relevance first; see the
    calibration note there for why the raw value must never be used
    directly.

    Returns None when no query is given — a bare summary/tag-less request
    is never an abstention case. Stopwords are dropped so filler like
    "what/the/we/about" doesn't manufacture overlap on no-answer queries.
    """
    sigs = []
    if query_tags:
        q = set(t.lower() for t in query_tags)
        if q:
            et = set(t.lower() for t in entry.get("tags", []))
            sigs.append(len(q & et) / len(q))
    if query_text:
        qw = {w for w in query_text.lower().split() if w and w not in _STOPWORDS}
        if qw:
            ew = _text_words(entry)
            sigs.append(len(qw & ew) / len(qw))
    if not sigs:
        # No lexical basis at all (no query text and no tags) -- a bare
        # request, not an abstention case, even if a cosine exists.
        return None
    if cosine is not None:
        sigs.append(_semantic_relevance(cosine, sem_lo, sem_hi))
    return max(sigs)


def _jaccard(a, b):
    """Word-set Jaccard similarity of two entries' text (zero-dependency)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _mmr_reorder(scored, lam=0.7):
    """Greedily reorder (score, type, entry) tuples for Maximal Marginal Relevance.

    Penalizes a candidate by its peak lexical similarity to already-selected
    entries, so near-duplicate restatements of one topic don't crowd the budget
    and a second relevant topic gets a seat. Entries linked by `related_to` are
    exempt from the penalty — those are intentional arcs meant to surface
    together, not redundancy. lam=1.0 reduces to pure relevance order.
    """
    if len(scored) < 2:
        return scored
    max_score = max((s for s, _t, _e in scored), default=0.0) or 1.0
    items = []
    for s, t, e in scored:
        items.append({
            "orig": s, "norm": s / max_score, "type": t, "entry": e,
            "words": _text_words(e), "id": e.get("id"),
            "related": set(e.get("related_to") or []),
        })
    selected, ordered = [], []
    while items:
        best_i, best_val = 0, None
        for i, d in enumerate(items):
            sim = 0.0
            for s in selected:
                if d["id"] in s["related"] or s["id"] in d["related"]:
                    continue  # arc-linked: no diversity penalty
                sim = max(sim, _jaccard(d["words"], s["words"]))
            val = lam * d["norm"] - (1 - lam) * sim
            if best_val is None or val > best_val:
                best_val, best_i = val, i
        chosen = items.pop(best_i)
        selected.append(chosen)
        ordered.append((chosen["orig"], chosen["type"], chosen["entry"]))
    return ordered


def _truncate_entry(entry, max_tokens):
    """Truncate an entry to fit within max_tokens. Returns a copy."""
    text = json.dumps(entry, indent=2)
    if estimate_tokens(text) <= max_tokens:
        return entry
    # Build truncated version with key fields only
    truncated = {"id": entry.get("id", "?")}
    for key in ("summary", "name", "rule"):
        if key in entry:
            truncated[key] = entry[key]
            break
    truncated["tags"] = entry.get("tags", [])
    truncated["status"] = entry.get("status", "active")
    truncated["_truncated"] = "Use get_context with this id for full entry"
    return truncated
