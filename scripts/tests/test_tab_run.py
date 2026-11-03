"""Проверки tab-run.sh, push-agent.sh и wait-done.sh с двойником herdr.

Запуск: `python3 -I scripts/tests/test_tab_run.py` или через pytest.
"""

import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
TAB_RUN = SCRIPTS / "tab-run.sh"
WAIT_DONE = SCRIPTS / "wait-done.sh"
PUSH = SCRIPTS / "push-agent.sh"
# Замер на станции (32 ядра), 5 прогонов: tab-run.sh с командой, отправкой и
# выходом вкладки — 0,3-0,5 с, wait-done.sh с --timeout-s 1 — 1,1 с, самая
# долгая операция теста; узел Jenkins медленнее на ядро в 4,5 раза: 5 с,
# с двойным запасом 10 с. Предел — предохранитель от зависания, а не время
# ожидания события.
LIMIT_S = 10

BRIEF = """# Задача probe

## Служебное (заполняет dispatch.sh)

- панель агента: `w9:p1`
- агент herdr и сессия Claude: `probe`, модель `opus`
"""

# Двойник herdr: журнал argv (поля через табуляцию); tab create и pane run —
# как у вкладки: команда идёт в новой сессии, её PID записывается в pane.pid.
# Управление отказами — файлами в $FAKE_DIR: get_missing (agent get отвечает
# agent_not_found), blocked (первый prompt отвечает agent_blocked).
FAKE_HERDR = r"""#!/usr/bin/env bash
printf '%s\t' "$@" >> "$FAKE_DIR/herdr.log"
printf '\n' >> "$FAKE_DIR/herdr.log"
case "$1 $2" in
    "tab create")
        echo '{"result":{"tab":{"tab_id":"w9:t2"},"root_pane":{"pane_id":"w9:p2"}}}' ;;
    "workspace list")
        echo '{"result":{"workspaces":[]}}' ;;
    "pane run")
        setsid bash -c "$4" > /dev/null 2>&1 < /dev/null &
        echo $! > "$FAKE_DIR/pane.pid" ;;
    "agent get")
        if [ -e "$FAKE_DIR/get_missing" ]; then
            echo '{"error":{"code":"agent_not_found"}}'
            exit 1
        fi ;;
    "agent prompt")
        if [ -e "$FAKE_DIR/blocked" ] && [ ! -e "$FAKE_DIR/blocked.used" ]; then
            : > "$FAKE_DIR/blocked.used"
            echo '{"error":{"code":"agent_blocked"}}'
            exit 1
        fi ;;
esac
exit 0
"""


