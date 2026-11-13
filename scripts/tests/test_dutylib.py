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


class RunMetaTest(unittest.TestCase):
    """meta.txt прогона tab-run.sh читается одним помощником для хука и runs-web."""

    def test_started_ms_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertEqual(dutylib.read_meta(run_dir), {})
            self.assertEqual(dutylib.run_started_ms(run_dir), 0)
            (run_dir / "meta.txt").write_text(
                "cwd: C:/x\nstarted_ms: 1700000000123\npid: 42\nшум\n",
                encoding="utf-8",
            )
            meta = dutylib.read_meta(run_dir)
            self.assertEqual((meta["cwd"], meta["pid"]), ("C:/x", "42"))
            self.assertEqual(dutylib.run_started_ms(run_dir), 1700000000123)
            (run_dir / "meta.txt").write_text("started_ms: ²\n", encoding="utf-8")
            self.assertEqual(dutylib.run_started_ms(run_dir), 0)
            self.assertFalse(dutylib.tab_run_alive(run_dir))

    def test_broken_byte_keeps_keys(self) -> None:
        # Испорченный байт не лишает хук ключей: runs-web.py показывает тот же meta.txt.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "meta.txt").write_bytes(
                b"cwd: /a\xff\nstarted_ms: 1700000000123\npid: 42\n"
            )
            meta = dutylib.read_meta(run_dir)
            self.assertEqual((meta["started_ms"], meta["pid"]), ("1700000000123", "42"))
            self.assertEqual(dutylib.run_started_ms(run_dir, meta), 1700000000123)


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


class ContainerMountsTest(unittest.TestCase):
    """Каталог, смонтированный в работающий контейнер, считается открытым процессом.

    Процессы контейнера rootless Docker чужого uid в `/proc` не читаются.
    """

    def with_docker(self, script: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = Path(tmp.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "docker"
        stub.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        stub.chmod(0o755)
        patcher = mock.patch.dict(
            os.environ, {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mount_of_running_container_marks_top_live(self) -> None:
        self.with_docker(
            'case $1 in ps) echo abc ;; inspect) printf "/data/results/run-1/runs/x\\n/proc\\n" ;; esac'
        )
        self.assertEqual(
            dutylib.container_mounts(),
            [Path("/data/results/run-1/runs/x"), Path("/proc")],
        )
        index = dutylib.open_paths()
        self.assertIn(Path("/data/results/run-1"), index)
        self.assertIn(
            "открыт файл процесса",
            dutylib.live_reason(Path("/data/results/run-1"), index),
        )

    def test_no_containers_or_no_docker(self) -> None:
        self.with_docker("exit 0")
        self.assertEqual(dutylib.container_mounts(), [])
        self.with_docker("exit 1")
        self.assertEqual(dutylib.container_mounts(), [])
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(dutylib.container_mounts(), [])

    def test_docker_timeout_means_mounts_unknown(self) -> None:
        self.with_docker("exit 0")
        for stage in ("ps", "inspect"):
            with self.subTest(stage=stage):
                self.assertEqual(self.mounts_with_hung(stage), [])

    def mounts_with_hung(self, stage: str) -> list[Path]:
        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIn("timeout", kwargs)
            if cmd[1] == stage:
                raise subprocess.TimeoutExpired(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")

        with mock.patch.object(dutylib.subprocess, "run", fake_run):
            return dutylib.container_mounts()


if __name__ == "__main__":
    unittest.main()
