"""Проверки сторожей сессии задачи: `python3 -I scripts/tests/test_task_guard.py` или pytest."""

import json
import os
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
# signals.py wait требует --timeout-s (параметр продукта); тест его не ждёт:
# событие — строка готовности в stderr, зависание ловит внешний предел прогона.
WAIT_TERM_S = 86400

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
    "Приоритеты: развилок нет — линейная задача\n"
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
            p.wait()
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
            check=False,
        )

    def herdr_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line.rstrip("\t").split("\t") for line in lines]

    def prompts(self) -> list[list[str]]:
        return [c for c in self.herdr_calls() if c[:2] == ["agent", "prompt"]]

    def write_stopped(
        self, name: str, agent_id: str = "id-1", at_ms: int | None = None
    ) -> Path:
        self.subtasks.mkdir(exist_ok=True)
        path = self.subtasks / f"{name}.stopped"
        record: dict[str, object] = {"agent_id": agent_id, "last": "жду"}
        if at_ms is not None:
            record["at_ms"] = at_ms
        path.write_text(json.dumps(record), encoding="utf-8")
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
        # Прежние отметки отложены до подтверждения запуска, а не стёрты.
        self.assertEqual(
            sorted(p.name for p in self.subtasks.iterdir()),
            ["a1.done.prev", "a1.started", "a1.stopped.prev"],
        )

    def test_agent_with_tag_writes_started_and_stop_names_it(self) -> None:
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "prompt": "Подзадача: a2\n" + GATE_LINES + "Сделай.",
                    "subagent_type": "general-purpose",
                },
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        record = json.loads((self.subtasks / "a2.started").read_text(encoding="utf-8"))
        self.assertIsInstance(record["at_ms"], int)
        # Запущена без итога и не остановлена: ход не блокируется.
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        # Рядом остановилась другая подзадача: причина называет и запущенную.
        self.write_stopped("a1")
        r = self.hook("stop", {})
        reason = json.loads(r.stdout)["reason"]
        self.assertIn("a1", reason)
        self.assertIn("Запущены и ещё без итога: a2", reason)
        # Итог записан: подзадача из перечня запущенных уходит.
        (self.subtasks / "a2.done").write_text("итог\n", encoding="utf-8")
        reason = json.loads(self.hook("stop", {}).stdout)["reason"]
        self.assertNotIn("Запущены", reason)

    def launch(
        self,
        name: str,
        description: str,
        agent_id: str | None = None,
        tool_use_id: str | None = None,
        confirm: bool = True,
    ) -> None:
        """Запуск подзадачи: pre-tool Agent с меткой, затем PostToolUse с agentId."""
        agent_id = agent_id or f"ag-{name}"
        tool_use_id = tool_use_id or f"toolu_{name}"
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_use_id": tool_use_id,
                "tool_input": {
                    "prompt": f"Подзадача: {name}\n" + GATE_LINES + "Сделай.",
                    "description": description,
                    "subagent_type": "general-purpose",
                    "run_in_background": True,
                },
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        if confirm:
            r = self.post_launch(name, self.async_launched(agent_id), tool_use_id)
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    @staticmethod
    def live_agent(
        description: str,
        agent_type: str = "general-purpose",
        agent_id: str = "a0b1c2",
        status: str = "running",
    ) -> dict:
        """Элемент background_tasks входа Stop для живого субагента."""
        return {
            "id": agent_id,
            "type": "subagent",
            "status": status,
            "description": description,
            "agent_type": agent_type,
        }

    def test_stop_live_subagent_does_not_block(self) -> None:
        self.launch("a3", "правка хука")
        record = json.loads((self.subtasks / "a3.started").read_text(encoding="utf-8"))
        self.assertEqual(record["description"], "правка хука")
        live = [self.live_agent("правка хука", agent_id="ag-a3")]
        r = self.hook("stop", {"background_tasks": live})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_ended_without_result_blocks(self) -> None:
        # Субагента нет среди живых, отметок .stopped и .done нет: он завершён
        # без итога и без SubagentStop (сбой хука, обрыв сессии).
        self.launch("a3", "правка хука")
        for tasks in (
            [],
            [self.live_agent("другое")],
            [self.live_agent("правка хука", "Explore")],
        ):
            with self.subTest(tasks=tasks):
                r = self.hook("stop", {"background_tasks": tasks})
                self.assertEqual(r.returncode, 0, r.stderr)
                out = json.loads(r.stdout)
                self.assertEqual(out["decision"], "block")
                self.assertIn("Завершены без итога: a3", out["reason"])
        (self.subtasks / "a3.done").write_text("итог\n", encoding="utf-8")
        r = self.hook("stop", {"background_tasks": []})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_names_live_next_to_blocking(self) -> None:
        self.launch("a3", "правка хука")
        self.launch("a4", "тесты")
        self.write_stopped("a1")
        live = [self.live_agent("тесты", agent_id="ag-a4")]
        r = self.hook("stop", {"background_tasks": live})
        reason = json.loads(r.stdout)["reason"]
        self.assertIn("a1 (agent_id id-1)", reason)
        self.assertIn("Завершены без итога: a3", reason)
        self.assertIn("Ещё идут: a4", reason)
        self.assertNotIn("Запущены и ещё без итога", reason)

    def test_stop_hook_active_names_ended_without_result(self) -> None:
        self.launch("a3", "правка хука")
        r = self.hook("stop", {"stop_hook_active": True, "background_tasks": []})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        shows = [c for c in self.herdr_calls() if c[:2] == ["notification", "show"]]
        self.assertEqual(len(shows), 1)
        self.assertIn("a3", shows[0][4])

    def test_stop_empty_description_judged_by_agent_id(self) -> None:
        # Живость судится по agentId, описание вызова не нужно.
        self.launch("a6", "")
        live = [self.live_agent("Сделай.", agent_id="ag-a6")]
        r = self.hook("stop", {"background_tasks": live})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_stop_names_covered_stopped_as_launched(self) -> None:
        # Остановившаяся и покрытая ожиданием подзадача называется, как прежде.
        self.launch("a2", "правка хука")
        self.write_stopped("a2")
        self.start_run()
        self.write_stopped("a1", at_ms=time.time_ns() // 1_000_000 + 60_000)
        r = self.hook("stop", {"background_tasks": []})
        reason = json.loads(r.stdout)["reason"]
        self.assertIn("a1 (agent_id id-1)", reason)
        self.assertIn("Запущены и ещё без итога: a2", reason)
        self.assertNotIn("Завершены без итога", reason)
        self.assertIn("Каждую возобнови", reason)

    def test_stop_started_without_description_is_not_judged(self) -> None:
        # Отметка прежней версии хука без описания: живость не определить,
        # ход не блокируется, как без поля background_tasks.
        self.subtasks.mkdir()
        (self.subtasks / "a5.started").write_text('{"at_ms": 1}', encoding="utf-8")
        r = self.hook("stop", {"background_tasks": []})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    # --- RP-2417: запуск подтверждается ответом Agent (agentId) ---

    def post_launch(
        self, name: str, response: object, tool_use_id: str = "toolu_1"
    ) -> subprocess.CompletedProcess:
        """PostToolUse на Agent: ответ инструмента с agentId и status."""
        return self.hook(
            "post-tool",
            {
                "tool_name": "Agent",
                "tool_use_id": tool_use_id,
                "tool_input": {"prompt": f"Подзадача: {name}\n" + GATE_LINES},
                "tool_response": response,
            },
        )

    @staticmethod
    def async_launched(agent_id: str) -> dict:
        return {"status": "async_launched", "agentId": agent_id, "description": "d"}

    def session_transcript(self, entries: list[dict]) -> str:
        path = self.root / "session.jsonl"
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8",
        )
        return str(path)

    def test_rejected_launch_forgets_started(self) -> None:
        # Вызов Agent отклонён после pre-tool (другой хук, отказ в разрешении):
        # PostToolUse не приходит, в транскрипте — tool_result с ошибкой.
        self.launch("r1", "правка", tool_use_id="toolu_r1", confirm=False)
        transcript = self.session_transcript(
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_r1",
                                "is_error": True,
                                "content": "Error: отклонено",
                            }
                        ]
                    },
                    "toolUseResult": "Error: отклонено",
                }
            ]
        )
        r = self.hook("stop", {"background_tasks": [], "transcript_path": transcript})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertFalse((self.subtasks / "r1.started").exists())
        # Ошибка выполнения Agent приходит PostToolUseFailure и после запуска
        # субагента; по входу события их не различить (RP-2445, п. 2): даже
        # ошибка до запуска считается остановкой без итога (отказ при сомнении).
        self.launch("r2", "правка", tool_use_id="toolu_r2", confirm=False)
        r = self.hook(
            "post-tool-failure",
            {
                "tool_name": "Agent",
                "tool_use_id": "toolu_r2",
                "tool_input": {"prompt": "Подзадача: r2\n" + GATE_LINES},
                "error": "Subagent nesting limit reached",
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertTrue((self.subtasks / "r2.stopped").exists())
        r = self.hook("stop", {"background_tasks": []})
        self.assertIn("r2 (agent_id ?)", json.loads(r.stdout)["reason"])

    def test_rejected_relaunch_restores_previous_marks(self) -> None:
        # Перезапуск остановившейся подзадачи отклонён: прежние отметки
        # (.stopped, .done, подтверждённый .started) возвращаются, подзадача
        # не теряется.
        def failure(name: str, tool_use_id: str) -> None:
            self.hook(
                "post-tool-failure",
                {
                    "tool_name": "Agent",
                    "tool_use_id": tool_use_id,
                    "tool_input": {"prompt": f"Подзадача: {name}\n" + GATE_LINES},
                    "error": "отказ",
                },
            )

        def rejected_in_transcript(tool_use_id: str) -> str:
            return self.session_transcript(
                [
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "is_error": True,
                                }
                            ]
                        },
                        "toolUseResult": "Error: отклонено",
                    }
                ]
            )

        # Отказ без PostToolUse: Stop находит ошибку в транскрипте.
        self.launch("a1", "правка", agent_id="ag-old", tool_use_id="toolu_old")
        self.write_stopped("a1", "ag-old")
        self.launch("a1", "правка", tool_use_id="toolu_new2", confirm=False)
        transcript = rejected_in_transcript("toolu_new2")
        r = self.hook("stop", {"background_tasks": [], "transcript_path": transcript})
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("a1 (agent_id ag-old)", out["reason"])
        record = json.loads((self.subtasks / "a1.started").read_text(encoding="utf-8"))
        self.assertEqual(record["agent_ids"], ["ag-old"])
        # Ошибка выполнения перезапуска (PostToolUseFailure): перезапуск мог
        # поработать, прежний итог его не покрывает — остановка без итога
        # (RP-2445, п. 2).
        self.launch("a2", "тесты", tool_use_id="toolu_a2")
        (self.subtasks / "a2.done").write_text("итог\n", encoding="utf-8")
        self.launch("a2", "тесты", tool_use_id="toolu_a2b", confirm=False)
        failure("a2", "toolu_a2b")
        self.assertFalse((self.subtasks / "a2.done").exists())
        self.assertTrue((self.subtasks / "a2.stopped").exists())
        self.assertEqual(sorted(self.subtasks.glob("a2.*.prev")), [])
        (self.subtasks / "a2.done").write_text("итог снят\n", encoding="utf-8")
        # Подтверждённый перезапуск снимает прежние отметки.
        self.launch("a1", "правка", agent_id="ag-new", tool_use_id="toolu_new3")
        self.assertFalse((self.subtasks / "a1.stopped").exists())
        live = [self.live_agent("x", agent_id="ag-new")]
        r = self.hook("stop", {"background_tasks": live})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def pre_agent(
        self, name: str, tool_use_id: str, transcript: str | None = None
    ) -> subprocess.CompletedProcess:
        """PreToolUse на Agent с меткой и транскриптом сессии."""
        data: dict[str, object] = {
            "tool_name": "Agent",
            "tool_use_id": tool_use_id,
            "tool_input": {
                "prompt": f"Подзадача: {name}\n" + GATE_LINES + "Сделай.",
                "description": "правка",
                "subagent_type": "general-purpose",
            },
        }
        if transcript is not None:
            data["transcript_path"] = transcript
        return self.hook("pre-tool", data)

    def test_concurrent_launch_same_tag_denied(self) -> None:
        # RP-2445, п. 3: два вызова Agent с одной меткой в одном сообщении.
        # Второй отклоняется, пока первый не получил ответа: иначе он
        # перезаписал бы tool_use_id, и PostToolUse первого был бы отброшен.
        transcript = self.session_transcript([])
        r = self.pre_agent("x", "toolu_x1", transcript)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        r = self.pre_agent("x", "toolu_x2", transcript)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("x", out["permissionDecisionReason"])
        record = json.loads((self.subtasks / "x.started").read_text(encoding="utf-8"))
        self.assertEqual(record["tool_use_id"], "toolu_x1")
        r = self.post_launch("x", self.async_launched("ag-x1"), "toolu_x1")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        record = json.loads((self.subtasks / "x.started").read_text(encoding="utf-8"))
        self.assertEqual(record["agent_ids"], ["ag-x1"])
        # Без транскрипта ответ первого вызова не найти: отказ (fail-closed).
        self.pre_agent("y", "toolu_y1")
        r = self.pre_agent("y", "toolu_y2")
        self.assertIn('"deny"', r.stdout)

    def test_retry_after_rejected_call_same_tag_allowed(self) -> None:
        # Первый вызов отклонён другим хуком (ошибка в транскрипте, PostToolUse
        # нет): повтор под тем же именем разрешён, прежние отметки возвращаются
        # и снова откладываются.
        self.launch("z", "правка", agent_id="ag-old", tool_use_id="toolu_old")
        self.write_stopped("z", "ag-old")
        self.pre_agent("z", "toolu_z1", self.session_transcript([]))
        transcript = self.session_transcript(
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_z1",
                                "is_error": True,
                            }
                        ]
                    },
                    "toolUseResult": "Error: отклонено",
                }
            ]
        )
        r = self.pre_agent("z", "toolu_z2", transcript)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        record = json.loads((self.subtasks / "z.started").read_text(encoding="utf-8"))
        self.assertEqual(record["tool_use_id"], "toolu_z2")
        prev = json.loads(
            (self.subtasks / "z.started.prev").read_text(encoding="utf-8")
        )
        self.assertEqual(prev["agent_ids"], ["ag-old"])
        self.assertTrue((self.subtasks / "z.stopped.prev").exists())
        # Повтор того же вызова (тот же tool_use_id) не отклоняется.
        r = self.pre_agent("z", "toolu_z2", transcript)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_foreground_failure_after_start_keeps_subtask(self) -> None:
        # RP-2445, п. 2. Порядок событий снят пробой Claude Code 2.1.323:
        # субагент на переднем плане с недоступной моделью даёт PreToolUse,
        # SubagentStart, StopFailure (без SubagentStop) и PostToolUseFailure
        # с текстом ошибки, без agentId. Субагент запускался: подзадача
        # не снимается, а считается остановившейся без итога.
        self.launch("f1", "правка", tool_use_id="toolu_f1", confirm=False)
        r = self.hook(
            "post-tool-failure",
            {
                "tool_name": "Agent",
                "tool_use_id": "toolu_f1",
                "tool_input": {"prompt": "Подзадача: f1\n" + GATE_LINES},
                "error": "Agent terminated early due to an API error: model_not_found",
                "is_interrupt": False,
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        record = json.loads((self.subtasks / "f1.started").read_text(encoding="utf-8"))
        self.assertEqual(record["launch"], "failed")
        stopped = json.loads((self.subtasks / "f1.stopped").read_text(encoding="utf-8"))
        self.assertIn("API error", stopped["last"])
        r = self.hook("stop", {"background_tasks": []})
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("f1", out["reason"])
        # Итог записан до сбоя: остановки без итога нет.
        self.launch("f2", "правка", tool_use_id="toolu_f2", confirm=False)
        (self.subtasks / "f2.done").write_text("итог\n", encoding="utf-8")
        self.hook(
            "post-tool-failure",
            {
                "tool_name": "Agent",
                "tool_use_id": "toolu_f2",
                "tool_input": {"prompt": "Подзадача: f2\n" + GATE_LINES},
                "error": "Agent terminated early due to an API error",
            },
        )
        self.assertFalse((self.subtasks / "f2.stopped").exists())
        # Перезапуск под тем же именем проходит и снимает отметки сбоя.
        self.launch("f1", "правка", agent_id="ag-f1", tool_use_id="toolu_f1b")
        self.assertFalse((self.subtasks / "f1.stopped").exists())
        live = [self.live_agent("x", agent_id="ag-f1")]
        r = self.hook("stop", {"background_tasks": live})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_unconfirmed_launch_blocks(self) -> None:
        # Ответа нет ни в PostToolUse, ни в транскрипте: отказ (fail-closed).
        self.launch("u1", "правка", tool_use_id="toolu_u1", confirm=False)
        transcript = self.session_transcript([])
        r = self.hook(
            "stop",
            {
                "background_tasks": [self.live_agent("правка")],
                "transcript_path": transcript,
            },
        )
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Запуск не подтверждён: u1", out["reason"])
        # Ответ найден в транскрипте: agentId записывается, подзадача живая.
        transcript = self.session_transcript(
            [
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_u1"}]
                    },
                    "toolUseResult": self.async_launched("ag-u1"),
                }
            ]
        )
        r = self.hook(
            "stop",
            {
                "background_tasks": [self.live_agent("x", agent_id="ag-u1")],
                "transcript_path": transcript,
            },
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        record = json.loads((self.subtasks / "u1.started").read_text(encoding="utf-8"))
        self.assertEqual(record["agent_ids"], ["ag-u1"])

    def test_same_description_matched_by_agent_id(self) -> None:
        self.launch("b1", "правка хука", agent_id="ag-1", tool_use_id="toolu_b1")
        self.launch("b2", "правка хука", agent_id="ag-2", tool_use_id="toolu_b2")
        live = [self.live_agent("правка хука", agent_id="ag-1")]
        r = self.hook("stop", {"background_tasks": live})
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Завершены без итога: b2 (agent_id ag-2)", out["reason"])
        self.assertIn("Ещё идут: b1", out["reason"])

    def test_teammate_and_remote_are_live(self) -> None:
        self.launch("t1", "команда", confirm=False, tool_use_id="toolu_t1")
        self.post_launch(
            "t1",
            {
                "status": "teammate_spawned",
                "agentId": "t1@team",
                "teammate_id": "tm-1",
                "prompt": "p",
            },
            "toolu_t1",
        )
        self.launch("c1", "облако", confirm=False, tool_use_id="toolu_c1")
        self.post_launch(
            "c1",
            {
                "status": "remote_launched",
                "taskId": "r-9",
                "sessionUrl": "u",
                "description": "облако",
                "prompt": "p",
                "outputFile": "o",
            },
            "toolu_c1",
        )
        live = [
            {"id": "tm-1", "type": "teammate", "status": "running", "description": "x"},
            {
                "id": "r-9",
                "type": "cloud session",
                "status": "running",
                "description": "y",
            },
        ]
        r = self.hook("stop", {"background_tasks": live})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_terminal_status_is_not_live(self) -> None:
        self.launch("s1", "правка", agent_id="ag-s1", tool_use_id="toolu_s1")
        for status in ("completed", "failed", "killed"):
            with self.subTest(status=status):
                live = [self.live_agent("правка", agent_id="ag-s1", status=status)]
                r = self.hook("stop", {"background_tasks": live})
                out = json.loads(r.stdout)
                self.assertEqual(out["decision"], "block")
                self.assertIn("Завершены без итога: s1", out["reason"])
        r = self.hook(
            "stop", {"background_tasks": [self.live_agent("x", agent_id="ag-s1")]}
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_response_status_checked(self) -> None:
        # Ответ Agent без известного статуса или без идентификатора — запуск
        # не подтверждён, даже если среди живых есть субагент с тем же описанием.
        for response in (
            {"status": "weird", "agentId": "ag-w"},
            {"status": "async_launched"},
            {"status": "remote_launched", "agentId": "ag-w"},
            "строка",
            None,
        ):
            with self.subTest(response=response):
                self.launch("w1", "правка", confirm=False, tool_use_id="toolu_w1")
                r = self.post_launch("w1", response, "toolu_w1")
                self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
                r = self.hook(
                    "stop",
                    {"background_tasks": [self.live_agent("правка", agent_id="ag-w")]},
                )
                out = json.loads(r.stdout)
                self.assertEqual(out["decision"], "block")
                self.assertIn("Запуск не подтверждён: w1", out["reason"])

    def test_stop_before_subagent_stop_names_agent_id(self) -> None:
        # Stop раньше SubagentStop только что закончившегося агента: отметки
        # остановки ещё нет, но agentId из PostToolUse даёт координатору адрес
        # для возобновления.
        self.launch("e1", "правка", agent_id="ag-e1", tool_use_id="toolu_e1")
        r = self.hook("stop", {"background_tasks": []})
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("e1 (agent_id ag-e1)", out["reason"])
        # SubagentStop пришёл следом: подзадача судится отметкой остановки.
        transcript = self.transcript("Подзадача: e1\nСделай.")
        self.hook(
            "subagent-stop",
            self.stop_data(agent_id="ag-e1", agent_transcript_path=transcript),
        )
        r = self.hook("stop", {"background_tasks": []})
        reason = json.loads(r.stdout)["reason"]
        self.assertIn("e1 (agent_id ag-e1)", reason)
        self.assertNotIn("Завершены без итога", reason)

    def test_subagent_post_tool_ignored(self) -> None:
        # Agent, запущенный субагентом: отметки координатора не трогаются.
        self.launch("v1", "правка", agent_id="ag-v1", tool_use_id="toolu_v1")
        before = (self.subtasks / "v1.started").read_text(encoding="utf-8")
        data = {
            "tool_name": "Agent",
            "agent_id": "id-9",
            "tool_use_id": "toolu_v1",
            "tool_input": {"prompt": "Подзадача: v1\n"},
            "tool_response": self.async_launched("ag-x"),
        }
        self.hook("post-tool", data)
        self.hook("post-tool-failure", {**data, "error": "x"})
        self.assertEqual(
            (self.subtasks / "v1.started").read_text(encoding="utf-8"), before
        )

    def test_subagent_stop_marks_registered(self) -> None:
        transcript = self.transcript("Подзадача: a1\nСделай.")
        r = self.hook("subagent-stop", self.stop_data(agent_transcript_path=transcript))
        self.assertEqual(r.returncode, 0, r.stderr)
        record = json.loads((self.subtasks / "a1.stopped").read_text(encoding="utf-8"))
        self.assertIs(record["registered"], False)
        (self.subtasks / "a1.started").write_text("{}", encoding="utf-8")
        self.hook("subagent-stop", self.stop_data(agent_transcript_path=transcript))
        record = json.loads((self.subtasks / "a1.stopped").read_text(encoding="utf-8"))
        self.assertIs(record["registered"], True)

    def test_agent_with_bad_tag_name(self) -> None:
        r = self.hook(
            "pre-tool",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "prompt": "Подзадача: А3\n" + GATE_LINES + "Сделай.",
                    "subagent_type": "general-purpose",
                },
            },
        )
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertIn("имя не подходит", out["additionalContext"])
        self.assertIn("[a-z0-9][a-z0-9_.-]*", out["additionalContext"])

    def test_deny_wait_does_not_recommend_wait_output(self) -> None:
        r = self.hook(
            "pre-tool",
            {"tool_name": "Monitor", "tool_input": {}, "agent_id": "id-1"},
        )
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("wait-done.sh", reason)
        self.assertNotIn("wait-output", reason)

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

    @staticmethod
    def sent(to: str, success: object = True) -> dict:
        return {
            "tool_name": "SendMessage",
            "tool_input": {"to": to},
            "tool_response": {"success": success, "message": "queued"},
        }

    def test_send_message_clears_stopped_after_success(self) -> None:
        mine = self.write_stopped("a1", "ag-77")
        other = self.write_stopped("a2", "ag-88")
        # До вызова отметка остаётся: отправка ещё может не удаться.
        r = self.hook(
            "pre-tool", {"tool_name": "SendMessage", "tool_input": {"to": "ag-77"}}
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertTrue(mine.exists())
        r = self.hook("post-tool", self.sent("ag-77"))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.hook("post-tool", self.sent("stranger"))
        self.assertTrue(other.exists())

    def test_send_message_failure_keeps_stopped(self) -> None:
        mine = self.write_stopped("a1", "ag-77")
        for response in (
            {"success": False, "message": "no such agent"},
            "ошибка",
            None,
        ):
            with self.subTest(response=response):
                data = self.sent("ag-77")
                data["tool_response"] = response
                self.hook("post-tool", data)
                self.assertTrue(mine.exists())
        # Субагент шлёт сообщение сам: отметки координатора не трогаются.
        self.hook("post-tool", {**self.sent("ag-77"), "agent_id": "id-9"})
        self.assertTrue(mine.exists())

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

    def start_run(
        self, name: str = "r1", started_ms: int | None = None
    ) -> tuple[Path, int]:
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
        start = time.time_ns() // 1_000_000 if started_ms is None else started_ms
        (run_dir / "meta.txt").write_text(
            f"cwd: /\nstarted_ms: {start}\npid: {proc.pid}\n", encoding="utf-8"
        )
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

    def test_stop_run_started_before_stop_is_not_wait(self) -> None:
        now = time.time_ns() // 1_000_000
        self.write_stopped("a1", at_ms=now)
        self.write_stopped("a2", "id-2", at_ms=now - 60_000)
        self.start_run(started_ms=now - 1_000)
        r = self.hook("stop", {})
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        # Прогон начат после остановки a2, но раньше остановки a1.
        self.assertIn("a1", out["reason"])
        self.assertNotIn("a2", out["reason"])
        self.start_run("r2", started_ms=now + 1)
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

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
                str(WAIT_TERM_S),
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
        self.assertIn("ждёт сигнал", waiter.stderr.readline())
        r = self.hook("stop", {})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    # --- ворота постановки: Agent/Task во всех сессиях ---

    def plain_env(self) -> dict:
        keep = ("DUTY_RESULTS_DIR",)
        return {
            k: v for k, v in self.env.items() if not k.startswith("DUTY_") or k in keep
        }

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
        path = self.results / "recall" / "topic-20261106-1200.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("справка\n", encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_gate_requires_priorities_line(self) -> None:
        base = (
            "Критерий готовности: `pytest -q` зелёный\nУсловие остановки: падение\n"
            "Справка: не нужна — проба\n"
        )
        for env in (self.plain_env(), self.env):
            self.assert_denied(self.agent_call(base + "Сделай.", env), "Приоритеты")
            self.assert_denied(
                self.agent_call(base + "Приоритеты: <порядок>\n", env), "Приоритеты"
            )
            self.assert_not_denied(
                self.agent_call(
                    base + "Приоритеты: развилок нет — линейная задача\n", env
                )
            )

    def test_gate_requires_recall_line(self) -> None:
        base = (
            "Критерий готовности: `pytest -q` зелёный\nУсловие остановки: падение\n"
            "Приоритеты: развилок нет — линейная задача\n"
        )
        for env in (self.plain_env(), self.env):
            self.assert_denied(self.agent_call(base + "Сделай.", env), "Справка")
            self.assert_denied(
                self.agent_call(base + "Справка: не нужна\n", env), "Справка"
            )
            self.assert_denied(
                self.agent_call(base + f"Справка: {self.root}/нет.md\n", env), "Справка"
            )

    def test_gate_recall_file_freshness(self) -> None:
        base = (
            "Критерий готовности: `pytest -q` зелёный\nУсловие остановки: падение\n"
            "Приоритеты: развилок нет — линейная задача\n"
        )
        path = self.recall_file(3600)
        for env in (self.plain_env(), self.env):
            self.assert_not_denied(self.agent_call(f"{base}Справка: {path}\n", env))
        for age in (25 * 3600, 30 * 24 * 3600):
            self.recall_file(age)
            for env in (self.plain_env(), self.env):
                self.assert_not_denied(self.agent_call(f"{base}Справка: {path}\n", env))
        other = self.root / "recall.md"
        other.write_text("x\n", encoding="utf-8")
        for env in (self.plain_env(), self.env):
            self.assert_denied(
                self.agent_call(f"{base}Справка: {other}\n", env), "Справка"
            )

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
        prompt = "## Критерий готовности\n\n`pytest -q` зелёный\n\n## Условие остановки\n\nпадение\n\n## Приоритеты\n\nразвилок нет — линейная задача\n\nСправка: не нужна — проба\n"
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
        for event in ("subagent-stop", "stop", "post-tool"):
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
        guard = entries["Monitor|Bash"]["hooks"][0]["command"]
        self.assertIn("DUTY_ROLE", guard)
        (post,) = config["hooks"]["PostToolUse"]
        # Agent и Task — подтверждение запуска agentId (RP-2417), SendMessage —
        # возобновление.
        self.assertEqual(
            set(post["matcher"].split("|")), {"SendMessage", "Agent", "Task"}
        )
        self.assertIn("DUTY_ROLE", post["hooks"][0]["command"])
        self.assertTrue(post["hooks"][0]["command"].endswith("post-tool"))
        (failure,) = config["hooks"]["PostToolUseFailure"]
        self.assertEqual(set(failure["matcher"].split("|")), {"Agent", "Task"})
        self.assertIn("DUTY_ROLE", failure["hooks"][0]["command"])
        self.assertTrue(failure["hooks"][0]["command"].endswith("post-tool-failure"))

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


class UncoveredTest(unittest.TestCase):
    """signals.py вызывается, только если прогоны покрыли не все остановки."""

    def setUp(self) -> None:
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("task_guard", HOOK)
        assert spec and spec.loader
        self.tg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tg)
        self.mock = mock
        self.task = Path("/нет")

    def check(self, runs: list[tuple[str, int]], expect_signals: bool) -> list:
        pending = [("a1", "id-1", 100), ("a2", "id-2", 200)]
        with (
            self.mock.patch.object(
                self.tg, "stopped_without_result", return_value=pending
            ),
            self.mock.patch.object(self.tg, "run_waits", return_value=runs),
            self.mock.patch.object(self.tg, "signal_waits", return_value=[]) as sig,
        ):
            left = self.tg.uncovered(self.task)
        self.assertEqual(sig.called, expect_signals)
        return left

    def test_runs_cover_all_skip_signals(self) -> None:
        self.assertEqual(self.check([("прогон r1", 300)], False), [])

    def test_partial_cover_asks_signals(self) -> None:
        left = self.check([("прогон r1", 150)], True)
        self.assertEqual([name for name, _, _ in left], ["a2"])


