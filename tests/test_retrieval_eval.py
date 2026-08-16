"""Regression pin for retrieval quality.

The rest of the suite proves the store is CORRECT. This one file is the only
place that asks whether it is USEFUL -- whether get_context returns the entry
that answers a question somebody would actually ask. A ranking change can leave
every other test green and still make the tool worse at its job.

Only the LEXICAL arm is pinned: it is bit-for-bit deterministic run to run and
needs no network, so it can fail honestly in CI. The embedding arm depends on a
local embedder and is measured by evals/run_retrieval_eval.py, not asserted here.

These run against the FROZEN corpus committed under evals/fixtures/corpus, so
they work on any clone and in CI. They used to read the real stores next to this
repo and skip when absent, which meant CI never defended the pin at all -- and
worse, the number moved whenever anybody recorded a decision. The skip below is
now a genuine "something is missing" guard rather than the normal case.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

EVAL_SCRIPT = REPO / "evals" / "run_retrieval_eval.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("_ck_retrieval_eval", EVAL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def harness():
    if not EVAL_SCRIPT.exists():
        pytest.skip("retrieval eval harness not present")
    return _load_harness()


@pytest.fixture(scope="module")
def cases(harness):
    all_cases = harness.load_golden()
    missing = sorted({
        c["store"] for c in all_cases
        if not os.path.isdir(os.path.join(harness.resolve_store_dir(c["store"]), ".context"))
    })
    if missing:
        pytest.skip("golden-set stores not present on this machine: %s" % ", ".join(missing))
    return all_cases


@pytest.fixture(scope="module")
def lexical_rows(harness, cases, tmp_path_factory):
    # A COPY, in a tmp dir. The eval must never write to a real .context, and
    # the harness's own default workdir is left alone so a warm embedding cache
    # is not disturbed by a test run.
    workdir = tmp_path_factory.mktemp("retrieval_eval")
    return harness.run_arm("lexical", cases, str(workdir), token_budget=4000)


class TestGoldenSetIntegrity:
    def test_golden_set_is_valid(self, harness, cases):
        """Every gold id resolves, history golds really are superseded, and
        negatives really have no answer. A golden set that references a renamed
        id measures nothing while reporting a clean number."""
        assert harness.validate(cases) == []

    def test_golden_set_shape(self, cases):
        by_type = {}
        for c in cases:
            by_type.setdefault(c["case_type"], []).append(c)
        assert len(cases) >= 40
        assert len(by_type.get("negative", [])) >= 8
        assert len(by_type.get("history", [])) >= 5
        assert len({c["store"] for c in cases}) >= 4

    def test_no_private_store_leaks_in(self, cases):
        """con-015-12da: this repo is public and a case quotes the entry it
        targets, so a case may only be drawn from a public-repo store."""
        allowed = {
            "context-keeper", "Clark", "Conductor", "cambium", "agentsync",
            "meristem", "xylem", "balatron",
            "fixtures/ck_history", "fixtures/xylem_history",
        }
        assert {c["store"] for c in cases} <= allowed


class TestLexicalRetrievalRegression:
    # Measured 2026-08-16 on the 44 positive cases, lexical arm, token_budget
    # 4000, include_related off, against the FROZEN corpus in
    # evals/fixtures/corpus (192 entries across 7 stores): recall@5 = 0.407.
    # Bit-for-bit reproducible across runs -- nothing in score_entry samples.
    #
    # Moved from 0.473 on a deliberate corpus refresh. TWO things changed in
    # that refresh, so which one moved the number was measured rather than
    # assumed:
    #
    #     corpus growth (a session's new entries)   0.473 -> 0.407   -0.066
    #     redaction of non-public project names     0.407 -> 0.407    0.000
    #
    # All of it is dilution: more entries to rank against, same ranker. The
    # redaction (build_corpus_fixture replaces private project NAMES with a
    # placeholder, keeping the surrounding prose) cost exactly nothing, which is
    # the answer that mattered -- it means the privacy gate is free rather than
    # paid for in retrieval quality.
    #
    # The first pin was 0.549 against LIVE stores, and it broke within a day
    # without a single line of ranking code changing: recording three new
    # context-keeper entries moved it to 0.473. That is the corpus growing, not
    # the ranker regressing, and a pin that cannot tell those apart is not a
    # regression test. Hence the frozen corpus -- the ranker is now the only
    # variable, and as a bonus this test no longer needs sibling repos that
    # exist on a dev machine and not in CI. Refresh deliberately with
    # evals/build_corpus_fixture.py and re-measure here in the same commit.
    #
    # Tolerance is +/- 0.03, about two cases' worth on this set: wide enough to
    # absorb a golden-set edit, tight enough that a real ranking regression
    # fails here.
    #
    # This test OWNS the number (con-009-6bdc). No constraint and no README
    # restates it -- if the value moves, it moves here, deliberately, with the
    # new measurement recorded above.
    POSITIVE_RECALL_AT_5 = 0.407
    TOLERANCE = 0.03

    def test_positive_recall_at_5_holds(self, harness, lexical_rows):
        positives = [r for r in lexical_rows if r["case_type"] == "positive"]
        agg = harness.aggregate(positives)
        actual = agg["recall@5"]
        assert abs(actual - self.POSITIVE_RECALL_AT_5) <= self.TOLERANCE, (
            "lexical recall@5 over %d positive cases is %.3f, pinned at %.3f "
            "+/- %.3f. If this is an intentional ranking change, re-run "
            "`python evals/run_retrieval_eval.py` and update the constant and "
            "its dated measurement in this file."
            % (len(positives), actual, self.POSITIVE_RECALL_AT_5, self.TOLERANCE))

    def test_lexical_arm_is_deterministic(self, harness, cases, tmp_path):
        """The pin is only meaningful if the number does not wander on its own."""
        a = harness.aggregate([r for r in harness.run_arm(
            "lexical", cases, str(tmp_path / "a"), 4000)
            if r["case_type"] == "positive"])
        b = harness.aggregate([r for r in harness.run_arm(
            "lexical", cases, str(tmp_path / "b"), 4000)
            if r["case_type"] == "positive"])
        assert a["recall@5"] == b["recall@5"]
        assert a["mrr"] == b["mrr"]

    def test_eval_does_not_write_to_real_stores(self, harness, cases, tmp_path):
        """The whole harness is worthless if measuring mutates what it measures
        -- and the semantic path really does write an embeddings.json next to
        the entries it embeds."""
        store = next(c["store"] for c in cases if not c["store"].startswith("fixtures/"))
        base = Path(harness.resolve_store_dir(store)) / ".context"
        before = {p.name: p.stat().st_mtime_ns for p in base.iterdir() if p.is_file()}
        harness.run_arm("lexical", cases, str(tmp_path / "ro"), 4000)
        after = {p.name: p.stat().st_mtime_ns for p in base.iterdir() if p.is_file()}
        assert after == before, "the eval modified a real store"


class TestHistoryIsCurrentlyUnreachable:
    """A KNOWN GAP, pinned so it cannot drift unnoticed in either direction.

    Measured 2026-08-05: all six history cases score recall@5 = 0.000 in the
    lexical arm (and in the embedding arm). Each asks explicitly for a prior
    state -- "what was the original sync design before the newest-wins merge" --
    and no superseded entry comes back in the top 5, usually not at all:
    score_entry demotes superseded by 15 points AND those entries are older, so
    recency compounds the demotion.

    Supersession is first-class at write time and read time as of dec-022-730e,
    and retrieval still cannot find the predecessor when asked for it directly.
    This test failing is GOOD NEWS -- it means someone fixed that. Update the
    measurement and turn it into a real floor when they do.
    """

    def test_history_recall_is_still_zero(self, harness, lexical_rows):
        history = [r for r in lexical_rows if r["case_type"] == "history"]
        assert history, "golden set lost its history cases"
        agg = harness.aggregate(history)
        assert agg["recall@5"] == 0.0, (
            "history recall@5 is now %.3f, not 0.000. If retrieval learned to "
            "surface superseded entries, this is the fix landing -- replace "
            "this assertion with a floor and record the new measurement."
            % agg["recall@5"])

    def test_superseded_entries_are_not_filtered_out_entirely(self, harness, cases, tmp_path):
        """The gap is RANKING, not exclusion -- worth distinguishing, because
        the two have completely different fixes. Deprecated entries really are
        filtered from get_context by design; superseded ones are not."""
        case = next(c for c in cases if c["case_type"] == "history")
        project_dir = harness.snapshot(case["store"], str(tmp_path))
        retrieved, _ = harness.run_case(
            case, project_dir, {"semantic": {"enabled": False}}, token_budget=200000)
        gold = case["expected"][0]
        assert gold in retrieved, (
            "superseded entry %s was not returned even with an effectively "
            "unlimited budget -- that would be exclusion, not demotion" % gold)
