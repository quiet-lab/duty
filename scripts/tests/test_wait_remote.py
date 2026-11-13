"""Проверки wait-remote.sh и ключа --follow у wait-done.sh на фальшивых прогонах.

Запуск: `python3 -I scripts/tests/test_wait_remote.py` или через pytest.
ssh подменяется сценарием в PATH, который выполняет команду на месте;
проба с настоящим ssh на localhost пропускается, если `ssh -o BatchMode=yes
localhost true` не проходит.

Сроков ожидания в проверках нет: каждое ожидание — до события (строка
вывода, событие inotify, конец процесса); удалённый процесс завершается,
когда проверка его останавливает. Зависание ловит только внешний предел
прогона целиком (~/.claude/CLAUDE.md, «Устройство кода»). Сроки --stall-s
и --timeout-s — проверяемое поведение сценариев, а не ожидание проверки.
"""

import fcntl
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

FAKE_SSH = r"""#!/bin/bash
while [ $# -gt 0 ]; do
    case $1 in
        -o) shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done
shift
cmd=$1
# Вывод оболочки узла до команды (.bashrc): строка перед шапкой, с переводом
# строки или без него (FAKE_SSH_NOISE_NONL), — склеивается с шапкой.
if [ -n "${FAKE_SSH_NOISE:-}" ]; then
    if [ -n "${FAKE_SSH_NOISE_NONL:-}" ]; then printf '%s' "$FAKE_SSH_NOISE"; else echo "$FAKE_SSH_NOISE"; fi
fi
spec=
if [ -n "${FAKE_SSH_DROP:-}" ] && [ -e "$FAKE_SSH_DROP" ]; then
    # В файле — строка на подключение с обрывом: «<записей до обрыва>[+<дописать>]»;
    # дописанные строки попадают в журнал FAKE_SSH_LOG до запуска команды;
    # «!» вместо «+» — журнал перед этим усекается; «-» в конце — от последней
    # записи до обрыва передаётся первая половина без перевода строки; «h» —
    # обрыв посреди шапки. Шапка записью не считается.
    mapfile -t specs < "$FAKE_SSH_DROP"
    if [ "${#specs[@]}" -le 1 ]; then rm -f "$FAKE_SSH_DROP"; else printf '%s\n' "${specs[@]:1}" > "$FAKE_SSH_DROP"; fi
    spec=${specs[0]:-0}
fi
[ -n "$spec" ] || exec bash -c "$cmd"
# Команда узла — своя группа процессов: обрыв снимает её целиком
# (наблюдатель узла — в своей группе и остаётся).
if [ "$spec" = h ]; then
    exec 3< <(exec setsid bash -c "$cmd")
    remote=$!
    IFS= read -r line <&3 && printf '%s' "${line:0:${#line}/2}"
    kill -- -"$remote" 2> /dev/null
    exit 255
fi
cut=0
if [[ $spec == *- ]]; then cut=1; spec=${spec%-}; fi
if [[ $spec == *'!'* ]]; then : > "$FAKE_SSH_LOG"; spec=${spec/'!'/+}; fi
records=${spec%%+*}
if [[ $spec == *+* ]]; then
    for ((i = 0; i < ${spec#*+}; i++)); do echo "дописано $i" >> "$FAKE_SSH_LOG"; done
fi
exec 3< <(exec setsid bash -c "$cmd")
remote=$!
i=0
header=0
while { (( ! header )) || (( i < records )); } && IFS= read -r line <&3; do
    if (( ! header )); then
        [[ $line != *'wait-remote '* ]] || header=1
        echo "$line"
        continue
    fi
    i=$(( i + 1 ))
    if (( cut && i == records )); then printf '%s' "${line:0:${#line}/2}"; break; fi
    echo "$line"
done
kill -- -"$remote" 2> /dev/null
[ -z "${FAKE_SSH_ERR:-}" ] || echo "ssh: connection refused" >&2
exit 255
"""


def real_ssh_available() -> bool:
    if shutil.which("ssh") is None:
        return False
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "localhost", "true"],
        check=False,
        capture_output=True,
    )
    return r.returncode == 0


def read_until(stream, text: str) -> str:
    """Строки потока до первой, содержащей text, включительно; конец потока — ошибка."""
    got = []
    while True:
        line = stream.readline()
        if not line:
            raise AssertionError(f"поток кончился до «{text}»: {''.join(got)}")
        got.append(line)
        if text in line:
            return "".join(got)


class WaitFollowTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "run"
        self.run_dir.mkdir()
        self.log = self.run_dir / "out.log"

    def start_follow(self, argv: list[str]) -> subprocess.Popen:
        """Запуск wait-done --follow; возврат — после записи .follow-off.

        С этого события смещение в журнале закреплено: дописанное после
        него — новые строки.
        """
        mark = subprocess.Popen(
            [
                "inotifywait",
                "-e",
                "close_write",
                "--include",
                r"\.follow-off$",
                str(self.run_dir),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(mark.kill)
        assert mark.stderr is not None
        read_until(mark.stderr, "Watches established.")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        mark.wait()
        return proc

    def follow_argv(self, stall_s: str) -> list[str]:
        return [
            "bash",
            str(WAIT_DONE),
            str(self.run_dir),
            "--log",
            str(self.log),
            "--stall-s",
            stall_s,
            "--follow",
        ]

    def test_follow_prints_lines_as_written(self) -> None:
        self.log.write_text("старая\n", encoding="utf-8")
        proc = self.start_follow(self.follow_argv("30"))
        assert proc.stdout is not None
        with self.log.open("a", encoding="utf-8") as f:
            f.write("раз\n")
        first = proc.stdout.readline()
        # Строка пришла до завершения прогона: процесс ещё ждёт done.
        self.assertIsNone(proc.poll())
        self.assertRegex(first, r"^\d\d:\d\d:\d\d раз$")
        with self.log.open("a", encoding="utf-8") as f:
            f.write("два\n")
        (self.run_dir / "done").write_text("3\n", encoding="utf-8")
        out, _ = proc.communicate()
        self.assertEqual(proc.returncode, 3)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d два$")
        self.assertRegex(lines[1], rf"^done rc=3 {self.run_dir} за \d+ мин$")
        self.assertNotIn("старая", out)

    def test_follow_restart_does_not_lose_lines(self) -> None:
        argv = self.follow_argv("30")
        self.log.write_text("", encoding="utf-8")
        first = subprocess.run(
            [*argv, "--timeout-s", "1"], check=False, capture_output=True, text=True
        )
        self.assertEqual(first.returncode, 124, first.stderr)
        # Строки, записанные между двумя вызовами (перезапуск после timeout), не теряются.
        self.log.write_text("между вызовами\n", encoding="utf-8")
        (self.run_dir / "done").write_text("0\n", encoding="utf-8")
        second = subprocess.run(argv, check=False, capture_output=True, text=True)
        self.assertIn("между вызовами", second.stdout)

    def test_follow_stall_prints_unfinished_last_line(self) -> None:
        self.log.write_text("", encoding="utf-8")
        proc = self.start_follow(self.follow_argv("1"))
        with self.log.open("a", encoding="utf-8") as f:
            f.write("без перевода строки")
        out, _ = proc.communicate()
        self.assertEqual(proc.returncode, 125, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d без перевода строки\n")

    def test_follow_requires_log(self) -> None:
        r = subprocess.run(
            ["bash", str(WAIT_DONE), str(self.run_dir), "--follow"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)

    def test_without_follow_log_lines_not_printed(self) -> None:
        self.log.write_text("тихо\n", encoding="utf-8")
        (self.run_dir / "done").write_text("0\n", encoding="utf-8")
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
        )
        self.assertEqual(r.stdout, f"done rc=0 {self.run_dir}\n")


class WaitRemoteTest(unittest.TestCase):
    use_fake = True

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "remote.log"
        self.log.write_text("", encoding="utf-8")
        self.env = dict(os.environ)
        # Спул наблюдателя узла — в каталоге проверки.
        self.env["TMPDIR"] = str(self.tmp)
        self.addCleanup(self.stop_watchers)
        if self.use_fake:
            self.bindir = self.tmp / "bin"
            self.bindir.mkdir()
            (self.bindir / "ssh").write_text(FAKE_SSH, encoding="utf-8")
            (self.bindir / "ssh").chmod(0o755)
            self.env["PATH"] = f"{self.bindir}:{self.env['PATH']}"
            self.env["FAKE_SSH_DROP"] = str(self.tmp / "drop")
            self.env["FAKE_SSH_LOG"] = str(self.log)

    def stop_watchers(self) -> None:
        """Снять наблюдателей узла, которые ещё живы (процесс pid 1 не кончается).

        Живой наблюдатель держит замок lock своего спула; его группа процессов
        (setsid) — PID из файла pid. Ожидание конца — освобождение замка.
        """
        for lock in self.tmp.glob("wait-remote-*/*/lock"):
            with lock.open("a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    continue
                except BlockingIOError:
                    pass
                try:
                    os.killpg(int((lock.parent / "pid").read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                fcntl.flock(f, fcntl.LOCK_EX)

    def remote(self) -> subprocess.Popen:
        """Удалённый процесс: живёт, пока проверка его не остановит (kill)."""
        p = subprocess.Popen(["sleep", "infinity"])
        self.addCleanup(p.kill)
        # Завершившийся процесс нужно подобрать, иначе он остаётся зомби и kill -0 видит его живым.
        threading.Thread(target=p.wait, daemon=True).start()
        return p

    def start(self, pid: int, *argv: str, path: str | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                "bash",
                str(WAIT_REMOTE),
                "localhost",
                str(pid),
                path or str(self.log),
                *argv,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(proc.wait)
        return proc

    def finish(self, proc: subprocess.Popen, head: str = "") -> tuple[str, str]:
        out, err = proc.communicate()
        return head + out, err

    def append(self, text: str) -> None:
        with self.log.open("a", encoding="utf-8") as f:
            f.write(text)

    def shown(self, out: str) -> list[str]:
        return [
            line.split(" ", 1)[1]
            for line in out.splitlines()
            if line[:2].isdigit() and line[2:3] == ":"
        ]

    def test_shows_progress_and_ends_with_process(self) -> None:
        self.append("в журнале\n")
        sleeper = self.remote()
        proc = self.start(sleeper.pid)
        assert proc.stdout is not None
        self.assertRegex(proc.stdout.readline(), r"^\d\d:\d\d:\d\d в журнале$")
        self.append("ход\n")
        self.assertRegex(proc.stdout.readline(), r"^\d\d:\d\d:\d\d ход$")
        self.assertIsNone(proc.poll())
        sleeper.kill()
        out, err = self.finish(proc)
        self.assertEqual(proc.returncode, 0, err)
        self.assertRegex(out, rf"remote done pid={sleeper.pid} localhost")

    def test_stall_exit_125(self) -> None:
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--stall-s", "1")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 125)
        self.assertRegex(out, r"stall \d+ localhost")

    def test_timeout_exit_124(self) -> None:
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--timeout-s", "1")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 124)
        self.assertIn("timeout", out)

    def test_bad_pid(self) -> None:
        r = subprocess.run(
            ["bash", str(WAIT_REMOTE), "localhost", "abc", "x"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)


class WaitRemoteFakeSshTest(WaitRemoteTest):
    def drops(self, *spec: int | str) -> None:
        """Подключения с обрывом: записей до обрыва, «N+K» — дописать K строк."""
        (self.tmp / "drop").write_text(
            "".join(f"{item}\n" for item in spec), encoding="utf-8"
        )

    def dead_pid(self) -> int:
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def age_log(self, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(self.log, (old, old))

    def test_reconnect_after_drop_while_process_alive(self) -> None:
        self.append("до обрыва\n")
        self.drops(1)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("до обрыва"), 1, out)
        self.assertRegex(out, r"remote done")

    def test_attempt_resets_after_new_lines_arrive(self) -> None:
        # Второе подключение дописывает строку и передаёт её: новая строка
        # сбрасывает попытки, два обрыва при --max-reconnects 1 их не исчерпывают.
        self.drops("1+1", "1+1")
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        head += read_until(proc.stdout, "обрыв связи")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertRegex(out, r"remote done")

    def test_reconnect_without_new_lines_does_not_reset_attempts(self) -> None:
        # Строка журнала показана до первого обрыва; переподключения её не повторяют
        # и новых строк не дают: счёт попыток не сбрасывается.
        self.append("одна\n")
        self.drops(1, *[0] * 30)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 3, out)
        self.assertEqual(out.count("одна"), 1, out)
        self.assertIn("связь с localhost потеряна, попыток 1", out)

    def test_lines_after_drop_continue_without_repeat(self) -> None:
        # Показано 20 из 100 строк, обрыв, в журнале +5 строк: после
        # переподключения приходят ровно эти 5 строк, и рост засчитывается —
        # второй обрыв при --max-reconnects 1 не исчерпывает попыток.
        self.append("".join(f"строка {i}\n" for i in range(1, 101)))
        self.drops(20, "5+5")
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        head += read_until(proc.stdout, "обрыв связи")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        expected = [f"строка {i}" for i in range(81, 101)] + [
            f"дописано {i}" for i in range(5)
        ]
        self.assertEqual(self.shown(out), expected, out)

    def test_truncated_log_after_drop_shown_from_start(self) -> None:
        # Показаны последние 20 из 30 строк, обрыв, журнал усечён и в нём
        # 3 новые строки: после переподключения приходят ровно они.
        self.append("".join(f"строка {i}\n" for i in range(1, 31)))
        self.drops(20, "3!3")
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0", "--max-reconnects", "1")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "дописано 2")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        expected = [f"строка {i}" for i in range(11, 31)] + [
            f"дописано {i}" for i in range(3)
        ]
        self.assertEqual(self.shown(out), expected, out)
        self.assertEqual(out.count("журнал усечён"), 1, out)

    def test_truncated_and_regrown_with_same_last_line(self) -> None:
        # RP-2289 п. 8: показаны 5 строк, обрыв, журнал усечён и снова дорос
        # до тех же строк (та же длина, та же последняя строка, тот же inode):
        # наблюдатель узла видит усечение по событию, строки нового журнала
        # приходят один раз, без потерь и повторов.
        old = "".join(f"o{i}\n" for i in range(5))
        self.append(old)
        self.drops(5)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        os.truncate(self.log, 0)
        head += read_until(proc.stdout, "журнал усечён")
        self.append(old)
        head += read_until(proc.stdout, "o4")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.shown(out), [f"o{i}" for i in range(5)] * 2, out)
        self.assertEqual(out.count("журнал усечён"), 1, out)

    def test_partial_line_on_drop_printed_once(self) -> None:
        # Обрыв посреди записи при живом процессе: обрывок не печатается,
        # после переподключения строка приходит целиком один раз.
        self.append("первая\nобрывок\n")
        self.drops("2-")
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрывок")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("обрывок"), 1, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d обрывок\n")

    def test_partial_last_line_printed_when_process_ends(self) -> None:
        # Последняя строка журнала без перевода строки не теряется.
        self.append("первая\nхвост")
        sleeper = self.remote()
        proc = self.start(sleeper.pid)
        assert proc.stdout is not None
        head = read_until(proc.stdout, "первая")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("хвост"), 1, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d хвост\nremote done")

    def test_shell_output_before_header_is_not_log(self) -> None:
        # Строка оболочки узла до шапки не занимает её место: шапка не печатается
        # строкой журнала, после обрыва повтора нет, оболочка — в stderr.
        self.env["FAKE_SSH_NOISE"] = "привет из bashrc"
        self.append("одна\n")
        self.drops(1)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        sleeper.kill()
        out, err = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("wait-remote", out)
        self.assertNotIn("привет из bashrc", out)
        self.assertIn("вывод оболочки узла: привет из bashrc", err)
        self.assertEqual(out.count("одна"), 1, out)

    def test_log_created_inside_connection(self) -> None:
        # Журнала нет при подключении (сообщение в stderr), он появляется,
        # обрыв: после переподключения нет ни повтора, ни сообщения об усечении.
        self.log.unlink()
        self.drops(2)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None and proc.stderr is not None
        read_until(proc.stderr, "пока нет")
        self.log.write_text("a1\na2\n", encoding="utf-8")
        head = read_until(proc.stdout, "a2")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.shown(out), ["a1", "a2"], out)
        self.assertNotIn("усечён", out)

    def test_log_truncated_inside_connection(self) -> None:
        # Журнал усечён внутри подключения: показываются новые строки,
        # после обрыва они не повторяются.
        self.append("".join(f"o{i}\n" for i in range(5)))
        self.drops(7)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "o4")
        self.log.write_text("n1\nn2\n", encoding="utf-8")
        head += read_until(proc.stdout, "n2")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(
            self.shown(out), [f"o{i}" for i in range(5)] + ["n1", "n2"], out
        )
        self.assertEqual(out.count("журнал усечён"), 1, out)

    def replace_log(self, text: str) -> None:
        new = self.tmp / "new.log"
        new.write_text(text, encoding="utf-8")
        os.replace(new, self.log)

    def test_log_replaced_inside_connection(self) -> None:
        # Журнал заменён (mv) внутри подключения: строки нового журнала
        # показываются с первой, после обрыва не повторяются.
        self.append("o1\no2\n")
        self.drops(4)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "o2")
        self.replace_log("r1\nr2\n")
        head += read_until(proc.stdout, "r2")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.shown(out), ["o1", "o2", "r1", "r2"], out)
        self.assertEqual(out.count("журнал усечён"), 1, out)

    def test_log_created_then_replaced_at_once(self) -> None:
        # RP-2289 п. 7: журнал создан и тут же заменён (mv) — tail -F (GNU
        # coreutils 9.12) после этого молчал. Строки заменившего журнала
        # приходят по одному разу; строка первого — не больше одного раза.
        self.log.unlink()
        sleeper = self.remote()
        proc = self.start(sleeper.pid)
        assert proc.stdout is not None and proc.stderr is not None
        read_until(proc.stderr, "пока нет")
        self.log.write_text("a1\n", encoding="utf-8")
        self.replace_log("r1\nr2\n")
        head = read_until(proc.stdout, "r2")
        sleeper.kill()
        out, _ = self.finish(proc, head)
        self.assertEqual(proc.returncode, 0, out)
        shown = self.shown(out)
        self.assertEqual([s for s in shown if s != "a1"], ["r1", "r2"], out)
        self.assertLessEqual(shown.count("a1"), 1, out)

    def test_shell_output_without_newline_glued_to_header(self) -> None:
        # Вывод оболочки без перевода строки склеен с шапкой: шапка найдена,
        # сторож взведён (без исправления — вечное ожидание).
        self.env["FAKE_SSH_NOISE"] = "\x1b]0;title\x07"
        self.env["FAKE_SSH_NOISE_NONL"] = "1"
        self.append("одна\n")
        proc = self.start(1, "--stall-s", "1")
        out, err = self.finish(proc)
        self.assertEqual(proc.returncode, 125, out)
        self.assertNotIn("wait-remote", out)
        self.assertIn("title", err)
        self.assertEqual(self.shown(out), ["одна"], out)

    def test_partial_line_on_drop_then_process_ended(self) -> None:
        # Обрыв посреди записи, процесс уже завершился: обрывок не печатается,
        # спул дочитывается следующим подключением — строка целиком один раз.
        self.append("первая\nобрывок-длинный-хвост\n")
        self.drops("2-")
        proc = self.start(self.dead_pid(), "--reconnect-s", "0")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.shown(out), ["первая", "обрывок-длинный-хвост"], out)

    def test_header_cut_then_process_ended(self) -> None:
        # Обрыв посреди шапки, процесс уже завершился: обрывок шапки не строка
        # журнала.
        self.append("одна\n")
        self.drops("h")
        proc = self.start(self.dead_pid(), "--reconnect-s", "0")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("wait-remote", out)
        self.assertEqual(self.shown(out), ["одна"], out)

    def test_restart_keeps_stall_from_log_mtime(self) -> None:
        # Перезапуск ожидания давно идущего процесса (pid 1) при неподвижном
        # журнале: отсчёт сторожа — от времени изменения журнала, а прежние
        # строки первого подключения ростом не считаются.
        self.append("старая\n")
        self.age_log(120)
        proc = self.start(1, "--stall-s", "60")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 125, out)
        self.assertIn("старая", out)
        self.assertRegex(out, r"stall 1[12]\d localhost")

    def test_old_log_of_new_process_is_not_stall(self) -> None:
        # Журнал изменён вчера, процесс только что запущен и молчит: отсчёт —
        # от запуска процесса, застоя нет, ожидание кончается по --timeout-s.
        self.append("вчерашняя\n")
        self.age_log(86400)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--stall-s", "900", "--timeout-s", "2")
        out, _ = self.finish(proc)
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
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 125, out)

    def test_header_drops_end_in_stall(self) -> None:
        # RP-2289 п. 9: обрывы раз за разом до шапки (записей нет): срок
        # сторожа проверяется при обрыве — код 125, а не 3 после всех попыток.
        self.drops(*["h"] * 10)
        proc = self.start(
            1, "--stall-s", "2", "--reconnect-s", "1", "--max-reconnects", "9"
        )
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 125, out)
        self.assertRegex(out, r"stall \d+ localhost")

    def test_foreign_process_is_alive_not_done(self) -> None:
        # pid 1 чужой (kill -0 даёт EPERM), но процесс жив: наблюдатель
        # конца процесса не пишет, ожидание кончается сторожем.
        proc = self.start(1, "--stall-s", "1")
        out, _ = self.finish(proc)
        self.assertEqual(proc.returncode, 125, out)
        self.assertNotIn("remote done", out)

    def test_ssh_stderr_is_not_log_line(self) -> None:
        self.env["FAKE_SSH_ERR"] = "1"
        self.drops(0)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, "--reconnect-s", "0")
        assert proc.stdout is not None
        head = read_until(proc.stdout, "обрыв связи")
        sleeper.kill()
        out, err = self.finish(proc, head)
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
                sleeper = self.remote()
                proc = self.start(sleeper.pid, path=path)
                assert proc.stdout is not None
                head = read_until(proc.stdout, "в тильде")
                sleeper.kill()
                out, _ = self.finish(proc, head)
                self.assertEqual(proc.returncode, 0, out)

    def test_missing_inotifywait_reported(self) -> None:
        # На узле нет inotifywait: явное сообщение и код 2, без отката
        # на другой способ слежения.
        for tool in ("bash", "dirname", "flock", "setsid", "stat", "tail", "ps"):
            found = shutil.which(tool)
            assert found is not None, tool
            (self.bindir / tool).symlink_to(found)
        self.env["PATH"] = str(self.bindir)
        proc = self.start(1)
        out, err = self.finish(proc)
        self.assertEqual(proc.returncode, 2, out + err)
        self.assertIn("на узле localhost нет: inotifywait (", err)

    def check_term_in_pause(self, *extra: str) -> None:
        # Пауза — sleep с числом секунд, которого нет у других процессов.
        pause = 5000 + os.getpid() % 1000
        self.drops(0)
        sleeper = self.remote()
        proc = self.start(sleeper.pid, *extra, "--reconnect-s", str(pause))
        assert proc.stdout is not None
        self.assertIn("обрыв связи", proc.stdout.readline())
        proc.send_signal(signal.SIGTERM)
        proc.wait()
        self.assertEqual(proc.returncode, 143)
        left = subprocess.run(
            ["pgrep", "-fx", f"sleep {pause}"], check=False, capture_output=True
        )
        self.assertEqual(left.returncode, 1, left.stdout)

    def test_term_in_reconnect_pause_leaves_no_orphans(self) -> None:
        self.check_term_in_pause("--timeout-s", "100")

    def test_term_in_reconnect_pause_without_timeout(self) -> None:
        self.check_term_in_pause()


