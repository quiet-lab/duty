"""Сервис без файла доступа: одна строка вместо трассировки.

Запуск: `python3 -I scripts/tests/test_not_configured.py`.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


class NotConfiguredTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.root,
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        (self.root / ".duty").mkdir()

    def run_script(
        self, script: str, toml: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        (self.root / ".duty" / "duty.toml").write_text(toml, encoding="utf-8")
        env = {
            **os.environ,
            **GIT_ENV,
            "DUTY_PROJECT": str(self.root),
            "DUTY_CONFIG_USER": str(self.root / "none.toml"),
            "HOME": str(self.root),
        }
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / script), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )

    def test_gates_bitbucket_without_token_file(self) -> None:
        for body in (
            '[bitbucket]\nurl = "http://x"\n',
            '[bitbucket]\nurl = "http://x"\ntoken_file = ""\n',
        ):
            r = self.run_script("gates.py", "schema = 1\n" + body)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn(
                "[bitbucket]: token_file не задан — pull request'ы не проверено",
                r.stdout,
            )

    def test_gates_bitbucket_token_file_missing(self) -> None:
        r = self.run_script(
            "gates.py",
            'schema = 1\n[bitbucket]\nurl = "http://x"\ntoken_file = "none-token"\n',
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("none-token", r.stdout)

    def test_jwait_without_env_file(self) -> None:
        r = self.run_script(
            "jwait.py",
            'schema = 1\n[jenkins]\nenv_file = ""\n',
            "some-job",
            "1",
        )
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("[jenkins]: env_file не задан", r.stdout + r.stderr)

    def test_week_stats_without_token_file(self) -> None:
        sets = self.root / "sets"
        sets.mkdir()
        env = {**os.environ, **GIT_ENV}
        subprocess.run(["git", "init", "-q"], cwd=sets, check=True, env=env)
        subprocess.run(
            ["git", "remote", "add", "origin", "http://x/scm/p/r.git"],
            cwd=sets,
            check=True,
            env=env,
        )
        r = self.run_script(
            "week-stats.py",
            'schema = 1\n[bitbucket]\nurl = "http://x"\nsets_repo = "sets"\n',
        )
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("token_file не задан", r.stdout)


if __name__ == "__main__":
    unittest.main()
