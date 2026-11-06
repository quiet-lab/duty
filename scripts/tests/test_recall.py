"""Проверки сбора справки: `python3 -I scripts/tests/test_recall.py`."""

import json
import os
import subprocess
import sys
import tempfile
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
        env.update(GIT_ENV, HOME=str(base / "home"), DUTY_CONFIG_USER=str(base / "u.toml"))
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
        write(self.root / "docs/open-questions/q.md", "# Вопрос\n\nHEARTBEAT при перезапуске?\n")
        write(self.root / "todo.local.md", "- heartbeat в очереди\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, env=env, check=True)
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
            "message": {"role": "user", "content": [{"type": "text", "text": "heartbeat"}]},
        }
        other = {
            "type": "assistant",
            "timestamp": "2026-11-05T12:00:00.000Z",
            "message": {"content": "heartbeat"},
        }
        rows = [json.dumps(r, ensure_ascii=False) for r in (user, listed, other)]
        write(self.transcripts / "s.jsonl", "\n".join(rows + ["не json heartbeat user"]) + "\n")

    def run_recall(self, *extra: str) -> str:
        done = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "тема", *extra, "--root", str(self.root),
             "--transcripts", str(self.transcripts)],
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_sections_and_matches(self) -> None:
        out = self.run_recall("--keyword", "heartbeat")
        for title in ("Записи решений", "Спецификации", "Правила", "Документы",
                      "История git", "Реплики пользователя"):
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
        self.assertNotIn("0001-channel.md", docs)
        history = self.run_recall("--keyword", "Heartbeat").split("## История git")[1]
        self.assertIn("добавлен heartbeat", history)

    def test_replies_only_string_user_newest_first(self) -> None:
        out = self.run_recall("--keyword", "heartbeat")
        replies = out.split("## Реплики пользователя")[1]
        self.assertIn("2026-11-05", replies)
        self.assertIn("раз в 30 с", replies)
        self.assertEqual(replies.count("- 2026-"), 1)

    def test_missing_files_and_dirs_are_skipped(self) -> None:
        (self.root / "todo.local.md").unlink()
        out = self.run_recall("--keyword", "нетТакого")
        self.assertEqual(out.count("Совпадений нет."), 6)
        done = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "тема", "--keyword", "x", "--root", str(self.root),
             "--transcripts", str(self.root / "нет")],
            capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL, check=False,
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("Каталога транскриптов нет", done.stdout)


if __name__ == "__main__":
    unittest.main()
