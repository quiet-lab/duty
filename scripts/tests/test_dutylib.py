"""Проверки общих помощников `dutylib`: запуск `python3 -I scripts/tests/test_dutylib.py`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dutylib


class DutylibConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / ".duty").mkdir()
        (self.root / ".duty" / "duty.toml").write_text(
            'schema = 1\n[jenkins]\nenv_file = "j.env"\ninsecure_tls = true\n'
            '[bitbucket]\ntoken_file = ""\n',
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
        env.update(
            DUTY_PROJECT=str(self.root),
            DUTY_CONFIG_USER=str(self.root / "none.toml"),
        )
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        dutylib.config.cache_clear()
        self.addCleanup(dutylib.config.cache_clear)

    def test_config_parses_once_per_key(self) -> None:
        with mock.patch.object(
            dutylib.duty_config, "load", wraps=dutylib.duty_config.load
        ) as load:
            a = dutylib.config()
            b = dutylib.config()
            self.assertIs(a, b)
            self.assertEqual(load.call_count, 1)
            dutylib.config_path("paths.busy")
            dutylib.config_path("paths.signals")
            self.assertEqual(load.call_count, 1)

    def test_env_change_reloads(self) -> None:
        first = dutylib.config_path("paths.busy")
        os.environ["DUTY_BUSY_DIR"] = str(self.root / "other")
        self.assertEqual(dutylib.config_path("paths.busy"), self.root / "other")
        del os.environ["DUTY_BUSY_DIR"]
        self.assertEqual(dutylib.config_path("paths.busy"), first)

    def test_explicit_root_is_sticky(self) -> None:
        cfg = dutylib.config(str(self.root))
        self.assertIs(dutylib.config(), cfg)

    def test_access_file_messages(self) -> None:
        with self.assertRaises(dutylib.NotConfigured) as e:
            dutylib.access_file("bitbucket", "token_file", "pull request'ы")
        self.assertEqual(
            str(e.exception),
            "[bitbucket]: token_file не задан — pull request'ы не проверено",
        )
        with self.assertRaises(dutylib.NotConfigured) as e:
            dutylib.access_file("jenkins", "env_file", "Jenkins")
        self.assertEqual(
            str(e.exception),
            "[jenkins]: env_file: нет файла j.env — Jenkins не проверено",
        )
        (self.root / "j.env").write_text("x", encoding="utf-8")
        self.assertEqual(
            dutylib.access_file("jenkins", "env_file", "Jenkins"), self.root / "j.env"
        )

    def test_ssl_context_insecure_only_by_key(self) -> None:
        ctx = dutylib.ssl_context(dutylib.config())
        self.assertFalse(ctx.check_hostname)

        class _Cfg:
            def get(self, key, default=None):
                return default

        self.assertTrue(dutylib.ssl_context(_Cfg()).check_hostname)


class TaskFinishedTest(unittest.TestCase):
    """Признак завершения задачи по строке «Шаг:» в state.md — один на плагин."""

    def test_finished_steps(self) -> None:
        for state in (
            "Шаг: завершено\n",
            "Шаг: 9 — завершено\n",
            "**Шаг:** Завершено.\n",
            "Шаг: остановлена\n",
            "# Задача\n\nШаг: 4 - Остановлена\n",
        ):
            self.assertTrue(dutylib.task_finished(state), state)

    def test_unfinished_steps(self) -> None:
        for state in (
            "Шаг: 5 — ревью завершено, идёт слияние\n",
            "Шаг: 3 — не завершено\n",
            "Шаг: передача\n",
            "Шаг: 3 — серия\n",
            "# Задача завершено\n",
            "",
        ):
            self.assertFalse(dutylib.task_finished(state), state)

    def test_cli_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.md"
            for text, code in (("Шаг: завершено\n", 0), ("Шаг: передача\n", 1)):
                state.write_text(text, encoding="utf-8")
                r = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        dutylib.__file__,
                        "task-finished",
                        str(state),
                    ],
                    check=False,
                )
                self.assertEqual(r.returncode, code, text)
            r = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    dutylib.__file__,
                    "task-finished",
                    f"{tmp}/none",
                ],
                check=False,
            )
            self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
