"""Проверки сигналов: запуск `python3 -I scripts/tests/test_signals.py` или через pytest."""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "signals.py"
# Замер на станции (32 ядра), 5 прогонов: от запуска wait до его выхода
# после signal — 0,14-0,19 с, самая долгая операция теста. Узел Jenkins
# медленнее на ядро в 4,5 раза: 0,19 * 4,5 = 0,86 с; с двойным запасом
# 1,7 с, округлено до 2 с. Предел — предохранитель от зависания, а не
# время ожидания события.
LIMIT_S = 2


class Base(unittest.TestCase):
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

    def run_cmd(
        self, *argv: str, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        # Срок у wait обязателен; тесты, которым срок безразличен, получают общий.
        if argv[:1] == ("wait",) and "--timeout-s" not in argv:
            argv = (*argv, "--timeout-s", str(LIMIT_S))
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *argv],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def start_wait(self, *argv: str) -> subprocess.Popen:
        if "--timeout-s" not in argv:
            argv = (*argv, "--timeout-s", str(LIMIT_S))
        p = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "wait", *argv],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(p)
        assert p.stderr is not None
        # wait печатает строку в stderr, когда наблюдение и отметка ожидания уже есть.
        ready, _, _ = select.select([p.stderr], [], [], LIMIT_S)
        self.assertTrue(ready, "wait не сообщил о готовности")
        self.assertIn("ждёт сигнал", p.stderr.readline())
        return p


