"""Verify entries against the artifact they describe, not against the calendar.

`prune_stale` ages entries by wall-clock: anything unverified for
`stale_threshold_days` is flagged. That answers "how long since someone looked",
which is not the question that matters. A decision about code nobody has touched
in a year is still true; a decision about a function rewritten yesterday is
already wrong, and by date it looks fresher.

This module asks the other question. An entry's `scope` usually names a file or
module. If commits have landed on that path since the entry was last verified,
the ground under the decision moved and it deserves a second look — regardless of
how recent `verified_at` is.

The failure this exists to catch is real and recent: an org-scope memory told
agents not to trust a conflict result that agentsync had since been fixed to stop
producing. It was the most-recalled item in the store -- 9 recalls -- steering
every agent that read it around a bug that no longer existed. Nothing flagged it,
because by date it looked healthy.

Stdlib plus the `git` binary, consistent with the zero-dependency posture. Every
failure path degrades to "no drift information" rather than raising: a store on a
machine without git, or outside a repo, must keep working exactly as before.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

# Scope values that name a policy domain rather than a path on disk.
NON_PATH_SCOPES = {"global", "all", "*", "", "repo", "project"}

# Cap on commits parsed. A store whose entries predate thousands of commits
# should degrade to "lots changed" rather than spend a second in git.
MAX_COMMITS = 4000


def _git(args, cwd, timeout=15):
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def repo_root(base_dir):
    """The work tree containing `base_dir`, or None if there isn't one."""
    out = _git(["rev-parse", "--show-toplevel"], base_dir)
    return out.strip() if out and out.strip() else None


def is_path_scope(scope):
    """True when `scope` looks like a file or directory rather than a domain."""
    if not scope or not isinstance(scope, str):
        return False
    s = scope.strip()
    if s.lower() in NON_PATH_SCOPES:
        return False
    # A path has a separator or a file extension. "auth" alone is a domain;
    # "src/auth.py" and "src/auth/" are paths.
    return "/" in s or "\\" in s or ("." in os.path.basename(s) and not s.endswith("."))


def _entry_time(entry):
    return entry.get("verified_at") or entry.get("updated_at") or entry.get("created_at") or ""


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _norm(path):
    return str(path).replace("\\", "/").strip().lstrip("./").rstrip("/")


def changed_paths_since(root, since_iso):
    """{normalised path: [(unix_ts, subject), ...]} for commits after `since_iso`.

    One `git log` for the whole store rather than one per entry: a store with 40
    scoped entries would otherwise pay 40 process spawns per quality scan. The
    window is bounded by the OLDEST entry, so each commit carries its timestamp
    and callers filter down to their own entry's verification time — without
    that, every entry inherits the oldest entry's window and a freshly verified
    entry keeps reporting drift it has already been checked against.
    """
    out = _git([
        "log", f"--since={since_iso}", "--name-only", "--no-merges",
        f"--max-count={MAX_COMMITS}", "--pretty=format:\x01%ct\x02%s",
    ], root, timeout=30)
    if out is None:
        return None

    changes = {}
    ts, subject = 0, ""
    for line in out.splitlines():
        if line.startswith("\x01"):
            head = line[1:]
            raw_ts, _, subject = head.partition("\x02")
            try:
                ts = int(raw_ts)
            except ValueError:
                ts = 0
            subject = subject.strip()
            continue
        p = _norm(line)
        if not p:
            continue
        changes.setdefault(p, []).append((ts, subject))
    return changes


def _touches(scope_norm, changed_path):
    """Does a commit touching `changed_path` fall under `scope_norm`?

    Directory scopes cover everything beneath them; a file scope matches itself.
    """
    if changed_path == scope_norm:
        return True
    return changed_path.startswith(scope_norm + "/")


def scan(entries, base_dir, root=None):
    """Drift report for entries whose scope names a path.

    Returns {entry_id: {...}} with `commits_since_verified`, `last_change`, and
    `scope_exists`. Entries without a path scope are absent from the result:
    "no information" and "no drift" are different answers and must not be
    collapsed, or an unscoped entry would read as verified.

    Returns None when there is no repo or git is unavailable.
    """
    root = root or repo_root(base_dir)
    if not root:
        return None

    scoped = []
    for e in entries:
        scope = e.get("scope")
        if not is_path_scope(scope):
            continue
        t = _parse_iso(_entry_time(e))
        if t is None:
            continue
        scoped.append((e, _norm(scope), t))
    if not scoped:
        return {}

    oldest = min(t for _, _, t in scoped)
    changed = changed_paths_since(root, oldest.astimezone(timezone.utc).isoformat())
    if changed is None:
        return None

    report = {}
    for e, scope_norm, t in scoped:
        # The git window is bounded by the OLDEST entry in the store, so every
        # commit must be re-filtered against THIS entry's verification time.
        # Skipping that step makes verifying an entry do nothing: it keeps
        # reporting the oldest entry's drift no matter how recently it was
        # checked, which defeats the whole point of the flag.
        cutoff = t.timestamp()
        commits, latest, latest_ts = 0, "", -1
        for p, events in changed.items():
            if not _touches(scope_norm, p):
                continue
            for ts, subject in events:
                if ts <= cutoff:
                    continue
                commits += 1
                if ts > latest_ts:
                    latest_ts, latest = ts, subject
        abs_scope = os.path.join(root, scope_norm.replace("/", os.sep))
        report[e.get("id")] = {
            "scope": scope_norm,
            "scope_exists": os.path.exists(abs_scope),
            "verified_at": _entry_time(e),
            "commits_since_verified": commits,
            "last_change": latest,
        }
    return report


def issues_for(drift):
    """Turn one entry's drift record into verify_quality issue dicts."""
    if not drift:
        return []
    out = []
    if not drift["scope_exists"]:
        out.append({
            "type": "orphaned_scope",
            "detail": (
                f"scope '{drift['scope']}' no longer exists in the repo. The entry "
                "describes something that has been moved, renamed, or deleted — "
                "update the scope or deprecate the entry."
            ),
        })
    elif drift["commits_since_verified"] > 0:
        n = drift["commits_since_verified"]
        out.append({
            "type": "code_drift",
            "detail": (
                f"{n} commit{'s' if n != 1 else ''} touched '{drift['scope']}' since "
                f"this was last verified ({drift['verified_at'][:10]}). Most recent: "
                f"\"{drift['last_change'][:90]}\". Re-read the code and either "
                "update_entry (which refreshes verified_at) or deprecate it."
            ),
        })
    return out
