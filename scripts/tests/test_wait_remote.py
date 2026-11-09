"""Проверки wait-remote.sh и ключа --follow у wait-done.sh на фальшивых прогонах.

Запуск: `python3 -I scripts/tests/test_wait_remote.py` или через pytest.
ssh подменяется сценарием в PATH, который выполняет команду на месте;
проба с настоящим ssh на localhost пропускается, если `ssh -o BatchMode=yes
localhost true` не проходит.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
WAIT_REMOTE = SCRIPTS / "wait-remote.sh"
WAIT_DONE = SCRIPTS / "wait-done.sh"
# Замер на станции (32 ядра): запуск сценария и выход по событию — до 0,1 с;
# узел Jenkins медленнее на ядро в 4,5 раза, с двойным запасом — 1 с. Паузы
# писателей в тестах (0,3 с) сделаны больше этого, предохранитель вызова — 20 с:
# защита от зависания, а не время ожидания события.
LIMIT_S = 20
PAUSE_S = 0.3

FAKE_SSH = """#!/bin/bash
while [ $# -gt 0 ]; do
    case $1 in
        -o) shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done
shift
if [ -n "${FAKE_SSH_DROP:-}" ] && [ -e "$FAKE_SSH_DROP" ] && [[ $1 == tail* ]]; then
    # В файле — число оставшихся обрывов (пусто — один).
    left=$(< "$FAKE_SSH_DROP")
    if [ "${left:-1}" -le 1 ]; then rm -f "$FAKE_SSH_DROP"; else echo $((left - 1)) > "$FAKE_SSH_DROP"; fi
    [ -n "${FAKE_SSH_QUIET:-}" ] || echo "до обрыва"
    [ -z "${FAKE_SSH_ERR:-}" ] || echo "ssh: connection refused" >&2
    exit 255
