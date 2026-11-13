"""Проверки сторожа прогресса wait-done.sh (--log, --stall-s).

Запуск: `python3 -I scripts/tests/test_wait_done.py` или через pytest.
"""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

WAIT_DONE = Path(__file__).resolve().parents[1] / "wait-done.sh"
# Замер на станции (32 ядра), 5 прогонов: запуск wait-done.sh до установки
# наблюдения и выход по done — 0,03-0,05 с; узел Jenkins медленнее на ядро
# в 4,5 раза: 0,23 с; с двойным запасом 0,5 с. Порог сторожа в тестах — 1 с
# (меньше целого числа секунд --stall-s не задать). Срока ожидания у самих
# тестов нет: вызов завершается по событию продукта (done, stall, timeout),
# зависание ловит внешний предел прогона.
STALL_S = 1


class WaitDoneStallTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "run"
        self.run_dir.mkdir()
        self.log = self.run_dir / "out.log"

    def wait(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(WAIT_DONE), str(self.run_dir), *argv],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_stall_without_log_file(self) -> None:
        t0 = time.monotonic()
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 125, r.stderr)
        self.assertRegex(r.stdout, rf"^stall \d+ {self.run_dir}$")
        self.assertGreaterEqual(time.monotonic() - t0, STALL_S - 0.2)

    def test_stall_without_log_counts_from_meta(self) -> None:
        """Журнала нет: отсчёт от started_ms прогона, а не от запуска ожидания."""
        started = time.time_ns() // 1_000_000 - 5_000
        (self.run_dir / "meta.txt").write_text(
            f"cwd: /\nstarted_ms: {started}\n", encoding="utf-8"
        )
        r = self.wait("--log", str(self.log), "--stall-s", "3")
        self.assertEqual(r.returncode, 125, r.stderr)
        # Тишина считается от начала прогона (5 с назад), а не от запуска
        # ожидания (тогда было бы 3).
        self.assertRegex(r.stdout, rf"^stall ([5-9]|\d\d+) {self.run_dir}$")

    def test_stall_when_log_stops_growing(self) -> None:
        self.log.write_text("начало\n", encoding="utf-8")
        # Журнал старше порога: отсчёт идёт от его mtime.
        old = time.time() - 5
        os.utime(self.log, (old, old))
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 125, r.stderr)

    def test_growing_log_keeps_waiting_until_done(self) -> None:
        """Каждая запись в журнал — событие; следующая идёт после показа прежней."""
        self.log.write_text("строка 0\n", encoding="utf-8")
        (self.run_dir / ".follow-off").write_text("0\n", encoding="utf-8")
        (self.run_dir / ".follow-start").write_text(f"{int(time.time())}\n")
        p = subprocess.Popen(
            [
                "bash",
                str(WAIT_DONE),
                str(self.run_dir),
                "--log",
                str(self.log),
                "--stall-s",
                str(STALL_S),
                "--follow",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(p.wait)
        self.addCleanup(p.stderr.close)
        self.addCleanup(p.stdout.close)
        # Строка в выводе — событие: наблюдение установлено, журнал прочитан.
        self.assertIn("строка 0", p.stdout.readline())
        for i in range(1, 5):
            with self.log.open("a", encoding="utf-8") as f:
                f.write(f"строка {i}\n")
            self.assertIn(f"строка {i}", p.stdout.readline())
        (self.run_dir / "done").write_text("7\n", encoding="utf-8")
        out, err = p.communicate()
        self.assertEqual(p.returncode, 7, out + err)
        self.assertIn(f"done rc=7 {self.run_dir}", out)

    def test_done_before_start_wins(self) -> None:
        (self.run_dir / "done").write_text("0\n", encoding="utf-8")
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 0)

    def test_timeout_before_stall(self) -> None:
        r = self.wait("--log", str(self.log), "--stall-s", "5", "--timeout-s", "1")
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertIn("timeout", r.stdout)

    def test_stall_before_timeout(self) -> None:
        r = self.wait("--log", str(self.log), "--stall-s", "1", "--timeout-s", "5")
        self.assertEqual(r.returncode, 125, r.stderr)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.wait("--log", str(self.log)).returncode, 2)
        self.assertEqual(self.wait("--stall-s", "5").returncode, 2)
        self.assertEqual(
            self.wait("--log", str(self.log), "--stall-s", "0").returncode, 2
        )
        other = self.run_dir.parent / "other.log"
        self.assertEqual(self.wait("--log", str(other), "--stall-s", "5").returncode, 2)

    def test_without_log_behaviour_unchanged(self) -> None:
        r = self.wait("--timeout-s", "1")
        self.assertEqual(r.returncode, 124)

    def follow_state(self) -> None:
        """Журнал с целой строкой и неоконченной; смещение — с начала журнала."""
        self.log.write_text("ready\npartial", encoding="utf-8")
        (self.run_dir / ".follow-off").write_text("0\n", encoding="utf-8")
        (self.run_dir / ".follow-start").write_text(f"{int(time.time())}\n")

    def test_timeout_flushes_unfinished_line(self) -> None:
        self.follow_state()
        r = self.wait(
            "--log", str(self.log), "--stall-s", "5", "--timeout-s", "1", "--follow"
        )
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertRegex(r.stdout, r"\d\d:\d\d:\d\d ready\n")
        self.assertRegex(r.stdout, r"\d\d:\d\d:\d\d partial\n")
        self.assertTrue(r.stdout.rstrip().endswith("timeout"), r.stdout)

    def test_stall_keeps_offset_before_unfinished_line(self) -> None:
        self.follow_state()
        r = self.wait("--log", str(self.log), "--stall-s", "1", "--follow")
        self.assertEqual(r.returncode, 125, r.stderr)
        self.assertIn("partial", r.stdout)
        # Смещение — за «ready\n»: после перезапуска строка печатается целиком.
        off = (self.run_dir / ".follow-off").read_text(encoding="utf-8").strip()
        self.assertEqual(off, str(len("ready\n")))


if __name__ == "__main__":
    unittest.main()
