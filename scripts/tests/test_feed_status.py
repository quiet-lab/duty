"""Статус задания-ленты не влияет на ворота: `python3 -I scripts/tests/test_feed_status.py`."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import gates

# Ключ -I убирает каталог скрипта из sys.path, поэтому branches.py подключается по пути.
_spec = importlib.util.spec_from_file_location("branches", SCRIPTS / "branches.py")
branches = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branches)

GATE = {
    "key": "dev2/qms/PR-1",
    "name": "dev2/qms/PR-1",
    "state": "SUCCESSFUL",
    "dateAdded": 100,
    "url": "",
}
FEED_RUNNING = {
    "key": "feed-x/qms-1",
    "name": "Feed build PR-1",
    "state": "INPROGRESS",
    "dateAdded": 200,
    "url": "",
}
FEED_FAILED = {
    "key": "t-feed-x/qms-1",
    "name": "Feed build PR-1",
    "state": "FAILED",
    "dateAdded": 300,
    "url": "",
}


MARKERS = ["feed-x/", "Feed build PR-"]


class _Cfg:
    def __init__(self, markers):
        self.markers = markers

    def get(self, key, default=None):
        return self.markers if key == "jenkins.feed_markers" else default


class FeedStatusTest(unittest.TestCase):
    def setUp(self):
        self._saved = gates._CFG
        gates._CFG = _Cfg(MARKERS)
        # У branches свой экземпляр gates (подключён по пути).
        self._saved_b = branches.gates._CFG
        branches.gates._CFG = _Cfg(MARKERS)

    def tearDown(self):
        gates._CFG = self._saved
        branches.gates._CFG = self._saved_b

    def test_empty_markers_no_feed(self):
        gates._CFG = _Cfg([])
        self.assertFalse(gates.is_feed(FEED_RUNNING))
        self.assertEqual(
            gates.gate_summary([GATE, FEED_RUNNING], {})[0],
            "dev2/qms/PR-1: SUCCESSFUL, feed-x/qms-1: INPROGRESS",
        )

    def test_is_feed_by_key_and_name(self):
        self.assertTrue(gates.is_feed(FEED_RUNNING))
        self.assertTrue(gates.is_feed({"key": "x", "name": "Feed build PR-7"}))
        self.assertFalse(gates.is_feed(GATE))

    def test_is_feed_tolerates_null_fields(self):
        self.assertFalse(gates.is_feed({"key": None, "name": None}))

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


class NoSectionTest(unittest.TestCase):
    def test_no_bitbucket_section_is_one_line_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            env = {**os.environ, "DUTY_CONFIG_USER": str(Path(tmp) / "нет.toml")}
            for key in ("DUTY_PROJECT", "CLAUDE_PROJECT_DIR"):
                env.pop(key, None)
            done = subprocess.run(
                [sys.executable, "-I", str(SCRIPTS / "gates.py"), "--root", tmp],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(len(done.stdout.strip().splitlines()), 1)
        self.assertIn("нет раздела [bitbucket]", done.stdout)


if __name__ == "__main__":
    unittest.main()
