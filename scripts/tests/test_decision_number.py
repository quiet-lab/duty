"""Проверки decision-number.sh: `python3 -I scripts/tests/test_decision_number.py`."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "decision-number.sh"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
# Замер на станции (32 ядра), 5 прогонов: проверка ожидания замка целиком —
# 0,23-0,25 с; узел Jenkins медленнее на ядро в 4,5 раза: 1,1 с; с двойным
# запасом 2,2 с, округлено до 3 с. Предел — предохранитель от зависания.
LIMIT_S = 3

# Двойник renumber-decisions.py: пишет аргументы в журнал и при insert
# переносит файл в каталог под следующим номером, как настоящий сценарий.
FAKE = r"""
import os
import sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
log = root / "calls.log"
log.open("a", encoding="utf-8").write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["insert"]:
    src = Path(sys.argv[-1])
    if os.environ.get("FAKE_FAIL"):
        # Обрыв посреди переименований, как EXDEV в настоящем сценарии.
        first = next((root / "docs" / "decisions").glob("*/0001-*.md"))
        first.rename(first.with_name(".renumber-0-" + first.name))
        src.rename(src.with_name(".renumber-1-" + src.name))
        raise OSError("обрыв")
    decisions = root / "docs" / "decisions"
    if "--catalog" in sys.argv:
        catalog = sys.argv[sys.argv.index("--catalog") + 1]
        target = next(decisions.glob(catalog + "-*"))
    else:
        after = int(sys.argv[sys.argv.index("--after") + 1])
        target = next(decisions.glob(f"*/{after:04d}-*.md")).parent
    slug = src.stem.split("-", 1)[1] if src.stem[:4].isdigit() else src.stem
    src.rename(target / ("0099-" + slug + ".md"))