def wait_gone(pid: int) -> bool:
    """Блокирующее ожидание конца процесса через pidfd; True — процесса нет."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        ready, _, _ = select.select([fd], [], [], LIMIT_S)
    finally:
        os.close(fd)
    return bool(ready)


class TabRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.root = root
        self.task = root / "task"
        self.task.mkdir()
        (self.task / "brief.md").write_text(BRIEF, encoding="utf-8")
        self.fake = root / "fake"
        self.fake.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        herdr = bin_dir / "herdr"
        herdr.write_text(FAKE_HERDR, encoding="utf-8")
        herdr.chmod(0o755)
        self.bin = bin_dir
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        self.env.update(
            HERDR_ENV="1",
            HERDR_WORKSPACE_ID="w9",
            DUTY_TASK=str(self.task),
            DUTY_NAME="probe",
            FAKE_DIR=str(self.fake),
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        )
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        pid_file = self.fake / "pane.pid"
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=LIMIT_S)
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def run_script(
        self, script: Path, *argv: str, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *argv],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def calls(self, *prefix: str) -> list[list[str]]:
        log = self.fake / "herdr.log"
        if not log.exists():
            return []
        rows = [ln.rstrip("\t").split("\t") for ln in log.read_text().splitlines()]
        return [r for r in rows if r[: len(prefix)] == list(prefix)]

    def tab_run(self, name: str, command: str) -> Path:
        r = self.run_script(TAB_RUN, name, "--", command)
        self.assertEqual(r.returncode, 0, r.stderr)
        return Path(json.loads(r.stdout)["run_dir"])

    def pane_pid(self) -> int:
        return int((self.fake / "pane.pid").read_text())

    def test_exit_code_and_push(self) -> None:
        run_dir = self.tab_run("r1", "exit 7")
        waited = self.run_script(WAIT_DONE, str(run_dir), "--timeout-s", str(LIMIT_S))
        self.assertEqual(waited.returncode, 7, waited.stderr)
        self.assertEqual(waited.stdout.strip(), f"done rc=7 {run_dir}")
        # Отправка идёт после done: ждём конца всей вкладки.
        self.assertTrue(wait_gone(self.pane_pid()))
        self.assertEqual((run_dir / "done").read_text().strip(), "7")
        meta = (run_dir / "meta.txt").read_text()
        self.assertRegex(meta, r"(?m)^pid: \d+$")
        self.assertIn("code: 7\n", meta)
        (prompt,) = self.calls("agent", "prompt")
        self.assertEqual(prompt[2:], ["probe", f"done rc=7 {run_dir}"])
        self.assertIn("отправлено", (run_dir / "push.log").read_text())
        self.assertEqual(len(self.calls("notification", "show")), 1)

    def test_second_signal_does_not_lose_done(self) -> None:
        """Второй сигнал во время записи итога не должен оставить прогон без done."""
        flag = self.root / "finishing"
        real_date = shutil.which("date")
        double = Path(self.env["PATH"].split(os.pathsep)[0]) / "date"
        double.write_text(
            f'#!/usr/bin/env bash\n[ ! -e "{flag}" ] || kill -TERM "$PPID"\n'
            f'exec "{real_date}" "$@"\n'
        )
        double.chmod(0o755)
        run_dir = self.tab_run("r1", f'touch "{flag}"')
        waited = self.run_script(WAIT_DONE, str(run_dir), "--timeout-s", str(LIMIT_S))
        self.assertEqual(waited.stdout.strip(), f"done rc=0 {run_dir}")

    def test_tab_closed_mid_run(self) -> None:
        ready = self.root / "ready"
        gate = self.root / "gate"
        os.mkfifo(ready)
        os.mkfifo(gate)
        reader = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)
        try:
            run_dir = self.tab_run("r1", f'echo up > "{ready}"; read -r _ < "{gate}"')
            ok, _, _ = select.select([reader], [], [], LIMIT_S)
            self.assertTrue(ok, "команда не запустилась")
        finally:
            os.close(reader)
        # Закрытие вкладки: SIGHUP всей группе вкладки, как у терминала.
        pid = self.pane_pid()
        os.killpg(pid, signal.SIGHUP)
        self.assertTrue(wait_gone(pid))
        self.assertEqual((run_dir / "done").read_text().strip(), "129")
        (prompt,) = self.calls("agent", "prompt")
        self.assertEqual(prompt[3], f"done rc=129 {run_dir}")

    def test_push_retries_after_blocked(self) -> None:
        (self.fake / "blocked").touch()
        r = self.run_script(PUSH, str(self.task), "строка")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls("agent", "prompt")), 2)
        self.assertEqual(len(self.calls("agent", "wait")), 1)

    def test_push_no_wait_gives_up(self) -> None:
        (self.fake / "blocked").touch()
        r = self.run_script(PUSH, str(self.task), "строка", "--no-wait")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(len(self.calls("agent", "prompt")), 1)
        self.assertEqual(self.calls("agent", "wait"), [])

    def test_push_agent_not_found(self) -> None:
        (self.fake / "get_missing").touch()
        r = self.run_script(PUSH, str(self.task), "строка")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual([c[2] for c in self.calls("agent", "get")], ["probe", "w9:p1"])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_push_without_service_block(self) -> None:
        (self.task / "brief.md").write_text("# Задача\n", encoding="utf-8")
        r = self.run_script(PUSH, str(self.task), "строка")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_push_uses_last_block(self) -> None:
        with (self.task / "brief.md").open("a", encoding="utf-8") as brief:
            brief.write(
                "\n## Служебное (после --resume)\n\n"
                "- панель агента: `w9:p7`\n"
                "- агент herdr и сессия Claude: `renamed`, модель `opus`\n"
            )
        r = self.run_script(PUSH, str(self.task), "строка\nвторая")
        self.assertEqual(r.returncode, 0, r.stderr)
        (prompt,) = self.calls("agent", "prompt")
        self.assertEqual(prompt[2:], ["renamed", "строка вторая"])

    # --- wait-done.sh ---

    def watcher_env(self) -> tuple[dict, Path]:
        """PATH с inotifywait-обёрткой: она сообщает свой PID, когда сценарий уже
        поставил на неё pdeathsig (setpriv выполняет обёртку после prctl)."""
        real = shutil.which("inotifywait")
        if real is None:
            self.skipTest("нет inotifywait")
        shim_dir = self.root / "shim"
        shim_dir.mkdir()
        fifo = self.root / "watcher-pid"
        os.mkfifo(fifo)
        shim = shim_dir / "inotifywait"
        shim.write_text(
            f'#!/usr/bin/env bash\necho $$ > "{fifo}"\nexec "{real}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        env = dict(self.env, PATH=f"{shim_dir}{os.pathsep}{self.env['PATH']}")
        return env, fifo

    def start_wait(self, run_dir: Path) -> tuple[subprocess.Popen, int]:
        env, fifo = self.watcher_env()
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            p = subprocess.Popen(
                ["bash", str(WAIT_DONE), str(run_dir)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.procs.append(p)
            ok, _, _ = select.select([reader], [], [], LIMIT_S)
            self.assertTrue(ok, "наблюдатель не запустился")
            return p, int(os.read(reader, 64))
        finally:
            os.close(reader)

    def test_wait_done_killed_stops_watcher(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        p, watcher = self.start_wait(run_dir)
        p.kill()
        p.wait(timeout=LIMIT_S)
        self.assertTrue(wait_gone(watcher), "inotifywait пережил wait-done.sh")

    def test_wait_done_finish_stops_watcher(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        p, watcher = self.start_wait(run_dir)
        (run_dir / "done").write_text("0\n", encoding="utf-8")
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("done rc=0", out)
        self.assertTrue(wait_gone(watcher), "inotifywait остался после done")

    def test_wait_done_timeout(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        r = self.run_script(WAIT_DONE, str(run_dir), "--timeout-s", "1")
        self.assertEqual(r.returncode, 124, r.stderr)


if __name__ == "__main__":
    unittest.main()
