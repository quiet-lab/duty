"""Проверки реестра занятости: запуск `python3 -I scripts/tests/test_busy.py`."""

import contextlib
import fcntl
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
            "resource": "db-main",
            "holder": "old",
            "task": "",
            "pid": dead_pid(),
            "since_ms": 0,
            "note": "",
        }
        (Path(self.tmp.name) / "db-main.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        self.assertEqual(run("check", "db-main")[0], 0)
        code, out = run(
            "claim", "db-main", "--holder", "new", "--pid", str(os.getpid())
        )
        self.assertEqual(code, 0)
        self.assertIn("устаревшая", out)
        data = json.loads(
            (Path(self.tmp.name) / "db-main.json").read_text(encoding="utf-8")
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

    def test_neighbor_does_not_see_unwritten_mark(self) -> None:
        """Сосед читает реестр в момент между созданием отметки и записью её
        содержимого: целой или пустой отметки там быть не должно."""
        path = Path(self.tmp.name) / "res.json"
        seen: list[object] = []
        real_dump = json.dump

        def dump(obj: object, fp: object, **kw: object) -> None:
            seen.append(busy.read_mark(path))
            real_dump(obj, fp, **kw)  # type: ignore[arg-type]

        busy.json.dump = dump  # type: ignore[assignment]
        self.addCleanup(setattr, busy.json, "dump", real_dump)
        code, _ = run("claim", "res", "--holder", "a", "--pid", str(os.getpid()))
        self.assertEqual(code, 0)
        self.assertEqual(seen, [None])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["holder"], "a")
        self.assertEqual(list(Path(self.tmp.name).glob(".*.tmp")), [])

    def test_release_holds_removal_lock(self) -> None:
        """release читает и удаляет под замком: сосед, снимающий устаревшую
        отметку, в это время замок получить не может."""
        root = Path(self.tmp.name)
        run("claim", "res", "--holder", "a", "--pid", str(os.getpid()))
        real_read = busy.read_mark
        got: list[bool] = []

        def read(where: Path) -> dict | None:
            fd = os.open(root / ".removal.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got.append(True)
            except BlockingIOError:
                got.append(False)
            finally:
                os.close(fd)
            return real_read(where)

        busy.read_mark = read  # type: ignore[assignment]
        self.addCleanup(setattr, busy, "read_mark", real_read)
        self.assertEqual(run("release", "res", "--holder", "a")[0], 0)
        self.assertEqual(got, [False])
        self.assertFalse((root / "res.json").exists())

    def test_lock_wait_is_bounded(self) -> None:
        """Замок занят зависшим держателем: claim и release завершаются кодом
        «занято» с причиной, а не висят."""
        root = Path(self.tmp.name)
        (root / "res.json").write_text(
            json.dumps(
                {"resource": "res", "holder": "м", "pid": dead_pid(), "since_ms": 1}
            ),
            encoding="utf-8",
        )
        fd = os.open(root / ".removal.lock", os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        real = busy.LOCK_WAIT_S
        busy.LOCK_WAIT_S = 0.05
        self.addCleanup(setattr, busy, "LOCK_WAIT_S", real)
        for argv in (
            ("claim", "res", "--holder", "x", "--pid", str(os.getpid())),
            ("release", "res"),
        ):
            code, out = run(*argv)
            self.assertEqual(code, busy.EXIT_BUSY, out)
            self.assertIn("замок снятия отметок не получен", out)
        self.assertTrue((root / "res.json").exists())

    def test_release_race_against_claim_keeps_new_mark(self) -> None:
        """release процессов и claim соседей: свежая отметка победителя не снимается чужим release."""
        root = Path(self.tmp.name)
        script = Path(busy.__file__).resolve()
        env = {**os.environ, "DUTY_BUSY_DIR": self.tmp.name}
        for round_no in range(8):
            (root / "res.json").write_text(
                json.dumps(
                    {"resource": "res", "holder": "a", "pid": dead_pid(), "since_ms": 1}
                ),
                encoding="utf-8",
            )
            cmds = [
                ["claim", "res", "--holder", f"h{i}", "--pid", str(os.getpid())]
                for i in range(4)
            ] + [["release", "res", "--holder", "a"]]
            procs = [
                subprocess.Popen(
                    [sys.executable, str(script), *c],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for c in cmds
            ]
            for p in procs:
                p.wait()
            # Победитель claim либо снят release ранее; живой отметки двух держателей нет.
            if (root / "res.json").exists():
                data = json.loads((root / "res.json").read_text(encoding="utf-8"))
                self.assertTrue(data["holder"].startswith("h"), f"круг {round_no}")
            (root / "res.json").unlink(missing_ok=True)

    def test_failed_write_removes_temp_file(self) -> None:
        root = Path(self.tmp.name)
        real_dump = json.dump

        def dump(obj: object, fp: object, **kw: object) -> None:
            raise OSError("диск заполнен")

        busy.json.dump = dump  # type: ignore[assignment]
        self.addCleanup(setattr, busy.json, "dump", real_dump)
        with self.assertRaises(OSError):
            run("claim", "res", "--holder", "a", "--pid", str(os.getpid()))
        self.assertEqual(list(root.glob(".*.tmp")), [])
        self.assertFalse((root / "res.json").exists())

    def test_release_without_mark_creates_nothing(self) -> None:
        root = Path(self.tmp.name) / "новый"
        os.environ["DUTY_BUSY_DIR"] = str(root)
        code, out = run("release", "res")
        self.assertEqual(code, 0)
        self.assertIn("отметки нет", out)
        self.assertFalse(root.exists())

    def test_ttl_marks_overdue_in_list(self) -> None:
        run(
            "claim",
            "r-ttl",
            "--holder",
            "a",
            "--ttl-min",
            "10",
            "--pid",
            str(os.getpid()),
        )
        run("claim", "r-free", "--holder", "a", "--pid", str(os.getpid()))
        path = Path(self.tmp.name) / "r-ttl.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["ttl_min"], 10)
        data["since_ms"] -= 25 * 60_000
        path.write_text(json.dumps(data), encoding="utf-8")
        _, out = run("list")
        ttl_line = next(
            line for line in out.splitlines() if line.startswith("| r-ttl ")
        )
        self.assertIn("⚠ просрочено +15 мин", ttl_line)
        free_line = next(
            line for line in out.splitlines() if line.startswith("| r-free ")
        )
        self.assertNotIn("просрочено", free_line)

    def test_ttl_must_be_positive(self) -> None:
        for value in ("0", "-5"):
            code, out = run(
                "claim",
                "r-bad",
                "--holder",
                "a",
                "--ttl-min",
                value,
                "--pid",
                str(os.getpid()),
            )
            self.assertEqual(code, 2, out)
            self.assertFalse((Path(self.tmp.name) / "r-bad.json").exists())

    def test_list_has_no_trailing_space_in_ttl_cell(self) -> None:
        run(
            "claim",
            "r-ttl",
            "--holder",
            "a",
            "--ttl-min",
            "10",
            "--pid",
            str(os.getpid()),
        )
        _, out = run("list")
        line = next(x for x in out.splitlines() if x.startswith("| r-ttl "))
        self.assertTrue(line.endswith("| 10 |"), repr(line))


if __name__ == "__main__":
    unittest.main()
