"""Task-aware session-start injection, and constraints that name their own check.

The summary was compact but untargeted: every constraint injected at full length
regardless of what the session was about. dec-005 had already established the
right idea at the wrong moment -- scope_guard re-injects a scoped constraint when
you edit a covered file. This brings that signal forward to session start, where
git already knows which files are dirty.

The property these tests exist to defend is cache stability. The injected block
is deterministically ordered so an unchanged store injects byte-identical text
across sessions; focus varies with the working tree, so it must be APPENDED
after everything stable rather than interleaved by relevance. Interleaving would
invalidate the prompt cache on every file touched -- a bigger cost than the
irrelevance it fixes.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import code_drift  # noqa: E402
import work_focus  # noqa: E402
from server import handle_get_project_summary, handle_verify_quality  # noqa: E402


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, errors="replace")


def make_repo(root):
    git(["init", "-q", "-b", "main"], root)
    git(["config", "user.email", "t@example.com"], root)
    git(["config", "user.name", "Test"], root)
    return root


def commit(root, relpath, content, message):
    p = Path(root) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", message], root)


def con(cid, rule, scope, **kw):
    e = {"id": cid, "rule": rule, "reason": "R" * 80, "scope": scope,
         "hardness": "absolute", "status": "active", "tags": ["a"],
         "schema_version": 4, "verified_at": "2099-01-01T00:00:00+00:00"}
    e.update(kw)
    return e


def write_store(root, constraints):
    ctx = Path(root) / ".context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "constraints.json").write_text(json.dumps(constraints), encoding="utf-8")
    return ctx


# ---------------------------------------------------------------------------
# what the session is about
# ---------------------------------------------------------------------------
class TestActivePaths:
    def test_uncommitted_changes_win(self, tmp_path):
        """Dirty files are the strongest signal of intent: they are what the
        developer is holding right now."""
        make_repo(tmp_path)
        commit(tmp_path, "old.py", "x = 1\n", "old work")
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "live.py").write_text("y = 1\n", encoding="utf-8")
        paths = work_focus.active_paths(str(tmp_path))
        assert "hooks/live.py" in paths
        assert "old.py" not in paths

    def test_clean_tree_falls_back_to_recent_commits(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "recent.py", "x = 1\n", "just did this")
        assert "recent.py" in work_focus.active_paths(str(tmp_path))

    def test_no_repo_means_no_signal(self, tmp_path):
        assert work_focus.active_paths(str(tmp_path)) == []

    def test_renames_report_the_new_path(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "before.py", "x = 1\n", "add")
        git(["mv", "before.py", "after.py"], tmp_path)
        paths = work_focus.active_paths(str(tmp_path))
        assert "after.py" in paths


# ---------------------------------------------------------------------------
# which rules are relevant
# ---------------------------------------------------------------------------
class TestRelevantEntries:
    def test_directory_scope_covers_files_beneath(self):
        entries = [con("con-1", "ASCII only", "hooks")]
        assert work_focus.relevant_entries(entries, ["hooks/session_start.py"])

    def test_sibling_prefix_is_not_covered(self):
        """'hooks' must not claim 'hooks_backup/' — the same separator guard
        code_drift needs, for the same reason."""
        entries = [con("con-1", "ASCII only", "hooks")]
        assert work_focus.relevant_entries(entries, ["hooks_backup/x.py"]) == []

    def test_global_scopes_are_excluded(self):
        """They are already in the stable block above; repeating them here
        spends the focus budget restating what was just said."""
        entries = [con("con-1", "Always X", "global")]
        assert work_focus.relevant_entries(entries, ["anything.py"]) == []

    def test_unrelated_scope_is_excluded(self):
        entries = [con("con-1", "Mirror rule", "mirror.py")]
        assert work_focus.relevant_entries(entries, ["hooks/x.py"]) == []


# ---------------------------------------------------------------------------
# the appended block
# ---------------------------------------------------------------------------
class TestFocusLines:
    def test_names_the_rule_and_where_it_applies(self):
        lines = work_focus.focus_lines(
            [con("con-1", "Hooks print ASCII only", "hooks")],
            root=None, paths=["hooks/session_start.py"])
        text = "\n".join(lines)
        assert "hooks/session_start.py" in text
        assert "con-1" in text and "ASCII" in text

    def test_absolute_and_advisory_are_distinguishable(self):
        lines = work_focus.focus_lines(
            [con("con-1", "Hard rule", "a.py"),
             con("con-2", "Soft rule", "a.py", hardness="advisory")],
            root=None, paths=["a.py"])
        text = "\n".join(lines)
        assert "! [con-1]" in text and "- [con-2]" in text

    def test_mentions_the_enforcing_check_when_named(self):
        lines = work_focus.focus_lines(
            [con("con-1", "Budget rule", "server.py",
                 enforced_by="TestToolSchemaBudget")],
            root=None, paths=["server.py"])
        assert "enforced by TestToolSchemaBudget" in "\n".join(lines)

    def test_capped_so_focus_cannot_crowd_out_the_summary(self):
        entries = [con(f"con-{i}", f"Rule {i}", "a.py") for i in range(20)]
        lines = work_focus.focus_lines(entries, root=None, paths=["a.py"])
        rule_lines = [l for l in lines if l.strip().startswith(("!", "-"))]
        assert len(rule_lines) <= work_focus.MAX_FOCUS_ENTRIES
        assert "more scoped here" in "\n".join(lines)

    def test_silent_when_nothing_is_relevant(self):
        assert work_focus.focus_lines(
            [con("con-1", "Mirror rule", "mirror.py")],
            root=None, paths=["hooks/x.py"]) == []

    def test_silent_without_a_signal(self):
        assert work_focus.focus_lines(
            [con("con-1", "R", "a.py")], root=None, paths=[]) == []


# ---------------------------------------------------------------------------
# cache stability — the property this must not break
# ---------------------------------------------------------------------------
class TestCacheStability:
    def _summary(self, root):
        return handle_get_project_summary({"project_dir": str(root)})["summary"]

    def test_focus_is_appended_never_interleaved(self, tmp_path):
        """The stable block must remain a byte-identical PREFIX of the output
        once focus is added. If focus were ranked in among the constraints, the
        prompt cache would miss on every changed file."""
        make_repo(tmp_path)
        write_store(tmp_path, [con("con-1", "Hooks print ASCII only", "hooks")])
        # Last commit touches nothing the constraint covers, so the clean-tree
        # fallback finds no relevant path and the baseline carries no focus.
        commit(tmp_path, "unrelated.py", "u = 1\n", "unrelated work")
        clean = self._summary(tmp_path)
        assert "working on" not in clean, "baseline should have no focus block"

        # now touch a covered file -> focus appears
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "x.py").write_text("x = 2\n", encoding="utf-8")
        focused = self._summary(tmp_path)

        assert focused.startswith(clean), \
            "focus changed the stable prefix instead of appending after it"
        assert len(focused) > len(clean)
        assert "working on" in focused

    def test_unchanged_store_and_tree_is_byte_identical(self, tmp_path):
        make_repo(tmp_path)
        write_store(tmp_path, [con("con-1", "R", "hooks")])
        commit(tmp_path, "hooks/x.py", "x = 1\n", "add")
        assert self._summary(tmp_path) == self._summary(tmp_path)


# ---------------------------------------------------------------------------
# enforced_by
# ---------------------------------------------------------------------------
class TestEnforcedBy:
    def test_missing_test_file_is_flagged(self, tmp_path):
        make_repo(tmp_path)
        write_store(tmp_path, [con("con-1", "Budget rule", "server.py",
                                   enforced_by="tests/test_gone.py::TestX")])
        commit(tmp_path, "server.py", "x = 1\n", "add")
        out = handle_verify_quality({"project_dir": str(tmp_path)})
        kinds = [i["type"] for f in out["flagged"] for i in f["issues"]]
        assert "enforcement_missing" in kinds

    def test_present_test_file_is_not_flagged(self, tmp_path):
        make_repo(tmp_path)
        write_store(tmp_path, [con("con-1", "Budget rule", "server.py",
                                   enforced_by="tests/test_here.py::TestX")])
        commit(tmp_path, "tests/test_here.py", "def test_x(): pass\n", "add test")
        commit(tmp_path, "server.py", "x = 1\n", "add")
        out = handle_verify_quality({"project_dir": str(tmp_path)})
        kinds = [i["type"] for f in out["flagged"] for i in f["issues"]]
        assert "enforcement_missing" not in kinds

    def test_never_executes_the_target(self, tmp_path):
        """Stores travel — import_snapshot pulls from git, mirror pulls from a
        Worker. A field that ran shell commands on import would be a code
        execution path into an offline, inspectable memory store."""
        make_repo(tmp_path)
        canary = tmp_path / "canary.txt"
        write_store(tmp_path, [con("con-1", "R", "server.py",
                                   enforced_by=f"python -c \"open(r'{canary}','w').write('x')\"")])
        commit(tmp_path, "server.py", "x = 1\n", "add")
        handle_verify_quality({"project_dir": str(tmp_path)})
        assert not canary.exists(), "enforced_by was executed"

    def test_drift_detail_points_at_the_check(self, tmp_path):
        drift = {"scope": "server.py", "scope_exists": True,
                 "verified_at": "2020-01-01T00:00:00+00:00",
                 "commits_since_verified": 3, "last_change": "changed things",
                 "enforced_by": "TestToolSchemaBudget"}
        detail = code_drift.issues_for(drift)[0]["detail"]
        assert "Run TestToolSchemaBudget" in detail

    def test_drift_detail_without_a_check_says_read_the_code(self):
        drift = {"scope": "server.py", "scope_exists": True,
                 "verified_at": "2020-01-01T00:00:00+00:00",
                 "commits_since_verified": 3, "last_change": "changed",
                 "enforced_by": ""}
        assert "Re-read the code" in code_drift.issues_for(drift)[0]["detail"]