class SignalsTest(Base):
    def test_signal_before_wait(self) -> None:
        self.assertEqual(
            self.run_cmd("signal", "stand-free", "готово", "--from", "a").returncode, 0
        )
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
            {"name": "x1", "text": "текст", "from": "w1", "task": "/t"},
        )
        self.assertIsInstance(data["at_ms"], int)

    def test_wait_then_signal(self) -> None:
        p = self.start_wait("pr-merged-app-2584", "--holder", "w1")
        mark = self.dir / f"pr-merged-app-2584.waiting-w1.{p.pid}"
        self.assertTrue(mark.exists())
        self.assertEqual(json.loads(mark.read_text(encoding="utf-8"))["pid"], p.pid)
        self.assertEqual(
            self.run_cmd("signal", "pr-merged-app-2584", "слит").returncode, 0
        )
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("сигнал pr-merged-app-2584: слит", out)
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
        self.assertEqual(
            [(w["name"], w["holder"], w["alive"]) for w in data["waits"]],
            [("other-1", "w2", True)],
        )
        p.terminate()
        p.communicate(timeout=LIMIT_S)

    def test_clear(self) -> None:
        self.run_cmd("signal", "c1", "т")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=LIMIT_S)
        (self.dir / "c1.waiting-old.1").write_text(
            json.dumps(
                {
                    "name": "c1",
                    "holder": "old",
                    "task": "",
                    "pid": dead.pid,
                    "since_ms": 0,
                }
            ),
            encoding="utf-8",
        )
        (self.dir / "c1.waiting-mine.2").write_text(
            json.dumps(
                {
                    "name": "c1",
                    "holder": "mine",
                    "task": "",
                    "pid": os.getpid(),
                    "since_ms": 0,
                }
            ),
            encoding="utf-8",
        )
        r = self.run_cmd("clear", "c1")
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.dir / "c1.json").exists())
        self.assertFalse((self.dir / "c1.waiting-old.1").exists())
        self.assertTrue((self.dir / "c1.waiting-mine.2").exists())
        self.assertIn("сигнала нет", self.run_cmd("clear", "c1").stdout)

    def test_invalid_names(self) -> None:
        for bad in ("Bad", "a.b", "-a", "_a", "", "a" * 101):
            for cmd in ("signal", "wait", "clear"):
                with self.subTest(name=bad, cmd=cmd):
                    self.assertEqual(self.run_cmd(cmd, bad).returncode, 2)
        self.assertEqual(self.run_cmd("wait", "ok", "--holder", "Bad").returncode, 2)

    def test_valid_names_with_underscore_and_double_dash(self) -> None:
        for good in ("a--b", "a_b", "task-done-app_py314", "a-", "0a"):
            with self.subTest(name=good):
                self.assertEqual(self.run_cmd("signal", good, "т").returncode, 0)
        r = self.run_cmd("wait", "a_b", "--holder", "app_py314")
        self.assertEqual(r.returncode, 0)

    def test_existing_signal_with_zero_timeout(self) -> None:
        self.run_cmd("signal", "z1", "есть")
        r = self.run_cmd("wait", "z1", "--timeout-s", "0")
        self.assertEqual(r.returncode, 0)
        self.assertIn("сигнал z1: есть", r.stdout)

    def test_zero_timeout_without_signal(self) -> None:
        self.assertEqual(
            self.run_cmd("wait", "z2", "--timeout-s", "0", "--holder", "w").returncode,
            124,
        )

    def test_same_holder_marks_do_not_collide(self) -> None:
        first = self.start_wait("dup", "--holder", "w1", "--timeout-s", "1")
        second = self.start_wait("dup", "--holder", "w1")
        self.assertEqual(len(list(self.dir.glob("dup.waiting-w1.*"))), 2)
        first.communicate(timeout=LIMIT_S)
        self.assertEqual(first.returncode, 124)
        left = list(self.dir.glob("dup.waiting-w1.*"))
        self.assertEqual([m.name for m in left], [f"dup.waiting-w1.{second.pid}"])
        second.terminate()
        second.communicate(timeout=LIMIT_S)
        self.assertEqual(list(self.dir.glob("dup.waiting-*")), [])

    def test_list_waits_only(self) -> None:
        self.run_cmd("signal", "have", "т")
        self.assertIn("ожиданий нет", self.run_cmd("list", "--waits").stdout)
        p = self.start_wait("lw-1", "--holder", "w3")
        out = self.run_cmd("list", "--waits").stdout
        self.assertIn("| lw-1 | w3 |", out)
        self.assertNotIn("| have |", out)
        self.assertTrue(out.rstrip().endswith("| нет |"))
        data = json.loads(self.run_cmd("list", "--waits", "--json").stdout)
        self.assertEqual(list(data), ["waits"])
        self.assertFalse(data["waits"][0]["given"])
        p.terminate()
        p.communicate(timeout=LIMIT_S)

    def test_corrupt_signal_files(self) -> None:
        for body in ("[]", '{"name": "bad", "te'):
            with self.subTest(body=body):
                (self.dir / "bad.json").write_text(body, encoding="utf-8")
                r = self.run_cmd("wait", "bad")
                self.assertEqual(r.returncode, 2)
                self.assertIn("повреждён", r.stderr)
                self.assertIn("| bad | ? | повреждён |", self.run_cmd("list").stdout)

    def test_keep_overwrites_corrupt_with_warning(self) -> None:
        (self.dir / "kc.json").write_text("[]", encoding="utf-8")
        r = self.run_cmd("signal", "kc", "новый", "--keep")
        self.assertEqual(r.returncode, 0)
        self.assertIn("повреждён", r.stderr)
        self.assertEqual(
            json.loads((self.dir / "kc.json").read_text(encoding="utf-8"))["text"],
            "новый",
        )
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

    def test_keep_wakes_waiter(self) -> None:
        p = self.start_wait("kw", "--holder", "w1")
        self.assertEqual(
            self.run_cmd("signal", "kw", "через keep", "--keep").returncode, 0
        )
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("сигнал kw: через keep", out)

    def test_keep_does_not_overwrite(self) -> None:
        self.run_cmd("signal", "k1", "первый", "--from", "a")
        r = self.run_cmd("signal", "k1", "второй", "--from", "b", "--keep")
        self.assertEqual(r.returncode, 0)
        self.assertIn("уже есть: первый", r.stdout)
        data = json.loads((self.dir / "k1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["text"], "первый")
        self.run_cmd("signal", "k1", "третий")
        self.assertEqual(
            json.loads((self.dir / "k1.json").read_text(encoding="utf-8"))["text"],
            "третий",
        )

    def test_sigterm_removes_mark(self) -> None:
        p = self.start_wait("t1", "--holder", "w1")
        mark = self.dir / f"t1.waiting-w1.{p.pid}"
        self.assertTrue(mark.exists())
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertFalse(mark.exists())

    def test_partial_write_does_not_end_wait(self) -> None:
        p = self.start_wait("pw", "--holder", "w1")
        # Неатомарная запись: файл создан и дописывается; ожидание не прерывается.
        with open(self.dir / "pw.json", "w", encoding="utf-8") as f:
            f.write('{"name": "pw", "text": "дописан", ')
            f.flush()
            self.assertIsNone(p.poll())
            f.write('"from": "a", "task": "", "at_ms": 1}')
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("сигнал pw: дописан", out)

    def test_list_survives_bad_field_types(self) -> None:
        (self.dir / "bs.json").write_text(
            json.dumps({"name": "bs", "text": "т", "from": "a", "at_ms": "вчера"}),
            encoding="utf-8",
        )
        (self.dir / "bw.waiting-w.7").write_text(
            json.dumps({"name": "bw", "holder": "w", "pid": None, "since_ms": "x"}),
            encoding="utf-8",
        )
        for argv in (("list",), ("list", "--waits"), ("list", "--json")):
            with self.subTest(argv=argv):
                r = self.run_cmd(*argv)
                self.assertEqual(r.returncode, 0, r.stderr)
        out = self.run_cmd("list").stdout
        self.assertIn("| bs | a | повреждён | 0 |", out)
        self.assertIn("| bw | w | повреждён | нет (устарело) | 0 |", out)
        self.assertEqual(self.run_cmd("signal", "bs", "н", "--keep").returncode, 0)

    def test_double_sigterm_cleans_up(self) -> None:
        p = self.start_wait("ds", "--holder", "w1")
        children = Path(f"/proc/{p.pid}/task/{p.pid}/children").read_text().split()
        self.assertEqual(len(children), 1)
        p.send_signal(signal.SIGTERM)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertEqual(list(self.dir.glob("ds.waiting-*")), [])
        # Наблюдатель завершён и собран родителем: каталога процесса нет.
        self.assertFalse(Path(f"/proc/{children[0]}").exists())

    def test_timeout_is_required(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "wait", "no-ttl"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("--timeout-s", r.stderr)

    def test_timeout_writes_state_md(self) -> None:
        task = self.dir / "task"
        task.mkdir()
        (task / "state.md").write_text("# state\nШаг: 1\n", encoding="utf-8")
        env = {**self.env, "DUTY_TASK": str(task)}
        r = self.run_cmd(
            "wait", "never2", "--timeout-s", "1", "--holder", "w1", env=env
        )
        self.assertEqual(r.returncode, 124, r.stderr)
        text = (task / "state.md").read_text(encoding="utf-8")
        self.assertIn("Просрочено: ожидание сигнала never2 (w1)", text)

    def test_list_marks_overdue_waits(self) -> None:
        old = int(time.time() * 1000) - 130 * 60_000
        mark = {
            "name": "late",
            "holder": "w1",
            "task": "t",
            "pid": os.getpid(),
            "since_ms": old,
            "timeout_s": 3600,
        }
        (self.dir / f"late.waiting-w1.{os.getpid()}").write_text(
            json.dumps(mark), encoding="utf-8"
        )
        fresh = dict(
            mark, name="fresh", since_ms=int(time.time() * 1000), timeout_s=3600
        )
        (self.dir / f"fresh.waiting-w2.{os.getpid()}").write_text(
            json.dumps(fresh), encoding="utf-8"
        )
        out = self.run_cmd("list", "--waits").stdout
        late = next(line for line in out.splitlines() if line.startswith("| late "))
        self.assertIn("⚠ просрочено +70 мин", late)
        fresh_line = next(
            line for line in out.splitlines() if line.startswith("| fresh ")
        )
        self.assertNotIn("просрочено", fresh_line)
        waits = json.loads(self.run_cmd("list", "--waits", "--json").stdout)["waits"]
        self.assertEqual(
            {w["name"]: w["ttl_min"] for w in waits}, {"late": 60, "fresh": 60}
        )


class ProjectFlagTest(Base):
    """--project: сигналы другого проекта; состояние — во временном каталоге."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.dir / "state"
        (self.state / "other" / "signals").mkdir(parents=True)
        (self.state / "mine").mkdir()
        cfg = self.dir / "user.toml"
        cfg.write_text(f'[paths]\nstate = "{self.state}"\n', encoding="utf-8")
        self.env["DUTY_CONFIG_USER"] = str(cfg)
        self.env["DUTY_SIGNALS_DIR"] = str(self.state / "mine" / "signals")
        self.other = self.state / "other" / "signals"

    def test_signal_and_wait_by_name(self) -> None:
        r = self.run_cmd("signal", "x-done", "ok", "--project", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.other / "x-done.json").exists())
        self.assertFalse((self.state / "mine" / "signals" / "x-done.json").exists())
        r = self.run_cmd("wait", "x-done", "--project", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("сигнал x-done: ok", r.stdout)

    def test_wait_then_signal_across_projects(self) -> None:
        p = self.start_wait("y-done", "--holder", "w1", "--project", "other")
        self.assertEqual(len(list(self.other.glob("y-done.waiting-w1.*"))), 1)
        self.run_cmd("signal", "y-done", "go", "--project", "other")
        out, _ = p.communicate(timeout=LIMIT_S)
        self.assertEqual(p.returncode, 0)
        self.assertIn("сигнал y-done: go", out)

    def test_by_path(self) -> None:
        root = self.dir / "repo"
        root.mkdir()
        (root / ".duty").mkdir()
        (root / ".duty" / "duty.toml").write_text(
            f'schema = 1\n[project]\nname = "other"\n[paths]\nsignals = "{self.other}"\n',
            encoding="utf-8",
        )
        r = self.run_cmd("signal", "z-done", "--project", str(root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.other / "z-done.json").exists())

    def test_unknown_project(self) -> None:
        r = self.run_cmd("signal", "x", "--project", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("неизвестный проект", r.stderr)
        self.assertIn("other", r.stderr)
        self.assertIn("mine", r.stderr)

    def test_bad_project_values(self) -> None:
        for bad in ("", "..", "."):
            r = self.run_cmd("signal", "x", "--project", bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("неизвестный проект", r.stderr)

    def test_clear_and_list(self) -> None:
        self.run_cmd("signal", "c-done", "t", "--project", "other")
        out = self.run_cmd("list", "--project", "other").stdout
        self.assertIn("проект: other", out)
        self.assertIn("| c-done |", out)
        data = json.loads(self.run_cmd("list", "--json", "--project", "other").stdout)
        self.assertEqual(data["project"], "other")
        self.assertNotIn("проект:", self.run_cmd("list").stdout)
        r = self.run_cmd("clear", "c-done", "--project", "other")
        self.assertIn("сигнал снят", r.stdout)
        self.assertFalse((self.other / "c-done.json").exists())


if __name__ == "__main__":
    unittest.main()
