"""Replacement evidence is a different question from subject overlap.

Every fixture here is SYNTHETIC. The measurements that motivated this module
came from the operator's live mesh, which spans private projects and one with no
remote, so the corpus itself can never appear in this repo (con-015-12da). What
travels is the shape of each case, not the data.

Measured on 34 hand-labelled pairs from that mesh:

    signal                                  precision   recall
    subject overlap + any change marker         21.9%     100%    (what this replaces)
    newer entry names older + change lang       80.0%    57.1%    (what ships)

The recall drop is deliberate and costs nothing: everything below the top tier
is still reported as a `lead`, it just stops being called evidence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def dec(eid, summary, **kw):
    e = {"id": eid, "summary": summary, "problem": "", "why_chosen": "",
         "tags": [], "status": "active"}
    e.update(kw)
    return e


class TestReplacementEvidence(unittest.TestCase):
    def ev(self, new, old, corpus=None):
        return server._supersession_evidence(new, old, corpus=corpus)

    def test_naming_the_older_beside_change_language_is_likely(self):
        old = dec("dec-004", "Cache warm-up runs on boot.")
        new = dec("dec-009", "REVERTED dec-004: cache warm-up now runs lazily.")
        self.assertEqual(self.ev(new, old)["tier"], "likely")

    def test_naming_without_change_language_is_only_a_lead(self):
        """Entries cite each other approvingly all the time. A citation is not a
        replacement, and treating it as one was worth 26 points of precision."""
        old = dec("dec-004", "Cache warm-up runs on boot.")
        new = dec("dec-009", "Lazy loading, following the same discipline "
                             "established in dec-004.")
        self.assertEqual(self.ev(new, old)["tier"], "lead")

    def test_shared_tags_alone_are_never_evidence(self):
        """The original defect: two unrelated rules both tagged `gotcha`, one of
        them containing the word `was`, scored as the highest confidence tier."""
        old = dec("con-001", "Never pass a secret as a command argument.",
                  tags=["gotcha", "scripts"])
        new = dec("con-007", "Never redirect stderr on a native call; the exit "
                             "code was wrong in every case we hit.",
                  tags=["gotcha", "scripts"])
        self.assertEqual(self.ev(new, old)["tier"], "lead")

    def test_the_id_stem_counts_when_it_is_unambiguous(self):
        """Authors write `dec-086`, not `dec-086-97c7`. Requiring the full id
        missed a literal 'REVERTED dec-086'."""
        old = dec("dec-086-97c7", "gae_lambda raised to 0.99.")
        new = dec("dec-088-a9c6", "REVERTED dec-086 on its variance trigger.")
        corpus = [old, new]
        self.assertEqual(self.ev(new, old, corpus)["tier"], "likely")

    def test_the_stem_is_refused_when_another_entry_could_claim_it(self):
        """`con-002` must not match inside `con-002-b4cc`, which made unrelated
        constraints in one store look like they cited each other."""
        old = dec("con-002-b4cc", "PowerShell redirection rule.")
        rival = dec("con-002", "An entirely different, older rule.")
        new = dec("con-009-1111", "Superseding con-002 with a new approach.")
        corpus = [old, rival, new]
        self.assertEqual(self.ev(new, old, corpus)["tier"], "lead")

    def test_evidence_ignores_related_to_link_lists(self):
        """related_to means 'related', not 'replaces'. Reading id lists as prose
        made the top tier fire on every linked pair."""
        old = dec("dec-004", "Cache warm-up runs on boot.")
        new = dec("dec-009", "Lazy loading was chosen for startup time.",
                  related_to=["dec-004"], constraints_created=["dec-004"])
        self.assertEqual(self.ev(new, old)["tier"], "lead")

    def test_signals_are_reported_so_a_reader_can_judge(self):
        old = dec("dec-004", "Cache warm-up runs on boot.")
        new = dec("dec-009", "REVERTED dec-004: cache warm-up now runs lazily.")
        signals = self.ev(new, old)["signals"]
        self.assertTrue(any("dec-004" in s for s in signals), signals)

    def test_subject_overlap_is_left_alone(self):
        """_supersession_score still answers its own question honestly; it was
        never wrong, it was being asked the wrong one."""
        a = dec("dec-001", "x", tags=["auth", "api"])
        b = dec("dec-002", "y", tags=["auth", "api"])
        self.assertGreater(server._supersession_score(a, b), 0.9)
        self.assertEqual(self.ev(a, b)["tier"], "lead")


if __name__ == "__main__":
    unittest.main()
