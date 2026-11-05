"""Проверки сторожа прогресса wait-done.sh (--log, --stall-s).

Запуск: `python3 -I scripts/tests/test_wait_done.py` или через pytest.
"""

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

WAIT_DONE = Path(__file__).resolve().parents[1] / "wait-done.sh"
# Замер на станции (32 ядра), 5 прогонов: запуск wait-done.sh до установки
# наблюдения и выход по done — 0,03-0,05 с; узел Jenkins медленнее на ядро
# в 4,5 раза: 0,23 с; с двойным запасом 0,5 с. Порог сторожа в тестах — 1 с
# (меньше целого числа секунд --stall-s не задать), предел вызова — 10 с:
# предохранитель от зависания, а не время ожидания события.
STALL_S = 1
LIMIT_S = 10


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
            timeout=LIMIT_S,
        )

    def test_stall_without_log_file(self) -> None:
        t0 = time.monotonic()
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 125, r.stderr)
        self.assertRegex(r.stdout, rf"^stall \d+ {self.run_dir}$")
        self.assertGreaterEqual(time.monotonic() - t0, STALL_S - 0.2)

    def test_stall_when_log_stops_growing(self) -> None:
        self.log.write_text("начало\n", encoding="utf-8")
        time.sleep(1.2)  # журнал старше порога: отсчёт от его mtime
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 125, r.stderr)

    def test_growing_log_keeps_waiting_until_done(self) -> None:
        def writer() -> None:
            # Рост журнала на 0,4 с реже порога, всего дольше порога в 2 раза.
            for i in range(5):
                with self.log.open("a", encoding="utf-8") as f:
                    f.write(f"строка {i}\n")
                time.sleep(0.4)
            (self.run_dir / "done").write_text("7\n", encoding="utf-8")

        th = threading.Thread(target=writer)
        th.start()
        self.addCleanup(th.join)
        r = self.wait("--log", str(self.log), "--stall-s", str(STALL_S))
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn(f"done rc=7 {self.run_dir}", r.stdout)

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


if __name__ == "__main__":
    unittest.main()
