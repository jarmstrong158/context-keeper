"""Quality scanning: verify_quality's checks, staleness, and mojibake repair.

Split out of tests/test_server.py; shared builders live in tests/helpers.py.
"""

from helpers import *  # noqa: F401,F403




# ===========================================================================
# 13. verify_quality
# ===========================================================================


class TestVerifyQuality:
    def test_legacy_decision_flagged(self, tmp_path):
        """Pre-v0.4 entries (no schema_version, no why_chosen) get flagged."""
        ctx = context_dir(tmp_path)
        ctx.mkdir(parents=True)
        # Write a legacy entry directly (bypassing the handler so no
        # auto-migration happens). Reasoning text is intentionally long
        # enough so the only flag we get is 'legacy'.
        legacy = [{
            "id": "dec-001",
            "summary": "Old decision",
            "rationale": (
                "Some long enough rationale text that easily clears the "
                "thin-reason threshold so we isolate the legacy flag in this test."
            ),
            "tags": ["old"],
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
            "verified_at": "2024-01-01T00:00:00+00:00",
        }]
        (ctx / "decisions.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        assert result["count"] >= 1
        flag = next(f for f in result["flagged"] if f["id"] == "dec-001")
        issue_types = [i["type"] for i in flag["issues"]]
        assert "legacy" in issue_types

    def test_thin_reason_flagged(self, tmp_path):
        """v0.4 entry with why_chosen that clears the 60-char validation
        minimum but trips the higher quality-scan threshold."""
        # 70 chars — above validation min (60), below test threshold (200)
        why = "Short answer that passes server-side validation but trips quality scan."
        assert 60 <= len(why) < 200
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Decision",
            "problem": "We needed a quick test of the thin-reason quality flag.",
            "why_chosen": why,
            "tags": ["test"],
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path, {"min_reason_chars": 200}))
        assert any(
            "thin_reason" in [i["type"] for i in f["issues"]]
            for f in result["flagged"]
        )

    def test_no_tags_flagged(self, tmp_path):
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Decision",
            "problem": "We needed an entry with no tags so we can flag it for the test.",
            "why_chosen": (
                "Tags are the primary retrieval signal — entries without tags "
                "won't surface from get_context queries, which is the issue we want to flag here."
            ),
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        assert any(
            "no_tags" in [i["type"] for i in f["issues"]]
            for f in result["flagged"]
        )

    def test_isolated_flagged(self, tmp_path):
        """Two entries sharing a tag but no related_to link → both flagged isolated."""
        for i in range(2):
            handle_record_decision({
                "project_dir": str(tmp_path),
                "summary": f"Decision {i}",
                "problem": "We needed two entries that share a tag for the isolation flag test.",
                "why_chosen": (
                    "Both entries share the 'shared' tag but neither links to the other, "
                    "which is exactly the missed-link condition the isolated flag is designed to catch."
                ),
                "tags": ["shared"],
            })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        isolated = [
            f for f in result["flagged"]
            if any(i["type"] == "isolated" for i in f["issues"])
        ]
        assert len(isolated) == 2

    def test_clean_entry_not_flagged(self, tmp_path):
        """Entry with full v0.4 schema + tags + related_to should pass clean."""
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "First decision",
            "problem": "We need a clean entry that passes every quality check we run today.",
            "why_chosen": (
                "Full v0.4 schema, populated tags, and a related_to link to dec-002 "
                "should make this entry survive verify_quality with zero flags."
            ),
            "tags": ["clean"],
            "related_to": ["dec-002"],
        })
        handle_record_decision({
            "project_dir": str(tmp_path),
            "summary": "Second decision",
            "problem": "We need a second entry so the related_to link target exists in scope.",
            "why_chosen": (
                "Same shape as the first entry, with related_to pointing back at dec-001 "
                "so neither shows up isolated despite sharing the 'clean' tag."
            ),
            "tags": ["clean"],
            "related_to": ["dec-001"],
        })
        from server import handle_verify_quality
        result = handle_verify_quality(project_params(tmp_path))
        flagged_ids = [f["id"] for f in result["flagged"]]
        assert "dec-001" not in flagged_ids
        assert "dec-002" not in flagged_ids




