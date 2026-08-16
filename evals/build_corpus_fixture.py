#!/usr/bin/env python3
"""Freeze the eval corpus into evals/fixtures/corpus/.

Run when you deliberately want to refresh the corpus; the OUTPUT is committed.

Why freeze at all. The golden set asks questions of real project stores, and the
regression test pins a number derived from the answers. Pointed at LIVE stores
that number measures two things at once -- the ranker, and whatever anybody
recorded since. It drifted the first time it was exercised: recording three new
context-keeper entries moved lexical recall@5 over the positive cases from 0.549
to 0.473, a 0.076 swing with no code change at all. A pin that moves when the
corpus moves cannot tell you the ranker regressed, which is the only thing it
was for.

So the corpus is frozen and the ranker is the only variable. Two further things
fall out of it, both of which the audit wanted:

  * the regression test stops SKIPPING in CI (it needed sibling repos that only
    exist on a dev machine), so the pin is actually defended on every push;
  * anyone cloning the repo can reproduce the published numbers.

`--live` on the harness still runs against the real stores when you want a
current read rather than a comparable one.

PUBLIC STORES ONLY, asserted below. A case's query and notes paraphrase the
entry they target and this repo is public (con-015-12da); the same reasoning
applies with more force to vendoring the entries themselves.
"""

import json
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPOS = os.path.dirname(os.path.dirname(HERE))
CORPUS = os.path.join(HERE, "fixtures", "corpus")

# Every store the golden set draws from. Each must be a PUBLIC repo.
STORES = ("context-keeper", "Clark", "Conductor", "cambium",
          "agentsync", "meristem", "xylem")

# Only the entry files. Deliberately excludes usage.json (retrieval telemetry),
# embeddings.json (a derived cache) and config.json (per-machine settings, and
# the place a remote URL would live).
ENTRY_FILES = ("decisions.json", "pipelines.json", "constraints.json")

# Repo names that are also ordinary English words. Matching these by name would
# fire on almost every entry, so the scan reports them as UNCHECKED instead of
# crying wolf. Keep it short and justify each addition.
_COMMON_WORDS = {"knowledge", "logs", "docs", "notes", "tools", "scratch"}


def _is_public(store):
    """Refuse to vendor a private or remote-less project's entries."""
    try:
        out = subprocess.run(
            ["gh", "repo", "view", f"jarmstrong158/{store}", "--json", "visibility",
             "-q", ".visibility"],
            capture_output=True, text=True, timeout=25)
    except Exception:
        return None  # cannot tell -- caller decides, never assume public
    if out.returncode != 0:
        return None
    return out.stdout.strip().upper() == "PUBLIC"


def _nonpublic_projects():
    """Every project on this machine that is NOT public: private repos, and any
    directory with no remote at all. Derived, never hand-listed — a hand-listed
    set is one `gh repo create --private` away from being wrong."""
    names = set()
    for name in sorted(os.listdir(REPOS)):
        path = os.path.join(REPOS, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        remote = subprocess.run(["git", "-C", path, "remote"],
                                capture_output=True, text=True, timeout=20)
        if not remote.stdout.strip():
            names.add(name)                      # no remote at all
            continue
        vis = subprocess.run(
            ["gh", "repo", "view", f"jarmstrong158/{name}", "--json", "visibility",
             "-q", ".visibility"], capture_output=True, text=True, timeout=25)
        if vis.returncode != 0 or vis.stdout.strip().upper() != "PUBLIC":
            names.add(name)                      # private, or cannot tell
    return names


def _scan_for_nonpublic_mentions():
    """A PUBLIC store's entries can still describe a PRIVATE project.

    The store-level check above asks "may we vendor this project's entries"; it
    cannot ask "do those entries talk about somebody else". They do: a public
    project's decision described a private game's sprite generator by filename,
    and another detailed a private repo's dependency bug. Both were committed
    and pushed here before anyone looked.

    So the copied fixture is scanned for the NAME of every non-public project on
    the machine, and a hit fails the build. Names are matched on word boundaries
    so a substring inside an unrelated word does not fire."""
    nonpublic = _nonpublic_projects()
    if not nonpublic:
        return [], []
    # A project whose name is also an ordinary word cannot be found by name:
    # `knowledge` is a private repo AND appears in dozens of entries as plain
    # English ("the knowledge layer", "knowledge.json"). Scanning for it fires on
    # nearly every file, and a check that fires everywhere is not a check. Those
    # names are returned as UNCHECKED so the gap is stated rather than hidden.
    unscannable = sorted(n for n in nonpublic if n.lower() in _COMMON_WORDS)
    scannable = nonpublic - set(unscannable)
    hits = []
    for base, _dirs, files in os.walk(CORPUS):
        for fname in files:
            path = os.path.join(base, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    blob = f.read()
            except OSError:
                continue
            for proj in scannable:
                if re.search(r"(?<![\w-])" + re.escape(proj) + r"(?![\w-])",
                             blob, re.I):
                    hits.append((os.path.relpath(path, HERE), proj))
    return hits, unscannable


def build(check_visibility=True):
    if os.path.isdir(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS, exist_ok=True)

    total = 0
    for store in STORES:
        if check_visibility:
            vis = _is_public(store)
            if vis is None:
                raise SystemExit(
                    "could not determine visibility for %r. Refusing: an "
                    "unknown answer is not a public one (con-015-12da). Re-run "
                    "with --no-visibility-check only if you have checked by hand."
                    % store)
            if not vis:
                raise SystemExit(
                    "%r is not a public repo; its entries must not be vendored "
                    "into this public repo (con-015-12da)." % store)

        src = os.path.join(REPOS, store, ".context")
        dst = os.path.join(CORPUS, store, ".context")
        os.makedirs(dst, exist_ok=True)
        n = 0
        for name in ENTRY_FILES:
            s = os.path.join(src, name)
            entries = []
            if os.path.exists(s):
                with open(s, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            n += len(entries)
            with open(os.path.join(dst, name), "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=True)
        # A config naming the fixture, so nothing mistakes it for a live store.
        with open(os.path.join(dst, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"project_name": "corpus-%s" % store,
                       "_note": "Frozen eval corpus. Not live state."}, f, indent=2)
        total += n
        print("  %-18s %4d entries" % (store, n))

    if check_visibility:
        hits, unchecked = _scan_for_nonpublic_mentions()
        if unchecked:
            print("\n  NOT CHECKED (name is an ordinary word): %s"
                  % ", ".join(unchecked))
        if hits:
            shutil.rmtree(CORPUS, ignore_errors=True)
            print("\nREFUSING: the frozen corpus names non-public projects.")
            for path, proj in sorted(set(hits)):
                print("   %-56s mentions %s" % (path, proj))
            raise SystemExit(
                "\nA public store's entries can still describe a private one "
                "(con-015-12da). Redact those entries, drop the store, or "
                "re-run with --no-visibility-check having checked by hand.")

    print("frozen corpus: %d entries across %d stores" % (total, len(STORES)))
    return total


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    build(check_visibility="--no-visibility-check" not in argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
