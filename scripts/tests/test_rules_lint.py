"""Проверки `scripts/rules-lint.py`: набор из конфигурации проекта, повторы, подкоманда brief."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2]
LINT = PLUGIN / "scripts" / "rules-lint.py"
# Замер: запуск на двойниках — 0,05 с; предел — предохранитель от зависания.
LIMIT_S = 10

# Переменные среды запуска, которые меняют поиск корня проекта и слой
# пользователя: тесты их убирают, чтобы не видеть настоящий проект.
CLEARED = ("DUTY_PROJECT", "CLAUDE_PROJECT_DIR", "DUTY_CONFIG_USER")

SENTENCE = (
    "Независимые подзадачи выполняются параллельно, если они не пересекаются "
    "по файлам, ветвям и ресурсам."
)


class RulesLintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update(
            HOME=str(self.root / "home"),
            DUTY_CONFIG_USER=str(self.root / "no-user.toml"),
        )
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def config(self, files: list[str], optional: list[str] | None = None) -> None:
        """Конфигурация проекта: docs.rules и docs.rules_optional."""
        listing = ", ".join(f'"{f}"' for f in files)
        extra = ", ".join(f'"{f}"' for f in optional or [])
        self.write(
            ".duty/duty.toml",
            f"schema = 1\n[docs]\nrules = [{listing}]\nrules_optional = [{extra}]\n",
        )

    def run_lint(
        self, *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(LINT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
            env=env
            or {
                "HOME": str(self.root / "home"),
                "DUTY_CONFIG_USER": str(self.root / "no-user.toml"),
                "PATH": os.environ.get("PATH", ""),
            },
        )

    def repeats(
        self, files: list[str], optional: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.config(files, optional)
        return self.run_lint("repeats", "--root", str(self.root))

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
        self.config(["a.md", "b.md"])
        r = self.run_lint("--root", str(self.root))
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

    def test_missing_file_is_finding(self) -> None:
        self.write("a.md", "Текст.\n")
        r = self.repeats(["a.md", "нет-такого.md", "каталог/*.md"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет-такого.md", r.stdout)
        self.assertIn("каталог/*.md", r.stdout)
        self.assertIn("2 находок", r.stdout)

    def test_missing_file_attribution_by_source(self) -> None:
        self.write("a.md", "Текст.\n")
        self.config(["нет-такого.md"])
        r = self.run_lint("repeats", "--root", str(self.root))
        self.assertIn("duty.toml:1: в наборе нет файла для «нет-такого.md»", r.stdout)
        # Файлы плагина на месте: сообщений «в плагине нет файла» нет.
        self.assertNotIn("в плагине нет файла", r.stdout)

    def test_missing_plugin_file_attribution(self) -> None:
        spec = importlib.util.spec_from_file_location("rules_lint", LINT)
        module = importlib.util.module_from_spec(spec)
        sys.modules["rules_lint"] = module
        self.addCleanup(sys.modules.pop, "rules_lint", None)
        spec.loader.exec_module(module)
        # Без корня проекта: подъём от текущего каталога не должен найти
        # конфигурацию дерева, из которого запущен тест.
        bare = self.root / "bare"
        bare.mkdir()
        cwd = os.getcwd()
        os.chdir(bare)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(module, "PLUGIN_FILES", ("reference/нет-файла.md",)):
            findings = module.run_repeats(None)
        self.assertEqual(
            findings,
            ["reference/нет-файла.md:1: в плагине нет файла «reference/нет-файла.md»"],
        )

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

    def test_optional_missing_is_note_only(self) -> None:
        self.write("a.md", "Текст.\n")
        r = self.repeats(["a.md"], ["~/.claude/CLAUDE.md"])
        self.assertEqual((r.returncode, r.stdout), (0, "0 находок\n"), r.stderr)
        self.assertIn("не проверен", r.stderr)
        # Обязательный отсутствующий файл по-прежнему находка.
        r = self.repeats(["a.md", "нет.md"], ["~/.claude/CLAUDE.md"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет.md", r.stdout)

    def test_duty_dir_markdown_repeat_with_rules(self) -> None:
        self.write("a.md", f"{SENTENCE}\n")
        self.write(".duty/b.md", f"{SENTENCE}\n")
        r = self.repeats(["a.md"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("повтор в .duty/b.md:1", r.stdout)

    def test_without_root_only_plugin_files(self) -> None:
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        r = subprocess.run(
            [sys.executable, "-I", str(LINT), "repeats"],
            cwd=bare,
            env={
                "HOME": str(bare),
                "DUTY_CONFIG_USER": str(bare / "no.toml"),
                "PATH": os.environ.get("PATH", ""),
            },
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "0 находок\n")

    def test_brief_subcommand(self) -> None:
        good = (
            "## Цель\n\nЦель задачи.\n\n## Критерий готовности\n\n`true`\n\n"
            "## Условие остановки\n\nСтоп при падении.\n\n## Ссылки\n\n- Прочитанное при постановке: `CLAUDE.md`\n- Справка: не нужна — проба\n"
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

    def test_brief_subcommand_recall(self) -> None:
        base = (
            "## Цель\n\nЦель задачи.\n\n## Критерий готовности\n\n`true`\n\n"
            "## Условие остановки\n\nСтоп при падении.\n\n## Ссылки\n\n- Прочитанное при постановке: `CLAUDE.md`\n"
        )
        r = self.run_lint("brief", "-", stdin=base)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Ссылки / Справка", r.stdout)
        report = self.root / "recall.md"
        report.write_text("справка\n", encoding="utf-8")
        text = base + "- Справка: recall.md\n"
        r = self.run_lint("brief", "-", "--root", str(self.root), stdin=text)
        self.assertEqual(r.returncode, 0, r.stdout)
        old = time.time() - 25 * 3600
        os.utime(report, (old, old))
        r = self.run_lint("brief", "-", "--root", str(self.root), stdin=text)
        self.assertEqual(r.returncode, 1)
        self.assertIn("старше суток", r.stdout)


if __name__ == "__main__":
    unittest.main()
