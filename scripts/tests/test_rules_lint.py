"""Проверки `scripts/rules-lint.py`: повторы между файлами правил и подкоманда brief."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
LINT = PLUGIN / "scripts" / "rules-lint.py"
# Замер: запуск на двойниках — 0,05 с; предел — предохранитель от зависания.
LIMIT_S = 10

SENTENCE = (
    "Независимые подзадачи выполняются параллельно, если они не пересекаются "
    "по файлам, ветвям и ресурсам."
)


class RulesLintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def config(self, files: list[str], extra: str = "") -> Path:
        listing = ", ".join(f'"{f}"' for f in files)
        path = self.root / "lint.toml"
        path.write_text(
            f"files = [{listing}]\nmin_words = 8\nmin_chars = 50\n{extra}\n",
            encoding="utf-8",
        )
        return path

    def run_lint(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(LINT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def repeats(
        self, files: list[str], extra: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return self.run_lint(
            "repeats",
            "--config",
            str(self.config(files, extra)),
            "--root",
            str(self.root),
        )

    def test_duplicate_found(self) -> None:
        self.write("a.md", f"# A\n\n{SENTENCE}\n")
        self.write("b.md", f"# B\n\nИное. {SENTENCE}\n")
        r = self.repeats(["a.md", "b.md"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("a.md:3: повтор в b.md:3:", r.stdout)
        self.assertIn("1 находок", r.stdout)

    def test_clean_set_is_silent(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write(
            "b.md",
            "Совсем другое предложение про сборку образов и их отправку в реестр.\n",
        )
        r = self.repeats(["a.md", "b.md"])
        self.assertEqual((r.returncode, r.stdout), (0, "0 находок\n"))

    def test_default_subcommand_is_repeats(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write("b.md", f"{SENTENCE}\n")
        config = self.config(["a.md", "b.md"])
        r = self.run_lint("--config", str(config), "--root", str(self.root))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_code_block_ignored(self) -> None:
        self.write("a.md", f"```text\n{SENTENCE}\n```\n")
        self.write("b.md", f"```text\n{SENTENCE}\n```\n")
        self.assertEqual(self.repeats(["a.md", "b.md"]).returncode, 0)

    def test_same_file_repeat_ignored(self) -> None:
        self.write("a.md", f"{SENTENCE}\n\n{SENTENCE}\n")
        self.write("b.md", "Другое.\n")
        self.assertEqual(self.repeats(["a.md", "b.md"]).returncode, 0)

    def test_short_repeat_below_threshold(self) -> None:
        text = "Это короткое предложение.\n"
        self.write("a.md", text)
        self.write("b.md", text)
        self.assertEqual(self.repeats(["a.md", "b.md"]).returncode, 0)

    def test_allowed_removes_finding(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write("b.md", f"{SENTENCE}\n")
        extra = f'[[allowed]]\ntext = "{SENTENCE}"\nreason = "обоснование"\n'
        self.assertEqual(self.repeats(["a.md", "b.md"], extra).returncode, 0)

    def test_missing_file_is_finding(self) -> None:
        self.write("a.md", "Текст.\n")
        r = self.repeats(["a.md", "нет-такого.md", "каталог/*.md"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет-такого.md", r.stdout)
        self.assertIn("каталог/*.md", r.stdout)
        self.assertIn("2 находок", r.stdout)

    def test_normalization(self) -> None:
        self.write(
            "a.md",
            "- Ёжик ЗАПУСКАЕТ `wait-done.sh` для ожидания внутри хода, а не в фоне **всегда**.\n",
        )
        self.write(
            "b.md",
            "Ежик запускает wait-done.sh для ожидания внутри хода а не в фоне всегда\n",
        )
        r = self.repeats(["a.md", "b.md"])
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_wrapped_paragraph_joined(self) -> None:
        self.write(
            "a.md",
            "Независимые подзадачи выполняются параллельно,\nесли они не пересекаются по файлам,\nветвям и ресурсам.\n",
        )
        self.write("b.md", f"{SENTENCE}\n")
        self.assertEqual(self.repeats(["a.md", "b.md"]).returncode, 1)

    def test_python_docstring_only(self) -> None:
        self.write("g.py", f'"""Модуль.\n\n{SENTENCE}\n"""\n\n# {SENTENCE}\nX = 1\n')
        self.write("b.md", f"{SENTENCE}\n")
        r = self.repeats(["g.py", "b.md"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.count("повтор"), 1)

    def test_allowed_empty_text_is_config_error(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write("b.md", f"{SENTENCE}\n")
        extra = '[[allowed]]\ntext = "…"\nreason = "x"\n'
        r = self.repeats(["a.md", "b.md"], extra)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("пуст", r.stderr)

    def test_allowed_is_equality_not_substring(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write("b.md", f"{SENTENCE}\n")
        whole = f'[[allowed]]\ntext = "{SENTENCE}"\nreason = "x"\n'
        self.assertEqual(self.repeats(["a.md", "b.md"], whole).returncode, 0)
        part = f'[[allowed]]\ntext = "{SENTENCE[:60]}"\nreason = "x"\n'
        self.assertEqual(self.repeats(["a.md", "b.md"], part).returncode, 1)

    def test_optional_missing_is_note_only(self) -> None:
        self.write("a.md", "Текст.\n")
        home = self.root / "home"
        home.mkdir()
        config = self.config(
            ["a.md", "~/.claude/CLAUDE.md"], 'optional = ["~/.claude/CLAUDE.md"]'
        )
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                str(LINT),
                "repeats",
                "--config",
                str(config),
                "--root",
                str(self.root),
            ],
            env={"HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )
        self.assertEqual((r.returncode, r.stdout), (0, "0 находок\n"), r.stderr)
        self.assertIn("не проверен", r.stderr)
        # Не необязательные отсутствующие файлы по-прежнему находка.
        config = self.config(
            ["a.md", "~/.claude/CLAUDE.md", "нет.md"],
            'optional = ["~/.claude/CLAUDE.md"]',
        )
        r = self.run_lint("repeats", "--config", str(config), "--root", str(self.root))
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет.md", r.stdout)

    def test_brief_subcommand(self) -> None:
        good = (
            "## Цель\n\nЦель задачи.\n\n## Критерий готовности\n\n`true`\n\n"
            "## Условие остановки\n\nСтоп при падении.\n\n## Ссылки\n\n- Прочитанное при постановке: `CLAUDE.md`\n"
        )
        path = self.root / "brief.md"
        path.write_text(good, encoding="utf-8")
        self.assertEqual(self.run_lint("brief", str(path)).returncode, 0)
        path.write_text(
            good.replace("## Условие остановки\n\nСтоп при падении.\n\n", ""),
            encoding="utf-8",
        )
        r = self.run_lint("brief", str(path))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Условие остановки", r.stdout)
        r = self.run_lint("brief", "-", stdin=good)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
