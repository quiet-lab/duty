"""Проверки decision-number.sh: `python3 -I scripts/tests/test_decision_number.py`."""

import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "decision-number.sh"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
# Замер на станции (32 ядра), 5 прогонов: проверка ожидания замка целиком —
# 0,23-0,25 с; узел Jenkins медленнее на ядро в 4,5 раза: 1,1 с; с двойным
# запасом 2,2 с, округлено до 3 с. Предел — предохранитель от зависания.

# Двойник renumber-decisions.py insert: как настоящий сценарий, переносит
# записи через временные имена и пишет указатель в одной транзакции
# с журналом отката; при ошибке и сигнале TERM или HUP отменяет только свои
# шаги. Аргументы пишутся в журнал вызовов.
#   FAKE_FAIL=middle — обрыв посреди переименований;
#   FAKE_FAIL=index — обрыв после записи указателя;
#   FAKE_ROLLBACK_FAIL — откат не выполняется, сообщение честное;
#   FAKE_PARALLEL — путь, который «параллельный процесс» меняет посреди вставки;
#   FAKE_WAIT_FIFO — вставка «зависает» посреди переименований до записи в канал.
FAKE = r"""
import os
import signal
import sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
decisions = root / "docs" / "decisions"
with (root / "calls.log").open("a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
undo = []
def terminate(signum, frame):
    raise SystemExit(128 + signum)
for sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, terminate)
def move(src, dst):
    src.rename(dst)
    undo.append(lambda: dst.rename(src))
def write(path, text):
    if path.exists():
        before = path.read_text(encoding="utf-8")
        undo.append(lambda: path.write_text(before, encoding="utf-8"))
    else:
        undo.append(path.unlink)
    path.write_text(text, encoding="utf-8")
try:
    if sys.argv[1:2] != ["insert"]:
        raise SystemExit("двойник понимает только insert")
    src = Path(sys.argv[-1])
    if "--catalog" in sys.argv:
        catalog = sys.argv[sys.argv.index("--catalog") + 1]
        target = next(decisions.glob(catalog + "-*"))
    else:
        after = int(sys.argv[sys.argv.index("--after") + 1])
        target = next(decisions.glob(f"*/{after:04d}-*.md")).parent
    first = next(decisions.glob("*/0001-*.md"))
    tmp = first.with_name(".renumber-0-" + first.name)
    move(first, tmp)
    if os.environ.get("FAKE_PARALLEL"):
        # Правка другого процесса, не вставки: журнал её не знает.
        Path(os.environ["FAKE_PARALLEL"]).write_text("чужая правка\n", encoding="utf-8")
    if os.environ.get("FAKE_WAIT_FIFO"):
        print("ready", file=sys.stderr, flush=True)
        Path(os.environ["FAKE_WAIT_FIFO"]).read_text(encoding="utf-8")
    if os.environ.get("FAKE_FAIL") == "middle":
        raise OSError("обрыв")
    move(tmp, first)
    slug = src.stem.split("-", 1)[1] if src.stem[:4].isdigit() else src.stem
    move(src, target / ("0099-" + slug + ".md"))
    write(decisions / "README.md", "указатель\n")
    if os.environ.get("FAKE_FAIL") == "index":
        raise OSError("обрыв после указателя")
except BaseException as error:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    failed = len(undo) if os.environ.get("FAKE_ROLLBACK_FAIL") else 0
    if not failed:
        for step in reversed(undo):
            step()
    print(
        f"перенумерация оборвана ({error!r}): "
        + (
            "каталог возвращён в исходное состояние"
            if not failed
            else f"откат не завершён, не отменено шагов: {failed}"
        ),
        file=sys.stderr,
    )
    raise
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
        (self.root / ".duty").mkdir()
        (self.root / ".duty" / "duty.toml").write_text(
            "schema = 1\n[docs]\n"
            # Ключ docs.decision_index вставке не нужен: указатель строит
            # сама команда вставки.
            'decision_insert = "python3 -I scripts/renumber-decisions.py insert"\n',
            encoding="utf-8",
        )
        subprocess.run([*GIT, "init", "-q", "-b", "master"], cwd=self.root, check=True)
        # Журнал вызовов двойника — не часть дерева проекта.
        (self.root / ".git" / "info").mkdir(exist_ok=True)
        (self.root / ".git" / "info" / "exclude").write_text(
            "calls.log\n", encoding="utf-8"
        )
        self.env = {
            **os.environ,
            "DUTY_CONFIG_USER": str(Path(self.tmp.name) / "no-user.toml"),
        }
        self.env.pop("DUTY_PROJECT", None)
        self.env.pop("CLAUDE_PROJECT_DIR", None)

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

    def test_insert_without_config_is_refused(self) -> None:
        (self.root / ".duty" / "duty.toml").write_text("schema = 1\n", encoding="utf-8")
        draft = Path(self.tmp.name) / "no-config.md"
        draft.write_text("# 0000. Н\n", encoding="utf-8")
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2)
        self.assertIn("задайте docs.decision_insert в .duty/duty.toml", r.stderr)
        self.assertTrue(draft.exists())

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

    def test_insert_last_single_transaction(self) -> None:
        draft = Path(self.tmp.name) / "new-topic.md"
        draft.write_text("# 0000. Новое\n", encoding="utf-8")
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.strip(), str(self.decisions / "02-code" / "0099-new-topic.md")
        )
        calls = (self.root / "calls.log").read_text(encoding="utf-8").splitlines()
        # Указатель строит сама вставка, отдельного шага --write-index нет;
        # вставке передаётся копия черновика в каталоге git.
        self.assertEqual(len(calls), 1, calls)
        self.assertRegex(
            calls[0],
            r"^insert --catalog 02 --last .*/\.git/decision-insert-src\.[^/]+/new-topic\.md$",
        )
        self.assertFalse(draft.exists())
        self.assertEqual(
            (self.decisions / "README.md").read_text(encoding="utf-8"), "указатель\n"
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
        self.assertRegex(
            calls[0], r"^insert --after 1 .*/decision-insert-src\.[^/]+/0000-mid\.md$"
        )

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
        out, _ = proc.communicate()
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

    def committed_repo(self) -> None:
        subprocess.run([*GIT, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*GIT, "commit", "-qm", "init"], cwd=self.root, check=True)

    def git_status(self) -> str:
        return subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def snapshot(self) -> dict[str, str]:
        """Файлы и каталоги дерева без .git и журнала вызовов двойника."""
        return {
            str(p.relative_to(self.root)) + ("" if p.is_file() else "/"): (
                p.read_text(encoding="utf-8") if p.is_file() else ""
            )
            for p in sorted(self.root.rglob("*"))
            if ".git" not in p.relative_to(self.root).parts and p.name != "calls.log"
        }

    def test_failed_insert_rolls_back(self) -> None:
        root, draft = self.other_fs_case()
        self.env["FAKE_FAIL"] = "middle"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2)
        self.assertIn("каталог возвращён в исходное состояние", r.stderr)
        self.assertIn("черновик цел", r.stderr)
        self.assertEqual(self.git_status(), "")
        self.assertTrue(draft.exists())
        self.assertEqual(list((root / ".git").glob("decision-insert*")), [])

    def test_failure_after_index_rolls_back_everything(self) -> None:
        """Сбой посреди вставки после записи указателя: откатывается всё."""
        self.committed_repo()
        before = self.snapshot()
        draft = Path(self.tmp.name) / "idx.md"
        draft.write_text("# 0000. Указатель\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "index"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2)
        self.assertIn("каталог возвращён в исходное состояние", r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.decisions / "README.md").exists())
        self.assertTrue(draft.exists())

    def test_previous_changes_untouched(self) -> None:
        """Правки, сделанные до вставки, переживают откат, в том числе в путях вставки."""
        self.committed_repo()
        record = self.decisions / "01-architecture" / "0001-r1.md"
        record.write_text("# 0001. R\n\nПравка до вставки.\n", encoding="utf-8")
        (self.root / "notes.md").write_text("заметки\n", encoding="utf-8")
        before = self.snapshot()
        status = self.git_status()
        draft = Path(self.tmp.name) / "prev.md"
        draft.write_text("# 0000. До\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "index"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git_status(), status)

    def test_parallel_edit_untouched(self) -> None:
        """Правка другого процесса во время вставки откатом не задевается."""
        self.committed_repo()
        parallel = self.root / "docs" / "parallel.md"
        tracked = self.decisions / "03-data" / "0003-r3.md"
        draft = Path(self.tmp.name) / "par.md"
        draft.write_text("# 0000. Параллельно\n", encoding="utf-8")
        for path in (parallel, tracked):
            with self.subTest(path.name):
                self.env["FAKE_FAIL"] = "index"
                self.env["FAKE_PARALLEL"] = str(path)
                r = self.run_script(
                    str(self.decisions / "02-code"), "--insert", str(draft)
                )
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(path.read_text(encoding="utf-8"), "чужая правка\n")
                self.assertTrue(draft.exists())

    def test_newline_paths_untouched(self) -> None:
        """Пути с переводом строки (свой и параллельный) откат не задевает."""
        self.committed_repo()
        mine = self.root / "docs" / "до\nвставки.md"
        mine.write_text("свой\n", encoding="utf-8")
        parallel = self.root / "docs" / "во время\nвставки.md"
        draft = Path(self.tmp.name) / "nl.md"
        draft.write_text("# 0000. Перевод строки\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "index"
        self.env["FAKE_PARALLEL"] = str(parallel)
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(mine.read_text(encoding="utf-8"), "свой\n")
        self.assertEqual(parallel.read_text(encoding="utf-8"), "чужая правка\n")
        self.assertFalse((self.root / "docs" / "до").exists())
        self.assertFalse((self.decisions / "README.md").exists())

    def test_no_own_rollback_by_git_status(self) -> None:
        """Сценарий не разбирает git status: откат — только у команды вставки."""
        text = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn('git -C "$root" status', code)
        self.assertNotIn("status --porcelain", code)
        self.assertNotIn("tr '\\0'", code)

    def test_rollback_failure_is_reported_honestly(self) -> None:
        """Откат команды вставки не завершён: итог не заявляет, что дерево возвращено."""
        self.committed_repo()
        draft = Path(self.tmp.name) / "stuck.md"
        draft.write_text("# 0000. Сбой отката\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "index"
        self.env["FAKE_ROLLBACK_FAIL"] = "1"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(draft))
        self.assertEqual(r.returncode, 2)
        self.assertIn("откат не завершён", r.stderr)
        self.assertIn("дерево требует ручной проверки", r.stderr)
        self.assertIn(f"исходный файл сохранён: {draft}", r.stderr)
        self.assertNotIn("возвращено в исходное состояние", r.stderr)
        self.assertTrue(draft.exists())

    def test_signal_during_insert_rolls_back(self) -> None:
        """TERM сценарию передаётся вставке: она откатывается, код 2."""
        self.committed_repo()
        before = self.snapshot()
        draft = Path(self.tmp.name) / "signal.md"
        draft.write_text("# 0000. Сигнал\n", encoding="utf-8")
        fifo = Path(self.tmp.name) / "wait.fifo"
        os.mkfifo(fifo)
        self.env["FAKE_WAIT_FIFO"] = str(fifo)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig.name):
                proc = subprocess.Popen(
                    [
                        "bash",
                        str(SCRIPT),
                        str(self.decisions / "02-code"),
                        "--insert",
                        str(draft),
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self.env,
                )
                self.addCleanup(proc.kill)
                assert proc.stderr is not None
                # Ожидание события: вставка сообщила о готовности строкой в stderr.
                for line in proc.stderr:
                    if line.strip() == "ready":
                        break
                # Сигнал получает только сценарий; вставка, ждущая канала,
                # обрывается переданным ей TERM, а не записью в канал.
                proc.send_signal(sig)
                rest = proc.stderr.read()
                self.assertEqual(proc.wait(), 2, rest)
                self.assertIn(f"получен сигнал {sig.name[3:]}", rest)
                self.assertIn("каталог возвращён в исходное состояние", rest)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(
                    list((self.root / ".git").glob("decision-insert*")), []
                )
                self.assertTrue(draft.exists())

    def test_failed_insert_keeps_draft_inside_repo(self) -> None:
        self.committed_repo()
        # Неотслеживаемый черновик внутри репозитория.
        new = self.root / "fresh-draft.md"
        new.write_text("# 0000. Черновик\n", encoding="utf-8")
        self.env["FAKE_FAIL"] = "middle"
        self.env["FAKE_ROLLBACK_FAIL"] = "1"
        r = self.run_script(str(self.decisions / "02-code"), "--insert", str(new))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
