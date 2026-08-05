#!/usr/bin/env python3
"""Survey every local store for supersessions that were made as in-place edits.

READ ONLY. This script opens no store for writing and calls no lifecycle tool.
It produces a proposal file for a human to review, and nothing else.

That restraint is the point. The store already carries the evidence that
reversals happened without ever being linked -- the context-keeper store held
36 active entries, 0 deprecated and 0 supersedes links while two of them
narrated their own history inline ("replacing the old additive-only sync";
"(was additive-only)"). But "these two entries look related" is not the same
claim as "this one replaced that one", and only the second justifies a
supersedes edge. An edge written from a heuristic silently demotes an entry
that may still be in force, and it is written into the one system whose job is
to be trusted about what changed. So the machine proposes and a human decides.

Two signals, both deliberately weak on their own:

  markers    An entry whose own text says it changed something ("was",
             "previously", "replaced", "no longer"). That is the author
             telling you a predecessor existed. It does not say WHICH entry.

  overlap    An active pair covering the same subject -- shared tags, shared
             scope path components -- scored by the same function the
             write-time advisory uses (server._supersession_score), so the
             backfill and the ongoing guard agree about what "same subject"
             means and a backfilled link matches what the advisory would have
             suggested at the time.

A pair carrying BOTH signals is the strongest proposal: someone wrote down
that something changed, and there is a sibling entry about the same subject
for it to have changed FROM.

Usage:
    python scripts/survey_supersessions.py
    python scripts/survey_supersessions.py --root C:/Users/me/repos --out /tmp/p.md
    python scripts/survey_supersessions.py --threshold 0.4 --json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

STORE_FILES = (
    ("decisions", "decisions.json"),
    ("pipelines", "pipelines.json"),
    ("constraints", "constraints.json"),
)

# The author telling you a predecessor existed. Word-anchored: a substring
# search for "was" also fires on "wasted", and "replace" on "irreplaceable".
_MARKERS = (
    ("was", re.compile(r"\bwas\b", re.I)),
    ("previously", re.compile(r"\bpreviously\b", re.I)),
    ("replaced", re.compile(r"\breplac(?:e|es|ed|ing|ement)\b", re.I)),
    ("no longer", re.compile(r"\bno longer\b", re.I)),
)

# "was" is ordinary past tense -- a well-written triggering_incident is full of
# it, and on the first real run it produced most of the marker hits across 23
# stores. The other three assert a REPLACEMENT rather than merely a past. Both
# are kept and reported (dropping "was" would hide real supersessions written
# as "the merge was additive-only"), but a proposal carrying a strong marker
# sorts above one carrying only "was", so the reviewer's attention goes to the
# candidates most likely to be real.
_STRONG_MARKERS = frozenset({"previously", "replaced", "no longer"})

# Fields whose prose can carry a supersession marker. Deliberately excludes
# `what_we_tried`, which is ABOUT prior attempts by design -- every well-written
# entry would match there, so it carries no signal.
_MARKER_FIELDS = (
    "summary", "rule", "name", "problem", "why_chosen", "reason",
    "purpose", "tradeoffs", "triggering_incident", "when_to_invoke",
)

_SNIPPET_CHARS = 160


def discover_stores(roots):
    """Every .context/ store under the given roots, plus the roots themselves.

    Returns [(project_name, base_dir)] sorted by name.
    """
    found = {}
    for root in roots:
        root = os.path.abspath(root)
        candidates = [root]
        try:
            candidates += [e.path for e in os.scandir(root) if e.is_dir()]
        except OSError:
            continue
        for path in candidates:
            base = os.path.join(path, ".context")
            if not os.path.isdir(base):
                continue
            found[os.path.normcase(base)] = (_project_name(base, path), base)
    return sorted(found.values(), key=lambda t: (t[0].lower(), t[1]))


def _project_name(base_dir, project_dir):
    try:
        with open(os.path.join(base_dir, "config.json"), "r", encoding="utf-8") as f:
            name = json.load(f).get("project_name")
        if name:
            return str(name)
    except Exception:
        pass
    return os.path.basename(os.path.normpath(project_dir))


def _entry_ordinal(entry):
    """Sort key placing the OLDER entry first: created_at, then id ordinal.

    created_at leads because it is what "older" means. The id ordinal is the
    tiebreak rather than the primary key because ids are per-store sequences
    and an imported store can carry ids that do not match its own history.
    """
    eid = str(entry.get("id") or "")
    prefix = eid.split("-")[0] if "-" in eid else ""
    try:
        ordinal = server._leading_int(eid, prefix)
    except Exception:
        ordinal = 0
    return (str(entry.get("created_at") or ""), ordinal or 0, eid)


def _markers_in(entry):
    """[(marker, field, snippet)] for every supersession marker in the text."""
    hits = []
    for field in _MARKER_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            continue
        for label, pattern in _MARKERS:
            m = pattern.search(value)
            if not m:
                continue
            start = max(0, m.start() - 60)
            snippet = server._compact(value[start:m.end() + 100], _SNIPPET_CHARS)
            hits.append((label, field, snippet))
    return hits


def _already_linked(entries):
    """Ids on either end of an existing supersedes edge -- nothing to propose."""
    linked = set()
    for e in entries:
        target = e.get("superseded_by")
        if target:
            linked.add(e.get("id"))
            linked.add(target)
    return linked


def survey_store(base_dir, threshold, project="?"):
    """Proposals and unpaired markers for one store. Opens nothing for writing."""
    proposals = []
    unpaired = []
    for type_name, filename in STORE_FILES:
        entries = server.read_json_file(os.path.join(base_dir, filename))
        linked = _already_linked(entries)
        active = [e for e in entries
                  if e.get("status", "active") == "active"
                  and e.get("id")
                  and e.get("id") not in linked]
        active.sort(key=_entry_ordinal)

        # Every same-kind pair, scored once. Older is the earlier of the two by
        # construction: the list is sorted oldest-first.
        pair_score = {}
        for i, older in enumerate(active):
            for newer in active[i + 1:]:
                score = server._supersession_score(newer, older)
                if score >= threshold:
                    pair_score[(older["id"], newer["id"])] = score

        marked = {}
        for e in active:
            hits = _markers_in(e)
            if hits:
                marked[e["id"]] = hits

        by_id = {e["id"]: e for e in active}
        proposed_newer = set()
        for (older_id, newer_id), score in pair_score.items():
            evidence = {
                "overlap_score": round(score, 2),
                "shared_tags": sorted(
                    {str(t).lower() for t in (by_id[older_id].get("tags") or [])}
                    & {str(t).lower() for t in (by_id[newer_id].get("tags") or [])}),
                "scope_overlap": server._scope_overlap(
                    by_id[older_id].get("scope"), by_id[newer_id].get("scope")),
                "markers": [
                    {"marker": m, "field": f, "text": s}
                    for (m, f, s) in marked.get(newer_id, [])
                ],
            }
            evidence["strong_markers"] = sorted(
                {m["marker"] for m in evidence["markers"]} & _STRONG_MARKERS)
            proposals.append({
                "project": project,
                "kind": type_name,
                "older_id": older_id,
                "newer_id": newer_id,
                "older_summary": _label(by_id[older_id]),
                "newer_summary": _label(by_id[newer_id]),
                "both_signals": bool(evidence["markers"]),
                "evidence": evidence,
            })
            if evidence["markers"]:
                proposed_newer.add(newer_id)

        # Markers with no sibling to pair against. NOT proposals -- reported so
        # a silent drop cannot read as "nothing there". Often the honest answer
        # is that the predecessor was never recorded as an entry at all.
        for eid, hits in sorted(marked.items()):
            if eid in proposed_newer:
                continue
            unpaired.append({
                "project": project,
                "kind": type_name,
                "id": eid,
                "summary": _label(by_id[eid]),
                "markers": [{"marker": m, "field": f, "text": s} for (m, f, s) in hits],
            })
    return proposals, unpaired


def _label(entry):
    return server._compact(
        entry.get("summary") or entry.get("rule") or entry.get("name") or "?", 120)


def render_report(proposals, unpaired, stores, threshold):
    """The reviewable artifact. ASCII only."""
    both = [p for p in proposals if p["both_signals"]]
    overlap_only = [p for p in proposals if not p["both_signals"]]
    lines = [
        "# Supersession backfill proposals",
        "",
        "Generated by scripts/survey_supersessions.py. **Nothing was written.**",
        "No store was opened for writing and deprecate_entry was not called.",
        "",
        "Each proposal is a QUESTION: did the newer entry replace the older one?",
        "If yes, link it with `deprecate_entry(older, reason, superseded_by=newer)`.",
        "If no, leave it -- an unlinked pair costs nothing, a wrong supersedes",
        "edge demotes a rule that is still in force.",
        "",
        f"- stores scanned: {len(stores)}",
        f"- overlap threshold: {threshold}",
        f"- proposals with BOTH signals (text marker + subject overlap): {len(both)}",
        f"- proposals from subject overlap only: {len(overlap_only)}",
        f"- text markers with no sibling to pair against: {len(unpaired)}",
        "",
    ]

    lines += ["## Both signals", ""]
    if both:
        lines.append(
            "The newer entry's own text says something changed, AND there is an "
            "older entry about the same subject. Strongest candidates. Those "
            "carrying a strong marker (previously / replaced / no longer) sort "
            "first; a bare `was` is often just past tense in an incident "
            "narrative, so it is kept but ranked below.")
        lines.append("")
        lines += _render_proposals(both)
    else:
        lines += ["None.", ""]

    lines += ["## Subject overlap only", ""]
    if overlap_only:
        lines.append(
            "Same subject, no text marker. Many of these are simply two entries "
            "about one area, which is fine and normal -- read before linking.")
        lines.append("")
        lines += _render_proposals(overlap_only)
    else:
        lines += ["None.", ""]

    lines += ["## Markers with no candidate predecessor", ""]
    if unpaired:
        lines.append(
            "These entries narrate a change but no sibling entry matches as the "
            "thing they changed FROM. Usually that means the predecessor was "
            "never recorded -- the history exists only as prose inside this "
            "entry. Nothing to link; listed so the gap is visible.")
        lines.append("")
        for u in unpaired:
            lines.append(f"- `{u['id']}` ({u['project']}/{u['kind']}) -- {u['summary']}")
            for m in u["markers"]:
                lines.append(f"  - **{m['marker']}** in `{m['field']}`: {m['text']}")
        lines.append("")
    else:
        lines += ["None.", ""]

    return "\n".join(lines) + "\n"


def _render_proposals(proposals):
    lines = []
    for p in sorted(proposals,
                    key=lambda x: (not x["evidence"].get("strong_markers"),
                                   -x["evidence"]["overlap_score"],
                                   x["project"], x["older_id"])):
        ev = p["evidence"]
        strong = ev.get("strong_markers") or []
        lines.append(
            f"### {p['project']} / {p['kind']}: `{p['older_id']}` -> `{p['newer_id']}`")
        lines.append("")
        if strong:
            lines.append(f"- **strong marker(s): {', '.join(strong)}**")
        lines.append(f"- older `{p['older_id']}`: {p['older_summary']}")
        lines.append(f"- newer `{p['newer_id']}`: {p['newer_summary']}")
        lines.append(f"- overlap score: {ev['overlap_score']}")
        if ev["shared_tags"]:
            lines.append("- shared tags: " + ", ".join(ev["shared_tags"]))
        if ev["scope_overlap"] is not None:
            lines.append(f"- scope component overlap: {round(ev['scope_overlap'], 2)}")
        for m in ev["markers"]:
            lines.append(f"- marker **{m['marker']}** in `{m['field']}`: {m['text']}")
        lines.append("")
    return lines


def main(argv=None):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--root", action="append", default=None,
        help="Directory whose children are projects. Repeatable. "
             "Default: the directory containing this repo.")
    # Default output is GITIGNORED, and that is a safety property, not tidiness.
    # This tool reads every .context store on the machine, so its output quotes
    # whatever those projects recorded -- including ones that are private, or
    # local-only with no remote at all. This repo is public. A proposal file
    # committed here would publish other projects' design history as a side
    # effect of a chore, which is exactly what happened the first time it ran.
    # Review it locally; it is a personal worklist, not repo documentation.
    parser.add_argument(
        "--out", default=os.path.join(repo, ".supersession_proposal.md"),
        help="Where to write the proposal file (default is gitignored).")
    parser.add_argument(
        "--threshold", type=float, default=server.DEFAULT_SUPERSESSION_THRESHOLD,
        help="Minimum subject-overlap score for a proposed pair.")
    parser.add_argument("--json", action="store_true", help="Write JSON instead of Markdown.")
    args = parser.parse_args(argv)

    roots = args.root or [os.path.dirname(repo)]
    stores = discover_stores(roots)

    proposals, unpaired = [], []
    for project, base_dir in stores:
        p, u = survey_store(base_dir, args.threshold, project=project)
        proposals += p
        unpaired += u

    if args.json:
        body = json.dumps({
            "stores": [{"project": n, "base_dir": b} for n, b in stores],
            "threshold": args.threshold,
            "proposals": proposals,
            "unpaired_markers": unpaired,
        }, indent=2)
    else:
        body = render_report(proposals, unpaired, stores, args.threshold)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(body)

    both = sum(1 for p in proposals if p["both_signals"])
    print("stores scanned: {}".format(len(stores)))
    print("proposals: {} ({} with both signals)".format(len(proposals), both))
    print("unpaired markers: {}".format(len(unpaired)))
    print("written: {}".format(args.out))
    print("NOTHING WAS WRITTEN TO ANY STORE -- review the file and decide the links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