FLOCK_DOUBLE = r"""#!/bin/bash
# Двойник flock: перед неблокирующим захватом замка наблюдателя сообщает
# об этом в stderr и ждёт записи «e» в спуле (события inotify), то есть
# конца процесса и выхода прежнего наблюдателя. Наблюдение за спулом
# устанавливается до проверки условия: запись «e» между проверкой
# и наблюдением не теряется.
if [ -n "${FAKE_FLOCK_SPOOL:-}" ] && [ "$1" = -n ]; then
    exec 5< <(exec inotifywait -m -e modify --format m -- "$FAKE_FLOCK_SPOOL" 2>&1)
    watcher=$!
    while IFS= read -r line <&5 && [ "$line" != 'Watches established.' ]; do :; done
    echo flock-reached >&2
    until [ "$(tail -n 1 -- "$FAKE_FLOCK_SPOOL" 2> /dev/null)" = e ]; do
        IFS= read -r line <&5 || break
    done
    kill "$watcher" 2> /dev/null
    exec 5<&-
fi
exec /usr/bin/flock "$@"
"""


class WaitRemoteNodeRaceTest(unittest.TestCase):
    """Наблюдатель завершился между чтением спула и захватом замка."""

    def test_no_second_watcher_after_end(self) -> None:
        node = SCRIPTS / "wait-remote-node.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            double = root / "bin" / "flock"
            double.write_text(FLOCK_DOUBLE, encoding="utf-8")
            double.chmod(0o755)
            log = root / "log"
            log.write_text("a\n", encoding="utf-8")
            target = subprocess.Popen(["sleep", "100000"])
            self.addCleanup(target.kill)
            self.addCleanup(target.wait)
            env = {**os.environ, "TMPDIR": str(root / "tmp")}
            (root / "tmp").mkdir()

            def connect(token: str, extra: dict[str, str]) -> subprocess.Popen:
                return subprocess.Popen(
                    ["bash", "-s", "--", str(log), str(target.pid), token, "first"],
                    stdin=node.open("rb"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**env, **extra},
                )

            first = connect("t1", {})
            assert first.stdout is not None
            self.assertIn("wait-remote t1", first.stdout.readline())
            (spool,) = (root / "tmp").glob("wait-remote-*/*/spool")
            second = connect(
                "t2",
                {
                    "FAKE_FLOCK_SPOOL": str(spool),
                    "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                },
            )
            assert second.stderr is not None
            self.assertEqual(second.stderr.readline().strip(), "flock-reached")
            target.kill()
            target.wait()
            out1, _ = first.communicate()
            out2, _ = second.communicate()
            self.assertEqual(out1.splitlines()[-2:], ["o a", "e"], out1)
            self.assertEqual(out2.splitlines()[-2:], ["o a", "e"], out2)
            self.assertNotIn("w", out2.splitlines(), out2)
            self.assertEqual(out2.splitlines().count("e"), 1, out2)

    def test_end_written_while_watcher_holds_lock(self) -> None:
        # Окно между записью «e» и выходом наблюдателя (освобождением замка
        # lock): подключение прочитало спул до «e», захват замка не удался —
        # оно идёт за спулом до выхода наблюдателя, получает «e» ровно один
        # раз, второго наблюдателя не запускает и удаляет каталог спула.
        # Наблюдатель изображают процесс target (его PID в файле pid)
        # и замок lock, который держит проверка.
        node = SCRIPTS / "wait-remote-node.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "log"
            log.write_text("a\n", encoding="utf-8")
            target = subprocess.Popen(["sleep", "100000"])
            # Очистки идут в обратном порядке: сначала kill, затем wait.
            self.addCleanup(target.wait)
            self.addCleanup(target.kill)
            watcher = subprocess.Popen(["sleep", "100000"])
            # Очистки идут в обратном порядке: сначала kill, затем wait.
            self.addCleanup(watcher.wait)
            self.addCleanup(watcher.kill)
            key = subprocess.run(
                ["cksum"],
                input=f"{target.pid} {log}\n",
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()[0]
            spool_dir = (
                root / "tmp" / f"wait-remote-{os.getuid()}" / f"{target.pid}-{key}"
            )
            spool_dir.mkdir(parents=True)
            spool = spool_dir / "spool"
            spool.write_text("o a\n", encoding="utf-8")
            (spool_dir / "pid").write_text(f"{watcher.pid}\n", encoding="utf-8")
            lock = (spool_dir / "lock").open("a")
            self.addCleanup(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
            with node.open("rb") as script:
                conn = subprocess.Popen(
                    ["bash", "-s", "--", str(log), str(target.pid), "t", "first"],
                    stdin=script,
                    stdout=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "TMPDIR": str(root / "tmp")},
                )
            assert conn.stdout is not None
            # Шапка печатается после попытки захвата замка.
            self.assertIn("wait-remote t 1 1 ", conn.stdout.readline())
            with spool.open("a", encoding="utf-8") as f:
                f.write("e\n")
            watcher.kill()
            watcher.wait()
            fcntl.flock(lock, fcntl.LOCK_UN)
            out, _ = conn.communicate()
            self.assertEqual(conn.returncode, 0, out)
            self.assertEqual(out.splitlines(), ["o a", "e"], out)
            self.assertFalse(spool_dir.exists())


@unittest.skipUnless(
    real_ssh_available(), "ssh на localhost по ключу недоступен (BatchMode)"
)
class WaitRemoteRealSshTest(WaitRemoteTest):
    use_fake = False


if __name__ == "__main__":
    unittest.main()
