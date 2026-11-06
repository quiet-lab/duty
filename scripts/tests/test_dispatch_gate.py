"""Ворота постановки в `scripts/dispatch.sh`: отказ до каталога задачи и до herdr."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
DISPATCH = PLUGIN / "scripts" / "dispatch.sh"
# Замер: отказ до herdr — 0,1 с, полный путь с двойником до первого сбоя — 0,5 с;
# узел Jenkins медленнее в 4,5 раза; предел — предохранитель от зависания.
LIMIT_S = 20

# Двойник herdr: пишет argv строкой и отвечает пустыми списками.
FAKE_HERDR = """#!/usr/bin/env bash
printf '%s\\t' "$@" >> "$FAKE_HERDR_LOG"
printf '\\n' >> "$FAKE_HERDR_LOG"
case "$1 $2" in
    "workspace list") echo '{"result":{"workspaces":[]}}' ;;
    "agent list") echo '{"result":{"agents":[]}}' ;;
    *) echo '{"result":{}}' ;;
esac
exit 0
"""

FULL = """# Задача t

## Цель

Сделать.

## Критерий готовности

`pytest -q` — зелёный.

## Условие остановки

Падение теста — отчёт.

## Приоритеты

Развилок нет — линейная задача.

## Ссылки

- Прочитанное при постановке: `CLAUDE.md`.
- Справка: не нужна — проба.
"""


class DispatchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.base = base
        self.root = base / "mono"
        (self.root / ".git").mkdir(parents=True)
        self.results = base / "results"
        self.results.mkdir()
        bin_dir = base / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("herdr", FAKE_HERDR),
            ("claude", "#!/usr/bin/env bash\nexit 0\n"),
        ):
            tool = bin_dir / name
            tool.write_text(body, encoding="utf-8")
            tool.chmod(0o755)
        self.log = base / "herdr.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        self.env.update(
            HERDR_ENV="1",
            DUTY_SIGNALS_DIR=str(base / "signals"),
            DUTY_RESULTS_DIR=str(self.results),
            DUTY_CONFIG_USER=str(base / "no-user-config.toml"),
            FAKE_HERDR_LOG=str(self.log),
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        )
        self.env.pop("CLAUDE_PROJECT_DIR", None)

    def dispatch(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(DISPATCH),
                "gate",
                "sonnet",
                *args,
                "--root",
                str(self.root),
                "--results",
                str(self.results),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=LIMIT_S,
            check=False,
        )

    def write_brief(self, text: str) -> Path:
        path = self.base / "brief.md"
        path.write_text(text, encoding="utf-8")
        return path

    def assert_refused(self, r: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(needle, r.stdout + r.stderr)
        self.assertFalse((self.results / "gate").exists(), "каталог задачи создан")
        self.assertFalse(self.log.exists(), "herdr вызван")

    def test_each_section_required(self) -> None:
        cases = {
            "Цель": "## Цель\n\nСделать.\n\n",
            "Критерий готовности": "## Критерий готовности\n\n`pytest -q` — зелёный.\n\n",
            "Условие остановки": "## Условие остановки\n\nПадение теста — отчёт.\n\n",
            "Приоритеты": "## Приоритеты\n\nРазвилок нет — линейная задача.\n\n",
            "Ссылки": "## Ссылки\n\n- Прочитанное при постановке: `CLAUDE.md`.\n- Справка: не нужна — проба.\n",
        }
        for name, cut in cases.items():
            with self.subTest(name=name):
                brief = self.write_brief(FULL.replace(cut, ""))
                self.assert_refused(self.dispatch("--brief", str(brief)), name)

    def test_read_subsection_required(self) -> None:
        brief = self.write_brief(
            FULL.replace(
                "- Прочитанное при постановке: `CLAUDE.md`.", "- Правила — `rules.md`."
            )
        )
        self.assert_refused(
            self.dispatch("--brief", str(brief)), "Прочитанное при постановке"
        )

    def test_dash_only_section_refused(self) -> None:
        brief = self.write_brief(FULL.replace("Падение теста — отчёт.", "—"))
        self.assert_refused(self.dispatch("--brief", str(brief)), "Условие остановки")

    def test_criterion_without_command_refused(self) -> None:
        brief = self.write_brief(FULL.replace("`pytest -q` — зелёный.", "Всё зелёное."))
        self.assert_refused(self.dispatch("--brief", str(brief)), "не названа команда")

    def test_full_brief_passes_gate(self) -> None:
        brief = self.write_brief(FULL)
        r = self.dispatch("--brief", str(brief))
        self.assertNotIn("ворота", r.stdout + r.stderr)
        self.assertNotIn("не проходит", r.stdout + r.stderr)
        self.assertTrue(
            self.log.exists(), "herdr не вызван: запуск остановлен воротами"
        )
        self.assertEqual(
            (self.results / "gate" / "brief.md").read_text(encoding="utf-8")[:12],
            "# Задача t\n\n",
        )

    def test_goal_requires_all_keys(self) -> None:
        keys = {
            "--done": "`true` зелёный",
            "--stop": "падение — отчёт",
            "--priorities": "развилок нет — линейная задача",
            "--read": "`CLAUDE.md`",
            "--recall": "не нужна — проба",
        }
        for missing in keys:
            with self.subTest(missing=missing):
                args = ["--goal", "Сделать"]
                for key, value in keys.items():
                    if key != missing:
                        args += [key, value]
                self.assert_refused(self.dispatch(*args), missing)

    def test_goal_without_priorities_refused(self) -> None:
        r = self.dispatch(
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "стоп",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "не нужна — проба",
        )
        self.assert_refused(r, "--priorities")

    def test_goal_done_without_command_refused(self) -> None:
        r = self.dispatch(
            "--goal",
            "Сделать",
            "--done",
            "всё готово",
            "--stop",
            "стоп",
            "--priorities",
            "развилок нет — линейная задача",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "не нужна — проба",
        )
        self.assert_refused(r, "не названа команда")

    def test_goal_builds_brief_with_values(self) -> None:
        r = self.dispatch(
            "--goal",
            "Цель & смысл",
            "--done",
            "`pytest -q` зелёный",
            "--stop",
            "падение — отчёт",
            "--priorities",
            "развилок нет — линейная задача",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "не нужна — решения A & B сверены",
        )
        self.assertNotIn("не проходит", r.stdout + r.stderr)
        text = (self.results / "gate" / "brief.md").read_text(encoding="utf-8")
        for value in (
            "Цель & смысл",
            "`pytest -q` зелёный",
            "падение — отчёт",
            "развилок нет — линейная задача",
            "Прочитанное при постановке: `CLAUDE.md`",
            "Справка: не нужна — решения A & B сверены",
        ):
            self.assertIn(value, text)
        self.assertNotIn("{{", text.split("## Раздел для субагентов")[0])

    def recall_file(self, age_s: float) -> Path:
        path = self.results / "recall" / "topic-20261106-1200.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("справка\n", encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def brief_with_recall(self, line: str) -> Path:
        return self.write_brief(FULL.replace("- Справка: не нужна — проба.", line))

    def test_recall_line_required(self) -> None:
        brief = self.brief_with_recall("")
        self.assert_refused(self.dispatch("--brief", str(brief)), "Ссылки / Справка")

    def test_recall_fresh_file_passes(self) -> None:
        self.recall_file(3600)
        for value in (
            "../results/recall/topic-20261106-1200.md",
            str(self.results / "recall" / "topic-20261106-1200.md"),
        ):
            with self.subTest(value=value):
                brief = self.brief_with_recall(f"- Справка: `{value}`")
                r = self.dispatch("--brief", str(brief))
                self.assertNotIn("не проходит", r.stdout + r.stderr)
                self.assertTrue(self.log.exists(), r.stdout + r.stderr)
                self.log.unlink()
                shutil.rmtree(self.results / "gate")

    def test_recall_old_file_refused(self) -> None:
        self.recall_file(25 * 3600)
        brief = self.brief_with_recall(
            "- Справка: ../results/recall/topic-20261106-1200.md"
        )
        self.assert_refused(self.dispatch("--brief", str(brief)), "старше суток")

    def test_recall_missing_file_refused(self) -> None:
        brief = self.brief_with_recall("- Справка: ../results/recall/нет.md")
        self.assert_refused(self.dispatch("--brief", str(brief)), "файла нет")

    def test_recall_not_needed_without_reason_refused(self) -> None:
        brief = self.brief_with_recall("- Справка: не нужна")
        self.assert_refused(self.dispatch("--brief", str(brief)), "Ссылки / Справка")

    def test_recall_not_a_recall_file_refused(self) -> None:
        (self.root / "todo.local.md").write_text("x\n", encoding="utf-8")
        brief = self.brief_with_recall("- Справка: todo.local.md")
        self.assert_refused(
            self.dispatch("--brief", str(brief)), "не итог /duty:recall"
        )

    def test_goal_recall_path_written_to_brief(self) -> None:
        self.recall_file(60)
        r = self.dispatch(
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "стоп",
            "--priorities",
            "развилок нет — линейная задача",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "../results/recall/topic-20261106-1200.md",
        )
        self.assertNotIn("не проходит", r.stdout + r.stderr)
        text = (self.results / "gate" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("- Справка: ../results/recall/topic-20261106-1200.md", text)

    def test_goal_recall_old_file_refused(self) -> None:
        self.recall_file(25 * 3600)
        r = self.dispatch(
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "стоп",
            "--priorities",
            "развилок нет — линейная задача",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "../results/recall/topic-20261106-1200.md",
        )
        self.assert_refused(r, "старше суток")

    def test_done_without_goal_is_usage_error(self) -> None:
        brief = self.write_brief(FULL)
        self.assert_refused(
            self.dispatch("--brief", str(brief), "--done", "`x`"), "--goal"
        )

    def test_no_brief_goal_or_resume_refused(self) -> None:
        self.assert_refused(self.dispatch(), "--brief")

    def test_resume_does_not_check_brief(self) -> None:
        task = self.results / "gate"
        task.mkdir()
        (task / "brief.md").write_text("# Задача\n\nбез разделов\n", encoding="utf-8")
        (task / "state.md").write_text("Шаг: 2\n", encoding="utf-8")
        r = self.dispatch("--resume")
        self.assertNotIn("не проходит", r.stdout + r.stderr)
        self.assertNotIn("нужна записка", r.stdout + r.stderr)
        self.assertTrue(
            self.log.exists(), "herdr не вызван: --resume остановлен воротами"
        )


if __name__ == "__main__":
    unittest.main()
