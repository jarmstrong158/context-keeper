"""Verify entries against the artifact, and notice which ones nobody wants.

Two additions, one thesis: an entry's health is a property of the thing it
describes and of whether anyone reads it — not of how long ago someone last
clicked verify.

The motivating incident is real. An org-scope memory told agents not to trust a
conflict result that agentsync had since been fixed to stop producing. It was
the most-recalled item in the store and nothing flagged it, because by date it
looked healthy. `test_drift_catches_the_incident_that_motivated_this` is that
failure, reconstructed.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import code_drift  # noqa: E402
import usage  # noqa: E402
from server import handle_verify_quality  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, errors="replace")


def make_repo(root):
    """A real git repo — code_drift shells out to git, so a fake would only
    prove the fake works."""
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


def entry(eid, scope, verified_at, **kw):
    e = {"id": eid, "scope": scope, "verified_at": verified_at,
         "status": "active", "tags": ["t"]}
    e.update(kw)
    return e


PAST = "2020-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# scope classification
# ---------------------------------------------------------------------------
class TestIsPathScope:
    @pytest.mark.parametrize("scope", ["src/auth.py", "install/x.py", "src/api/",
                                       "a\\b.py", "server.py"])
    def test_paths_are_paths(self, scope):
        assert code_drift.is_path_scope(scope)

    @pytest.mark.parametrize("scope", ["global", "", None, "auth", "*", "repo", 42])
    def test_domains_are_not_paths(self, scope):
        """A domain scope must not be treated as a missing file and reported as
        orphaned — that would flag every global constraint in the store."""
        assert not code_drift.is_path_scope(scope)


# ---------------------------------------------------------------------------
# drift detection
# ---------------------------------------------------------------------------
class TestDriftScan:
    def test_drift_catches_the_incident_that_motivated_this(self, tmp_path):
        """An entry verified long ago, describing code that has since changed.

        By calendar it is one stale entry among many; by artifact it is wrong.
        """
        make_repo(tmp_path)
        commit(tmp_path, "agentsync_server.py", "def check_conflicts(): pass\n", "initial")
        commit(tmp_path, "agentsync_server.py",
               "def check_conflicts():\n    return 'unknown'\n",
               "Downgrade an unfilterable conflict to unknown")

        report = code_drift.scan(
            [entry("con-001", "agentsync_server.py", PAST)], str(tmp_path))
        d = report["con-001"]
        assert d["commits_since_verified"] == 2
        assert d["scope_exists"] is True
        assert "unknown" in d["last_change"]

        issues = code_drift.issues_for(d)
        assert [i["type"] for i in issues] == ["code_drift"]
        assert "2 commits" in issues[0]["detail"]

    def test_untouched_code_does_not_drift(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "quiet.py", "x = 1\n", "add quiet")
        commit(tmp_path, "noisy.py", "y = 1\n", "add noisy")
        # verified AFTER both commits
        now = "2099-01-01T00:00:00+00:00"
        report = code_drift.scan([entry("dec-1", "quiet.py", now)], str(tmp_path))
        assert report["dec-1"]["commits_since_verified"] == 0
        assert code_drift.issues_for(report["dec-1"]) == []

    def test_directory_scope_covers_files_beneath_it(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "src/api/routes.py", "r = 1\n", "add routes")
        report = code_drift.scan([entry("dec-1", "src/api", PAST)], str(tmp_path))
        assert report["dec-1"]["commits_since_verified"] == 1

    def test_directory_scope_does_not_swallow_a_sibling_prefix(self, tmp_path):
        """'src/api' must not match 'src/api_v2/...' — prefix matching without a
        separator would silently over-report drift on unrelated code."""
        make_repo(tmp_path)
        commit(tmp_path, "src/api_v2/routes.py", "r = 1\n", "add v2")
        report = code_drift.scan([entry("dec-1", "src/api", PAST)], str(tmp_path))
        assert report["dec-1"]["commits_since_verified"] == 0

    def test_deleted_scope_is_orphaned_not_drifted(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "gone.py", "x = 1\n", "add")
        git(["rm", "-q", "gone.py"], tmp_path)
        git(["commit", "-q", "-m", "remove gone.py"], tmp_path)
        report = code_drift.scan([entry("dec-1", "gone.py", PAST)], str(tmp_path))
        assert report["dec-1"]["scope_exists"] is False
        issues = code_drift.issues_for(report["dec-1"])
        assert [i["type"] for i in issues] == ["orphaned_scope"]

    def test_domain_scoped_entries_are_absent_not_clean(self, tmp_path):
        """Absent from the report, not reported with zero drift: 'we cannot
        tell' and 'nothing changed' are different answers."""
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "add")
        report = code_drift.scan([entry("con-1", "global", PAST)], str(tmp_path))
        assert report == {}

    def test_no_repo_returns_none_not_empty(self, tmp_path):
        """None (could not look) must stay distinguishable from {} (looked,
        found nothing), or a non-repo store would read as fully verified."""
        assert code_drift.scan([entry("d", "a.py", PAST)], str(tmp_path)) is None


