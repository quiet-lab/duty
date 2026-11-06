"""Проверки перечня уборки: запуск `python3 -I scripts/tests/test_cleanup_candidates.py`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "cleanup-candidates.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class ExcludeWorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Каталог проекта лежит внутри tmp, чтобы стенд (родитель корня проекта) не выходил за него.
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
        (self.root / ".duty").mkdir()
        (self.root / ".duty" / "duty.toml").write_text(
            f'schema = 1\n[paths]\nresults = "{self.base / "results"}"\n',
            encoding="utf-8",
        )
        (self.root / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\tparent = main\n', encoding="utf-8"
        )
        self.sub = sub

    def add_gone_worktree(self, branch: str) -> Path:
        wt = self.base / f"wt-{branch}"
        git(self.sub, "worktree", "add", "-b", branch, str(wt))
        git(wt, "commit", "--allow-empty", "-m", branch)
        git(wt, "push", "-u", "origin", branch)
        git(self.sub, "push", "origin", "--delete", branch)  # ветвь исчезла в origin
        return wt

    def candidates(self, *extra: str) -> str:
        env = {
            **os.environ,
            "HOME": str(self.base / "home"),
            "DUTY_PROJECT": str(self.root),
            "DUTY_CONFIG_USER": str(self.base / "no-user-config.toml"),
            **GIT_ENV,
        }
        r = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(self.root), *extra],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def row(self, out: str, wt: Path) -> str:
        rows = [line for line in out.splitlines() if str(wt) in line]
        self.assertEqual(len(rows), 1, out)
        return rows[0]

    def test_gone_branch_worktree_is_candidate_without_exclude(self) -> None:
        wt = self.add_gone_worktree("feat-x")
        self.assertIn(
            "удалить: ветвь удалена в origin", self.row(self.candidates(), wt)
        )

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
        env = {
            **os.environ,
            "DUTY_CONFIG_USER": str(self.base / "no-user-config.toml"),
            **GIT_ENV,
        }
        r = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(self.root), "--exclude"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(r.returncode, 0)

    def test_detached_worktree_is_kept(self) -> None:
        wt = self.add_gone_worktree("feat-d")
        git(wt, "checkout", "--detach")
        row = self.row(self.candidates(), wt)
        self.assertIn("оставлено", row)
        self.assertNotIn("worktree remove", row)

    def test_skip_trees_hides_stand_revision_trees(self) -> None:
        wt = self.base / "stand" / "app-wt" / "abc123"
        wt.parent.mkdir(parents=True)
        git(self.sub, "worktree", "add", "--detach", str(wt))
        self.assertIn(str(wt), self.candidates())  # без skip_trees дерево в перечне
        (self.root / ".duty" / "duty.toml").write_text(
            f'schema = 1\n[paths]\nresults = "{self.base / "results"}"\n'
            f'[cleanup]\nskip_trees = ["{self.base}/stand/*-wt"]\n',
            encoding="utf-8",
        )
        out = self.candidates()
        self.assertNotIn(str(wt), out)
        self.assertNotIn("без ветви", out)

    def add_merged_branch(self, branch: str) -> None:
        git(self.sub, "branch", branch)  # вершина совпадает с main, значит ветвь слита

    def test_foreign_merged_branch_is_kept_without_named_flag(self) -> None:
        self.add_merged_branch("someone")
        out = self.candidates()
        row = [line for line in out.splitlines() if "someone" in line]
        self.assertEqual(len(row), 1, out)
        self.assertIn("оставлено: чужая ветвь", row[0])
        self.assertNotIn("branch -D", row[0])

    def test_own_merged_branch_is_candidate(self) -> None:
        self.add_merged_branch("mine")
        git(self.root, "config", "user.name", "t")
        out = self.candidates()
        row = [line for line in out.splitlines() if "mine" in line]
        self.assertEqual(len(row), 1, out)
        self.assertIn("branch -D", row[0])

    def test_keep_trees_expands_tilde(self) -> None:
        keep = self.base / "home" / "keep"
        keep.parent.mkdir()
        git(self.sub, "worktree", "add", "-b", "kept", str(keep))
        git(keep, "commit", "--allow-empty", "-m", "kept")
        git(keep, "push", "-u", "origin", "kept")
        git(self.sub, "push", "origin", "--delete", "kept")
        (self.root / ".duty" / "duty.toml").write_text(
            f'schema = 1\n[paths]\nresults = "{self.base / "results"}"\n'
            '[cleanup]\nkeep_trees = ["~/keep"]\n',
            encoding="utf-8",
        )
        out = self.candidates()
        self.assertNotIn(str(keep), out)

    def write_extension(self, name: str, body: str) -> None:
        folder = self.root / ".duty" / "cleanup.d"
        folder.mkdir(exist_ok=True)
        script = folder / name
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)

    # Дефект: уборка удалила каталог идущей серии results/20261106-002951-qms-*
    # (запись пишущего процесса открыта внутри, summary.json ещё нет).
    def passed_video(self, top: str) -> Path:
        video = self.base / "results" / top / "pt-e2e" / "t1" / "video.webm"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"v")
        return video

    def video_row(self, out: str, video: Path) -> str:
        rel = str(video.relative_to(self.base))
        rows = [line for line in out.splitlines() if rel in line]
        self.assertEqual(len(rows), 1, out)
        return rows[0]

    def spawn(self, *args: str, **kw) -> subprocess.Popen:
        # Вывод не в трубу тестового прогона: живой потомок держал бы её открытой.
        kw.setdefault("stdin", subprocess.DEVNULL)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        proc = subprocess.Popen(list(args), **kw)
        if proc.stdin is not None:
            self.addCleanup(proc.stdin.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_passed_video_of_finished_run_is_candidate(self) -> None:
        video = self.passed_video("20261106-000000-qms-a")
        self.assertIn("удалить: тест прошёл", self.video_row(self.candidates(), video))

    def test_video_in_run_with_open_file_is_kept(self) -> None:
        video = self.passed_video("20261106-002951-qms-b")
        log = (video.parents[2] / "log.txt").open("w")
        self.addCleanup(log.close)
        self.spawn("sleep", "600", stdout=log)
        row = self.video_row(self.candidates(), video)
        self.assertIn("оставлено: прогон идёт", row)
        self.assertNotIn("rm -f", row)

    def test_video_in_unfinished_task_is_kept(self) -> None:
        video = self.passed_video("task-a")
        task = video.parents[2]
        (task / "brief.md").write_text("# Задача\n", encoding="utf-8")
        (task / "state.md").write_text("Шаг: 3 — серия\n", encoding="utf-8")
        self.assertIn(
            "оставлено: прогон идёт", self.video_row(self.candidates(), video)
        )
        (task / "state.md").write_text("Шаг: завершено\n", encoding="utf-8")
        self.assertIn("удалить: тест прошёл", self.video_row(self.candidates(), video))

    def test_video_in_task_with_live_tab_run_is_kept(self) -> None:
        video = self.passed_video("task-b")
        run = video.parents[2] / "runs" / "full5"
        run.mkdir(parents=True)
        (run / "cmd.sh").write_text("read -r _\n", encoding="utf-8")
        # Путь cmd.sh только в командной строке ($0), файл не открыт: проверяется
        # именно признак tab-run.sh (pid: в meta.txt, нет done), а не открытый файл.
        proc = self.spawn(
            "bash", "-c", "read -r _", str(run / "cmd.sh"), stdin=subprocess.PIPE
        )
        (run / "meta.txt").write_text(f"pid: {proc.pid}\n", encoding="utf-8")
        self.assertIn(
            "оставлено: прогон идёт — прогон full5 без done",
            self.video_row(self.candidates(), video),
        )
        proc.kill()
        proc.wait()
        (run / "done").write_text("0\n", encoding="utf-8")
        self.assertIn("удалить: тест прошёл", self.video_row(self.candidates(), video))

    def test_live_mode_reports_open_file(self) -> None:
        top = self.base / "results" / "20261106-002951-qms-c"
        top.mkdir(parents=True)
        log = (top / "log.txt").open("w")
        self.addCleanup(log.close)
        self.spawn("sleep", "600", stdout=log)
        idle = self.base / "results" / "idle"
        idle.mkdir()
        r = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--live", str(top), str(idle)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{top}\tживой: открыт файл процесса", r.stdout)
        self.assertIn(f"{idle}\tне живой", r.stdout)

    def test_extension_json_line_becomes_row(self) -> None:
        self.write_extension(
            "10-ext.sh",
            """printf '%s\\n' '{"kind":"стенд","sub":"","parent":"","what":"деревья ext","size":"1M","verdict":"удалить: тест","cmd":"echo x"}'\n""",
        )
        out = self.candidates()
        rows = [line for line in out.splitlines() if "деревья ext" in line]
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(
            rows[0], "| стенд |  |  | деревья ext | 1M | удалить: тест | `echo x` |"
        )

    def test_failing_extension_gives_not_received_row(self) -> None:
        self.write_extension("20-bad.sh", "echo oops >&2\nexit 1\n")
        out = self.candidates()
        rows = [line for line in out.splitlines() if "20-bad.sh" in line]
        self.assertEqual(len(rows), 1, out)
        self.assertIn("| расширение |", rows[0])
        self.assertIn("не получено: код выхода 1", rows[0])

    def test_non_json_extension_line_gives_not_received_row(self) -> None:
        self.write_extension("30-text.sh", "echo not-json\n")
        rows = [line for line in self.candidates().splitlines() if "30-text.sh" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("не получено: строка не JSON", rows[0])


if __name__ == "__main__":
    unittest.main()
