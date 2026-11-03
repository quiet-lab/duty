"""Проверки `scripts/brief_check.py`: форма записки и приглашения."""

import sys
import unittest
from pathlib import Path

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

## Ссылки

- Правила задачи — `rules.md`.
- Прочитанное при постановке: `CLAUDE.md`.

## Служебное (заполняет dispatch.sh)

- workspace: `w1`

## Передача

—
"""


def without(text: str, name: str) -> str:
    """Записка без заголовка раздела `name` (его тело сливается с предыдущим)."""
    return text.replace(f"## {name}\n", "")


class CheckBriefTest(unittest.TestCase):
    def test_full_brief_passes(self) -> None:
        self.assertEqual(brief_check.check_brief(FULL), [])

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

- `cd claude-plugins/duty && python3 -m pytest -q scripts/tests`.

## Условие остановки

- Два правила противоречат друг другу — вопрос главной сессии.

## Раздел для субагентов

```text
Подзадача: <имя>
Критерий готовности: <команда и ожидаемый результат>
```

## Ссылки

- Правила задачи — `rules.md` рядом.
- Прочитанное при постановке: `~/.claude/CLAUDE.md` (разделы «Постановка
  задачи», «Субагенты»); `CLAUDE.md` монорепозитория.

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
                self.assertEqual(
                    brief_check.check_prompt(text), ["Критерий готовности"]
                )

    def test_value_with_placeholder_part_is_value(self) -> None:
        text = "Критерий готовности: wait-done.sh <каталог> зелёный\nУсловие остановки: падение теста"
        self.assertEqual(brief_check.check_prompt(text), [])

    def test_labeled_value_forms(self) -> None:
        cases = (
            "Критерий готовности:\n`pytest -q` зелёный\nУсловие остановки:\n- падение теста",
            "1. Критерий готовности: `pytest -q` зелёный\n2. Условие остановки: падение теста",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(brief_check.check_prompt(text), [])

    def test_label_followed_by_label_is_empty(self) -> None:
        text = "Критерий готовности:\nУсловие остановки: падение теста"
        self.assertEqual(brief_check.check_prompt(text), ["Критерий готовности"])
        text = "Критерий готовности:\n<команда>\nУсловие остановки: падение теста"
        self.assertEqual(brief_check.check_prompt(text), ["Критерий готовности"])


class CheckPromptTest(unittest.TestCase):
    DONE = "Критерий готовности: `pytest -q` зелёный"
    STOP = "Условие остановки: падение теста — отчёт"

    def test_both_lines(self) -> None:
        self.assertEqual(
            brief_check.check_prompt(f"Сделай.\n{self.DONE}\n{self.STOP}\n"), []
        )

    def test_missing_one(self) -> None:
        self.assertEqual(brief_check.check_prompt(self.DONE), ["Условие остановки"])
        self.assertEqual(brief_check.check_prompt(self.STOP), ["Критерий готовности"])

    def test_none(self) -> None:
        self.assertEqual(
            brief_check.check_prompt("Сделай."),
            ["Критерий готовности", "Условие остановки"],
        )

    def test_case_bullets_and_bold(self) -> None:
        text = "- **критерий готовности:** `pytest`\n* УСЛОВИЕ ОСТАНОВКИ: стоп"
        self.assertEqual(brief_check.check_prompt(text), [])

    def test_headings(self) -> None:
        text = "## Критерий готовности\n\n`x` зелёный\n\n### Условие остановки\n\nПадение.\n"
        self.assertEqual(brief_check.check_prompt(text), [])

    def test_heading_with_empty_body(self) -> None:
        text = "## Критерий готовности\n\n## Условие остановки\n\nПадение.\n"
        self.assertEqual(brief_check.check_prompt(text), ["Критерий готовности"])

    def test_empty_values(self) -> None:
        for value in ("", "—", "<команда и ожидаемый результат>", "-"):
            with self.subTest(value=value):
                text = f"Критерий готовности: {value}\n{self.STOP}"
                self.assertEqual(
                    brief_check.check_prompt(text), ["Критерий готовности"]
                )

    def test_inside_code_block_counts(self) -> None:
        text = f"```text\n{self.DONE}\n{self.STOP}\n```"
        self.assertEqual(brief_check.check_prompt(text), [])


if __name__ == "__main__":
    unittest.main()
