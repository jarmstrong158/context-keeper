"""The checks verify_quality runs, one function each.

These lived as a 117-line loop inside handle_verify_quality with seven checks
inlined, sharing a mutable list and a scope of loop-local variables. Nothing
could be exercised without running all of it, adding one meant editing the
middle of the longest function in the file, and no project could disable one.

Each check takes a _QualityContext and returns a list of issue dicts. Order in
QUALITY_CHECKS is the order issues are reported, and is part of the response
shape.

Depends on scope_rules, mojibake, and the two optional modules (code_drift,
usage) -- never on server, so the checks stay importable and testable without
the protocol layer.
"""

import os

import mojibake
import scope_rules

try:
    import code_drift
except ImportError:  # pragma: no cover - defensive, matches server.py
    code_drift = None
try:
    import usage
except ImportError:  # pragma: no cover - defensive
    usage = None



def _project_root_for(base_dir):
    """The project directory containing a .context/ dir, or None."""
    return os.path.dirname(os.path.abspath(base_dir)) if base_dir else None


def _suggest_scope(entry, project_root):
    """A concrete scope path this constraint could use, or None.

    `global` scope is not a formatting nit -- it is the difference between
    a rule that participates and one that does not. A global constraint
    gets no drift check (no path to compare commits against), no
    .claude/rules/ projection (no pattern to build), and never fires
    scope_guard. Its only route to the model is the session-start summary,
    which is also the thing that truncates first on a large store.

    Only suggests when there is real evidence, because a bare "consider
    adding a scope" on every global constraint is noise the reader learns
    to skip -- and some rules genuinely are global.
    """
    if not project_root:
        return None

    def _exists(rel):
        return rel and os.path.exists(os.path.join(project_root, rel))

    # Strongest evidence: the entry names its own enforcing test/command.
    # "tests/test_server.py::TestX" -> "tests/test_server.py".
    enforced = (entry.get("enforced_by") or "").strip()
    if enforced:
        candidate = enforced.split("::", 1)[0].strip().replace("\\", "/")
        candidate = candidate.split()[0] if candidate else ""
        if _exists(candidate) and scope_rules.to_glob_patterns(candidate):
            return candidate

    # Next best: a tag that names a real directory or module in the repo.
    for tag in entry.get("tags", []):
        for candidate in (f"{tag}/", f"{tag}.py", tag):
            rel = candidate.rstrip("/")
            if _exists(rel) and scope_rules.to_glob_patterns(candidate):
                return candidate
    return None


# ============================================================
# Quality checks
#
# These ran as one 117-line loop with seven checks inlined, sharing a mutable
# `issues` list and a scope of loop-local variables. Nothing could be tested
# without running all of it, adding an eighth meant editing the middle of the
# longest function in the file, and no project could turn one off. They are the
# same seven checks in the same order -- the registry is what changed.
# ============================================================


class _QualityContext:
    """Everything a check may look at, computed once per entry."""

    __slots__ = ("type_name", "entry", "entry_id", "base_dir",
                 "min_reason", "tag_index", "drift", "usage_data")

    def __init__(self, type_name, entry, entry_id, base_dir,
                 min_reason, tag_index, drift, usage_data):
        self.type_name = type_name
        self.entry = entry
        self.entry_id = entry_id
        self.base_dir = base_dir
        self.min_reason = min_reason
        self.tag_index = tag_index
        self.drift = drift
        self.usage_data = usage_data


def _check_legacy(c):
    """Pre-v0.4 entries: schema_version is set to 4 by v0.4 writes, so older
    entries lack it AND lack the structured fields."""
    if c.entry.get("schema_version") == 4:
        return []
    if c.type_name == "decisions" and not c.entry.get("why_chosen"):
        return [{
            "type": "legacy",
            "detail": "Pre-v0.4 decision: missing structured fields (problem, why_chosen). The freeform 'rationale' is preserved but won't show the full why. Consider re-recording with the v0.4 schema.",
        }]
    if c.type_name == "pipelines" and not c.entry.get("purpose"):
        return [{
            "type": "legacy",
            "detail": "Pre-v0.4 pipeline: missing 'purpose' field. Re-record so future sessions know why this pipeline exists, not just what it does.",
        }]
    # A constraint's triggering_incident is optional, so its absence alone is
    # not legacy -- thin_reason is what catches an under-explained one.
    return []


def _check_thin_reason(c):
    """The rationale field for this kind, measured against the threshold."""
    if c.type_name == "decisions":
        text = (c.entry.get("why_chosen") or "") + " " + (c.entry.get("rationale") or "")
    elif c.type_name == "constraints":
        text = c.entry.get("reason") or ""
    elif c.type_name == "pipelines":
        text = c.entry.get("purpose") or ""
    else:
        text = ""
    n = len(text.strip())
    if n >= c.min_reason:
        return []
    return [{
        "type": "thin_reason",
        "detail": f"Reasoning text is only {n} chars (threshold: {c.min_reason}). Use update_entry to expand the rationale with concrete context.",
    }]


