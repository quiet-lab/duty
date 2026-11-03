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
import busy


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
        code, _ = run(
            "claim",
            "dev-stand-1",
            "--holder",
            "a",
            "--note",
            "прогон",
            "--pid",
            str(os.getpid()),
        )
        self.assertEqual(code, 0)
        data = json.loads(
            (Path(self.tmp.name) / "dev-stand-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["holder"], "a")
        self.assertEqual(data["pid"], os.getpid())
        self.assertIsInstance(data["since_ms"], int)

    def test_claim_without_pid_is_usage_error(self) -> None:
        code, out = run("claim", "dev-stand-1", "--holder", "a")
        self.assertEqual(code, 2)
        self.assertIn('--pid "$PPID"', out)
        self.assertFalse((Path(self.tmp.name) / "dev-stand-1.json").exists())

    def test_claim_with_dead_pid_is_usage_error(self) -> None:
        code, out = run(
            "claim", "dev-stand-1", "--holder", "a", "--pid", str(dead_pid())
        )
        self.assertEqual(code, 2)
        self.assertIn("не жив", out)
        self.assertFalse((Path(self.tmp.name) / "dev-stand-1.json").exists())

    def test_repeat_claim_by_live_holder_is_busy(self) -> None:
        run(
            "claim",
            "station",
            "--holder",
            "a",
            "--note",
            "замер",
            "--pid",
            str(os.getpid()),
        )
        code, out = run("claim", "station", "--holder", "b", "--pid", str(os.getpid()))
        self.assertEqual(code, 3)
        self.assertIn("занято: a, замер", out)
        self.assertEqual(run("check", "station")[0], 3)

    def test_stale_pid_is_replaced(self) -> None:
        # Устаревшая отметка — файл, оставленный держателем, который завершился.
        stale = {
            "resource": "studio-oktell-ocp",
            "holder": "old",
            "task": "",
            "pid": dead_pid(),
            "since_ms": 0,
            "note": "",
        }
        (Path(self.tmp.name) / "studio-oktell-ocp.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        self.assertEqual(run("check", "studio-oktell-ocp")[0], 0)
        code, out = run(
            "claim", "studio-oktell-ocp", "--holder", "new", "--pid", str(os.getpid())
        )
        self.assertEqual(code, 0)
        self.assertIn("устаревшая", out)
        data = json.loads(
            (Path(self.tmp.name) / "studio-oktell-ocp.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["holder"], "new")

    def test_release_foreign_needs_force(self) -> None:
        run("claim", "dev-stand-2", "--holder", "a", "--pid", str(os.getpid()))
        code, _ = run("release", "dev-stand-2", "--holder", "b")
        self.assertEqual(code, 3)
        self.assertTrue((Path(self.tmp.name) / "dev-stand-2.json").exists())
        code, out = run("release", "dev-stand-2", "--holder", "b", "--force")
        self.assertEqual(code, 0)
        self.assertIn("предупреждение", out)
        self.assertFalse((Path(self.tmp.name) / "dev-stand-2.json").exists())

    def test_release_own(self) -> None:
        run("claim", "station", "--holder", "a", "--pid", str(os.getpid()))
        self.assertEqual(run("release", "station", "--holder", "a")[0], 0)
        self.assertEqual(run("check", "station")[0], 0)

    def test_list(self) -> None:
        self.assertIn("отметок нет", run("list")[1])
        run(
            "claim",
            "dev-stand-1",
            "--holder",
            "a",
            "--task",
            "t.md",
            "--pid",
            str(os.getpid()),
        )
        stale = {
            "resource": "station",
            "holder": "b",
            "task": "",
            "pid": dead_pid(),
            "since_ms": 0,
            "note": "",
        }
        (Path(self.tmp.name) / "station.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        _, table = run("list")
        self.assertIn("| dev-stand-1 | a | t.md |", table)
        self.assertIn("нет (устарела)", table)
        rows = json.loads(run("list", "--json")[1])
        self.assertEqual(
            {r["resource"]: r["alive"] for r in rows},
            {"dev-stand-1": True, "station": False},
        )

    def test_bad_name_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            run("check", "../x")

    def test_race_over_stale_mark_has_one_winner(self) -> None:
        """Устаревшую отметку снимают все участники гонки: снятие не должно задеть
        отметку победителя, поставленную между чтением и удалением."""
        root = Path(self.tmp.name)
        path = root / "res.json"
        script = Path(busy.__file__).resolve()
        for round_no in range(12):
            path.write_text(
                json.dumps(
                    {
                        "resource": "res",
                        "holder": "мёртвый",
                        "pid": dead_pid(),
                        "since_ms": 1,
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "DUTY_BUSY_DIR": self.tmp.name}
            procs = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        "claim",
                        "res",
                        "--holder",
                        f"h{i}",
                        "--pid",
                        str(os.getpid()),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                for i in range(8)
            ]
            codes = [p.wait() for p in procs]
            for p in procs:
                if p.stdout:
                    p.stdout.close()
            self.assertEqual(
                sorted(codes), [0] + [busy.EXIT_BUSY] * 7, f"круг {round_no}: {codes}"
            )
            winner = next(f"h{i}" for i, c in enumerate(codes) if c == 0)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["holder"], winner
            )
            path.unlink()

    def test_stale_removal_spares_mark_set_after_reading(self) -> None:
        """Та же гонка без случайности: сосед ставит отметку между чтением и снятием."""
        path = Path(self.tmp.name) / "res.json"
        stale = {
            "resource": "res",
            "holder": "мёртвый",
            "pid": dead_pid(),
            "since_ms": 1,
        }
        fresh = {
            "resource": "res",
            "holder": "победитель",
            "pid": os.getpid(),
            "since_ms": 2,
        }
        path.write_text(json.dumps(stale), encoding="utf-8")
        real_read = busy.read_mark
        reads: list[Path] = []

        def read(where: Path) -> dict | None:
            rec = real_read(where)
            if not reads:
                path.write_text(json.dumps(fresh), encoding="utf-8")
            reads.append(where)
            return rec

        busy.read_mark = read  # type: ignore[assignment]
        self.addCleanup(setattr, busy, "read_mark", real_read)
        code, _ = run("claim", "res", "--holder", "x", "--pid", str(os.getpid()))
        self.assertEqual(code, busy.EXIT_BUSY)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), fresh)


if __name__ == "__main__":
    unittest.main()