# ---------------------------------------------------------------------------
# usage tracking
# ---------------------------------------------------------------------------
class TestUsage:
    def test_counts_injections_and_retrievals_separately(self, tmp_path):
        usage.record(str(tmp_path), ["dec-1"], "injected")
        usage.record(str(tmp_path), ["dec-1"], "injected")
        usage.record(str(tmp_path), ["dec-1"], "retrieved")
        s = usage.stats_for(str(tmp_path), "dec-1")
        assert s == {"injected": 2, "retrieved": 1}

    def test_unknown_entry_reads_as_zero(self, tmp_path):
        assert usage.stats_for(str(tmp_path), "nope") == {"injected": 0, "retrieved": 0}

    def test_flags_carried_but_never_sought(self, tmp_path):
        for _ in range(usage.UNUSED_INJECTION_THRESHOLD):
            usage.record(str(tmp_path), ["dec-1"], "injected")
        issues = usage.issues_for(usage.stats_for(str(tmp_path), "dec-1"))
        assert [i["type"] for i in issues] == ["unused"]

    def test_one_retrieval_clears_the_flag(self, tmp_path):
        for _ in range(usage.UNUSED_INJECTION_THRESHOLD * 2):
            usage.record(str(tmp_path), ["dec-1"], "injected")
        usage.record(str(tmp_path), ["dec-1"], "retrieved")
        assert usage.issues_for(usage.stats_for(str(tmp_path), "dec-1")) == []

    def test_new_entries_are_not_flagged(self, tmp_path):
        """Below the threshold an entry is new, not unwanted."""
        usage.record(str(tmp_path), ["dec-1"], "injected")
        assert usage.issues_for(usage.stats_for(str(tmp_path), "dec-1")) == []

    def test_corrupt_sidecar_degrades_to_no_stats(self, tmp_path):
        (tmp_path / usage.USAGE_FILENAME).write_text("{not json", encoding="utf-8")
        assert usage.read(str(tmp_path)) == {}
        usage.record(str(tmp_path), ["dec-1"], "injected")  # must not raise

    def test_record_never_raises_on_a_bad_dir(self):
        usage.record("/no/such/dir/anywhere", ["dec-1"], "injected")

    def test_unknown_kind_is_ignored(self, tmp_path):
        usage.record(str(tmp_path), ["dec-1"], "bogus")
        assert usage.stats_for(str(tmp_path), "dec-1") == {"injected": 0, "retrieved": 0}


# ---------------------------------------------------------------------------
# wiring into verify_quality
# ---------------------------------------------------------------------------
class TestVerifyQualityIntegration:
    def _store(self, root, scope, verified_at):
        ctx = Path(root) / ".context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "constraints.json").write_text(json.dumps([{
            "id": "con-001", "rule": "R" * 40, "reason": "R" * 120,
            "scope": scope, "verified_at": verified_at, "status": "active",
            "tags": ["a"], "schema_version": 4,
        }]), encoding="utf-8")
        return ctx

    def test_drifted_entry_is_flagged(self, tmp_path):
        make_repo(tmp_path)
        self._store(tmp_path, "watched.py", PAST)
        commit(tmp_path, "watched.py", "x = 2\n", "change the watched file")
        out = handle_verify_quality({"project_dir": str(tmp_path)})
        assert out["drift_checked"] is True
        kinds = [i["type"] for f in out["flagged"] for i in f["issues"]]
        assert "code_drift" in kinds

    def test_reports_when_drift_could_not_be_checked(self, tmp_path):
        """No repo: the caller must be able to tell that nothing was verified,
        rather than reading a clean scan as a clean bill of health."""
        self._store(tmp_path, "watched.py", PAST)
        out = handle_verify_quality({"project_dir": str(tmp_path)})
        assert out["drift_checked"] is False

    def test_drift_check_can_be_disabled(self, tmp_path):
        make_repo(tmp_path)
        self._store(tmp_path, "watched.py", PAST)
        commit(tmp_path, "watched.py", "x = 2\n", "change it")
        out = handle_verify_quality({"project_dir": str(tmp_path), "check_drift": False})
        assert out["drift_checked"] is False
        kinds = [i["type"] for f in out["flagged"] for i in f["issues"]]
        assert "code_drift" not in kinds


