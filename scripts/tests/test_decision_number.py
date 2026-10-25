"""Проверки decision-number.sh: `python3 -I scripts/tests/test_decision_number.py`."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "decision-number.sh"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def number(directory: Path, *flags: str) -> str:
    done = subprocess.run(
        ["bash", str(SCRIPT), str(directory), *flags], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


class DecisionNumberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.decisions = self.root / "docs" / "decisions"
        self.decisions.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "master")
        (self.decisions / "0001-a.md").write_text("a\n", encoding="utf-8")
        git(self.root, "add", "docs/decisions/0001-a.md")
        git(self.root, "commit", "-q", "-m", "a")

    def test_files_only_outside_git(self) -> None:
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        (plain / "0003-x.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(number(plain, "--peek"), "0004")

    def test_other_local_branch_counts(self) -> None:
        git(self.root, "checkout", "-q", "-b", "other")
        (self.decisions / "0005-b.md").write_text("b\n", encoding="utf-8")
        git(self.root, "add", "docs/decisions/0005-b.md")
        git(self.root, "commit", "-q", "-m", "b")
        git(self.root, "checkout", "-q", "master")
        self.assertFalse((self.decisions / "0005-b.md").exists())
        self.assertEqual(number(self.decisions, "--peek"), "0006")

    def test_uncommitted_file_in_other_worktree_counts(self) -> None:
        git(self.root, "branch", "third")
        wt = Path(self.tmp.name) / "wt-third"
        git(self.root, "worktree", "add", "-q", str(wt), "third")
        (wt / "docs" / "decisions" / "0007-c.md").write_text("c\n", encoding="utf-8")
        self.assertEqual(number(self.decisions, "--peek"), "0008")

    def test_reservation_uses_computed_number(self) -> None:
        git(self.root, "branch", "other")
        git(self.root, "checkout", "-q", "other")
        (self.decisions / "0005-b.md").write_text("b\n", encoding="utf-8")
        git(self.root, "add", "docs/decisions/0005-b.md")
        git(self.root, "commit", "-q", "-m", "b")
        git(self.root, "checkout", "-q", "master")
        self.assertEqual(number(self.decisions), "0006")
        self.assertTrue((self.decisions / "0006-reserved.md").is_file())
        self.assertFalse((self.decisions / ".number-lock").exists())
        self.assertEqual(number(self.decisions, "--peek"), "0007")

    def test_lock_is_shared_between_worktrees(self) -> None:
        git(self.root, "branch", "third")
        wt = Path(self.tmp.name) / "wt-third"
        git(self.root, "worktree", "add", "-q", str(wt), "third")
        (self.root / ".git" / "decision-number.lock").mkdir()
        # PATH без inotifywait: занятый замок не ждётся, вызов завершается кодом 3.
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        for tool in ("git", "sed", "find", "sort", "tail", "rmdir", "mkdir"):
            found = shutil.which(tool)
            assert found is not None
            (bin_dir / tool).symlink_to(found)
        done = subprocess.run(
            [shutil.which("bash") or "bash", str(SCRIPT), str(wt / "docs" / "decisions")],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(bin_dir)},
        )
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("decision-number.lock", done.stderr)
        self.assertFalse(list((wt / "docs" / "decisions").glob("*-reserved.md")))

    @unittest.skipUnless(shutil.which("inotifywait"), "нужен inotifywait")
    def test_interrupted_waiter_keeps_foreign_lock(self) -> None:
        lock = self.root / ".git" / "decision-number.lock"
        lock.mkdir()
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), str(self.decisions)], stderr=subprocess.PIPE, text=True
        )
        # Момент входа в inotifywait снаружи не наблюдается без опроса, но первая
        # строка stderr печатается перед ожиданием: её приход и есть готовность.
        assert proc.stderr is not None
        first = proc.stderr.readline()
        proc.terminate()
        proc.wait()
        self.assertIn("оставшийся после аварии замок снимается rmdir", first)
        self.assertIn(str(lock), first)
        self.assertTrue(lock.is_dir())
        self.assertFalse(list(self.decisions.glob("*-reserved.md")))


if __name__ == "__main__":
    os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    unittest.main()
