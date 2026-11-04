"""Проверки wait-remote.sh и ключа --follow у wait-done.sh на фальшивых прогонах.

Запуск: `python3 -I scripts/tests/test_wait_remote.py` или через pytest.
ssh подменяется сценарием в PATH, который выполняет команду на месте;
проба с настоящим ssh на localhost пропускается, если `ssh -o BatchMode=yes
localhost true` не проходит.
"""

import os
import shutil
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
    rm -f "$FAKE_SSH_DROP"
    echo "до обрыва"
    exit 255
fi
exec bash -c "$1"
"""


def real_ssh_available() -> bool:
    if shutil.which("ssh") is None:
        return False
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "localhost", "true"],
                       check=False, capture_output=True, timeout=10)
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
        proc = subprocess.Popen(["bash", str(WAIT_DONE), str(self.run_dir), "--log", str(self.log),
                                 "--stall-s", "30", "--follow"],
                                stdout=subprocess.PIPE, text=True)
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

    def test_follow_requires_log(self) -> None:
        r = subprocess.run(["bash", str(WAIT_DONE), str(self.run_dir), "--follow"],
                           check=False, capture_output=True, text=True, timeout=LIMIT_S)
        self.assertEqual(r.returncode, 2)

    def test_without_follow_log_lines_not_printed(self) -> None:
        def writer() -> None:
            time.sleep(PAUSE_S)
            self.log.write_text("тихо\n", encoding="utf-8")
            (self.run_dir / "done").write_text("0\n", encoding="utf-8")

        th = threading.Thread(target=writer)
        th.start()
        self.addCleanup(th.join)
        r = subprocess.run(["bash", str(WAIT_DONE), str(self.run_dir), "--log", str(self.log), "--stall-s", "30"],
                           check=False, capture_output=True, text=True, timeout=LIMIT_S)
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
        proc = subprocess.Popen(["bash", str(WAIT_REMOTE), "localhost", str(pid), str(self.log), *argv],
                                stdout=subprocess.PIPE, text=True, env=self.env)
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
        r = subprocess.run(["bash", str(WAIT_REMOTE), "localhost", "abc", "x"],
                           check=False, capture_output=True, text=True, timeout=LIMIT_S)
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


@unittest.skipUnless(real_ssh_available(), "ssh на localhost по ключу недоступен (BatchMode)")
class WaitRemoteRealSshTest(WaitRemoteTest):
    use_fake = False


if __name__ == "__main__":
    unittest.main()
