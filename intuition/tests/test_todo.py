import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import dashboard, research, todo


class TestQueue(unittest.TestCase):
    def test_omniroute_exposes_curated_cross_provider_models(self):
        options = dict(dashboard.TODO_MODEL_OPTIONS[research.BACKEND_OMNIROUTE])
        self.assertIn("auto", options)
        self.assertIn("gpt-5.6-sol", options)
        self.assertIn("claude-opus-5", options)
        self.assertIn("gemini-3.1-pro", options)
        cli = dict(dashboard.TODO_MODEL_OPTIONS[research.BACKEND_CLI])
        self.assertNotIn("gpt-5.6-sol", cli)

    def test_auto_model_dropdown_includes_live_omniroute_routes(self):
        with patch.object(dashboard.omniroute_provider, "models", return_value=[
                "auto/best-coding", "auto/cheap", "codex/gpt-5.6-sol"]):
            options = dict(dashboard.todo_model_options()["auto"])
        self.assertEqual(options["auto"], "Auto model")
        self.assertEqual(options["auto/best-coding"], "Auto · Best Coding")
        self.assertIn("auto/cheap", options)
        self.assertNotIn("codex/gpt-5.6-sol", options)

    def test_add_update_remove_and_persist(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            item = q.add("Review lab", direction="pull", priority="high",
                         details="Check the submitted notebook")
            self.assertEqual(item["status"], "open")
            q.update(item["id"], status="in_progress")
            q.save()
            again = todo.Queue(root)
            self.assertEqual(again.get(item["id"])["status"], "in_progress")
            self.assertTrue(again.remove(item["id"]))

    def test_validates_requests(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            with self.assertRaises(ValueError):
                q.add(" ")
            with self.assertRaises(ValueError):
                q.add("x", direction="sideways")
            item = q.add("x")
            with self.assertRaises(ValueError):
                q.update(item["id"], status="lost")

    def test_snapshot_orders_active_high_priority_first(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            low = q.add("low", priority="low")
            q.add("high", priority="high")
            q.update(low["id"], status="done")
            snap = q.snapshot()
            self.assertEqual([i["title"] for i in snap["items"]], ["high", "low"])
            self.assertEqual(snap["counts"]["done"], 1)

    def test_corrupt_store_is_safe(self):
        with TemporaryDirectory() as root:
            path = todo.queue_path(root)
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("not json")
            self.assertEqual(len(todo.Queue(root)), 0)

    def test_ntulearn_sync_replaces_imports_and_keeps_manual_queries(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            manual = q.add("My own query")
            self.assertEqual(q.sync_ntulearn([{
                "source_id": "_42_1", "title": "Lab 1",
                "course": "SC2005", "due": "2026-08-20T08:00:00Z",
                "kind": "Assignment",
            }]), 1)
            q.sync_ntulearn([{
                "source_id": "_43_1", "title": "Quiz",
                "course": "SC2005", "due": "", "kind": "Test",
            }])
            self.assertIsNotNone(q.get(manual["id"]))
            imported = [i for i in q.items if i.get("source") == "ntulearn"]
            self.assertEqual([i["title"] for i in imported], ["Quiz"])

    def test_research_result_is_persisted_and_survives_ntulearn_refresh(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            item = q.add("Explain paging")
            q.set_research(item["id"], {"text": "Answer", "model": "m"})
            q.save()
            self.assertEqual(todo.Queue(root).get(item["id"])["research"]["text"],
                             "Answer")
            q.sync_ntulearn([{"source_id": "x", "title": "Quiz"}])
            imported = next(i for i in q.items if i.get("source") == "ntulearn")
            q.set_research(imported["id"], {"text": "Plan"})
            q.sync_ntulearn([{"source_id": "x", "title": "Quiz"}])
            self.assertEqual(q.get(imported["id"])["research"]["text"], "Plan")

    def test_queries_are_scoped_to_course_codes(self):
        with TemporaryDirectory() as root:
            q = todo.Queue(root)
            manual = q.add("Explain tutorial 2", course="AY2026-2027 SC2005")
            self.assertEqual(manual["course"], "SC2005")
            q.sync_ntulearn([{
                "source_id": "quiz", "title": "Quiz 1",
                "course": "26S1-MH2100-CALCULUS III",
            }])
            imported = next(i for i in q.items if i.get("source") == "ntulearn")
            self.assertEqual(imported["course"], "MH2100")


if __name__ == "__main__":
    unittest.main()