fi
exec bash -c "$1"
"""


def real_ssh_available() -> bool:
    if shutil.which("ssh") is None:
        return False
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "localhost", "true"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    return r.returncode == 0


class WaitFollowTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "run"
        self.run_dir.mkdir()
        self.log = self.run_dir / "out.log"

    def test_follow_prints_lines_as_written(self) -> None:
        self.log.write_text("старая\n", encoding="utf-8")
        proc = subprocess.Popen(
            [
                "bash",
                str(WAIT_DONE),
                str(self.run_dir),
                "--log",
                str(self.log),
                "--stall-s",
                "30",
                "--follow",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        time.sleep(PAUSE_S)
        with self.log.open("a", encoding="utf-8") as f:
            f.write("раз\n")
        first = proc.stdout.readline()
        # Строка пришла до завершения прогона: процесс ещё ждёт done.
        self.assertIsNone(proc.poll())
        self.assertRegex(first, r"^\d\d:\d\d:\d\d раз$")
        with self.log.open("a", encoding="utf-8") as f:
            f.write("два\n")
        (self.run_dir / "done").write_text("3\n", encoding="utf-8")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 3)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d два$")
        self.assertRegex(lines[1], rf"^done rc=3 {self.run_dir} за \d+ мин$")
        self.assertNotIn("старая", out)

    def test_follow_restart_does_not_lose_lines(self) -> None:
        argv = [
            "bash",
            str(WAIT_DONE),
            str(self.run_dir),
            "--log",
            str(self.log),
            "--stall-s",
            "30",
            "--follow",
        ]
        self.log.write_text("", encoding="utf-8")
        first = subprocess.run(
            [*argv, "--timeout-s", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
        )
        self.assertEqual(first.returncode, 124, first.stderr)
        # Строки, записанные между двумя вызовами (перезапуск после timeout), не теряются.
        self.log.write_text("между вызовами\n", encoding="utf-8")
        (self.run_dir / "done").write_text("0\n", encoding="utf-8")
        second = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=LIMIT_S
        )
        self.assertIn("между вызовами", second.stdout)

    def test_follow_stall_prints_unfinished_last_line(self) -> None:
        self.log.write_text("", encoding="utf-8")
        argv = [
            "bash",
            str(WAIT_DONE),
            str(self.run_dir),
            "--log",
            str(self.log),
            "--stall-s",
            "1",
            "--follow",
        ]

        def writer() -> None:
            time.sleep(PAUSE_S)
            with self.log.open("a", encoding="utf-8") as f:
                f.write("без перевода строки")

        th = threading.Thread(target=writer)
        th.start()
        self.addCleanup(th.join)
        r = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=LIMIT_S
        )
        self.assertEqual(r.returncode, 125, r.stderr)
        self.assertRegex(r.stdout, r"\d\d:\d\d:\d\d без перевода строки\n")

    def test_follow_requires_log(self) -> None:
        r = subprocess.run(
            ["bash", str(WAIT_DONE), str(self.run_dir), "--follow"],
            check=False,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
        )
        self.assertEqual(r.returncode, 2)

    def test_without_follow_log_lines_not_printed(self) -> None:
        def writer() -> None:
            time.sleep(PAUSE_S)
            self.log.write_text("тихо\n", encoding="utf-8")
            (self.run_dir / "done").write_text("0\n", encoding="utf-8")

        th = threading.Thread(target=writer)
        th.start()
        self.addCleanup(th.join)
        r = subprocess.run(
            [
                "bash",
                str(WAIT_DONE),
                str(self.run_dir),
                "--log",
                str(self.log),
                "--stall-s",
                "30",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
        )
        self.assertEqual(r.stdout, f"done rc=0 {self.run_dir}\n")


class WaitRemoteTest(unittest.TestCase):
    use_fake = True

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "remote.log"
        self.log.write_text("", encoding="utf-8")
        self.env = dict(os.environ)
        if self.use_fake:
            bindir = self.tmp / "bin"
            bindir.mkdir()
            (bindir / "ssh").write_text(FAKE_SSH, encoding="utf-8")
            (bindir / "ssh").chmod(0o755)
            self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
            self.env["FAKE_SSH_DROP"] = str(self.tmp / "drop")

    def remote(self, secs: float) -> subprocess.Popen:
        p = subprocess.Popen(["sleep", str(secs)])
        self.addCleanup(p.kill)
        # Завершившийся процесс нужно подобрать, иначе он остаётся зомби и kill -0 видит его живым.
        threading.Thread(target=p.wait, daemon=True).start()
        return p

    def start(self, pid: int, *argv: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["bash", str(WAIT_REMOTE), "localhost", str(pid), str(self.log), *argv],
            stdout=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(proc.wait)
        return proc

    def append(self, text: str) -> None:
        with self.log.open("a", encoding="utf-8") as f:
            f.write(text)

    def test_shows_progress_and_ends_with_process(self) -> None:
        self.append("в журнале\n")
        sleeper = self.remote(1.5)
        proc = self.start(sleeper.pid)
        assert proc.stdout is not None
        self.assertRegex(proc.stdout.readline(), r"^\d\d:\d\d:\d\d в журнале$")
        self.append("ход\n")
        self.assertRegex(proc.stdout.readline(), r"^\d\d:\d\d:\d\d ход$")
        self.assertIsNone(proc.poll())
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0)
        self.assertRegex(out, rf"remote done pid={sleeper.pid} localhost")

    def test_stall_exit_125(self) -> None:
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--stall-s", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 125)
        self.assertRegex(out, r"stall \d+ localhost")

    def test_timeout_exit_124(self) -> None:
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--timeout-s", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 124)
        self.assertIn("timeout", out)

    def test_bad_pid(self) -> None:
        r = subprocess.run(
            ["bash", str(WAIT_REMOTE), "localhost", "abc", "x"],
            check=False,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
        )
        self.assertEqual(r.returncode, 2)


class WaitRemoteFakeSshTest(WaitRemoteTest):
    def test_reconnect_after_drop_while_process_alive(self) -> None:
        (self.tmp / "drop").write_text("", encoding="utf-8")
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("до обрыва", out)
        self.assertIn("обрыв связи", out)
        self.assertRegex(out, r"remote done")

    def drops(self, count: int) -> None:
        (self.tmp / "drop").write_text(f"{count}\n", encoding="utf-8")

    def test_attempt_resets_after_lines_arrive(self) -> None:
        # Два обрыва подряд, между ними строки журнала: счёт попыток начинается заново.
        self.drops(2)
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertRegex(out, r"remote done")

    def test_max_reconnects_exhausted_exit_3(self) -> None:
        self.env["FAKE_SSH_QUIET"] = "1"
        self.drops(5)
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 3, out)
        self.assertIn("связь с localhost потеряна", out)

    def test_foreign_process_is_alive_not_done(self) -> None:
        # pid 1 чужой (kill -0 даёт EPERM), но /proc/1 есть: процесс жив.
        self.env["FAKE_SSH_QUIET"] = "1"
        self.drops(1)
        proc = self.start(1, "--reconnect-s", "0", "--max-reconnects", "0")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 3, out)
        self.assertNotIn("remote done", out)

    def test_ssh_stderr_is_not_log_line(self) -> None:
        self.env["FAKE_SSH_QUIET"] = "1"
        self.env["FAKE_SSH_ERR"] = "1"
        self.drops(1)
        sleeper = self.remote(2)
        proc = subprocess.Popen(
            [
                "bash",
                str(WAIT_REMOTE),
                "localhost",
                str(sleeper.pid),
                str(self.log),
                "--reconnect-s",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(proc.kill)
        out, err = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("connection refused", out)
        self.assertIn("connection refused", err)

    def test_log_path_forms(self) -> None:
        # «~/» раскрывает узел; пробелы и кавычки в пути не ломают команду.
        home = self.tmp / "home"
        home.mkdir()
        (home / "a b'c.log").write_text("в тильде\n", encoding="utf-8")
        self.env["HOME"] = str(home)
        for path in ("~/a b'c.log", str(home / "a b'c.log")):
            with self.subTest(path=path):
                sleeper = self.remote(1.5)
                proc = subprocess.Popen(
                    ["bash", str(WAIT_REMOTE), "localhost", str(sleeper.pid), path],
                    stdout=subprocess.PIPE,
                    text=True,
                    env=self.env,
                )
                self.addCleanup(proc.kill)
                out, _ = proc.communicate(timeout=LIMIT_S)
                self.assertRegex(out, r"\d\d:\d\d:\d\d в тильде")

    def test_term_in_reconnect_pause_leaves_no_orphans(self) -> None:
        self.env["FAKE_SSH_QUIET"] = "1"
        self.drops(1)
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--timeout-s", "100", "--reconnect-s", "60")
        assert proc.stdout is not None
        self.assertIn("обрыв связи", proc.stdout.readline())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 143)
        left = subprocess.run(
            ["pgrep", "-f", str(self.log)], check=False, capture_output=True
        )
        self.assertEqual(left.returncode, 1, left.stdout)


@unittest.skipUnless(
    real_ssh_available(), "ssh на localhost по ключу недоступен (BatchMode)"
)
class WaitRemoteRealSshTest(WaitRemoteTest):
    use_fake = False


if __name__ == "__main__":
    unittest.main()
