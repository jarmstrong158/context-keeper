"""The one place that knows what a constraint's `scope` means.

con-011-76f8 requires every surface answering "does this scope cover this file"
to agree. It said so because they didn't: a substring test silently over-matches
(`hooks/` matches `webhooks/send.py`, `server.py` matches `test_server.py`), and
scope_guard injects each constraint at most once per session -- so a false
positive MARKS THAT CONSTRAINT DELIVERED and the file it actually governs never
receives it. An over-eager match causes a silent under-delivery.

The constraint was written, the hook was fixed, and the invariant still came out
false, because "must agree" was prose and each surface kept its own copy:

    hooks/scope_guard.py  _scope_covers   whole components  (correct)
    work_focus.py         _covers         startswith(s+"/") (disagreed on 2 of 10 cases)
    server.py             score_entry     raw substring     (the original bug, in the ranking path)
    server.py             _scope_to_paths glob projection   (its own normalization)

Measured before this module existed: `server.py` vs `pkg/server.py` was covered
by the hook and not by work_focus, `hooks/` vs a bare `hooks` path the reverse,
and querying `scope="hooks/"` handed a `webhooks/`-scoped entry the identical
+20 ranking boost as the real one. Prose cannot hold an invariant that four
functions each implement separately. One implementation can.

Imports nothing. That is load-bearing, not minimalism: `hooks/scope_guard.py`
runs on PreToolUse before every Edit and Write, and con-010-acde caps what it is
allowed to pull in. A dependency added here is paid on the critical path of
every edit in every project, forever.

SEMANTICS (the hook's, which tests/test_server.py::TestScopeCovers pins):
  * A scope whose last component contains a dot is a FILE scope and matches the
    tail of the path exactly -- `server.py` covers `pkg/server.py`, never
    `test_server.py`.
  * Anything else is a DIRECTORY scope: its components must appear consecutively
    as whole components, with at least one component after them, so a scope
    never matches the directory entry itself.
  * `global` / `all` / `*` / empty are DOMAIN scopes. They cover nothing by path
    -- not everything. A domain-scoped rule is delivered by the session summary
    and has no path to check against, which is exactly why verify_quality flags
    it (dec-021-b607).

Paths may be absolute or repo-relative and may use either separator; callers
were doing both, and normalizing here is what lets them share one answer.
"""

# Scopes that name a domain rather than a path. Matched case-insensitively.
DOMAIN_SCOPES = frozenset({"global", "all", "*", ""})


def normalize(text):
    """Lowercased, forward-slashed, surrounding-slash-stripped."""
    return str(text or "").replace("\\", "/").strip().strip("/").lower()


def is_domain(scope):
    """True when `scope` names a domain ('global') rather than a path."""
    return normalize(scope) in DOMAIN_SCOPES


def components(scope):
    """A scope's whole path components, or () for a domain scope.

    `.` segments are dropped; `..` is preserved so callers that must reject it
    (see _scope_to_paths, con-012-e6c4) can still see it.
    """
    s = normalize(scope)
    if s in DOMAIN_SCOPES:
        return ()
    return tuple(p for p in s.split("/") if p and p != ".")


def is_file_scope(scope):
    """A trailing component containing a dot reads as a file, not a directory."""
    parts = components(scope)
    return bool(parts) and "." in parts[-1]


def covers(scope, path):
    """Does `scope` cover `path`? THE coverage decision. Components, never substrings.

    This is the question scope_guard asks before an edit, work_focus asks about
    the working tree, and the .claude/rules projection answers in glob form. They
    must not be able to disagree, so they all end up here.
    """
    s_parts = components(scope)
    if not s_parts:
        return False
    parts = [p for p in normalize(path).split("/") if p]
    n = len(s_parts)
    if is_file_scope(scope):
        return parts[-n:] == list(s_parts)
    # Directory scope: the run must be followed by at least one component, so a
    # scope never matches the directory entry itself.
    for i in range(len(parts) - n):
        if parts[i:i + n] == list(s_parts):
            return True
    return False


def overlap(a, b):
    """Shared leading components / longest scope, or None if either is a domain.

    A different question from covers(): "are these two scopes about the same
    area", used for ranking and for the write-time supersession advisory. Front-
    anchored because a scope is a repo-relative path -- `hooks/` and
    `hooks/scope_guard.py` describe the same area (0.5), `hooks/` and `webhooks/`
    share no component at all (0.0).

    None means "no signal", which is not 0.0 ("a signal, and it says unrelated").
    A domain-scoped entry must not drag a mean down as though it were unrelated.
    """
    ca, cb = components(a), components(b)
    if not ca or not cb:
        return None
    shared = 0
    for x, y in zip(ca, cb):
        if x != y:
            break
        shared += 1
    return shared / max(len(ca), len(cb))


# ============================================================
# Glob projection: the same scope, expressed for Claude Code's own
# .claude/rules/ frontmatter. Lives here rather than in server.py because
# it is scope reasoning -- and because the quality checks' scope
# suggestion needs it, which would otherwise import server (con-010).
# ============================================================


# Characters that make a scope unprojectable, for two different reasons with
# one shared consequence: the rule silently never fires.
#
#   Glob metacharacters — Claude Code reads `[` as the start of a bracket
#   expression, and a pattern whose bracket never closes matches nothing.
#
#   A double quote (or a control character) — each pattern is emitted as a
#   double-quoted YAML scalar, so an embedded quote closes it early and the
#   whole frontmatter block stops parsing. The harness then loads NO rule
#   from that file, not even the patterns that were fine.
#
# Both are skipped and REPORTED rather than emitted, because a rule file that
# never fires is indistinguishable from coverage.
_GLOB_META = set("[]{}*?")
_YAML_UNSAFE = set('"')


def _scope_to_paths(scope):
    """Glob patterns for a constraint scope, or [] if it can't be expressed.

    Directory scopes match everything beneath them; file scopes match the
    file. Each gets a repo-root-anchored form and a `**/`-prefixed form, so
    a scope recorded as `hooks/` still fires in a nested checkout.
    """
    s = str(scope or "").replace("\\", "/").strip()
    if not s or is_domain(s):
        return []
    if any(ch in _GLOB_META or ch in _YAML_UNSAFE or ord(ch) < 32 for ch in s):
        return []
    # A `..` component cannot become a meaningful glob -- the harness matches
    # patterns against repo-relative paths, which never contain one -- and it
    # would let a scope reach outside the project. Caught here because this is
    # the single chokepoint every scope passes through on its way to a pattern.
    if ".." in s.split("/"):
        return []
    s = s.strip("/")
    if not s:
        return []
    # `is_file_scope` rather than os.path.basename: same question ("does the
    # last component carry a dot"), and it keeps this module import-free, which
    # is what lets the PreToolUse hook load it (con-010-acde).
    if is_file_scope(s):
        return [s, f"**/{s}"]
    return [f"{s}/**/*", f"**/{s}/**/*"]


to_glob_patterns = _scope_to_paths