"""


class DecisionNumberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.decisions = self.root / "docs" / "decisions"
        for catalog, numbers in (
            ("01-architecture", (1, 2)),
            ("02-code", ()),
            ("03-data", (3,)),
        ):
            (self.decisions / catalog).mkdir(parents=True)
            for n in numbers:
                (self.decisions / catalog / f"{n:04d}-r{n}.md").write_text(
                    f"# {n:04d}. R\n", encoding="utf-8"
                )
        merge = self.decisions / "03-data" / "0003.merge"
        merge.mkdir()
        (merge / "0042-old.md").write_text("старая\n", encoding="utf-8")
        (self.root / "scripts").mkdir()
        self.fake = self.root / "scripts" / "renumber-decisions.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        subprocess.run([*GIT, "init", "-q", "-b", "master"], cwd=self.root, check=True)
        self.env = {**os.environ, "DUTY_RENUMBER": str(self.fake)}

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def test_next_number_by_catalog(self) -> None:
        self.assertEqual(
            self.run_script(str(self.decisions / "01-architecture")).stdout.strip(),
            "0003",
        )
        # Пустой каталог: за последней записью предыдущих каталогов.
        self.assertEqual(
            self.run_script(str(self.decisions / "02-code")).stdout.strip(), "0003"
        )
        # Источники объединения в NNNN.merge/ не считаются.
        self.assertEqual(
            self.run_script(str(self.decisions / "03-data")).stdout.strip(), "0004"
        )

    def test_rejects_non_catalog(self) -> None:
        r = self.run_script(str(self.decisions))
        self.assertEqual(r.returncode, 2)
        self.assertIn("не тематический каталог", r.stderr)
        self.assertEqual(
            self.run_script(
                str(self.decisions / "01-architecture"), "--after", "1"
            ).returncode,
            2,
        )

    def test_insert_last_then_index(self) -> None:
        draft = Path(self.tmp.name) / "new-topic.md"
        draft.write_text("# 0000. Новое\n", encoding="utf-8")
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.strip(), str(self.decisions / "02-code" / "0099-new-topic.md")
        )
        calls = (self.root / "calls.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            calls, [f"insert --catalog 02 --last {draft}", "--write-index"]
        )
        git_dir = self.root / ".git"
        self.assertFalse((git_dir / "decision-insert.lock").exists())

    def test_insert_after(self) -> None:
        draft = Path(self.tmp.name) / "0000-mid.md"
        draft.write_text("# 0000. Середина\n", encoding="utf-8")
        r = self.run_script(
            str(self.decisions / "01-architecture"),
            "--insert",
            str(draft),
            "--after",
            "0001",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = (self.root / "calls.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[0], f"insert --after 1 {draft}")

    def test_insert_finds_record_in_target_catalog(self) -> None:
        # Тема r1 уже есть в 01-architecture: путь берётся из каталога вставки.
        for args in (("03-data",), ("01-architecture", "--after", "3")):
            draft = Path(self.tmp.name) / "r1.md"
            draft.write_text("# 0000. Тёзка\n", encoding="utf-8")
            extra = list(args[1:])
            r = self.run_script(
                str(self.decisions / args[0]), "--insert", str(draft), *extra
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(
                r.stdout.strip(), str(self.decisions / "03-data" / "0099-r1.md")
            )
            (self.decisions / "03-data" / "0099-r1.md").unlink()

    def test_insert_after_missing_record(self) -> None:
        draft = Path(self.tmp.name) / "x.md"
        draft.write_text("# 0000. X\n", encoding="utf-8")
        r = self.run_script(
            str(self.decisions / "02-code"), "--insert", str(draft), "--after", "77"
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("записи 77 нет", r.stderr)

    def test_insert_waits_for_lock_event(self) -> None:
        lock = self.root / ".git" / "decision-insert.lock"
        lock.mkdir()
        draft = Path(self.tmp.name) / "locked.md"
        draft.write_text("# 0000. З\n", encoding="utf-8")
        proc = subprocess.Popen(
            [
                "bash",
                str(SCRIPT),
                str(self.decisions / "02-code"),
                "--insert",
                str(draft),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        assert proc.stderr is not None
        # Сообщение о занятом замке печатается перед ожиданием события.
        self.assertIn("замок", proc.stderr.readline())
        lock.rmdir()
        out, _ = proc.communicate(timeout=LIMIT_S)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(out.strip().endswith("0099-locked.md"))

    def other_fs_case(self) -> tuple[Path, Path]:
        """Репозиторий на диске, черновик на tmpfs: разные файловые системы."""
        disk = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, disk, True)
        shm = Path(tempfile.mkdtemp(dir="/dev/shm"))
        self.addCleanup(shutil.rmtree, shm, True)
        if disk.stat().st_dev == shm.stat().st_dev:
            self.skipTest("нет двух файловых систем")
        self.root = disk / "repo"
        shutil.copytree(Path(self.tmp.name) / "repo", self.root)
        self.decisions = self.root / "docs" / "decisions"
        self.env = {
            **os.environ,
            "DUTY_RENUMBER": str(self.root / "scripts" / "renumber-decisions.py"),
        }
        subprocess.run([*GIT, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*GIT, "commit", "-qm", "init"], cwd=self.root, check=True)
        draft = shm / "cross-fs.md"
        draft.write_text("# 0000. Другая ФС\n", encoding="utf-8")
        return self.root, draft

    def test_insert_from_other_filesystem(self) -> None:
        _, draft = self.other_fs_case()
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.decisions / "02-code" / "0099-cross-fs.md").exists())
        self.assertFalse(draft.exists())
        self.assertEqual(list((self.root / ".git").glob("decision-insert*")), [])

    def test_failed_insert_rolls_back(self) -> None:
        root, draft = self.other_fs_case()
        self.env["FAKE_FAIL"] = "1"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2)
        self.assertIn("откат", r.stderr)
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(status, "")
        self.assertTrue(draft.exists())
        self.assertEqual(list((root / ".git").glob("decision-insert*")), [])

    def test_failed_insert_keeps_draft_inside_repo(self) -> None:
        draft = self.root / "draft-in-repo.md"
        draft.write_text("# 0000. Черновик\n", encoding="utf-8")
        subprocess.run([*GIT, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*GIT, "commit", "-qm", "init"], cwd=self.root, check=True)
        draft.unlink()
        draft.write_text("# 0000. Черновик\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "1"
        # Неотслеживаемый черновик: имя другое, чтобы его не было в HEAD.
        new = self.root / "fresh-draft.md"
        draft.rename(new)
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(new))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
