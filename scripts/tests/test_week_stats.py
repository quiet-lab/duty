"""Проверки «Недели в цифрах»: `python3 -I scripts/tests/test_week_stats.py`."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("week_stats", SCRIPTS / "week-stats.py")
assert spec and spec.loader
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)

CONFIG = """schema = 1

[project]
name = "t"

[docs]
decisions = "docs/decisions"

[stats]
test_repos = ["tests-repo"]
rules_paths = [".claude/", "plugins/"]
pipeline_paths = ["deploy/", "scripts/"]
"""
_tmp = tempfile.TemporaryDirectory()


def setUpModule() -> None:
    """Проект с `.duty/duty.toml`; пользовательский слой указывает на несуществующий файл."""
    root = Path(_tmp.name) / "proj"
    (root / ".duty").mkdir(parents=True)
    (root / ".duty" / "duty.toml").write_text(CONFIG, encoding="utf-8")
    os.environ["DUTY_CONFIG_USER"] = str(Path(_tmp.name) / "нет.toml")
    ws.gates.config(str(root))


def tearDownModule() -> None:
    _tmp.cleanup()


H = ws.HOUR_MS
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def build(
    job: str, number: int, result: str, start: int, dur: int, sha: str = ""
) -> dict:
    actions = [{"lastBuiltRevision": {"SHA1": sha}}] if sha else []
    return {
        "job": job,
        "number": number,
        "result": result,
        "timestamp": start,
        "duration": dur,
        "building": False,
        "actions": actions,
    }


class WeekStatsTest(unittest.TestCase):
    def test_jenkins_walk_is_parallel(self) -> None:
        # Сборки двух заданий запрашиваются одновременно: двойник отвечает
        # на запрос сборок, только когда пришли оба (барьер без срока: обход
        # по одному зависает, его ловит внешний предел прогона целиком).
        barrier = threading.Barrier(2)
        folders = {
            "http://j/": {"jobs": [{"name": "d", "url": "http://j/d/"}]},
            "http://j/d/": {
                "jobs": [
                    {"name": "a", "url": "http://j/d/a/"},
                    {"name": "b", "url": "http://j/d/b/"},
                    {"name": "fake", "url": "http://j/d/fake/"},
                ]
            },
        }

        requests: list[str] = []

        def fake_jenkins(url: str, env: dict) -> dict | None:
            base = url.split("/api/json")[0] + "/"
            requests.append(base)
            if base in folders:
                return folders[base]
            # Лист: сборки приходят в том же ответе, что и признак задания.
            barrier.wait()
            return {"_class": "job", "allBuilds": [{"timestamp": 5, "number": 1}]}

        old = ws.gates.jenkins
        ws.gates.jenkins = fake_jenkins
        self.addCleanup(setattr, ws.gates, "jenkins", old)
        old_config = ws.gates.config
        ws.gates.config = lambda *_: {"jenkins.fake_job": "fake"}
        self.addCleanup(setattr, ws.gates, "config", old_config)
        builds = ws.jenkins_builds({"JENKINS_URL": "http://j/"}, 0)
        self.assertEqual([b["job"] for b in builds], ["d/a", "d/b"])
        # Корень, папка и по одному запросу на каждый из двух листьев;
        # проверочное задание не запрашивается.
        self.assertEqual(
            sorted(requests),
            ["http://j/", "http://j/d/", "http://j/d/a/", "http://j/d/b/"],
        )

    def test_busy_merges_overlaps(self) -> None:
        builds = [
            build("a", 1, "SUCCESS", 0, 2 * H),
            build("b", 1, "SUCCESS", H, 2 * H),
            build("c", 1, "SUCCESS", 5 * H, H),
        ]
        self.assertEqual(ws.busy_ms(builds, 0, 10 * H), 4 * H)

    def test_random_failure_is_rerun_success_on_same_revision(self) -> None:
        builds = [
            build("q", 1, "FAILURE", 0, 1, "aa"),
            build("q", 2, "SUCCESS", 1, 1, "aa"),
            build("q", 3, "FAILURE", 2, 1, "bb"),
            build("r", 1, "FAILURE", 0, 1, "aa"),
        ]
        self.assertEqual(ws.random_failures(builds), (3, 1))

    def test_review_stats_counts_new_form_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-rounds.tsv"
            path.write_text(
                "дата\tпроект\tзадача\tмодель\tуровень\tнаходок\tвоспроизведено\tисправлено\tтикет\n"
                "2026-11-02\tp\tt1\topus\thigh\t5\t2\t2\tPRJ-1\n"
                "2026-11-02\tp\tt2\tsonnet\tmedium\t3\t0\t0\t—\n"
                "2026-11-02\tp\tt3\t1\tsonnet\tда\tстарая форма\n"
                "2020-01-01\tp\tt4\topus\thigh\t9\t9\t9\tPRJ-2\n",
                encoding="utf-8",
            )
            since = 1793577600000  # начало суток 2026-11-02 UTC
            text = ws.review_stats(path, since)
        self.assertEqual(
            text,
            "ревью 2: находок 8, воспроизведено ломающих 2, исправлено 2; "
            "строк другой формы пропущено: 1",
        )

    def test_classify_path(self) -> None:
        self.assertEqual(ws.classify_path(".", "apps.python/app/x.py"), "код")
        self.assertEqual(ws.classify_path(".", "apps.python/tests/test_x.py"), "тесты")
        self.assertEqual(ws.classify_path("tests-repo", "helpers/a.ts"), "тесты")
        self.assertEqual(ws.classify_path(".", "docs/README.md"), "документация")
        self.assertEqual(ws.classify_path(".", "scripts/x.py"), "конвейер")
        self.assertEqual(ws.classify_path("plugins/duty", "scripts/x.py"), "правила")

    def test_git_since_epoch_is_not_now(self) -> None:
        # `--since=@0` git понимал как «сейчас»: коммит прошлой секунды отсекался.
        for ms, max_age in ((0, "0"), (1793577600000, "1793577600")):
            out = subprocess.run(
                ["git", "rev-parse", f"--since={ws.git_since(ms)}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(out, f"--max-age={max_age}")

    def test_git_stats_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run([*GIT, "init", "-q", "-b", "master"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run([*GIT, "add", "src/a.py"], cwd=root, check=True)
            subprocess.run(
                [
                    *GIT,
                    "commit",
                    "-q",
                    "-m",
                    "код",
                    "-m",
                    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
                ],
                cwd=root,
                check=True,
            )
            rec = root / "docs" / "decisions" / "01-a"
            rec.mkdir(parents=True)
            (rec / "0001-x.md").write_text("# 0001. X\n", encoding="utf-8")
            subprocess.run([*GIT, "add", "docs"], cwd=root, check=True)
            subprocess.run([*GIT, "commit", "-q", "-m", "запись"], cwd=root, check=True)
            stats = ws.git_stats(root, 0)
            self.assertEqual(stats["commits"], 2)
            self.assertEqual(stats["models"], {"Opus": 1, "без подписи Claude": 1})
            self.assertEqual(stats["primary"], {"код": 1, "документация": 1})
            self.assertEqual(ws.new_records(root, 0), 1)

    def test_offline_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([*GIT, "init", "-q"], cwd=tmp, check=True)
            done = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(SCRIPTS / "week-stats.py"),
                    "--root",
                    tmp,
                    "--offline",
                    "--review-stats",
                    str(Path(tmp) / "нет.tsv"),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertIn("| Время жизни набора | не считалось (--offline) |", done.stdout)
        self.assertIn("| Ревью | не получено: нет", done.stdout)
        self.assertIn("| Доля моделей в коммитах | коммитов нет |", done.stdout)


if __name__ == "__main__":
    unittest.main()
