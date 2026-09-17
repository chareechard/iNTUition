import json
import os
import tempfile
import unittest

from intuition import faculty_db


class FacultyDirectoryTests(unittest.TestCase):
    def test_catalogue_has_both_ntu_units(self):
        schools = {
            school
            for row in faculty_db.directory()
            for school in row.get("schools", [])
        }
        self.assertEqual(schools, {"SPMS", "CCDS"})
        metadata = faculty_db.metadata()
        self.assertGreaterEqual(metadata["count"], 100)
        self.assertGreaterEqual(metadata["rawRecordCount"], metadata["count"])
        self.assertEqual(metadata["sourceCounts"]["CCDS Faculty Directory"], 115)
        self.assertEqual(metadata["sourceCounts"]["SPMS Mathematics Faculty"], 70)
        self.assertGreaterEqual(len(faculty_db.directory(school="CCDS")), 100)
        self.assertGreaterEqual(len(faculty_db.directory(school="SPMS")), 60)

    def test_topic_match_is_deterministic_and_ranked(self):
        matches = faculty_db.match_topic(
            "Constrained lightweight cipher evaluation",
            "Study symmetric-key cryptography for IoT devices.",
        )
        self.assertIn("Thomas PEYRIN", {match["name"] for match in matches})
        self.assertTrue(all(match["score"] >= faculty_db.MIN_MATCH_SCORE
                            for match in matches))
        self.assertTrue(matches[0]["matched_terms"])

    def test_generic_overlap_does_not_create_a_faculty_match(self):
        self.assertEqual(
            faculty_db.match_topic(
                "Character Tables of Small Groups",
                "Study finite group computation.",
            ),
            [],
        )

    def test_unrelated_topic_can_return_no_match(self):
        self.assertEqual(
            faculty_db.match_topic("Campus lunch menu", "Compare meal prices."),
            [],
        )

    def test_match_suggestions_maps_every_topic_to_a_professor(self):
        suggestions = [
            {"title": "Campus lunch menu", "topic": "Compare canteen meal prices."},
            {"title": "Character Tables of Small Groups",
             "topic": "Study finite group computation."},
            {"title": "Constrained lightweight cipher evaluation",
             "topic": "Study symmetric-key cryptography for IoT devices."},
            {"title": "Zebra migration patterns",
             "topic": "Track wildlife across the savannah."},
        ]
        rows = faculty_db.match_suggestions(suggestions)
        self.assertEqual(len(rows), len(suggestions))
        # Total: no topic is left without a lead.
        self.assertTrue(all(row for row in rows))
        # Injective: the primary lead is distinct across topics.
        primaries = [row[0]["id"] for row in rows]
        self.assertEqual(len(set(primaries)), len(primaries))
        # A topic with no real catalogue match still gets a lead, flagged weak;
        # a strong match is never flagged weak.
        by_title = dict(zip((s["title"] for s in suggestions), rows))
        self.assertTrue(by_title["Campus lunch menu"][0].get("weak"))
        self.assertFalse(by_title["Constrained lightweight cipher evaluation"][0]
                         .get("weak"))

    def test_match_suggestions_is_stable_in_input_order_and_empty_safe(self):
        self.assertEqual(faculty_db.match_suggestions([]), [])
        one = faculty_db.match_suggestions(
            [{"title": "Constrained lightweight cipher evaluation",
              "topic": "Symmetric-key cryptography for IoT."}])
        self.assertEqual(len(one), 1)
        self.assertTrue(one[0])

    def test_get_returns_the_named_faculty_member(self):
        any_record = faculty_db.directory()[0]
        fetched = faculty_db.get(any_record["id"])
        self.assertEqual(fetched["id"], any_record["id"])
        self.assertEqual(fetched["name"], any_record["name"])
        self.assertIn("research_interests", fetched)

    def test_get_returns_none_for_unknown_or_blank_id(self):
        self.assertIsNone(faculty_db.get("not-a-real-id"))
        self.assertIsNone(faculty_db.get(""))

    def test_catalogue_reloads_after_file_becomes_available(self):
        original_path = faculty_db._DATA_PATH
        original_catalogue = faculty_db._CATALOGUE
        original_signature = faculty_db._CATALOGUE_SIGNATURE
        try:
            with tempfile.TemporaryDirectory() as root:
                faculty_db._DATA_PATH = os.path.join(root, "faculty.json")
                faculty_db._CATALOGUE = {}
                faculty_db._CATALOGUE_SIGNATURE = None
                self.assertEqual(faculty_db.metadata()["count"], 0)
                with open(faculty_db._DATA_PATH, "w", encoding="utf-8") as stream:
                    json.dump({
                        "version": 1,
                        "faculty": [{
                            "id": "test",
                            "name": "Test Faculty",
                            "school": "SPMS",
                            "tags": ["test methods"],
                        }],
                    }, stream)
                self.assertEqual(faculty_db.metadata()["count"], 1)
        finally:
            faculty_db._DATA_PATH = original_path
            faculty_db._CATALOGUE = original_catalogue
            faculty_db._CATALOGUE_SIGNATURE = original_signature


if __name__ == "__main__":
    unittest.main()