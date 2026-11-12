"""Проверки сбора справки: `python3 -I scripts/tests/test_recall.py`."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "recall.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
TOML = """schema = 1
[project]
name = "p"
[docs]
decisions = "docs/decisions"
openspec = ["openspec"]
rules = ["CLAUDE.md"]
recall = ["docs/", "todo.local.md"]
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RecallTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "proj"
        self.transcripts = base / "transcripts"
        self.transcripts.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.update(
            GIT_ENV, HOME=str(base / "home"), DUTY_CONFIG_USER=str(base / "u.toml")
        )
        self.env = env
        write(self.root / ".duty/duty.toml", TOML)
        write(self.root / ".gitignore", ".duty/local.toml\n")
        write(
            self.root / "docs/decisions/01-a/0001-channel.md",
            "# 0001. Канал\n\nТема: канал\n\nВыбран Heartbeat раз в минуту.\n\nПересмотр: нет.\n",
        )
        write(
            self.root / "openspec/specs/chan/spec.md",
            "# chan\n\n## Requirements\n\n### Requirement: Heartbeat канала\n\nКанал шлёт heartbeat.\n",
        )
        write(
            self.root / "openspec/changes/archive/old/specs/x/spec.md",
            "heartbeat в архиве\n",
        )
        write(self.root / "CLAUDE.md", "# Правила\n\nНичего.\n")
        write(
            self.root / "docs/open-questions/q.md",
            "# Вопрос\n\nHEARTBEAT при перезапуске?\n",
        )
        write(self.root / "todo.local.md", "- heartbeat в очереди\n")
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=self.root, env=env, check=True
        )
        subprocess.run(["git", "add", "docs"], cwd=self.root, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "добавлен heartbeat"],
            cwd=self.root,
            env=env,
            check=True,
        )
        user = {
            "type": "user",
            "timestamp": "2026-11-05T10:00:00.000Z",
            "message": {"role": "user", "content": "Давай Heartbeat раз в 30 с"},
        }
        listed = {
            "type": "user",
            "timestamp": "2026-11-05T11:00:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "heartbeat"}],
            },
        }
        other = {
            "type": "assistant",
            "timestamp": "2026-11-05T12:00:00.000Z",
            "message": {"content": "heartbeat"},
        }
        invoked = dict(
            user,
            timestamp="2026-11-06T10:00:00.000Z",
            message={
                "role": "user",
                "content": "<command-name>/duty:recall</command-name><command-args>heartbeat</command-args>",
            },
        )
        pasted = dict(
            user,
            timestamp="2026-11-06T11:00:00.000Z",
            message={"role": "user", "content": "heartbeat " + "x" * 4100},
        )
        service = [
            dict(user, message={"role": "user", "content": text})
            for text in (
                "<task-notification>heartbeat отчёт субагента</task-notification>",
                "  <local-command-stdout>heartbeat вывод</local-command-stdout>",
                "<system-reminder>heartbeat напоминание</system-reminder>",
                "<cross-session-message>heartbeat от агента</cross-session-message>",
                "<bash-stdout>heartbeat</bash-stdout>",
                "done rc=0 /home/u/results/x/runs/heartbeat-run",
                (
                    "субагент alpha остановился без итога: возобнови (SendMessage to=a1) "
                    "или перезапусти; его последние слова: heartbeat"
                ),
            )
        ]
        flagged = [
            dict(
                user, isMeta=True, message={"role": "user", "content": "heartbeat мета"}
            ),
            dict(
                user,
                isSidechain=True,
                message={"role": "user", "content": "heartbeat боковая цепочка"},
            ),
        ]
        rows = [
            json.dumps(r, ensure_ascii=False)
            for r in (user, listed, other, invoked, pasted, *service, *flagged)
        ]
        write(
            self.transcripts / "s.jsonl",
            "\n".join(rows + ["не json heartbeat user"]) + "\n",
        )

    def run_recall(self, *extra: str) -> str:
        started = time.monotonic()
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "тема",
                *extra,
                "--root",
                str(self.root),
                "--transcripts",
                str(self.transcripts),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        print(f"recall.py: {time.monotonic() - started:.2f} с", file=sys.stderr)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_sections_and_matches(self) -> None:
        out = self.run_recall("--keyword", "heartbeat")
        for title in (
            "Записи решений",
            "Спецификации",
            "Правила",
            "Документы",
            "История git",
            "Реплики пользователя",
        ):
            self.assertIn(f"## {title}", out)
        decisions = out.split("## Спецификации")[0]
        self.assertIn("docs/decisions/01-a/0001-channel.md", decisions)
        self.assertIn("(0001. Канал)", decisions)
        specs = out.split("## Спецификации")[1].split("## Правила")[0]
        self.assertIn("openspec/specs/chan/spec.md", specs)
        self.assertIn("Requirement: Heartbeat канала", specs)
        self.assertNotIn("archive", specs)
        docs = out.split("## Документы")[1].split("## История git")[0]
        self.assertIn("docs/open-questions/q.md", docs)
        self.assertIn("todo.local.md", docs)
        # Разделы независимы: файл записи решений виден и в «Документах» (docs/).
        self.assertIn("0001-channel.md", docs)
        self.assertIn("0001-channel.md", decisions)
        history = self.run_recall("--keyword", "Heartbeat").split("## История git")[1]
        self.assertIn("добавлен heartbeat", history)

    def raw_recall(self, env: dict[str, str], *extra: str) -> str:
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "тема",
                *extra,
                "--root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_git_history_is_limited_by_time(self) -> None:
        old = "2020-01-01T00:00:00"
        write(self.root / "docs/old.md", "zzold\n")
        env = dict(self.env, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old)
        subprocess.run(
            ["git", "add", "docs/old.md"], cwd=self.root, env=env, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "старое zzold"],
            cwd=self.root,
            env=env,
            check=True,
        )
        git_part = self.run_recall("--keyword", "zzold").split("## История git")[1]
        git_part = git_part.split("## Реплики")[0]
        self.assertNotIn("старое zzold", git_part)
        self.assertIn("Совпадений нет.", git_part)

    def test_file_limit_keeps_best_ranked_not_first_by_path(self) -> None:
        for i in range(45):
            write(self.root / f"docs/open-questions/f{i:02d}.md", "heartbeat\n")
        write(self.root / "docs/zz-best.md", "heartbeat\n" * 5)
        docs = self.run_recall("--keyword", "heartbeat").split("## Документы")[1]
        docs = docs.split("## История git")[0]
        self.assertIn("docs/zz-best.md", docs)
        self.assertEqual(docs.count("\n- `"), 40)
        self.assertRegex(docs, r"… ещё файлов: \d+")
        self.assertLess(docs.index("zz-best.md"), docs.index("f00.md"))

    def test_transcripts_of_root_subdirectories_are_found(self) -> None:
        home = Path(self.env["HOME"])
        projects = home / ".claude" / "projects"
        prefix = str(self.root).replace("/", "-")

        def session(name: str, cwd: str, text: str) -> None:
            row = {
                "type": "user",
                "cwd": cwd,
                "timestamp": "2026-11-07T10:00:00.000Z",
                "message": {"role": "user", "content": text},
            }
            write(
                projects / name / "s.jsonl", json.dumps(row, ensure_ascii=False) + "\n"
            )

        session(prefix, str(self.root), "heartbeat из корня")
        session(prefix + "-wt-a", str(self.root / "wt/a"), "heartbeat из worktree")
        session(prefix + "-x", str(self.root) + "-x", "heartbeat соседнего проекта")
        replies = self.raw_recall(self.env, "--keyword", "heartbeat").split(
            "## Реплики пользователя"
        )[1]
        self.assertIn("из корня", replies)
        self.assertIn("из worktree", replies)
        self.assertNotIn("соседнего", replies)

    def test_replies_only_string_user_newest_first(self) -> None:
        out = self.run_recall("--keyword", "heartbeat")
        replies = out.split("## Реплики пользователя")[1]
        self.assertIn("2026-11-05", replies)
        self.assertIn("раз в 30 с", replies)
        self.assertEqual(replies.count("- 2026-"), 1)

    def test_replies_skip_skill_calls_and_long_lines(self) -> None:
        replies = self.run_recall("--keyword", "heartbeat").split(
            "## Реплики пользователя"
        )[1]
        self.assertNotIn("command-name", replies)
        self.assertNotIn("xxxx", replies)

    def test_replies_skip_service_records(self) -> None:
        replies = self.run_recall("--keyword", "heartbeat").split(
            "## Реплики пользователя"
        )[1]
        for marker in (
            "отчёт",
            "вывод",
            "напоминание",
            "от агента",
            "мета",
            "боков",
            "done rc",
            "остановился",
        ):
            self.assertNotIn(marker, replies)
        self.assertEqual(replies.count("- 2026-"), 1)

    def test_git_failure_is_reported(self) -> None:
        bare = self.root.parent / "bare"
        write(bare / ".duty/duty.toml", TOML)
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "тема",
                "--keyword",
                "heartbeat",
                "--root",
                str(bare),
                "--transcripts",
                str(self.transcripts),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        out = done.stdout
        git_part = out.split("## История git")[1].split("## Реплики")[0]
        self.assertIn("git log не выполнен:", git_part)
        self.assertNotIn("Совпадений нет.", git_part)

    def test_save(self) -> None:
        results = self.root.parent / "res"
        env = dict(self.env, DUTY_RESULTS_DIR=str(results))
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "save",
                "Канал данных",
                "--root",
                str(self.root),
            ],
            input="# итог\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        path = Path(done.stdout.strip())
        self.assertEqual(path.parent, results / "recall")
        self.assertRegex(path.name, r"^kanal-dannykh-\d{8}-\d{4}\.md$")
        self.assertEqual(path.read_text(encoding="utf-8"), "# итог\n")

    def test_missing_files_and_dirs_are_skipped(self) -> None:
        (self.root / "todo.local.md").unlink()
        out = self.run_recall("--keyword", "нетТакого")
        self.assertEqual(out.count("Совпадений нет."), 6)
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "тема",
                "--keyword",
                "x",
                "--root",
                str(self.root),
                "--transcripts",
                str(self.root / "нет"),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("Каталога транскриптов нет", done.stdout)


if __name__ == "__main__":
    unittest.main()
