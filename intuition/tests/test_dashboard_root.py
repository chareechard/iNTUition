"""Changing the sync folder must re-point every root-anchored store.

Reassigning ``download_root`` on its own left the ledger and cache reading the old
folder while freshly built plans were keyed to the new one, so every archived file
looked new and the whole course was downloaded again.
"""
import os
import unittest
from tempfile import TemporaryDirectory

from intuition import dashboard


def temp_root():
    """A scratch download root.

    Cleanup errors are ignored: State opens SQLite stores whose connections are
    left open, so Windows refuses to unlink the WAL files. That leak is the
    stores' own, not something rebinding introduces.
    """
    return TemporaryDirectory(ignore_cleanup_errors=True)


class TestRebindRoot(unittest.TestCase):
    def test_rebind_repoints_ledger_and_cache(self):
        with temp_root() as first, temp_root() as second:
            state = dashboard.State(first)
            state.ledger.record("CE2003/a.pdf", "d1", None, 1)
            state.plan = [{"path": "stale"}]

            state.rebind_root(second)

            self.assertEqual(state.download_root, os.path.abspath(second))
            self.assertTrue(state.ledger.path.startswith(os.path.abspath(second)))
            self.assertIsNone(state.ledger.get("CE2003/a.pdf"))
            self.assertEqual(state.plan, [], "a plan for the old root is meaningless")

    def test_rebind_makes_the_root_absolute(self):
        """A relative value would otherwise resolve against the server's cwd."""
        with temp_root() as first:
            state = dashboard.State(first)
            state.rebind_root(os.path.join(first, "nested", "..", "nested"))
            self.assertTrue(os.path.isabs(state.download_root))
            self.assertEqual(state.download_root,
                             os.path.join(os.path.abspath(first), "nested"))

    def test_rebind_to_the_same_root_keeps_existing_state(self):
        with temp_root() as first:
            state = dashboard.State(first)
            state.ledger.record("CE2003/a.pdf", "d1", None, 1)
            state.plan = [{"path": "keep"}]

            state.rebind_root(first)

            self.assertIsNotNone(state.ledger.get("CE2003/a.pdf"))
            self.assertEqual(state.plan, [{"path": "keep"}])

    def test_rebind_does_not_deadlock(self):
        """note() takes the same lock, so rebind must not hold it while logging."""
        with temp_root() as first, temp_root() as second:
            state = dashboard.State(first)
            state.rebind_root(second)  # would hang forever if the lock were held
            self.assertTrue(any("Sync folder set to" in line for line in state.log))


if __name__ == "__main__":
    unittest.main()