def _check_no_tags(c):
    if c.entry.get("tags"):
        return []
    return [{
        "type": "no_tags",
        "detail": "Entry has no tags. Tags are the primary retrieval signal — add 2-4 lowercase, hyphen-separated tags.",
    }]


def _check_isolated(c):
    """Shares tags with siblings but carries no related_to link: a missed arc."""
    own_tags = set(t.lower() for t in c.entry.get("tags", []))
    own_links = set(c.entry.get("related_to", []) or [])
    siblings = set()
    for tag in own_tags:
        siblings |= c.tag_index.get(tag, set())
    siblings.discard(c.entry_id)
    unlinked = siblings - own_links
    if not (own_tags and unlinked and not own_links):
        return []
    return [{
        "type": "isolated",
        "detail": f"Shares tags with {len(unlinked)} other entries but has no related_to links. Suggested links: {sorted(unlinked)[:5]}",
    }]


def _check_code_drift(c):
    """The code this entry describes moved, or moved away entirely."""
    if not c.drift or code_drift is None:
        return []
    return list(code_drift.issues_for(c.drift.get(c.entry_id)))


def _check_enforced_by(c):
    """A constraint naming its own check is only useful while the name
    resolves. Never executed -- see code_drift.enforcement_issues."""
    if code_drift is None or not c.entry.get("enforced_by"):
        return []
    try:
        return list(code_drift.enforcement_issues(
            c.entry, code_drift.repo_root(c.base_dir)))
    except Exception:
        return []


def _check_unused(c):
    """Carried into every session and never actually sought.

    DECISIONS ONLY. A constraint is delivered by injection on purpose -- the
    session-start summary, the .claude/rules projection and scope_guard all push
    it at you, and nobody queries a rule they are already being handed. So
    "injected many times, never retrieved" is a constraint working exactly as
    designed, and flagging it was measuring decision-shaped behaviour against a
    rule.

    That miscalibration was not academic: it flagged 45 constraints, 36 of them
    `absolute`, and a bulk action on the flag would have retired live rules --
    "never treat the won flag as proof of a win" among them. A signal that fires
    on healthy entries is not a signal, and one that fires on the rules is worse
    than useless because acting on it removes the rules.

    Pipelines are excluded for the same reason: they are looked up by name when
    you already know the flow exists."""
    if usage is None or c.type_name != "decisions":
        return []
    return list(usage.issues_for(
        usage.stats_for(c.base_dir, c.entry_id, c.usage_data)))


def _check_global_scope(c):
    """A global-scoped constraint is invisible to every path-based delivery
    mechanism. Only flagged when a concrete path can be suggested."""
    if c.type_name != "constraints" or not scope_rules.is_domain(c.entry.get("scope") or "global"):
        return []
    suggestion = _suggest_scope(c.entry, _project_root_for(c.base_dir))
    if not suggestion:
        return []
    return [{
        "type": "global_scope",
        "detail": (
            f"Scope is 'global', so this rule gets no drift check, "
            f"no .claude/rules/ file, and never fires scope_guard -- "
            f"it reaches the model only via the session-start summary. "
            f"Evidence suggests scope='{suggestion}'. Set it with "
            f"update_entry, or leave it global if the rule really "
            f"does apply everywhere."
        ),
    }]


def _check_mojibake(c):
    """Text corrupted before con-008-dc30 forced UTF-8 on the transport.

    The store has to be able to report this itself: the damage is legible
    enough that nobody re-reads the entry, so it degrades every retrieval that
    surfaces it without ever announcing itself.
    """
    garbled = mojibake.entry_fields(c.entry)
    if not garbled:
        return []
    return [{
        "type": "mojibake",
        "detail": (
            f"{len(garbled)} field(s) carry cp1252-misdecoded UTF-8 "
            f"({', '.join(garbled[:4])}). Written before the transport "
            "was forced to UTF-8 (con-008). Repair with the "
            "repair_mojibake handler: "
            "context-keeper repair_mojibake '{\"apply\": true}'"
        ),
    }]


# Order is the reported order of issues on an entry, and is part of the
# response shape.
QUALITY_CHECKS = (
    ("legacy", _check_legacy),
    ("thin_reason", _check_thin_reason),
    ("no_tags", _check_no_tags),
    ("isolated", _check_isolated),
    ("code_drift", _check_code_drift),
    ("enforced_by", _check_enforced_by),
    ("unused", _check_unused),
    ("global_scope", _check_global_scope),
    ("mojibake", _check_mojibake),
)
