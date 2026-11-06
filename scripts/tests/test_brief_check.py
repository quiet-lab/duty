"""Проверки `scripts/brief_check.py`: форма записки и приглашения."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "scripts"))

import brief_check

FULL = """# Задача x

## Цель

Сделать.

## Критерий готовности

`pytest -q` — зелёный.

## Условие остановки

Падение теста, не починенное за один подход.

## Приоритеты

Правка одного репозитория важнее общей оснастки.

## Ссылки

- Правила задачи — `rules.md`.
- Прочитанное при постановке: `CLAUDE.md`.
- Справка: не нужна — проба.

## Служебное (заполняет dispatch.sh)

- workspace: `w1`

## Передача

—
"""


PRIORITIES_OK = "Приоритеты: развилок нет — линейная задача"
RECALL_OK = f"\n{PRIORITIES_OK}\nСправка: не нужна — проба\n"


def check_prompt(text: str) -> list[str]:
    """Приглашение с годными строками «Приоритеты» и «Справка»: проверяются остальные."""
    return brief_check.check_prompt(text + RECALL_OK)


def without(text: str, name: str) -> str:
    """Записка без заголовка раздела `name` (его тело сливается с предыдущим)."""
    return text.replace(f"## {name}\n", "")


class CheckBriefTest(unittest.TestCase):
    def test_full_brief_passes(self) -> None:
        self.assertEqual(brief_check.check_brief(FULL), [])

    def test_brief_without_priorities_rejected(self) -> None:
        self.assertEqual(
            brief_check.check_brief(without(FULL, "Приоритеты")), ["Приоритеты"]
        )
        empty = FULL.replace(
            "Правка одного репозитория важнее общей оснастки.", "{{PRIORITIES}}"
        )
        self.assertEqual(brief_check.check_brief(empty), ["Приоритеты"])

    def test_missing_section_named(self) -> None:
        for name in brief_check.SECTIONS:
            with self.subTest(name=name):
                self.assertIn(name, brief_check.check_brief(without(FULL, name)))

    def test_empty_bodies(self) -> None:
        for filler in ("—", "-", "", "{{DONE}}", "<критерий>", "- —"):
            with self.subTest(filler=filler):
                text = FULL.replace("`pytest -q` — зелёный.", filler).replace(
                    "Падение теста, не починенное за один подход.", filler
                )
                got = brief_check.check_brief(text)
                self.assertIn("Критерий готовности", got)
                self.assertIn("Условие остановки", got)

    def test_criterion_without_command(self) -> None:
        text = FULL.replace("`pytest -q` — зелёный.", "Всё работает.")
        self.assertEqual(
            brief_check.check_brief(text), [brief_check.DONE_COMMAND_VIOLATION]
        )

    def test_criterion_placeholder_command_not_counted(self) -> None:
        text = FULL.replace("`pytest -q` — зелёный.", "`<команда>` зелёная.")
        self.assertEqual(
            brief_check.check_brief(text), [brief_check.DONE_COMMAND_VIOLATION]
        )

    def test_criterion_code_block_is_command(self) -> None:
        text = FULL.replace("`pytest -q` — зелёный.", "```sh\npytest -q\n```\nЗелёный.")
        self.assertEqual(brief_check.check_brief(text), [])

    def test_read_required(self) -> None:
        for line in (
            "- Прочитанное при постановке: {{READ}}",
            "- Прочитанное при постановке:",
            "- Прочитанное при постановке: —",
            "",
        ):
            with self.subTest(line=line):
                text = FULL.replace("- Прочитанное при постановке: `CLAUDE.md`.", line)
                self.assertEqual(
                    brief_check.check_brief(text), [brief_check.LINKS_READ_VIOLATION]
                )

    def test_read_forms(self) -> None:
        for line in (
            "Прочитанное при постановке: `CLAUDE.md`",
            "- Прочитанное при постановке:\n  `CLAUDE.md`, `docs/README.md`",
            "### Прочитанное при постановке\n\n`CLAUDE.md`",
        ):
            with self.subTest(line=line):
                text = FULL.replace("- Прочитанное при постановке: `CLAUDE.md`.", line)
                self.assertEqual(brief_check.check_brief(text), [])

    def test_headings_in_code_blocks_ignored(self) -> None:
        text = without(FULL, "Условие остановки").replace(
            "## Ссылки", "```text\n## Условие остановки\n\nТекст.\n```\n\n## Ссылки"
        )
        self.assertIn("Условие остановки", brief_check.check_brief(text))

    def test_service_block_and_handoff_do_not_matter(self) -> None:
        text = FULL.split("## Служебное")[0]
        self.assertEqual(brief_check.check_brief(text), [])

    def test_real_task_brief_shape(self) -> None:
        text = """# Задача rules-gate

