"""Tests for pairing practice material with professor-published solutions."""
import unittest

from intuition import solution_pairs


def _file(id_, rel_path):
    return {"id": id_, "name": rel_path.rsplit("/", 1)[-1], "rel_path": rel_path,
            "mime_type": "application/pdf", "modified": "2026-01-01T00:00:00Z"}


class ClassifyMaterialTests(unittest.TestCase):
    def test_solution_variants(self):
        for name in ("AY 1516 - Solutions.pdf", "Tut01_solutions.pdf",
                     "Tutorial Problem Set 1 - Solution Key (Teaching Week 2).pdf",
                     "Paper - Answer Key.pdf", "Q3 Model Answers.pdf",
                     "Midterm Marking Scheme.pdf"):
            self.assertEqual(solution_pairs.classify_material(name), "solution", name)

    def test_hint_is_not_a_solution(self):
        self.assertEqual(
            solution_pairs.classify_material("Tutorial 01_2026-hints.pdf"), "hint")

    def test_modelling_is_not_a_false_positive(self):
        # A bare "model" keyword previously matched "...ObjectOrientedModelling..."
        self.assertEqual(
            solution_pairs.classify_material(
                "CECZ2002_PPT_Chapter01ObjectOrientedModelling_V1.0.pdf"),
            "practice")

    def test_plain_practice_file(self):
        self.assertEqual(solution_pairs.classify_material("Tut01.pdf"), "practice")


class PairPracticeWithSolutionsTests(unittest.TestCase):
    def test_exact_base_match(self):
        files = [
            _file("q1", "MH1300/Misc/Midterms/AY 1516 - Questions.pdf"),
            _file("s1", "MH1300/Misc/Midterms/AY 1516 - Solutions.pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["solution"]["id"], "s1")
        self.assertEqual(pairs[0]["practice"]["id"], "q1")
        self.assertEqual(pairs[0]["match"], "exact")

    def test_prefix_fallback_for_numbered_variant(self):
        files = [
            _file("t1", "MH1300/Tutorials/Tut01.pdf"),
            _file("t1b", "MH1300/Tutorials/Tut01_1.pdf"),
            _file("s1", "MH1300/Tutorials/Tut01_solutions.pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        # exact match ("Tut01") is preferred over the numbered variant
        self.assertEqual(pairs[0]["practice"]["id"], "t1")
        self.assertEqual(pairs[0]["match"], "exact")

    def test_hints_only_leaves_practice_unmatched(self):
        files = [
            _file("t1", "MH2500/Tutorial 01_2026.pdf"),
            _file("h1", "MH2500/Tutorial 01_2026-hints.pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(pairs, [])  # no solution document -> nothing to pair

    def test_unmatched_solution_still_reported(self):
        files = [_file("s1", "MH1300/Orphan - Solutions.pdf")]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        self.assertIsNone(pairs[0]["practice"])
        self.assertEqual(pairs[0]["match"], "none")

    def test_different_folders_do_not_cross_match(self):
        files = [
            _file("q1", "MH1300/A/Tut01.pdf"),
            _file("s1", "MH1300/B/Tut01_solutions.pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        self.assertIsNone(pairs[0]["practice"])

    def test_revision_qualifier_after_the_keyword_still_matches(self):
        files = [
            _file("q1", "MH1300/MH1300_1819 - Questions.pdf"),
            _file("s1", "MH1300/MH1300_1819 - Solutions Updated.pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["practice"]["id"], "q1")
        self.assertEqual(pairs[0]["match"], "exact")

    def test_problem_set_in_the_title_is_not_stripped_as_a_question_marker(self):
        # "Problem Set" is part of this course's own tutorial title, not a
        # signal that the file is the question paper - stripping "Problem"
        # here previously broke the match against the solution key.
        files = [
            _file("q1", "MH2100/Tutorial Problem Set 1 (Teaching Week 2).pdf"),
            _file("s1", "MH2100/Tutorial Problem Set 1 - Solution Key (Teaching Week 2).pdf"),
        ]
        pairs = solution_pairs.pair_practice_with_solutions(files)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["practice"]["id"], "q1")
        self.assertEqual(pairs[0]["match"], "exact")


class IsTutorialMaterialTests(unittest.TestCase):
    def test_tutorials_folder_variants_match(self):
        for rel_path in (
            "MH1300/Tutorials/Tut01.pdf",
            "MH2100/Tutorial Problem Sets/Tutorial Problem Set 1 (Teaching Week 2)/"
            "Tutorial Problem Set 1 (Teaching Week 2).pdf",
            "SC2001/tutorial/week3.pdf",
        ):
            self.assertTrue(solution_pairs.is_tutorial_material(rel_path), rel_path)

    def test_exam_and_lecture_folders_do_not_match(self):
        for rel_path in (
            "MH1300/Misc/Midterm Exams (Past Year)/AY 1516 - Questions.pdf",
            "MH1300/Lecture Slides/Lecture01.pdf",
            "MH1300/Misc/Past Year Final Exams/MH1300_1516 - Solutions.pdf",
        ):
            self.assertFalse(solution_pairs.is_tutorial_material(rel_path), rel_path)

    def test_filename_alone_does_not_count(self):
        # Only the directory portion counts - a filename that happens to say
        # "tutorial" while living outside any Tutorial folder should not match.
        self.assertFalse(
            solution_pairs.is_tutorial_material("MH1300/Misc/Tutorial-style notes.pdf"))

    def test_empty_or_root_path_does_not_match(self):
        self.assertFalse(solution_pairs.is_tutorial_material(""))
        self.assertFalse(solution_pairs.is_tutorial_material("Tut01.pdf"))


class NewSolutionsTests(unittest.TestCase):
    def test_only_untagged_solutions_are_returned(self):
        class FakeNotebook:
            def __init__(self, tagged_ids):
                self.tagged_ids = tagged_ids

            def get(self, document_id):
                return {"markdown": "SOL"} if document_id in self.tagged_ids else None

        files = [
            _file("s1", "MH1300/AY 1516 - Solutions.pdf"),
            _file("s2", "MH1300/AY 1617 - Solutions.pdf"),
            _file("q1", "MH1300/AY 1516 - Questions.pdf"),
        ]
        fresh = solution_pairs.new_solutions(files, FakeNotebook({"s1"}))
        self.assertEqual([f["id"] for f in fresh], ["s2"])


if __name__ == "__main__":
    unittest.main()
