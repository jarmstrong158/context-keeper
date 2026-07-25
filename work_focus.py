"""Surface the rules that cover the code you are about to touch.

The session-start summary is compact -- ~1.3k tokens against a ~36k store -- but
it is not targeted. Every constraint is injected at full length whether or not
it has anything to do with the session's work, and decisions are grouped by
topic rather than by relevance to what is in front of you. In practice a session
opens with a full set of rules for parts of the repo nobody is going near.

dec-005 already established the right idea at the wrong moment: scope_guard
re-injects a scoped constraint when you edit a file it covers, because that is
when the rule matters. Session start has no equivalent signal even though git
knows exactly which files are dirty and which were touched recently. This module
supplies it.

Two properties this must not break, both load-bearing:

  Cache stability. The injected block is deterministically ordered with a stable
  prefix so an unchanged store injects byte-identical text across sessions and
  the prompt cache hits. Focus varies with the working tree, so it is APPENDED
  after everything stable -- never interleaved. The cached prefix stays
  byte-identical and only the tail moves.

  Budget. The focus block carries its own small cap and is appended after the
  summary's own truncation, matching how the other additive orientation fields
  are already handled: compact by design and accounted separately, rather than
  competing with constraints for the same budget and silently evicting them.

Stdlib plus the git binary. No work tree, no git, or a clean tree all mean the
same thing here -- no focus block at all, and the summary is exactly what it was
before.
"""
from __future__ import annotations

import os
import subprocess

# How far back "recently worked on" reaches when the tree is clean. Small on
# purpose: the point is what this session is about, not project history.
RECENT_COMMITS = 5

# Hard cap on the block. Task focus that crowds out the constraints it is meant
# to prioritise has defeated itself.
MAX_FOCUS_ENTRIES = 6
MAX_PATHS_SHOWN = 5


def _git(args, cwd, timeout=10):
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _norm(path):
    """Normalise to forward slashes with no './' prefix or trailing slash.

    lstrip("./") would be wrong here: it strips CHARACTERS, so '.context/'
    becomes 'context/' and '.github/workflows/ci.yml' loses its leading dot.
    Every dotfile path in the repo would silently fail to match its scope.
    """
    p = str(path).replace("\\", "/").strip().strip('"')
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


# The memory store is not what you are working on. It also mutates as a side
# effect of reading it -- usage.record() writes usage.json during
# get_project_summary -- so leaving it in would make generating a summary
# change the focus signal for the next one.
IGNORED_PREFIXES = (".context", ".context-keeper", ".git")


def _is_ignored(path):
    return any(path == p or path.startswith(p + "/") for p in IGNORED_PREFIXES)


def active_paths(root):
    """Files this session is plausibly about: uncommitted first, else recent.

    Uncommitted changes are the strongest available signal of intent -- they are
    what the developer is holding right now. Only when the tree is clean (a fresh
    session, or work just committed) does this fall back to the last few commits,
    which answers "what were we just doing".
    """
    if not root:
        return []

    # -uall: without it git collapses an untracked directory to "hooks/", so a
    # brand-new file never matches a file-scoped constraint — precisely the case
    # where the rules for that area are most worth surfacing.
    out = _git(["status", "--porcelain", "-uall"], root)
    if out:
        paths = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            p = line[3:]
            # Renames read as "old -> new"; the new path is the live one.
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            p = _norm(p)
            if p and not _is_ignored(p):
                paths.append(p)
        if paths:
            return paths

    out = _git(["log", f"--max-count={RECENT_COMMITS}", "--name-only",
                "--no-merges", "--pretty=format:"], root)
    if not out:
        return []
    seen, paths = set(), []
    for line in out.splitlines():
        p = _norm(line)
        if p and p not in seen and not _is_ignored(p):
            seen.add(p)
            paths.append(p)
    return paths


def _covers(scope, path):
    """Does a constraint scoped to `scope` cover `path`?

    Directory scopes cover everything beneath; a file scope matches itself.
    Guarded on the separator so 'hooks' does not also claim 'hooks_backup/'.
    """
    s = _norm(scope).rstrip("/")
    if not s:
        return False
    return path == s or path.startswith(s + "/")


def relevant_entries(entries, paths):
    """Entries whose scope covers at least one active path, in store order.

    Domain scopes ('global') are excluded on purpose: they are already in the
    stable block above, and repeating them here would spend the focus budget
    restating what the reader has just been told.
    """
    if not paths:
        return []
    hits = []
    for e in entries:
        scope = e.get("scope")
        if not scope or _norm(scope).lower() in ("global", "all", "*", ""):
            continue
        if any(_covers(scope, p) for p in paths):
            hits.append(e)
    return hits


def focus_lines(entries, root, paths=None):
    """The appended block, or [] when there is nothing worth appending.

    Returned as lines rather than text so the caller controls spacing and can
    keep the join identical to the rest of the summary.
    """
    paths = active_paths(root) if paths is None else paths
    if not paths:
        return []
    hits = relevant_entries(entries, paths)
    if not hits:
        return []

    shown = paths[:MAX_PATHS_SHOWN]
    more = len(paths) - len(shown)
    where = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")

    lines = ["", f"Rules covering what you are working on ({where}):"]
    for e in hits[:MAX_FOCUS_ENTRIES]:
        rule = (e.get("rule") or e.get("summary") or "").strip()
        marker = "!" if e.get("hardness") == "absolute" else "-"
        line = f"  {marker} [{e.get('id')}] {rule}"
        if e.get("enforced_by"):
            # A constraint that names its own check turns "go read the code"
            # into "run this" -- see con-009.
            line += f"  (enforced by {e['enforced_by']})"
        lines.append(line)
    if len(hits) > MAX_FOCUS_ENTRIES:
        lines.append(f"  ... and {len(hits) - MAX_FOCUS_ENTRIES} more scoped here")
    return lines
