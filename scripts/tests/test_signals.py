"""Проверки сигналов: запуск `python3 -I scripts/tests/test_signals.py` или через pytest."""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "signals.py"
# Каждая операция занимает доли секунды (запуск Python и inotifywait);
# предел в 10 с — запас на порядок на самом медленном узле, только как
# предохранитель от зависания теста, а не как время ожидания события.
LIMIT_S = 10


class SignalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        self.env["DUTY_SIGNALS_DIR"] = self.tmp.name
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=LIMIT_S)
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def run_cmd(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *argv], env=env or self.env,
                              capture_output=True, text=True, timeout=LIMIT_S)

    def start_wait(self, *argv: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "wait", *argv], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        assert p.stderr is not None
        # wait печатает строку в stderr, когда наблюдение и отметка ожидания уже есть.
        ready, _, _ = select.select([p.stderr], [], [], LIMIT_S)
        self.assertTrue(ready, "wait не сообщил о готовности")
        self.assertIn("ждёт сигнал", p.stderr.readline())
        return p

    def test_signal_before_wait(self) -> None:
        self.assertEqual(self.run_cmd("signal", "stand-free", "готово", "--from", "a").returncode, 0)
        r = self.run_cmd("wait", "stand-free")
        self.assertEqual(r.returncode, 0)
        self.assertIn("сигнал stand-free: готово", r.stdout)
        self.assertIn("от a,", r.stdout)
        self.assertEqual(list(self.dir.glob("*.waiting-*")), [])

    def test_signal_file_format(self) -> None:
        env = dict(self.env, DUTY_NAME="w1", DUTY_TASK="/t")
        self.run_cmd("signal", "x1", "текст", env=env)
        data = json.loads((self.dir / "x1.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {k: data[k] for k in ("name", "text", "from", "task")},
            {"name": "x1", "text": "текст", "from": "w1", "task": "/t"})
        self.assertIsInstance(data["at_ms"], int)

    def test_wait_then_signal(self) -> None:
        p = self.start_wait("pr-merged-qms-2584", "--holder", "w1")
        mark = self.dir / "pr-merged-qms-2584.waiting-w1"
        self.assertTrue(mark.exists())
        self.assertEqual(json.loads(mark.read_text(encoding="utf-8"))["pid"], p.pid)
        self.assertEqual(self.run_cmd("signal", "pr-merged-qms-2584", "слит").returncode, 0)
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("сигнал pr-merged-qms-2584: слит", out)
        self.assertFalse(mark.exists())

    def test_timeout(self) -> None:
        r = self.run_cmd("wait", "never", "--timeout-s", "1", "--holder", "w1")
        self.assertEqual(r.returncode, 124)
        self.assertIn("срок 1 с истёк", r.stderr)
        self.assertEqual(list(self.dir.glob("*.waiting-*")), [])

    def test_list_and_json(self) -> None:
        self.assertIn("сигналов и ожиданий нет", self.run_cmd("list").stdout)
        self.run_cmd("signal", "done-1", "т", "--from", "a")
        p = self.start_wait("other-1", "--holder", "w2")
        out = self.run_cmd("list").stdout
        self.assertIn("| done-1 | a | т |", out)
        self.assertIn("| other-1 | w2 |", out)
        self.assertIn("| да |", out)
        data = json.loads(self.run_cmd("list", "--json").stdout)
        self.assertEqual([s["name"] for s in data["signals"]], ["done-1"])
        self.assertEqual([(w["name"], w["holder"], w["alive"]) for w in data["waits"]],
                         [("other-1", "w2", True)])
        p.terminate()
        p.communicate(timeout=LIMIT_S)

    def test_clear(self) -> None:
        self.run_cmd("signal", "c1", "т")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=LIMIT_S)
        (self.dir / "c1.waiting-old").write_text(
            json.dumps({"name": "c1", "holder": "old", "task": "", "pid": dead.pid, "since_ms": 0}),
            encoding="utf-8")
        (self.dir / "c1.waiting-mine").write_text(
            json.dumps({"name": "c1", "holder": "mine", "task": "", "pid": os.getpid(), "since_ms": 0}),
            encoding="utf-8")
        r = self.run_cmd("clear", "c1")
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.dir / "c1.json").exists())
        self.assertFalse((self.dir / "c1.waiting-old").exists())
        self.assertTrue((self.dir / "c1.waiting-mine").exists())
        self.assertIn("сигнала нет", self.run_cmd("clear", "c1").stdout)

    def test_invalid_names(self) -> None:
        for bad in ("Bad", "a.b", "a--b", "-a", "a-", "", "a" * 101):
            for cmd in ("signal", "wait", "clear"):
                with self.subTest(name=bad, cmd=cmd):
                    self.assertEqual(self.run_cmd(cmd, bad).returncode, 2)
        self.assertEqual(self.run_cmd("wait", "ok", "--holder", "Bad").returncode, 2)

    def test_keep_does_not_overwrite(self) -> None:
        self.run_cmd("signal", "k1", "первый", "--from", "a")
        r = self.run_cmd("signal", "k1", "второй", "--from", "b", "--keep")
        self.assertEqual(r.returncode, 0)
        self.assertIn("уже есть: первый", r.stdout)
        data = json.loads((self.dir / "k1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["text"], "первый")
        self.run_cmd("signal", "k1", "третий")
        self.assertEqual(json.loads((self.dir / "k1.json").read_text(encoding="utf-8"))["text"], "третий")

    def test_sigterm_removes_mark(self) -> None:
        p = self.start_wait("t1", "--holder", "w1")
        mark = self.dir / "t1.waiting-w1"
        self.assertTrue(mark.exists())
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertFalse(mark.exists())


if __name__ == "__main__":
    unittest.main()