## Цель

Сделать ворота.

## Критерий готовности

Все команды зелёные:

- `cd duty && python3 -m pytest -q scripts/tests`.

## Условие остановки

- Два правила противоречат друг другу — вопрос пользователю.

## Приоритеты

- Развилок нет — линейная задача.

## Раздел для субагентов

```text
Подзадача: <имя>
Критерий готовности: <команда и ожидаемый результат>
```

## Ссылки

- Правила задачи — `rules.md` рядом.
- Прочитанное при постановке: `~/.claude/CLAUDE.md` (разделы «Постановка
  задачи», «Субагенты»); `CLAUDE.md` монорепозитория.
- Справка: не нужна — готовая записка с решениями.

## Служебное (заполняет dispatch.sh)

- workspace: `w2A`
"""
        self.assertEqual(brief_check.check_brief(text), [])


class ReviewFindingsTest(unittest.TestCase):
    def brief(
        self, done: str = "`pytest -q` зелёный", stop: str = "Падение теста."
    ) -> str:
        return FULL.replace("`pytest -q` — зелёный.", done).replace(
            "Падение теста, не починенное за один подход.", stop
        )

    def test_path_like_fragment_is_not_command(self) -> None:
        for done in (
            "Всё работает, см. `README.md`.",
            "См. `docs/a/b`.",
            "См. `a.b.c`.",
        ):
            with self.subTest(done=done):
                self.assertEqual(
                    brief_check.check_brief(self.brief(done)),
                    [brief_check.DONE_COMMAND_VIOLATION],
                )

    def test_command_forms(self) -> None:
        for done in (
            "`pytest`",
            "`just test`",
            "`./x.sh`",
            "`python3 -m pytest -q`",
            "```\nls\n```",
        ):
            with self.subTest(done=done):
                self.assertEqual(brief_check.check_brief(self.brief(done)), [])

    def test_heading_line_is_not_content(self) -> None:
        text = self.brief(stop="### Подробности")
        self.assertEqual(brief_check.check_brief(text), ["Условие остановки"])

    def test_template_fragments_are_placeholders(self) -> None:
        for value in (
            "<команда> и <результат>",
            "<a>, <b>",
            "`<команда>`",
            "{{X}} {{Y}}",
        ):
            with self.subTest(value=value):
                text = f"Критерий готовности: {value}\nУсловие остановки: падение теста"
                self.assertEqual(check_prompt(text), ["Критерий готовности"])

    def test_value_with_placeholder_part_is_value(self) -> None:
        text = "Критерий готовности: wait-done.sh <каталог> зелёный\nУсловие остановки: падение теста"
        self.assertEqual(check_prompt(text), [])

    def test_labeled_value_forms(self) -> None:
        cases = (
            "Критерий готовности:\n`pytest -q` зелёный\nУсловие остановки:\n- падение теста",
            "1. Критерий готовности: `pytest -q` зелёный\n2. Условие остановки: падение теста",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(check_prompt(text), [])

    def test_label_followed_by_label_is_empty(self) -> None:
        text = "Критерий готовности:\nУсловие остановки: падение теста"
        self.assertEqual(check_prompt(text), ["Критерий готовности"])
        text = "Критерий готовности:\n<команда>\nУсловие остановки: падение теста"
        self.assertEqual(check_prompt(text), ["Критерий готовности"])


class CheckPromptTest(unittest.TestCase):
    DONE = "Критерий готовности: `pytest -q` зелёный"
    STOP = "Условие остановки: падение теста — отчёт"

    def test_both_lines(self) -> None:
        self.assertEqual(check_prompt(f"Сделай.\n{self.DONE}\n{self.STOP}\n"), [])

    def test_missing_one(self) -> None:
        self.assertEqual(check_prompt(self.DONE), ["Условие остановки"])
        self.assertEqual(check_prompt(self.STOP), ["Критерий готовности"])

    def test_none(self) -> None:
        self.assertEqual(
            check_prompt("Сделай."),
            ["Критерий готовности", "Условие остановки"],
        )

    def test_case_bullets_and_bold(self) -> None:
        text = "- **критерий готовности:** `pytest`\n* УСЛОВИЕ ОСТАНОВКИ: стоп"
        self.assertEqual(check_prompt(text), [])

    def test_headings(self) -> None:
        text = "## Критерий готовности\n\n`x` зелёный\n\n### Условие остановки\n\nПадение.\n"
        self.assertEqual(check_prompt(text), [])

    def test_heading_with_empty_body(self) -> None:
        text = "## Критерий готовности\n\n## Условие остановки\n\nПадение.\n"
        self.assertEqual(check_prompt(text), ["Критерий готовности"])

    def test_empty_values(self) -> None:
        for value in ("", "—", "<команда и ожидаемый результат>", "-"):
            with self.subTest(value=value):
                text = f"Критерий готовности: {value}\n{self.STOP}"
                self.assertEqual(check_prompt(text), ["Критерий готовности"])

    def test_inside_code_block_counts(self) -> None:
        text = f"```text\n{self.DONE}\n{self.STOP}\n```"
        self.assertEqual(check_prompt(text), [])


class RecallLineTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / "results" / "recall").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        env["DUTY_RESULTS_DIR"] = str(self.root / "results")
        env["DUTY_CONFIG_USER"] = str(self.root / "no-user.toml")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = self.root / "results" / "recall" / "topic-20261106-1200.md"
        self.file.write_text("справка\n", encoding="utf-8")
        self.touch(3600)

    def touch(self, age_s: float) -> None:
        stamp = self.NOW - age_s
        os.utime(self.file, (stamp, stamp))

    def brief(self, line: str) -> str:
        return FULL.replace("- Справка: не нужна — проба.", line)

    def check(self, line: str) -> list[str]:
        return brief_check.check_brief(self.brief(line), root=self.root, now=self.NOW)

    def test_missing_line(self) -> None:
        self.assertEqual(self.check(""), [brief_check.LINKS_RECALL_VIOLATION])
        self.assertEqual(self.check("- Справка: {{RECALL}}"), ["Ссылки / Справка"])
        self.assertEqual(self.check("- Справка: —"), ["Ссылки / Справка"])

    def test_fresh_file_absolute_and_relative(self) -> None:
        for value in (
            str(self.file),
            f"`{self.file}`",
            "results/recall/topic-20261106-1200.md",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.check(f"- Справка: {value}"), [])

    def test_old_file(self) -> None:
        self.touch(25 * 3600)
        self.assertEqual(
            self.check(f"- Справка: {self.file}"), ["Ссылки / Справка (старше суток)"]
        )
        self.touch(23 * 3600)
        self.assertEqual(self.check(f"- Справка: {self.file}"), [])

    def test_future_file(self) -> None:
        self.touch(-3600)
        self.assertEqual(
            self.check(f"- Справка: {self.file}"),
            ["Ссылки / Справка (время в будущем)"],
        )
        self.touch(-60)
        self.assertEqual(self.check(f"- Справка: {self.file}"), [])

    def test_missing_file(self) -> None:
        for value in (
            str(self.root / "нет.md"),
            "results/recall/нет.md",
            "results/recall",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self.check(f"- Справка: {value}"),
                    ["Ссылки / Справка (файла нет)"],
                )

    def test_not_recall_file(self) -> None:
        (self.root / "todo.local.md").write_text("x\n", encoding="utf-8")
        (self.root / "results" / "other.md").write_text("x\n", encoding="utf-8")
        (self.root / "results" / "recall" / "t.txt").write_text("x\n", encoding="utf-8")
        for value in (
            "todo.local.md",
            str(self.root / "todo.local.md"),
            "results/other.md",
            "results/recall/t.txt",
        ):
            with self.subTest(value=value):
                stamp = self.NOW - 60
                os.utime(
                    self.root / value if not value.startswith("/") else value,
                    (stamp, stamp),
                )
                self.assertEqual(
                    self.check(f"- Справка: {value}"),
                    ["Ссылки / Справка (не итог /duty:recall)"],
                )

    def test_not_needed_with_reason(self) -> None:
        for sep in ("—", "–", "-", ":", ",", ";"):
            with self.subTest(sep=sep):
                self.assertEqual(
                    self.check(f"- Справка: не нужна {sep} решения уже сверены"), []
                )
                self.assertEqual(
                    self.check(f"- Справка: не нужна{sep} решения уже сверены"), []
                )

    def test_not_needed_without_reason(self) -> None:
        for value in (
            "не нужна",
            "не нужна —",
            "не нужна — ...",
            "не нужна — <причина>",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self.check(f"- Справка: {value}"),
                    ["Ссылки / Справка (не нужна — без причины)"],
                )

    def test_relative_path_uses_environment_root(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "DUTY_PROJECT"}
        env["CLAUDE_PROJECT_DIR"] = str(self.root)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(
            brief_check.check_brief(
                self.brief("- Справка: results/recall/topic-20261106-1200.md"),
                now=self.NOW,
            ),
            [],
        )

    def test_real_clock_by_default(self) -> None:
        stamp = time.time() - 3600
        os.utime(self.file, (stamp, stamp))
        self.assertEqual(
            brief_check.check_brief(self.brief(f"- Справка: {self.file}")), []
        )

    def test_prompt_priorities_line(self) -> None:
        base = "Критерий готовности: `x` зелёный\nУсловие остановки: падение\nСправка: не нужна — проба\n"
        got = lambda extra: brief_check.check_prompt(base + extra)
        self.assertEqual(got(""), ["Приоритеты"])
        self.assertEqual(got("Приоритеты: развилок нет — линейная задача\n"), [])
        self.assertEqual(
            got("Приоритеты: не менять схему данных; один репозиторий\n"), []
        )
        self.assertEqual(got("Приоритеты: <порядок предпочтений>\n"), ["Приоритеты"])
        self.assertEqual(got("Приоритеты: —\n"), ["Приоритеты"])

    def test_prompt_recall_line(self) -> None:
        base = f"Критерий готовности: `x` зелёный\nУсловие остановки: падение\n{PRIORITIES_OK}\n"
        got = lambda extra: brief_check.check_prompt(
            base + extra, root=self.root, now=self.NOW
        )
        self.assertEqual(got(""), ["Справка"])
        self.assertEqual(got(f"Справка: {self.file}\n"), [])
        self.assertEqual(got("Справка: results/нет.md\n"), ["Справка (файла нет)"])
        self.assertEqual(got("Справка: не нужна — проба\n"), [])
        self.assertEqual(
            got("Справка: не нужна\n"), ["Справка (не нужна — без причины)"]
        )
        self.assertEqual(got("Справка: CLAUDE.md\n"), ["Справка (файла нет)"])
        self.touch(30 * 3600)
        self.assertEqual(got(f"Справка: {self.file}\n"), ["Справка (старше суток)"])
        self.assertEqual(
            brief_check.check_prompt(
                base + f"Справка: {self.file}\n",
                root=self.root,
                now=self.NOW,
                fresh=False,
            ),
            [],
        )


class CheckQuestionTest(unittest.TestCase):
    def test_question_forms(self) -> None:
        for text, ok in (
            ("Что делать? По документам: решено в записи 0032.", True),
            ("По документам:\nзаписей нет\nЧто делать?", True),
            ("Что делать?", False),
            ("Что делать? По документам:", False),
            ("Что делать? По документам: <что найдено>", False),
            ("Что делать? По документам: —", False),
            ("по документам: решено", True),
            ("ПО ДОКУМЕНТАМ: —", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(brief_check.check_question(text), ok)


if __name__ == "__main__":
    unittest.main()