# ---------------------------------------------------------------------------
# passive capture candidates
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import commit_capture_reminder as ccr  # noqa: E402


class TestCommitCapture:
    """The hook reads the commit message and only speaks when there is
    rationale to capture. Firing identically on 'fix typo' and on a real design
    change is what trains a reader to skip the reminder entirely."""

    def test_extracts_a_double_quoted_message(self):
        assert ccr.extract_message('git commit -m "use JSON instead of YAML"') \
            == "use JSON instead of YAML"

    def test_extracts_a_single_quoted_message(self):
        assert ccr.extract_message("git commit -m 'never log secrets'") == "never log secrets"

    def test_extracts_a_heredoc_body(self):
        cmd = "git commit -q -F - <<'MSG'\nSubject line\n\nbecause it broke\nMSG"
        assert "because it broke" in ccr.extract_message(cmd)

    def test_unreadable_message_falls_back_to_generic(self):
        ctx = ccr.build_context("git commit -F notes.txt")
        assert ctx and "Commit detected." in ctx

    @pytest.mark.parametrize("msg,kind", [
        ("Use git push as CAS instead of a lock server", "decision"),
        ("Never write secrets to the backup directory", "constraint"),
        ("Turns out panphon fails to load under cp1252", "constraint"),
    ])
    def test_names_the_kind_the_phrasing_implies(self, msg, kind):
        ctx = ccr.build_context('git commit -m "%s"' % msg)
        assert ctx is not None and kind in ctx

    def test_quotes_the_phrase_it_matched(self):
        ctx = ccr.build_context('git commit -m "Use JSON instead of YAML for the store"')
        assert '"instead of"' in ctx

    @pytest.mark.parametrize("msg", ["wip", "fix typo", "lint", "bump deps",
                                     "Merge branch 'main'"])
    def test_silent_on_trivial_commits(self, msg):
        assert ccr.build_context('git commit -m "%s"' % msg) is None

    def test_silent_on_a_plain_descriptive_commit(self):
        """No rationale markers: the diff already says what changed, and a
        reminder here is pure noise."""
        assert ccr.build_context('git commit -m "Add the parser module and its tests"') is None

    def test_ignores_non_commit_commands(self, capsys):
        import io as _io
        sys.stdin = _io.StringIO(json.dumps({"tool_input": {"command": "git status"}}))
        try:
            ccr.main()
        finally:
            sys.stdin = sys.__stdin__
        assert capsys.readouterr().out == ""

    def test_output_is_ascii_only(self):
        """con-001: Windows hook stdout is cp1252 and non-ASCII raises."""
        ctx = ccr.build_context('git commit -m "Chose git-as-CAS instead of a lock server"')
        ctx.encode("ascii")


class TestPerEntryCutoff:
    """The git window is bounded by the oldest entry in the store, so every
    commit must be re-filtered against each entry's own verification time.

    Without that, verifying an entry does nothing: it keeps reporting the
    oldest entry's drift however recently it was checked. That bug shipped and
    was caught by refreshing a real entry and watching the flag refuse to clear.
    """

    def test_verifying_an_entry_clears_its_drift(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "watched.py", "x = 1\n", "first")
        old = entry("dec-old", "other.py", PAST)  # holds the window wide open
        commit(tmp_path, "other.py", "y = 1\n", "touch other")
        fresh = entry("dec-fresh", "watched.py", "2099-01-01T00:00:00+00:00")

        report = code_drift.scan([old, fresh], str(tmp_path))
        assert report["dec-old"]["commits_since_verified"] > 0
        assert report["dec-fresh"]["commits_since_verified"] == 0, \
            "a freshly verified entry inherited the oldest entry's window"

    def test_each_entry_counts_only_commits_after_its_own_check(self, tmp_path):
        import time as _t
        from datetime import datetime, timezone
        make_repo(tmp_path)
        commit(tmp_path, "shared.py", "a = 1\n", "one")
        _t.sleep(1.1)  # git timestamps are whole seconds
        midpoint = datetime.now(timezone.utc).isoformat()
        _t.sleep(1.1)
        commit(tmp_path, "shared.py", "a = 2\n", "two")
        commit(tmp_path, "shared.py", "a = 3\n", "three")

        report = code_drift.scan([
            entry("dec-all", "shared.py", PAST),
            entry("dec-mid", "shared.py", midpoint),
        ], str(tmp_path))
        assert report["dec-all"]["commits_since_verified"] == 3
        assert report["dec-mid"]["commits_since_verified"] == 2
        assert report["dec-mid"]["last_change"] == "three"