# ===========================================================================
# Read-modify-write races: index must not survive the re-read (audit #2)
# ===========================================================================


class TestStaleIndexWrites:
    """update_entry / deprecate_entry must re-find by id after the re-read.

    Both handlers used to take `index` from _find_entry_by_id's read, then
    _load_entries_for_write the file AGAIN and blind-assign
    entries[index] = entry. If the file changed in between -- another
    agent's record_*, a mirror pull, or the hand-edit README.md:537
    explicitly invites -- that index points at a DIFFERENT record and the
    write silently overwrites it. The merge path in deprecate_entry was
    already hardened against exactly this (it rebuilds an id->index map
    after the re-read) but the fix was never back-ported to the two plain
    paths.

    Each test reproduces the corruption by changing the store between the
    two reads, which is what a hand-edit or a concurrent write looks like
    from the handler's point of view.
    """

    def _decisions(self, tmp_path):
        return context_dir(tmp_path) / "decisions.json"

    def _seed_three(self, tmp_path):
        ids = []
        for n in ("alpha", "bravo", "charlie"):
            r = handle_record_decision(decision_params(
                tmp_path, summary=f"Decision about {n} subsystem", tags=[n]))
            ids.append(r["entry"]["id"])
        return ids

    def _change_between_reads(self, monkeypatch, path, mutate, fire_on=2):
        """Run mutate(path) exactly once, on the handler's Nth read of `path`.

        Read 1 is _find_entry_by_id's (where the stale index came from); read
        2 is _load_entries_for_write's, so firing on read 2 places the change
        precisely in the window between them -- the race being fixed. The
        mutated content is what read 2 returns.
        """
        import server as srv
        original = srv._read_json_file_checked
        state = {"reads": 0, "fired": False}

        def hooked(p):
            if os.path.abspath(p) == os.path.abspath(path):
                state["reads"] += 1
                if not state["fired"] and state["reads"] == fire_on:
                    state["fired"] = True
                    mutate(path)
            return original(p)

        monkeypatch.setattr(srv, "_read_json_file_checked", hooked)
        return state

    @staticmethod
    def _reverse_on_disk(p):
        disk = json.loads(Path(p).read_text(encoding="utf-8"))
        disk.reverse()
        Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")

    def test_update_entry_does_not_clobber_a_reordered_neighbor(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        # 'a' is at index 0 on the first read; after the reversal index 0 is
        # 'c'. The old code wrote the mutated 'a' into slot 0 -- destroying 'c'.
        self._change_between_reads(monkeypatch, str(path), self._reverse_on_disk)
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})
        assert "error" not in r, r

        disk = json.loads(path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in disk}
        # Nothing lost, nothing duplicated.
        assert len(disk) == 3
        assert sorted(by_id) == sorted([a, b, c])
        # The update landed on 'a' ...
        assert by_id[a]["summary"] == "Rewritten alpha summary"
        # ... and the neighbors are untouched, not overwritten by a copy of 'a'.
        assert by_id[c]["summary"] == "Decision about charlie subsystem"
        assert by_id[b]["summary"] == "Decision about bravo subsystem"

    def test_deprecate_entry_does_not_clobber_a_reordered_neighbor(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._reverse_on_disk)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "Superseded during a concurrent-edit regression test."})
        assert "error" not in r, r

        disk = json.loads(path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in disk}
        assert len(disk) == 3
        assert sorted(by_id) == sorted([a, b, c])
        # Exactly ONE entry got deprecated, and it is the one we asked for.
        assert by_id[a]["status"] == "deprecated"
        assert by_id[b].get("status") != "deprecated"
        assert by_id[c].get("status") != "deprecated"

    def test_update_entry_preserves_a_concurrent_edit_to_another_field(
            self, tmp_path, monkeypatch):
        """The stale COPY was assigned back wholesale, so a concurrent edit to
        a field the caller never mentioned got reverted. Applying the update
        to the freshly-read object keeps it."""
        a, _b, _c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        def hand_edit(p):
            disk = json.loads(Path(p).read_text(encoding="utf-8"))
            for e in disk:
                if e["id"] == a:
                    e["tradeoffs"] = "Added by hand while the tool was running"
            Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")

        self._change_between_reads(monkeypatch, str(path), hand_edit)
        handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})

        disk = json.loads(path.read_text(encoding="utf-8"))
        entry = next(e for e in disk if e["id"] == a)
        assert entry["summary"] == "Rewritten alpha summary"
        assert entry["tradeoffs"] == "Added by hand while the tool was running"

    @staticmethod
    def _deleter(target_id):
        def delete(p):
            disk = json.loads(Path(p).read_text(encoding="utf-8"))
            disk = [e for e in disk if e["id"] != target_id]
            Path(p).write_text(json.dumps(disk, indent=2), encoding="utf-8")
        return delete

    def test_update_entry_errors_if_the_entry_vanishes_mid_write(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._deleter(a))
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Rewritten alpha summary"}})
        assert "error" in r and "retry" in r["error"].lower()

        # Refusing is the point: the survivors stay intact and the vanished
        # entry is not resurrected into someone else's slot.
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert sorted(e["id"] for e in disk) == sorted([b, c])
        assert all("Rewritten alpha" not in e["summary"] for e in disk)

    def test_deprecate_entry_errors_if_the_entry_vanishes_mid_write(
            self, tmp_path, monkeypatch):
        a, b, c = self._seed_three(tmp_path)
        path = self._decisions(tmp_path)

        self._change_between_reads(monkeypatch, str(path), self._deleter(a))
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "Deprecating an entry that vanished under us."})
        assert "error" in r and "retry" in r["error"].lower()

        disk = json.loads(path.read_text(encoding="utf-8"))
        assert sorted(e["id"] for e in disk) == sorted([b, c])
        assert not any(e.get("status") == "deprecated" for e in disk)

    def test_update_entry_happy_path_unchanged(self, tmp_path):
        """Guard: no concurrent change, behavior identical to before."""
        a, _b, _c = self._seed_three(tmp_path)
        r = handle_update_entry({
            "project_dir": str(tmp_path), "id": a,
            "updates": {"summary": "Plain update, no race"}})
        assert r["success"] is True
        assert r["entry"]["summary"] == "Plain update, no race"
        disk = json.loads(self._decisions(tmp_path).read_text(encoding="utf-8"))
        assert len(disk) == 3
        assert next(e for e in disk if e["id"] == a)["summary"] == "Plain update, no race"

    def test_deprecate_entry_happy_path_unchanged(self, tmp_path):
        a, _b, _c = self._seed_three(tmp_path)
        r = handle_deprecate_entry({
            "project_dir": str(tmp_path), "id": a,
            "reason": "No race here; the ordinary deprecation path must still work."})
        assert r["success"] is True and r["status"] == "deprecated"
        disk = json.loads(self._decisions(tmp_path).read_text(encoding="utf-8"))
        assert len(disk) == 3
        assert next(e for e in disk if e["id"] == a)["status"] == "deprecated"




