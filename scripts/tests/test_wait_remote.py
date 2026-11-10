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

FAKE_SSH = r"""#!/bin/bash
while [ $# -gt 0 ]; do
    case $1 in
        -o) shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done
shift
# Вывод оболочки узла до команды (.bashrc): строка перед шапкой.
[[ -z "${FAKE_SSH_NOISE:-}" || $1 != *"exec tail --pid"* ]] || echo "$FAKE_SSH_NOISE"
if [ -n "${FAKE_SSH_DROP:-}" ] && [ -e "$FAKE_SSH_DROP" ] && [[ $1 == *"exec tail --pid"* ]]; then
    # В файле — строка на подключение с обрывом: «<строк до обрыва>[+<дописать>]»;
    # дописанные строки попадают в журнал FAKE_SSH_LOG до запуска команды;
    # «!» вместо «+» — журнал перед этим усекается; «-» в конце — последняя
    # строка до обрыва передаётся без перевода строки.
    mapfile -t spec < "$FAKE_SSH_DROP"
    if [ "${#spec[@]}" -le 1 ]; then rm -f "$FAKE_SSH_DROP"; else printf '%s
' "${spec[@]:1}" > "$FAKE_SSH_DROP"; fi
    cur=${spec[0]:-0}
    cut=0
    if [[ $cur == *- ]]; then cut=1; cur=${cur%-}; fi
    if [[ $cur == *'!'* ]]; then : > "$FAKE_SSH_LOG"; cur=${cur/'!'/+}; fi
    lines=${cur%%+*}
    if [[ $cur == *+* ]]; then
        for ((i = 0; i < ${cur#*+}; i++)); do echo "дописано $i" >> "$FAKE_SSH_LOG"; done
    fi
    # Настоящая команда на месте: шапка и строки tail, затем обрыв.
    exec 3< <(exec bash -c "$1")
    remote=$!
    IFS= read -r line <&3 && echo "$line"
    for ((i = 0; i < lines; i++)); do
        IFS= read -r line <&3 || break
        if (( cut && i == lines - 1 )); then printf '%s' "$line"; else echo "$line"; fi
    done
    kill "$remote" 2> /dev/null
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
            self.env["FAKE_SSH_LOG"] = str(self.log)

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
    def drops(self, *spec: int | str) -> None:
        """Подключения с обрывом: строк журнала до обрыва, «N+K» — дописать K строк."""
        (self.tmp / "drop").write_text(
            "".join(f"{item}\n" for item in spec), encoding="utf-8"
        )

    def test_reconnect_after_drop_while_process_alive(self) -> None:
        self.append("до обрыва\n")
        self.drops(1)
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("до обрыва"), 1, out)
        self.assertIn("обрыв связи", out)
        self.assertRegex(out, r"remote done")

    def test_attempt_resets_after_new_lines_arrive(self) -> None:
        # Каждое подключение с обрывом дописывает строку и передаёт её: новая
        # строка сбрасывает попытки, два обрыва при --max-reconnects 1 их не исчерпывают.
        self.drops("1+1", "1+1")
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertRegex(out, r"remote done")

    def test_reconnect_without_new_lines_does_not_reset_attempts(self) -> None:
        # Строка журнала показана до первого обрыва; переподключения её не повторяют
        # и новых строк не дают: счёт попыток не сбрасывается.
        self.append("одна\n")
        self.drops(1, *[0] * 30)
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 3, out)
        self.assertEqual(out.count("одна"), 1, out)
        self.assertIn("связь с localhost потеряна, попыток 1", out)

    def test_lines_after_drop_continue_without_repeat(self) -> None:
        # Показано 20 из 100 строк, обрыв, в журнале +5 строк: после
        # переподключения приходят ровно эти 5 строк, и рост засчитывается —
        # второй обрыв при --max-reconnects 1 не исчерпывает попыток.
        self.append("".join(f"строка {i}\n" for i in range(1, 101)))
        self.drops(20, "5+5")
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        shown = [
            line.split(" ", 1)[1]
            for line in out.splitlines()
            if " строка " in line or " дописано " in line
        ]
        expected = [f"строка {i}" for i in range(81, 101)] + [
            f"дописано {i}" for i in range(5)
        ]
        self.assertEqual(shown, expected, out)

    def test_truncated_log_after_drop_shown_from_start(self) -> None:
        # Показаны последние 20 из 30 строк, обрыв, журнал усечён и в нём
        # 3 новые строки: после переподключения приходят ровно они.
        self.append("".join(f"строка {i}\n" for i in range(1, 31)))
        self.drops(20, "3!3")
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        shown = [
            line.split(" ", 1)[1]
            for line in out.splitlines()
            if " строка " in line or " дописано " in line
        ]
        expected = [f"строка {i}" for i in range(11, 31)] + [
            f"дописано {i}" for i in range(3)
        ]
        self.assertEqual(shown, expected, out)
        self.assertIn("журнал усечён", out)

    def test_partial_line_on_drop_printed_once(self) -> None:
        # Обрыв посреди строки при живом процессе: обрывок не печатается,
        # после переподключения строка приходит целиком один раз.
        self.append("первая\nобрывок\n")
        self.drops("2-")
        sleeper = self.remote(2)
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("обрывок"), 1, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d обрывок\n")

    def test_partial_last_line_printed_when_process_ends(self) -> None:
        # Последняя строка журнала без перевода строки не теряется.
        self.append("первая\nхвост")
        sleeper = self.remote(1.5)
        proc = self.start(sleeper.pid)
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("хвост"), 1, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d хвост\nremote done")

    def test_shell_output_before_header_is_not_log(self) -> None:
        # Строка оболочки узла до шапки не занимает её место: шапка не печатается
        # строкой журнала, после обрыва повтора нет, оболочка — в stderr.
        self.env["FAKE_SSH_NOISE"] = "привет из bashrc"
        self.append("одна\n")
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
        self.assertNotIn("wait-remote", out)
        self.assertNotIn("привет из bashrc", out)
        self.assertIn("вывод оболочки узла: привет из bashrc", err)
        self.assertEqual(out.count("одна"), 1, out)

    def age_log(self, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(self.log, (old, old))

    def test_restart_keeps_stall_from_log_mtime(self) -> None:
        # Перезапуск ожидания давно идущего процесса (pid 1) при неподвижном
        # журнале: отсчёт сторожа — от времени изменения журнала, а прежние
        # строки первого подключения ростом не считаются.
        self.append("старая\n")
        self.age_log(120)
        proc = self.start(1, "--stall-s", "60")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 125, out)
        self.assertIn("старая", out)
        self.assertRegex(out, r"stall 1[12]\d localhost")

    def test_old_log_of_new_process_is_not_stall(self) -> None:
        # Журнал изменён вчера, процесс только что запущен и молчит: отсчёт —
        # от запуска процесса, застоя нет, ожидание кончается по --timeout-s.
        self.append("вчерашняя\n")
        self.age_log(86400)
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, "--stall-s", "900", "--timeout-s", "2")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 124, out)
        self.assertNotIn("stall", out)

    def test_reconnects_do_not_reset_stall(self) -> None:
        # Частые обрывы без роста журнала: пауза переподключения не обнуляет
        # отсчёт сторожа, застой обнаруживается раньше, чем кончатся попытки
        # (без исправления — код 3 после всех обрывов).
        self.drops(*[0] * 10)
        proc = self.start(
            1, "--stall-s", "2", "--reconnect-s", "1", "--max-reconnects", "9"
        )
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 125, out)

    def test_foreign_process_is_alive_not_done(self) -> None:
        # pid 1 чужой (kill -0 даёт EPERM), но /proc/1 есть: процесс жив.
        self.drops(0)
        proc = self.start(1, "--reconnect-s", "0", "--max-reconnects", "0")
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 3, out)
        self.assertNotIn("remote done", out)

    def test_ssh_stderr_is_not_log_line(self) -> None:
        self.env["FAKE_SSH_ERR"] = "1"
        self.drops(0)
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

    def check_term_in_pause(self, *extra: str) -> None:
        # Пауза — sleep с числом секунд, которого нет у других процессов.
        pause = 5000 + os.getpid() % 1000
        self.drops(0)
        sleeper = self.remote(15)
        proc = self.start(sleeper.pid, *extra, "--reconnect-s", str(pause))
        assert proc.stdout is not None
        self.assertIn("обрыв связи", proc.stdout.readline())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 143)
        left = subprocess.run(
            ["pgrep", "-fx", f"sleep {pause}"], check=False, capture_output=True
        )
        self.assertEqual(left.returncode, 1, left.stdout)

    def test_term_in_reconnect_pause_leaves_no_orphans(self) -> None:
        self.check_term_in_pause("--timeout-s", "100")

    def test_term_in_reconnect_pause_without_timeout(self) -> None:
        self.check_term_in_pause()


@unittest.skipUnless(
    real_ssh_available(), "ssh на localhost по ключу недоступен (BatchMode)"
)
class WaitRemoteRealSshTest(WaitRemoteTest):
    use_fake = False


if __name__ == "__main__":
    unittest.main()