class TestVerifyAgainstSha:
    """A timestamp is a soft boundary. Clock skew between machines, a rebase,
    or an amended commit all move it — and a store that syncs between a desktop
    and a phone has two clocks. A SHA names an exact point in history.

    The fallback matters as much as the feature: an entry recorded before SHA
    stamping, or one whose commit has been rebased away, must degrade to the
    timestamp and SAY so rather than silently reporting a different basis.
    """

    def _sha(self, root):
        return git(["rev-parse", "HEAD"], root).stdout.strip()

    def test_counts_from_the_sha_not_the_clock(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        sha = self._sha(tmp_path)
        commit(tmp_path, "a.py", "x = 2\n", "two")
        commit(tmp_path, "a.py", "x = 3\n", "three")

        # verified_at deliberately lies (far in the past). The SHA must win.
        e = entry("dec-1", "a.py", PAST, verified_sha=sha)
        d = code_drift.scan([e], str(tmp_path))["dec-1"]
        assert d["verified_against"] == "sha"
        assert d["commits_since_verified"] == 2, "timestamp leaked into the count"
        assert d["last_change"] == "three"

    def test_a_future_timestamp_cannot_hide_real_drift(self, tmp_path):
        """The inverse: a clock running ahead would zero the count under the
        timestamp path. The SHA is immune."""
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        sha = self._sha(tmp_path)
        commit(tmp_path, "a.py", "x = 2\n", "two")
        e = entry("dec-1", "a.py", "2099-01-01T00:00:00+00:00", verified_sha=sha)
        d = code_drift.scan([e], str(tmp_path))["dec-1"]
        assert d["commits_since_verified"] == 1

    def test_verifying_at_head_clears_drift(self, tmp_path):
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        commit(tmp_path, "a.py", "x = 2\n", "two")
        e = entry("dec-1", "a.py", PAST, verified_sha=self._sha(tmp_path))
        d = code_drift.scan([e], str(tmp_path))["dec-1"]
        assert d["commits_since_verified"] == 0
        assert code_drift.issues_for(d) == []

    def test_unknown_sha_falls_back_to_the_timestamp(self, tmp_path):
        """A rebase or amend can erase the commit an entry was verified at.
        That must degrade, not crash or silently report zero."""
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        e = entry("dec-1", "a.py", PAST, verified_sha="0" * 40)
        d = code_drift.scan([e], str(tmp_path))["dec-1"]
        assert d["verified_against"] == "timestamp"
        assert d["commits_since_verified"] == 1

    def test_entries_without_a_sha_still_work(self, tmp_path):
        """Everything recorded before this feature has no verified_sha."""
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        d = code_drift.scan([entry("dec-1", "a.py", PAST)], str(tmp_path))["dec-1"]
        assert d["verified_against"] == "timestamp"
        assert d["commits_since_verified"] == 1

    def test_head_sha_is_empty_outside_a_repo(self, tmp_path):
        assert code_drift.head_sha(str(tmp_path)) == ""

    def test_recording_stamps_the_current_head(self, tmp_path):
        """The write paths stamp it server-side, so it is an observation about
        the machine doing the write rather than something a caller asserts."""
        from server import handle_record_entry
        make_repo(tmp_path)
        commit(tmp_path, "a.py", "x = 1\n", "one")
        (tmp_path / ".context").mkdir(exist_ok=True)
        r = handle_record_entry({
            "project_dir": str(tmp_path), "kind": "constraint",
            "rule": "R" * 40, "reason": "R" * 80, "scope": "a.py",
        })
        assert r["entry"]["verified_sha"] == self._sha(tmp_path)