class TestDemojibake:
    def test_repairs_a_known_misdecode(self):
        assert demojibake(_GARBLED) == _CLEAN

    @pytest.mark.parametrize("clean", [
        "penalty at -10 × min(N, 50)",   # multiplication sign
        "scale ÷ 2",                      # division sign
        "cafà and hôtel",            # a-grave, o-circumflex
        "schön and ähnlich",         # umlauts
        "« quoted »",                # guillemets
        "45° ± 2",                   # degree, plus-minus
    ])
    def test_every_marker_family_round_trips(self, clean):
        """The detector GATES the repair, so a family missing from
        _MOJIBAKE_MARKERS is one demojibake never attempts.

        That is not hypothetical: the multiplication sign was absent, so eleven
        entries across Clark and balatron sat unrepaired through dec-020's
        160-entry heal and stayed invisible to verify_quality. Each case below
        is corrupted the way the transport used to corrupt it, then must be
        detected AND repaired back to the original.
        """
        garbled = clean.encode("utf-8").decode("cp1252")
        assert garbled != clean
        assert looks_like_mojibake(garbled), f"not detected: {clean!r}"
        assert demojibake(garbled) == clean

    def test_legitimate_uses_of_those_characters_are_not_flagged(self):
        """The other half: markers are multi-character sequences precisely so
        that correct text using the same characters is left alone."""
        for clean in ("penalty at -10 × min(N, 50)", "45° ± 2",
                      "an em — dash", "café latte", "« quoted »"):
            assert not looks_like_mojibake(clean), clean
            assert demojibake(clean) is None, clean

    def test_clean_text_is_left_alone(self):
        for text in (_CLEAN, "plain ascii text", "already fine — em dash",
                     "accented café latte", ""):
            assert demojibake(text) is None, text

    def test_non_strings_are_ignored(self):
        for value in (None, 42, [], {}, True):
            assert demojibake(value) is None

    def test_repair_is_verified_as_an_exact_inverse(self):
        """Re-applying the corruption to the candidate must reproduce the
        input byte for byte. A partial or approximate repair of someone's
        recorded reasoning is worse than legible damage: it looks fixed."""
        repaired = demojibake(_GARBLED)
        assert repaired.encode("utf-8").decode("cp1252") == _GARBLED

    def test_repair_is_idempotent(self):
        once = demojibake(_GARBLED)
        assert demojibake(once) is None

    def test_marker_prefilter_gates_the_transform(self):
        """Some innocent strings survive the encode/decode round trip. The
        marker check is what keeps those from being 'repaired'."""
        assert looks_like_mojibake(_GARBLED) is True
        assert looks_like_mojibake(_CLEAN) is False




