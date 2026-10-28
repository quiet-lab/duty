"""Проверки перечня уборки: запуск `python3 -I scripts/tests/test_cleanup_candidates.py`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "cleanup-candidates.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class ExcludeWorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Каталог проекта лежит внутри tmp, чтобы стенд (родитель/ocp-dev-stand) не выходил за него.
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "proj" / "root"
        origin = self.base / "origin.git"
        sub = self.root / "sub"
        sub.mkdir(parents=True)
        git(self.base, "init", "--bare", "-b", "main", str(origin))
        git(sub, "init", "-b", "main")
        git(sub, "commit", "--allow-empty", "-m", "base")
        git(sub, "remote", "add", "origin", str(origin))
        git(sub, "push", "origin", "main")
        git(sub, "branch", "--set-upstream-to=origin/main", "main")
        git(self.root, "init", "-b", "main")
        (self.root / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\tparent = main\n', encoding="utf-8")
        self.sub = sub

    def add_gone_worktree(self, branch: str) -> Path:
        wt = self.base / f"wt-{branch}"
        git(self.sub, "worktree", "add", "-b", branch, str(wt))
        git(wt, "commit", "--allow-empty", "-m", branch)
        git(wt, "push", "-u", "origin", branch)
        git(self.sub, "push", "origin", "--delete", branch)  # ветвь исчезла в origin
        return wt

    def candidates(self, *extra: str) -> str:
        env = {**os.environ, "HOME": str(self.base / "home"), "CLAUDE_PROJECT_DIR": str(self.root), **GIT_ENV}
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.root), *extra],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def row(self, out: str, wt: Path) -> str:
        rows = [line for line in out.splitlines() if str(wt) in line]
        self.assertEqual(len(rows), 1, out)
        return rows[0]

    def test_gone_branch_worktree_is_candidate_without_exclude(self) -> None:
        wt = self.add_gone_worktree("feat-x")
        self.assertIn("удалить: ветвь удалена в origin", self.row(self.candidates(), wt))

    def test_exclude_protects_worktree_of_branch_gone_in_origin(self) -> None:
        wt = self.add_gone_worktree("feat-x")
        row = self.row(self.candidates("--exclude", "feat-x"), wt)
        self.assertIn("оставлено: ветвь исключена", row)
        self.assertNotIn("worktree remove", row)

    def test_exclude_glob_protects_worktree(self) -> None:
        wt = self.add_gone_worktree("feat-y")
        self.assertIn("оставлено", self.row(self.candidates("--exclude=feat-*"), wt))

    def test_exclude_of_other_branch_does_not_protect(self) -> None:
        wt = self.add_gone_worktree("feat-z")
        self.assertIn("удалить", self.row(self.candidates("--exclude", "other"), wt))

    def test_repeated_exclude_protects_each_worktree(self) -> None:
        a, b = self.add_gone_worktree("feat-a"), self.add_gone_worktree("feat-b")
        out = self.candidates("--exclude", "feat-a", "--exclude", "feat-b")
        self.assertIn("оставлено", self.row(out, a))
        self.assertIn("оставлено", self.row(out, b))

    def test_exclude_without_pattern_is_error(self) -> None:
        env = {**os.environ, **GIT_ENV}
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.root), "--exclude"],
                           capture_output=True, text=True, env=env, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_detached_worktree_is_kept(self) -> None:
        wt = self.add_gone_worktree("feat-d")
        git(wt, "checkout", "--detach")
        row = self.row(self.candidates(), wt)
        self.assertIn("оставлено", row)
        self.assertNotIn("worktree remove", row)


if __name__ == "__main__":
    unittest.main()
