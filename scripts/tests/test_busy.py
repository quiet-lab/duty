"""Проверки реестра занятости: запуск `python3 -I scripts/tests/test_busy.py`."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import busy  # noqa: E402


def run(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = busy.main(list(argv))
    return code, buf.getvalue()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class BusyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DUTY_BUSY_DIR"] = self.tmp.name

    def test_claim_creates_mark(self) -> None:
        code, _ = run("claim", "dev-stand", "--holder", "a", "--note", "прогон", "--pid", str(os.getpid()))
        self.assertEqual(code, 0)
        data = json.loads((Path(self.tmp.name) / "dev-stand.json").read_text(encoding="utf-8"))
        self.assertEqual(data["holder"], "a")
        self.assertEqual(data["pid"], os.getpid())
        self.assertIsInstance(data["since_ms"], int)

    def test_claim_without_pid_is_usage_error(self) -> None:
        code, out = run("claim", "dev-stand", "--holder", "a")
        self.assertEqual(code, 2)
        self.assertIn('--pid "$PPID"', out)
        self.assertFalse((Path(self.tmp.name) / "dev-stand.json").exists())

    def test_claim_with_dead_pid_is_usage_error(self) -> None:
        code, out = run("claim", "dev-stand", "--holder", "a", "--pid", str(dead_pid()))
        self.assertEqual(code, 2)
        self.assertIn("не жив", out)
        self.assertFalse((Path(self.tmp.name) / "dev-stand.json").exists())

    def test_repeat_claim_by_live_holder_is_busy(self) -> None:
        run("claim", "station", "--holder", "a", "--note", "замер", "--pid", str(os.getpid()))
        code, out = run("claim", "station", "--holder", "b", "--pid", str(os.getpid()))
        self.assertEqual(code, 3)
        self.assertIn("занято: a, замер", out)
        self.assertEqual(run("check", "station")[0], 3)

    def test_stale_pid_is_replaced(self) -> None:
        # Устаревшая отметка — файл, оставленный держателем, который завершился.
        stale = {"resource": "pr-train", "holder": "old", "task": "", "pid": dead_pid(),
                 "since_ms": 0, "note": ""}
        (Path(self.tmp.name) / "pr-train.json").write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(run("check", "pr-train")[0], 0)
        code, out = run("claim", "pr-train", "--holder", "new", "--pid", str(os.getpid()))
        self.assertEqual(code, 0)
        self.assertIn("устаревшая", out)
        data = json.loads((Path(self.tmp.name) / "pr-train.json").read_text(encoding="utf-8"))
        self.assertEqual(data["holder"], "new")

    def test_release_foreign_needs_force(self) -> None:
        run("claim", "docs-tree", "--holder", "a", "--pid", str(os.getpid()))
        code, _ = run("release", "docs-tree", "--holder", "b")
        self.assertEqual(code, 3)
        self.assertTrue((Path(self.tmp.name) / "docs-tree.json").exists())
        code, out = run("release", "docs-tree", "--holder", "b", "--force")
        self.assertEqual(code, 0)
        self.assertIn("предупреждение", out)
        self.assertFalse((Path(self.tmp.name) / "docs-tree.json").exists())

    def test_release_own(self) -> None:
        run("claim", "station", "--holder", "a", "--pid", str(os.getpid()))
        self.assertEqual(run("release", "station", "--holder", "a")[0], 0)
        self.assertEqual(run("check", "station")[0], 0)

    def test_list(self) -> None:
        self.assertIn("отметок нет", run("list")[1])
        run("claim", "dev-stand", "--holder", "a", "--task", "t.md", "--pid", str(os.getpid()))
        stale = {"resource": "station", "holder": "b", "task": "", "pid": dead_pid(),
                 "since_ms": 0, "note": ""}
        (Path(self.tmp.name) / "station.json").write_text(json.dumps(stale), encoding="utf-8")
        _, table = run("list")
        self.assertIn("| dev-stand | a | t.md |", table)
        self.assertIn("нет (устарела)", table)
        rows = json.loads(run("list", "--json")[1])
        self.assertEqual({r["resource"]: r["alive"] for r in rows}, {"dev-stand": True, "station": False})

    def test_bad_name_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            run("check", "../x")


if __name__ == "__main__":
    unittest.main()
