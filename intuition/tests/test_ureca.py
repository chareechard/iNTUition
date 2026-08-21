import json
import unittest
from tempfile import TemporaryDirectory

from intuition import ureca


class TestStore(unittest.TestCase):
    def test_add_update_remove_and_persist(self):
        with TemporaryDirectory() as root:
            store = ureca.Store(root)
            item = store.add("Predicting exam stress from calendar load")
            self.assertEqual(item["status"], "drafting")
            self.assertEqual(item["category"], "URECA")
            self.assertEqual(set(item["deliverables"]), set(ureca.DELIVERABLES))
            self.assertFalse(any(item["deliverables"].values()))

            store.update(item["id"], background="Students juggle many deadlines.",
                        supervisor="Prof Tan", status="sent",
                        deliverables={"abstract": True})
            store.save()

            again = ureca.Store(root)
            reloaded = again.get(item["id"])
            self.assertEqual(reloaded["background"], "Students juggle many deadlines.")
            self.assertEqual(reloaded["supervisor"], "Prof Tan")
            self.assertEqual(reloaded["status"], "sent")
            self.assertTrue(reloaded["deliverables"]["abstract"])
            self.assertFalse(reloaded["deliverables"]["poster"])

            self.assertTrue(again.remove(item["id"]))
            self.assertIsNone(again.get(item["id"]))

    def test_rejects_blank_title_and_unknown_category_or_status(self):
        with TemporaryDirectory() as root:
            store = ureca.Store(root)
            with self.assertRaises(ValueError):
                store.add("   ")
            item = store.add("Untitled")
            with self.assertRaises(ValueError):
                store.update(item["id"], category="Fulbright")
            with self.assertRaises(ValueError):
                store.update(item["id"], status="funded")
            with self.assertRaises(ValueError):
                store.update(item["id"], title="")

    def test_update_missing_item_returns_none(self):
        with TemporaryDirectory() as root:
            store = ureca.Store(root)
            self.assertIsNone(store.update("nope", title="x"))

    def test_next_registration_deadline_rolls_over_30_september(self):
        import datetime
        self.assertEqual(ureca.next_registration_deadline(datetime.date(2026, 8, 19)),
                         "2026-09-30")
        self.assertEqual(ureca.next_registration_deadline(datetime.date(2026, 9, 30)),
                         "2026-09-30")
        self.assertEqual(ureca.next_registration_deadline(datetime.date(2026, 10, 1)),
                         "2027-09-30")


class TestDraftParsing(unittest.TestCase):
    def test_a_clean_draft_survives(self):
        raw = json.dumps({
            "background": "Timetabling conflicts are common at NTU.",
            "objectives": "1. Survey students. 2. Build a predictor.",
            "methodology": "Collect calendar data and train a small model.",
            "outcomes": "A validated predictor and a short report.",
            "budgetNotes": "A survey incentive pool, within the $500 cap.",
            "timelineNotes": "Aug-Oct: survey. Nov-Mar: build. Apr-Jun: write up.",
        })
        draft = ureca.parse_draft_response(raw)
        self.assertEqual(draft["background"], "Timetabling conflicts are common at NTU.")
        self.assertIn("Survey students", draft["objectives"])
        self.assertIn("$500 cap", draft["budgetNotes"])
        self.assertIn("Aug-Oct", draft["timelineNotes"])

    def test_garbage_response_yields_empty_strings_not_an_error(self):
        draft = ureca.parse_draft_response("not json")
        self.assertEqual(draft, {key: "" for key in ureca.DRAFT_FIELDS})

    def test_markdown_fence_is_unwrapped(self):
        raw = "```json\n" + json.dumps({"background": "A gap exists."}) + "\n```"
        self.assertEqual(ureca.parse_draft_response(raw)["background"], "A gap exists.")


class TestSuggestParsing(unittest.TestCase):
    def test_a_clean_list_survives(self):
        raw = json.dumps([
            {"title": "Predicting exam stress", "topic": "Use calendar load to flag at-risk weeks."},
            {"title": "Timetable clash detector", "topic": "Detect scheduling conflicts automatically."},
        ])
        suggestions = ureca.parse_suggest_response(raw)
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["title"], "Predicting exam stress")
        self.assertIn("at-risk", suggestions[0]["topic"])

    def test_garbage_response_yields_no_suggestions(self):
        self.assertEqual(ureca.parse_suggest_response("not json"), [])

    def test_entries_missing_a_title_or_topic_are_dropped(self):
        raw = json.dumps([{"title": "Only a title"}, {"topic": "Only a topic"},
                          {"title": "Complete", "topic": "Has both fields"}])
        suggestions = ureca.parse_suggest_response(raw)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["title"], "Complete")

    def test_caps_at_max_suggestions(self):
        raw = json.dumps([{"title": "T{}".format(i), "topic": "Idea {}".format(i)}
                          for i in range(10)])
        self.assertEqual(len(ureca.parse_suggest_response(raw)), ureca.MAX_SUGGESTIONS)

    def test_markdown_fence_is_unwrapped(self):
        raw = "```json\n" + json.dumps([{"title": "T", "topic": "An idea"}]) + "\n```"
        self.assertEqual(ureca.parse_suggest_response(raw)[0]["title"], "T")

    def test_a_response_truncated_mid_array_salvages_the_complete_entries(self):
        complete = json.dumps({"title": "Complete idea", "topic": "A full sentence."})
        raw = "[" + complete + ',{"title":"Cut off","topic":"This never clos'
        suggestions = ureca.parse_suggest_response(raw)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["title"], "Complete idea")

    def test_a_brace_inside_a_string_does_not_break_salvage(self):
        raw = ('[{"title":"T","topic":"Uses a } brace in the middle of a sentence."},'
               '{"title":"Second","topic":"Also complete."}]')
        suggestions = ureca.parse_suggest_response(raw)
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[1]["title"], "Second")


if __name__ == "__main__":
    unittest.main()
