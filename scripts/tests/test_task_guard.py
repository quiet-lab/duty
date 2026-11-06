"""Проверки сторожей сессии задачи: `python3 -I scripts/tests/test_task_guard.py` или pytest."""

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
HOOK = PLUGIN / "hooks" / "task-guard.py"
SIGNALS = PLUGIN / "scripts" / "signals.py"
ASK = PLUGIN / "scripts" / "ask.py"
# Замер на станции (32 ядра), 5 прогонов: хук stop с опросом signals.py и ask.py
# и отправкой через push-agent.sh — 0,25-0,4 с, самая долгая операция теста;
# узел Jenkins медленнее на ядро в 4,5 раза: 1,8 с, с двойным запасом 3,6 с,
# округлено до 4 с. Предел — предохранитель от зависания, а не время ожидания.
LIMIT_S = 4

BRIEF = """# Задача probe

## Цель

Проверить сторожей.

## Служебное (заполняет dispatch.sh)

- символ workspace: `Ⓑ`
- панель агента: `w9:p1`
- агент herdr и сессия Claude: `probe`, модель `opus`
"""

# Строки, которых требуют ворота постановки (scripts/brief_check.py).
GATE_LINES = (
    "Критерий готовности: `pytest -q` зелёный\n"
    "Условие остановки: падение теста — отчёт\n"
    "Справка: не нужна — проба\n"
)

