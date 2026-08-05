"""Tests for scripts/survey_supersessions.py.

The load-bearing property is the negative one: the survey WRITES NOTHING. It
exists to propose links a human then decides, so a test that only checked its
output would miss the failure that actually matters -- a backfill tool that
quietly edits 8 project stores on a heuristic.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import (  # noqa: E402
    handle_deprecate_entry,
    handle_record_constraint,
    handle_record_decision,
)


@pytest.fixture(scope="module")
def survey():
    path = Path(__file__).parent.parent / "scripts" / "survey_supersessions.py"
    spec = importlib.util.spec_from_file_location("_survey_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LONG = "x" * 70


def _project(root: Path, name: str) -> Path:
    d = root / name
    ctx = d / ".context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "config.json").write_text(
        json.dumps({"project_name": name}), encoding="utf-8")
    return d


def _decision(project: Path, summary, **kw):
    params = {"project_dir": str(project), "summary": summary,
              "problem": _LONG, "why_chosen": _LONG}
    params.update(kw)
    return handle_record_decision(params)


def _constraint(project: Path, rule, **kw):
    params = {"project_dir": str(project), "rule": rule, "reason": _LONG}
    params.update(kw)
    return handle_record_constraint(params)


class TestSurveyWritesNothing:
    def test_stores_are_byte_identical_afterwards(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        _decision(p, "Sync is additive-only", tags=["mirror"])
        _decision(p, "Sync was changed to newest-wins", tags=["mirror"])
        store = p / ".context" / "decisions.json"
        before = store.read_bytes()

        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        assert proposals  # it did find something to propose
        assert store.read_bytes() == before

    def test_main_writes_only_the_report(self, survey, tmp_path, capsys):
        p = _project(tmp_path, "proj")
        _decision(p, "The original plan", tags=["plan"])
        _decision(p, "The plan, previously wrong, redone", tags=["plan"])
        store = p / ".context" / "decisions.json"
        before = store.read_bytes()

        out = tmp_path / "out" / "proposal.md"
        rc = survey.main(["--root", str(tmp_path), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert store.read_bytes() == before
        assert "NOTHING WAS WRITTEN" in capsys.readouterr().out

    def test_report_is_ascii(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        _decision(p, "A plan that was different before", tags=["plan"])
        _decision(p, "A plan", tags=["plan"])
        out = tmp_path / "p.md"
        survey.main(["--root", str(tmp_path), "--out", str(out)])
        out.read_text(encoding="utf-8").encode("ascii")  # raises if it is not


class TestSurveySignals:
    def test_pairs_older_to_newer(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        old = _decision(p, "Store data in flat files", tags=["storage"])
        new = _decision(p, "Store data in JSON, replacing flat files", tags=["storage"])
        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        hit = next(x for x in proposals if x["newer_id"] == new["id"])
        assert hit["older_id"] == old["id"]
        assert hit["both_signals"] is True
        assert any(m["marker"] == "replaced" for m in hit["evidence"]["markers"])

    def test_marker_words_are_word_anchored(self, survey, tmp_path):
        """A substring search for 'was' also fires on 'wasted'."""
        p = _project(tmp_path, "proj")
        e = _decision(p, "A plan", tags=["plan"],
                      problem="Effort was wasted on an irreplaceable component. " + _LONG)
        markers = {m for (m, _f, _s) in survey._markers_in(
            {"summary": "A plan", "problem": "Effort wasted on irreplaceable parts."})}
        assert markers == set()
        # ...but the real "was" in the recorded entry IS a marker.
        assert any(m == "was" for (m, _f, _s) in survey._markers_in(e["entry"]))

    def test_already_linked_pairs_are_not_proposed(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        old = _decision(p, "Original approach here", tags=["arch"])
        new = _decision(p, "Replacement approach here", tags=["arch"],
                        supersedes=[old["id"]])
        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        assert not any(x["older_id"] == old["id"] and x["newer_id"] == new["id"]
                       for x in proposals)

    def test_deprecated_entries_are_not_proposed(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        old = _decision(p, "A wrong decision", tags=["arch"])
        handle_deprecate_entry({"project_dir": str(p), "id": old["id"],
                                "reason": "It was wrong from the start."})
        _decision(p, "A right decision", tags=["arch"])
        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        assert not any(old["id"] in (x["older_id"], x["newer_id"]) for x in proposals)

    def test_unpaired_markers_are_reported_not_dropped(self, survey, tmp_path):
        """A marker with no sibling is not a proposal, but a silent drop would
        read as 'nothing there'."""
        p = _project(tmp_path, "proj")
        lone = _decision(p, "A lone entry that was previously configured differently",
                         tags=["solo"])
        proposals, unpaired = survey.survey_store(
            str(p / ".context"), 0.5, project="proj")
        assert not proposals
        assert [u["id"] for u in unpaired] == [lone["id"]]

    def test_scope_overlap_uses_whole_components(self, survey, tmp_path):
        """con-011-76f8: `hooks/` and `webhooks/` are not the same area."""
        p = _project(tmp_path, "proj")
        _constraint(p, "A rule about hooks", scope="hooks/")
        _constraint(p, "A rule about webhooks, replacing nothing", scope="webhooks/")
        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        assert proposals == []

    def test_kinds_are_never_paired_across(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        _constraint(p, "A constraint about the mirror", scope="mirror.py", tags=["mirror"])
        _decision(p, "A decision about the mirror, replacing the old one", tags=["mirror"])
        proposals, _ = survey.survey_store(str(p / ".context"), 0.5, project="proj")
        assert all(x["evidence"] for x in proposals)
        for x in proposals:
            assert x["older_id"].split("-")[0] == x["newer_id"].split("-")[0]

    def test_threshold_filters_weak_pairs(self, survey, tmp_path):
        p = _project(tmp_path, "proj")
        _decision(p, "Decision one", tags=["a", "b", "c"])
        _decision(p, "Decision two", tags=["a"])
        assert survey.survey_store(str(p / ".context"), 0.9, project="proj")[0] == []
        assert survey.survey_store(str(p / ".context"), 0.3, project="proj")[0] != []


class TestStoreDiscovery:
    def test_finds_child_projects_and_the_root_itself(self, survey, tmp_path):
        _project(tmp_path, "alpha")
        _project(tmp_path, "beta")
        (tmp_path / "not_a_project").mkdir()
        found = {name for name, _base in survey.discover_stores([str(tmp_path)])}
        assert {"alpha", "beta"} <= found
        assert "not_a_project" not in found

    def test_a_root_with_no_stores_is_not_an_error(self, survey, tmp_path):
        assert survey.discover_stores([str(tmp_path / "nope")]) == []
