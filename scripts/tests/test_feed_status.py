"""Статус «Лента сборок PR-…» не влияет на ворота: `python3 -I scripts/tests/test_feed_status.py`."""

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import gates  # noqa: E402

# Ключ -I убирает каталог скрипта из sys.path, поэтому branches.py подключается по пути.
_spec = importlib.util.spec_from_file_location("branches", SCRIPTS / "branches.py")
branches = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branches)

GATE = {"key": "dev2/qms/PR-1", "name": "dev2/qms/PR-1", "state": "SUCCESSFUL", "dateAdded": 100, "url": ""}
FEED_RUNNING = {"key": "pr-feed/qms-1", "name": "Лента сборок PR-1", "state": "INPROGRESS", "dateAdded": 200, "url": ""}
FEED_FAILED = {"key": "t-pr-feed/qms-1", "name": "Лента сборок PR-1", "state": "FAILED", "dateAdded": 300, "url": ""}


class FeedStatusTest(unittest.TestCase):
    def test_is_feed_by_key_and_name(self):
        self.assertTrue(gates.is_feed(FEED_RUNNING))
        self.assertTrue(gates.is_feed({"key": "x", "name": "Лента сборок PR-7"}))
        self.assertFalse(gates.is_feed(GATE))

    def test_gate_summary_ignores_newer_feed(self):
        builds, gate = gates.gate_summary([GATE, FEED_RUNNING], {})
        self.assertEqual(builds, "dev2/qms/PR-1: SUCCESSFUL")
        self.assertEqual(gate, "—")

    def test_gate_summary_only_feed_is_no_builds(self):
        self.assertEqual(gates.gate_summary([FEED_RUNNING], {}), ("нет сборок", "—"))

    def _green(self, values):
        # У branches свой экземпляр модуля gates (подключён по пути).
        original = branches.gates.bb
        branches.gates.bb = lambda path: {"values": values}
        try:
            return branches.green("0" * 40)
        finally:
            branches.gates.bb = original

    def test_green_ignores_feed_inprogress_and_failed(self):
        self.assertEqual(self._green([GATE, FEED_RUNNING]), "зелёные")
        self.assertEqual(self._green([GATE, FEED_FAILED]), "зелёные")

    def test_green_still_sees_gate_failure(self):
        failed = dict(GATE, state="FAILED")
        self.assertEqual(self._green([failed, FEED_RUNNING]), "красные")

    def test_green_only_feed_is_no_builds(self):
        self.assertEqual(self._green([FEED_RUNNING]), "нет сборок")


if __name__ == "__main__":
    unittest.main()