# Двойник herdr: дописывает argv строкой (поля через табуляцию), всё принимает.
FAKE_HERDR = """#!/usr/bin/env bash
printf '%s\\t' "$@" >> "$FAKE_HERDR_LOG"
printf '\\n' >> "$FAKE_HERDR_LOG"
exit 0
"""


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.root = root
        self.results = root / "results"
        self.task = self.results / "probe"
        self.subtasks = self.task / "subtasks"
        self.task.mkdir(parents=True)
        (self.task / "brief.md").write_text(BRIEF, encoding="utf-8")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        herdr = bin_dir / "herdr"
        herdr.write_text(FAKE_HERDR, encoding="utf-8")
        herdr.chmod(0o755)
        self.log = root / "herdr.log"
        self.signals = root / "signals"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        self.env.update(
            DUTY_ROLE="worker",
            DUTY_TASK=str(self.task),
            DUTY_NAME="probe",
            DUTY_SIGNALS_DIR=str(self.signals),
            DUTY_RESULTS_DIR=str(self.results),
            FAKE_HERDR_LOG=str(self.log),
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        )
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=LIMIT_S)
            for f in (p.stdout, p.stderr, p.stdin):
                if f:
                    f.close()

    def hook(
        self, event: str, data: dict | str, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        stdin = data if isinstance(data, str) else json.dumps(data)
        return subprocess.run(
            [sys.executable, "-I", str(HOOK), event],
            input=stdin,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def herdr_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line.rstrip("\t").split("\t") for line in lines]

    def prompts(self) -> list[list[str]]:
        return [c for c in self.herdr_calls() if c[:2] == ["agent", "prompt"]]

    def write_stopped(self, name: str, agent_id: str = "id-1") -> Path:
        self.subtasks.mkdir(exist_ok=True)
        path = self.subtasks / f"{name}.stopped"
        path.write_text(
            json.dumps({"agent_id": agent_id, "last": "жду"}), encoding="utf-8"
        )
        return path

    def transcript(self, content: object) -> str:
        path = self.root / "agent.jsonl"
        record = {"type": "user", "message": {"content": content}}
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        return str(path)

    def stop_data(self, **extra: object) -> dict:
        return {"agent_id": "id-1", "last_assistant_message": "ждёт\nответа", **extra}

    # --- pre-tool: запреты ожидания в субагенте ---

    def test_pre_tool_denies_background_in_subagent(self) -> None:
        for tool, tool_input in (
            ("Monitor", {"command": "tail -f x"}),
            ("Bash", {"command": "sleep 1", "run_in_background": True}),
        ):
            with self.subTest(tool=tool):
                r = self.hook(
                    "pre-tool",
                    {"tool_name": tool, "tool_input": tool_input, "agent_id": "id-1"},
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                out = json.loads(r.stdout)["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "deny")
                self.assertIn("wait-done.sh", out["permissionDecisionReason"])

    def test_pre_tool_allows_foreground_and_coordinator(self) -> None:
        cases = [
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_id": "id-1"},
            {
                "tool_name": "Bash",
                "tool_input": {"command": "x", "run_in_background": True},
                "agent_type": "duty:worker",
            },
            {
                "tool_name": "Monitor",
                "tool_input": {"command": "x"},
                "agent_type": "duty:worker",
            },
        ]
        for data in cases:
            with self.subTest(data=data):
                r = self.hook("pre-tool", data)
                self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_not_worker_is_silent(self) -> None:
        self.env["DUTY_ROLE"] = "main"
        r = self.hook(
            "pre-tool",
            {"tool_name": "Monitor", "tool_input": {}, "agent_id": "id-1"},
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    # --- pre-tool: Agent и SendMessage ---

    def test_agent_with_tag_clears_marks(self) -> None:
        self.write_stopped("a1")
        (self.subtasks / "a1.done").write_text("итог\n", encoding="utf-8")
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "prompt": "Подзадача: a1\n" + GATE_LINES + "Сделай.",
                    "subagent_type": "general-purpose",
                },
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(list(self.subtasks.iterdir()), [])

    def test_agent_without_tag(self) -> None:
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "prompt": GATE_LINES + "Сделай.",
                    "subagent_type": "general-purpose",
                },
            },
        )
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertIn("Подзадача", out["additionalContext"])
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_input": {"prompt": "Найди.", "subagent_type": "Explore"},
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_send_message_clears_stopped(self) -> None:
        mine = self.write_stopped("a1", "ag-77")
        other = self.write_stopped("a2", "ag-88")
        self.hook(
            "pre-tool", {"tool_name": "SendMessage", "tool_input": {"to": "ag-77"}}
        )
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.hook(
            "pre-tool", {"tool_name": "SendMessage", "tool_input": {"to": "stranger"}}
        )
        self.assertTrue(other.exists())

    # --- subagent-stop ---

    def test_subagent_stop_without_result(self) -> None:
        for content in (
            "Подзадача: a1\nСделай.",
            [{"type": "text", "text": "Подзадача: a1\nСделай."}],
        ):
            with self.subTest(content=content):
                (self.subtasks / "a1.stopped").unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                r = self.hook(
                    "subagent-stop",
                    self.stop_data(agent_transcript_path=self.transcript(content)),
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                record = json.loads(
                    (self.subtasks / "a1.stopped").read_text(encoding="utf-8")
                )
                self.assertEqual(record["agent_id"], "id-1")
                self.assertEqual(record["last"], "ждёт ответа")
                (prompt,) = self.prompts()
                self.assertEqual(prompt[2], "probe")
                self.assertIn("субагент a1 остановился без итога", prompt[3])
                self.assertIn("SendMessage to=id-1", prompt[3])

    def test_subagent_stop_with_result(self) -> None:
        self.subtasks.mkdir()
        (self.subtasks / "a1.done").write_text("итог\n", encoding="utf-8")
        self.write_stopped("a1")
        r = self.hook(
            "subagent-stop",
            self.stop_data(agent_transcript_path=self.transcript("Подзадача: a1\n")),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.subtasks / "a1.stopped").exists())
        self.assertEqual(self.prompts(), [])

    def test_subagent_stop_untracked(self) -> None:
        for path in (self.transcript("Без метки"), str(self.root / "нет.jsonl")):
            with self.subTest(path=path):
                r = self.hook(
                    "subagent-stop", self.stop_data(agent_transcript_path=path)
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(self.subtasks.exists())
                self.assertEqual(self.prompts(), [])

    # --- stop ---

    def test_stop_without_stopped_is_silent(self) -> None:
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_blocks_with_names(self) -> None:
        self.write_stopped("a1")
        r = self.hook("stop", {})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("a1", out["reason"])

    def test_stop_with_done_is_silent(self) -> None:
        self.write_stopped("a1")
        (self.subtasks / "a1.done").write_text("итог\n", encoding="utf-8")
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_hook_active_notifies(self) -> None:
        self.write_stopped("a1")
        r = self.hook("stop", {"stop_hook_active": True})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        shows = [c for c in self.herdr_calls() if c[:2] == ["notification", "show"]]
        self.assertEqual(len(shows), 1)
        self.assertIn("a1", shows[0][4])

    def start_run(self, name: str = "r1") -> tuple[Path, int]:
        """Прогон: cmd.sh ждёт строку из FIFO, пока тест её не напишет."""
        run_dir = self.task / "runs" / name
        run_dir.mkdir(parents=True)
        fifo = run_dir / "gate"
        os.mkfifo(fifo)
        cmd = run_dir / "cmd.sh"
        cmd.write_text(f'read -r _ < "{fifo}"\n', encoding="utf-8")
        proc = subprocess.Popen(["bash", str(cmd)], stdin=subprocess.DEVNULL)
        self.procs.append(proc)
        self.addCleanup(self.open_gate, fifo)
        (run_dir / "meta.txt").write_text(f"pid: {proc.pid}\n", encoding="utf-8")
        return run_dir, proc.pid

    @staticmethod
    def open_gate(fifo: Path) -> None:
        # O_RDWR на FIFO не блокируется; строка отпускает read в cmd.sh.
        fd = os.open(fifo, os.O_RDWR)
        try:
            os.write(fd, b"\n")
        finally:
            os.close(fd)

    def test_stop_live_run_is_wait(self) -> None:
        self.write_stopped("a1")
        run_dir, _ = self.start_run()
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        (run_dir / "done").write_text("0\n", encoding="utf-8")
        r = self.hook("stop", {})
        self.assertEqual(json.loads(r.stdout)["decision"], "block")

    def test_stop_foreign_pid_is_not_wait(self) -> None:
        self.write_stopped("a1")
        run_dir, _ = self.start_run()
        # Процесс жив, но это не cmd.sh прогона: PID переиспользован.
        other = subprocess.Popen(["bash", "-c", "read -r _ <&0"], stdin=subprocess.PIPE)
        self.procs.append(other)
        (run_dir / "meta.txt").write_text(f"pid: {other.pid}\n", encoding="utf-8")
        r = self.hook("stop", {})
        self.assertEqual(json.loads(r.stdout)["decision"], "block")

    def test_stop_signal_wait_is_wait(self) -> None:
        self.write_stopped("a1")
        waiter = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(SIGNALS),
                "wait",
                "stand-free",
                "--holder",
                "probe",
                "--timeout-s",
                str(LIMIT_S),
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.procs.append(waiter)
        # signals.py wait оставляет inotifywait сиротой при SIGKILL: убирается вся группа.
        self.addCleanup(lambda: os.killpg(waiter.pid, signal.SIGKILL))
        assert waiter.stderr is not None
        # wait печатает строку в stderr, когда отметка ожидания уже записана.
        ready, _, _ = select.select([waiter.stderr], [], [], LIMIT_S)
        self.assertTrue(ready, "wait не сообщил о готовности")
        self.assertIn("ждёт сигнал", waiter.stderr.readline())
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_open_question_is_wait(self) -> None:
        self.write_stopped("a1")
        ask = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ASK),
                "new",
                "x",
                "--title",
                "Вопрос",
                "--step",
                "1",
                "--depends",
                "шаг",
                "--option",
                "а",
                "--option",
                "б",
                "--recommended",
                "1",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )
        self.assertEqual(ask.returncode, 0, ask.stderr)
        self.assertTrue((self.task / "questions" / "01-x.md").exists())
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    # --- ворота постановки: Agent/Task во всех сессиях ---

    def plain_env(self) -> dict:
        return {k: v for k, v in self.env.items() if not k.startswith("DUTY_")}

    def agent_call(
        self, prompt: str, env: dict | None, tool: str = "Agent", **extra: str
    ):
        tool_input = {"prompt": prompt, "subagent_type": "general-purpose", **extra}
        return self.hook(
            "pre-tool", {"tool_name": tool, "tool_input": tool_input}, env=env
        )

    def assert_denied(self, r: subprocess.CompletedProcess, *names: str) -> None:
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        for name in names:
            self.assertIn(name, out["permissionDecisionReason"])
        self.assertIn("brief-template.md", out["permissionDecisionReason"])

    def assert_not_denied(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 0, r.stderr)
        if r.stdout:
            self.assertNotIn("permissionDecision", r.stdout)

    def test_gate_denies_without_lines_in_any_session(self) -> None:
        for label, env in (("без роли", self.plain_env()), ("worker", self.env)):
            with self.subTest(session=label):
                r = self.agent_call("Сделай.", env)
                self.assert_denied(r, "Критерий готовности", "Условие остановки")

    def test_gate_names_missing_line(self) -> None:
        for env in (self.plain_env(), self.env):
            r = self.agent_call("Критерий готовности: `pytest -q` зелёный", env)
            self.assert_denied(r, "Условие остановки")
            self.assertNotIn(
                "не хватает: Критерий",
                json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"],
            )
            r = self.agent_call("Условие остановки: падение — отчёт", env)
            self.assert_denied(r, "Критерий готовности")

    def test_gate_allows_both_lines(self) -> None:
        for env in (self.plain_env(), self.env):
            self.assert_not_denied(self.agent_call(GATE_LINES + "Сделай.", env))

    def recall_file(self, age_s: float) -> Path:
        path = self.root / "recall.md"
        path.write_text("справка\n", encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_gate_requires_recall_line(self) -> None:
        base = "Критерий готовности: `pytest -q` зелёный\nУсловие остановки: падение\n"
        for env in (self.plain_env(), self.env):
            self.assert_denied(self.agent_call(base + "Сделай.", env), "Справка")
            self.assert_denied(
                self.agent_call(base + "Справка: не нужна\n", env), "Справка"
            )
            self.assert_denied(
                self.agent_call(base + f"Справка: {self.root}/нет.md\n", env), "Справка"
            )

    def test_gate_recall_file_freshness(self) -> None:
        base = "Критерий готовности: `pytest -q` зелёный\nУсловие остановки: падение\n"
        path = self.recall_file(3600)
        for env in (self.plain_env(), self.env):
            self.assert_not_denied(self.agent_call(f"{base}Справка: {path}\n", env))
        self.recall_file(25 * 3600)
        r = self.agent_call(f"{base}Справка: {path}\n", self.plain_env())
        self.assert_denied(r, "Справка")

    def test_gate_free_types(self) -> None:
        for kind in (
            "Explore",
            "Plan",
            "claude-code-guide",
            "duty:inspector",
            "duty:runner",
        ):
            for env in (self.plain_env(), self.env):
                with self.subTest(kind=kind):
                    self.assert_not_denied(
                        self.agent_call("Найди.", env, subagent_type=kind)
                    )

    # --- ворота вопроса пользователю ---

    def ask_call(self, *questions: object, env: dict | None = None):
        items = [{"question": q} if isinstance(q, str) else q for q in questions]
        return self.hook(
            "pre-tool",
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": items}},
            env=env or self.plain_env(),
        )

    def test_ask_denied_without_phrase(self) -> None:
        for env in (self.plain_env(), self.env):
            r = self.ask_call("Какой вариант выбрать?", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("/duty:recall", out["permissionDecisionReason"])
            self.assertIn("По документам:", out["permissionDecisionReason"])

    def test_ask_allowed_with_phrase(self) -> None:
        for env in (self.plain_env(), self.env):
            r = self.ask_call(
                "Какой вариант? По документам: решено в записи 0032.", env=env
            )
            self.assert_not_denied(r)
            self.assertEqual(r.stdout, "")

    def test_ask_empty_phrase_denied(self) -> None:
        r = self.ask_call("Какой вариант? По документам: <что найдено>")
        self.assertIn("deny", r.stdout)

    def test_ask_one_bad_question_denies(self) -> None:
        r = self.ask_call("Первый? По документам: записей нет.", "Второй без справки?")
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("2", out["permissionDecisionReason"])
        self.assert_not_denied(
            self.ask_call("Первый? По документам: нет.", "Второй? По документам: нет.")
        )

    def test_ask_single_json_in_stdout(self) -> None:
        r = self.ask_call("Без справки?", env=self.env)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        json.loads(r.stdout)

    def test_hooks_json_matches_ask_user_question(self) -> None:
        config = json.loads(
            (PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        entry = config["hooks"]["PreToolUse"][0]
        self.assertEqual(
            set(entry["matcher"].split("|")), {"Agent", "Task", "AskUserQuestion"}
        )
        self.assertNotIn("DUTY_ROLE", entry["hooks"][0]["command"])

    def test_gate_task_tool_like_agent(self) -> None:
        self.assert_denied(
            self.agent_call("Сделай.", self.plain_env(), tool="Task"),
            "Условие остановки",
        )
        self.assert_not_denied(
            self.agent_call(GATE_LINES, self.plain_env(), tool="Task")
        )

    def test_gate_headings_pass(self) -> None:
        prompt = "## Критерий готовности\n\n`pytest -q` зелёный\n\n## Условие остановки\n\nпадение\n\nСправка: не нужна — проба\n"
        self.assert_not_denied(self.agent_call(prompt, self.plain_env()))

    def test_gate_empty_values_denied(self) -> None:
        for value in ("", "<команда и ожидаемый результат>"):
            with self.subTest(value=value):
                prompt = f"Критерий готовности: {value}\nУсловие остановки: падение"
                self.assert_denied(
                    self.agent_call(prompt, self.plain_env()), "Критерий готовности"
                )

    def test_gate_applies_to_subagent_calling_agent(self) -> None:
        data = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Сделай."},
            "agent_id": "id-1",
        }
        self.assert_denied(
            self.hook("pre-tool", data, env=self.plain_env()), "Критерий готовности"
        )
        self.assert_denied(self.hook("pre-tool", data), "Критерий готовности")

    # --- ворота: вызов из навыка (по транскрипту) ---

    def skill_transcript(self, entries: list[dict]) -> Path:
        path = self.root / "transcript.jsonl"
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def user(content: object, **extra: object) -> dict:
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            **extra,
        }

    @staticmethod
    def skill_use() -> dict:
        block = {"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}}
        return {"type": "assistant", "message": {"content": [block]}}

    def call_with(self, prompt: str, transcript: object) -> subprocess.CompletedProcess:
        data = {
            "tool_name": "Agent",
            "tool_input": {"prompt": prompt, "subagent_type": "general-purpose"},
            "transcript_path": str(transcript),
        }
        return self.hook("pre-tool", data, env=self.plain_env())

    def test_gate_skill_slash_command_passes(self) -> None:
        path = self.skill_transcript(
            [
                self.user(
                    "<command-name>/code-review</command-name>\n<command-args>high</command-args>"
                ),
                self.user([{"type": "text", "text": "текст навыка"}], isMeta=True),
            ]
        )
        self.assert_not_denied(self.call_with("Сделай.", path))

    def test_gate_skill_tool_in_same_turn_passes(self) -> None:
        path = self.skill_transcript(
            [
                self.user("проверь дифф"),
                self.skill_use(),
                self.user([{"type": "tool_result", "content": "ok"}]),
                self.user([{"type": "text", "text": "текст навыка"}], isMeta=True),
            ]
        )
        self.assert_not_denied(self.call_with("Сделай.", path))

    def test_gate_skill_in_previous_turn_denied(self) -> None:
        path = self.skill_transcript(
            [
                self.user("<command-name>/code-review</command-name>"),
                self.skill_use(),
                self.user([{"type": "text", "text": "теперь другое"}]),
            ]
        )
        self.assert_denied(self.call_with("Сделай.", path), "Критерий готовности")

    def test_gate_no_transcript_denied(self) -> None:
        for path in (self.root / "нет.jsonl", ""):
            with self.subTest(path=str(path)):
                self.assert_denied(
                    self.call_with("Сделай.", path), "Критерий готовности"
                )

    def test_gate_lines_pass_with_transcript(self) -> None:
        path = self.skill_transcript([self.user("обычное сообщение")])
        self.assert_not_denied(self.call_with(GATE_LINES + "Сделай.", path))

    def test_gate_broken_json_is_silent(self) -> None:
        r = self.hook("pre-tool", "{не json", env=self.plain_env())
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_no_role_other_tools_and_events_silent(self) -> None:
        env = self.plain_env()
        for tool, tool_input in (
            ("Bash", {"command": "ls"}),
            ("Monitor", {"command": "x"}),
            ("SendMessage", {"to": "a"}),
        ):
            with self.subTest(tool=tool):
                r = self.hook(
                    "pre-tool",
                    {"tool_name": tool, "tool_input": tool_input, "agent_id": "id-1"},
                    env=env,
                )
                self.assertEqual((r.returncode, r.stdout), (0, ""))
        for event in ("subagent-stop", "stop"):
            r = self.hook(event, {"agent_id": "id-1"}, env=env)
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_hooks_json_gate_has_no_role_condition(self) -> None:
        config = json.loads(
            (PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        entries = {e["matcher"]: e for e in config["hooks"]["PreToolUse"]}
        gate = entries["Agent|Task|AskUserQuestion"]["hooks"][0]["command"]
        self.assertNotIn("DUTY_ROLE", gate)
        self.assertIn("task-guard.py", gate)
        guard = entries["Monitor|Bash|SendMessage"]["hooks"][0]["command"]
        self.assertIn("DUTY_ROLE", guard)

    # --- испорченный ввод ---

    def test_broken_json(self) -> None:
        for event in ("pre-tool", "subagent-stop", "stop"):
            with self.subTest(event=event):
                r = self.hook(event, "{не json")
                self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_fake_herdr_is_used(self) -> None:
        # Двойник стоит первым в PATH: иначе проверки отправили бы строки в живой herdr.
        self.assertEqual(
            shutil.which("herdr", path=self.env["PATH"]),
            str(self.root / "bin" / "herdr"),
        )


if __name__ == "__main__":
    unittest.main()