class TranscriptTailTest(unittest.TestCase):
    """RP-2445, п. 4: хвост транскрипта читается один раз на Stop."""

    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("task_guard", HOOK)
        assert spec and spec.loader
        self.tg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tg)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = self.root / "task"
        (self.task / "subtasks").mkdir(parents=True)

    def test_unsettled_marks_read_tail_once(self) -> None:
        from unittest import mock

        names = [f"n{index}" for index in range(5)]
        entries = []
        for name in names:
            record = {"at_ms": 1, "tool_use_id": f"toolu_{name}"}
            path = self.task / "subtasks" / f"{name}.started"
            path.write_text(json.dumps(record), encoding="utf-8")
            block = {"type": "tool_result", "tool_use_id": f"toolu_{name}"}
            entries.append(
                {
                    "type": "user",
                    "message": {"content": [block]},
                    "toolUseResult": {
                        "status": "async_launched",
                        "agentId": f"ag-{name}",
                    },
                }
            )
        transcript = self.root / "session.jsonl"
        transcript.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )
        real_open = Path.open
        opened: list[str] = []

        def counting_open(path: Path, *args: object, **kwargs: object):
            if path == transcript:
                opened.append(str(path))
            return real_open(path, *args, **kwargs)

        live = [{"id": f"ag-{name}", "status": "running"} for name in names]
        with mock.patch.object(Path, "open", counting_open):
            running, ended, unknown, unconfirmed = self.tg.launched_without_result(
                self.task, live, str(transcript)
            )
        self.assertEqual(len(opened), 1)
        # Поведение прежнее: каждый запуск подтверждён ответом из транскрипта.
        self.assertEqual((running, ended, unknown, unconfirmed), (names, [], [], []))
        record = json.loads(
            (self.task / "subtasks" / "n3.started").read_text(encoding="utf-8")
        )
        self.assertEqual(record["agent_ids"], ["ag-n3"])

    def test_last_turn_skill_reads_tail(self) -> None:
        # Общий генератор: навык находится, обрезанная строка пропускается.
        transcript = self.root / "skill.jsonl"
        lines = [
            '{"обрезанная',
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "<command-name>/code-review</command-name>"},
                }
            ),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.tg.last_turn_skill(str(transcript)), "code-review")
        self.assertIsNone(self.tg.last_turn_skill(str(self.root / "нет.jsonl")))


if __name__ == "__main__":
    unittest.main()