class TestRepairMojibake:
    def _garble(self, tmp_path, entry_id, field, value):
        path = context_dir(tmp_path) / "decisions.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            if e["id"] == entry_id:
                e[field] = value
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def test_dry_run_reports_without_writing(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        result = handle_repair_mojibake({"project_dir": str(tmp_path)})
        assert result["applied"] is False
        assert result["entries_affected"] == 1
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["why_chosen"] == _GARBLED, "dry run wrote to disk"

    def test_apply_repairs_the_entry(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["why_chosen"] == _CLEAN

    def test_verified_at_is_not_refreshed(self, tmp_path):
        """An encoding fix is not a claim that anyone re-confirmed the entry
        is still true. Resetting the staleness clock here would erase the
        exact signal prune_stale and the drift check exist to raise."""
        rec = handle_record_decision(decision_params(tmp_path))
        before = rec["entry"]["verified_at"]
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["verified_at"] == before

    def test_updated_at_is_bumped_so_the_repair_wins_the_mirror_merge(self, tmp_path):
        rec = handle_record_decision(decision_params(tmp_path))
        before = rec["entry"]["updated_at"]
        self._garble(tmp_path, rec["id"], "why_chosen", _GARBLED)
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["updated_at"] >= before

    def test_clean_store_is_a_no_op(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert result["entries_affected"] == 0

    def test_repairs_nested_alternatives(self, tmp_path):
        rec = handle_record_decision(decision_params(
            tmp_path,
            alternatives=[{"option": _GARBLED, "reason_rejected": _GARBLED}]))
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert result["fields_affected"] == 2
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["alternatives"][0]["option"] == _CLEAN

    def test_ids_are_never_rewritten(self, tmp_path):
        """Rewriting an id would break every related_to pointing at it."""
        rec = handle_record_decision(decision_params(tmp_path))
        handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        on_disk = json.loads(
            (context_dir(tmp_path) / "decisions.json").read_text(encoding="utf-8"))
        assert on_disk[0]["id"] == rec["id"]

    def test_refuses_to_write_over_a_corrupt_store(self, tmp_path):
        handle_record_decision(decision_params(tmp_path))
        (context_dir(tmp_path) / "decisions.json").write_text("{ not json",
                                                              encoding="utf-8")
        result = handle_repair_mojibake({"project_dir": str(tmp_path), "apply": True})
        assert "error" in result

    def test_not_in_tools_list(self):
        from server import HANDLERS, TOOLS
        assert "repair_mojibake" in HANDLERS
        assert "repair_mojibake" not in {t["name"] for t in TOOLS}




class TestVerifyQualityFlagsMojibake:
    def test_flagged_with_a_repair_instruction(self, tmp_path):
        rec = handle_record_constraint(constraint_params(tmp_path))
        path = context_dir(tmp_path) / "constraints.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries[0]["reason"] = _GARBLED
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

        result = handle_verify_quality({"project_dir": str(tmp_path),
                                        "check_drift": False})
        entry = next(f for f in result["flagged"] if f["id"] == rec["id"])
        issue = next(i for i in entry["issues"] if i["type"] == "mojibake")
        assert "repair_mojibake" in issue["detail"]

    def test_clean_entries_are_not_flagged(self, tmp_path):
        handle_record_constraint(constraint_params(tmp_path))
        result = handle_verify_quality({"project_dir": str(tmp_path),
                                        "check_drift": False})
        types = {i["type"] for f in result["flagged"] for i in f["issues"]}
        assert "mojibake" not in types




class TestGlobalScopeFlag:
    """47% of real constraints were `global`, which excludes them from the
    drift check, the .claude/rules/ projection and scope_guard alike. Their
    only route to the model is the summary -- the thing that truncates."""

    def _issues(self, tmp_path, entry_id):
        r = handle_verify_quality({"project_dir": str(tmp_path),
                                   "check_drift": False})
        entry = next((f for f in r["flagged"] if f["id"] == entry_id), None)
        return [i["type"] for i in (entry or {}).get("issues", [])]

    def test_flagged_when_enforced_by_names_a_real_file(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_thing.py").write_text("x = 1", encoding="utf-8")
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="global",
            enforced_by="tests/test_thing.py::TestThing::test_case"))
        assert "global_scope" in self._issues(tmp_path, rec["id"])

    def test_traversal_in_enforced_by_is_not_suggested(self, tmp_path):
        """A `..` component cannot become a repo-relative glob, and it would
        let a scope reach outside the project. Windows os.path.exists
        normalizes such a path and reports True even when a component is
        missing, so this only failed on POSIX -- caught by CI, not locally."""
        (tmp_path / "server.py").write_text("x = 1", encoding="utf-8")
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="global", enforced_by="tests/../server.py::TestThing"))
        assert "global_scope" not in self._issues(tmp_path, rec["id"])

    def test_flagged_when_a_tag_names_a_real_directory(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="global", tags=["hooks"]))
        assert "global_scope" in self._issues(tmp_path, rec["id"])

    def test_not_flagged_without_evidence(self, tmp_path):
        """A bare 'consider adding a scope' on every global constraint is
        noise the reader learns to skip, and some rules really are global."""
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="global", tags=["philosophy", "process"]))
        assert "global_scope" not in self._issues(tmp_path, rec["id"])

    def test_scoped_constraints_are_never_flagged(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="hooks/", tags=["hooks"]))
        assert "global_scope" not in self._issues(tmp_path, rec["id"])

    def test_decisions_are_never_flagged(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        rec = handle_record_decision(decision_params(tmp_path, tags=["hooks"]))
        assert "global_scope" not in self._issues(tmp_path, rec["id"])

    def test_suggestion_is_a_projectable_scope(self, tmp_path):
        """A suggestion that cannot become a rules pattern is not a fix."""
        (tmp_path / "hooks").mkdir()
        rec = handle_record_constraint(constraint_params(
            tmp_path, scope="global", tags=["hooks"]))
        r = handle_verify_quality({"project_dir": str(tmp_path),
                                   "check_drift": False})
        entry = next(f for f in r["flagged"] if f["id"] == rec["id"])
        issue = next(i for i in entry["issues"] if i["type"] == "global_scope")
        suggested = issue["detail"].split("scope='")[1].split("'")[0]
        assert _scope_to_paths(suggested)
