"""Проверки вопросов: запуск `python3 -I scripts/tests/test_ask.py` или через pytest."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ask.py"
# Замер на станции (32 ядра), 5 прогонов: wait до выхода после answer — 0,2-0,3 с,
# самая долгая операция теста; узел Jenkins медленнее на ядро в 4,5 раза: 1,35 с,
# с тройным запасом 4 с. Предел — предохранитель от зависания.
LIMIT_S = 4

BRIEF = """# Задача probe

## Цель

Проверить вопросы.

## Служебное (заполняет dispatch.sh)

- символ workspace: `Ⓑ`
- агент herdr и сессия Claude: `probe`, модель `sonnet`
"""


class AskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.results = root / "results"
        self.task = self.results / "probe"
        self.task.mkdir(parents=True)
        (self.task / "brief.md").write_text(BRIEF, encoding="utf-8")
        self.signals = root / "signals"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        self.env.update(
            DUTY_RESULTS_DIR=str(self.results),
            DUTY_SIGNALS_DIR=str(self.signals),
            DUTY_TASK=str(self.task),
            DUTY_NAME="probe",
        )

    def run_cmd(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *argv],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def new(self, slug: str = "readme", *extra: str) -> subprocess.CompletedProcess:
        return self.run_cmd(
            "new",
            slug,
            "--title",
            "Чем дополнить README",
            "--step",
            "2",
            "--depends",
            "правка README",
            "--option",
            "строка назначения — коротко",
            "--option",
            "таблица модулей — подробно",
            "--recommended",
            "1",
            *extra,
        )

    def test_new_writes_header_and_signal(self) -> None:
        r = self.new()
        self.assertEqual(r.returncode, 0, r.stderr)
        path = self.task / "questions" / "01-readme.md"
        text = path.read_text(encoding="utf-8")
        for line in (
            "# Вопрос 01: Чем дополнить README",
            "- символ: Ⓑ",
            "- задача: probe",
            "- модель: sonnet",
            "- цель задачи: Проверить вопросы.",
            "- шаг: 2",
            "- от ответа зависит: правка README",
            "- необратимо: нет",
            "- рекомендуемый вариант: 1",
            "1. строка назначения — коротко",
            "## Ответ",
        ):
            self.assertIn(line, text)
        sig = json.loads(
            (self.signals / "question-probe-01.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sig["text"], "Чем дополнить README")
        self.assertIn("продолжать по рекомендуемому", r.stdout)

    def test_numbers_grow_and_env_wins(self) -> None:
        self.env["DUTY_SYMBOL"] = "Ⓒ"
        self.new()
        r = self.new("second", "--irreversible")
        self.assertIn("ask.py wait 02", r.stdout)
        text = (self.task / "questions" / "02-second.md").read_text(encoding="utf-8")
        self.assertIn("- символ: Ⓒ", text)
        self.assertIn("- необратимо: да", text)

    def test_bad_arguments(self) -> None:
        r = self.run_cmd(
            "new",
            "x",
            "--title",
            "t",
            "--step",
            "s",
            "--depends",
            "d",
            "--option",
            "один",
            "--recommended",
            "1",
        )
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.new("Bad Slug").returncode, 2)
        r = self.run_cmd(
            "new",
            "x",
            "--title",
            "t",
            "--step",
            "s",
            "--depends",
            "d",
            "--option",
            "а",
            "--option",
            "б",
            "--recommended",
            "3",
        )
        self.assertEqual(r.returncode, 2)

    def test_answer_and_list(self) -> None:
        self.new()
        r = self.run_cmd("list", "--open")
        self.assertIn(
            "| Ⓑ | probe | 01 | Чем дополнить README | нет | 1. строка назначения — коротко | нет |",
            r.stdout,
        )
        r = self.run_cmd(
            "answer",
            "probe",
            "1",
            "строка назначения",
            "--ref",
            "docs/decisions/README.md",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.task / "questions" / "01-readme.md").read_text(encoding="utf-8")
        self.assertIn("строка назначения\n\n- ссылка: docs/decisions/README.md", text)
        self.assertTrue((self.signals / "answer-probe-01.json").exists())
        self.assertEqual(self.run_cmd("list", "--open").stdout.strip(), "вопросов нет")
        rows = json.loads(self.run_cmd("list", "--json").stdout)
        self.assertEqual(
            [(r["task"], r["nn"], r["answered"]) for r in rows], [("probe", "01", True)]
        )
        self.assertEqual(self.run_cmd("answer", "probe", "01", "ещё раз").returncode, 2)
        self.assertEqual(
            self.run_cmd("answer", "probe", "07", "нет такого").returncode, 2
        )

    def test_wait_returns_answer(self) -> None:
        self.new("block", "--irreversible")
        waiter = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "wait", "01"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        assert waiter.stderr is not None
        # signals.py wait сообщает в stderr, когда наблюдение уже поставлено.
        self.assertIn("ждёт сигнал", waiter.stderr.readline())
        self.run_cmd(
            "answer", "probe", "01", "вариант 2", "--ref", "ответ пользователя"
        )
        out, _ = waiter.communicate(timeout=LIMIT_S)
        self.assertEqual(waiter.returncode, 0)
        self.assertIn("вариант 2", out)

    def test_new_clears_stale_answer_signal(self) -> None:
        # Каталог задачи пересоздан: вопрос 01 уже был отвечен, сигнал остался.
        self.new()
        self.run_cmd("answer", "probe", "01", "старый ответ")
        (self.task / "questions" / "01-readme.md").unlink()
        self.new("again", "--irreversible")
        self.assertFalse((self.signals / "answer-probe-01.json").exists())
        # Срок 1 с — сигнала нет, ожидание заканчивается по сроку (код 124).
        r = self.run_cmd("wait", "01", "--timeout-s", "1")
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertNotIn("старый ответ", r.stdout)


if __name__ == "__main__":
    unittest.main()
