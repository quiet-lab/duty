"""Проверки runs-web.py: запуск `python3 -I scripts/tests/test_runs_web.py` или через pytest."""

import http.client
import importlib.util
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "runs-web.py"
TAB_RUN = Path(__file__).resolve().parents[1] / "tab-run.sh"
# Замер на станции (32 ядра): запуск inotifywait и первая дозапись — до 0,05 с.
# Узел Jenkins медленнее на ядро в 4,5 раза: 0,05 * 4,5 = 0,23 с; с двойным запасом
# 0,5 с, округлено до 5 с. Предел — предохранитель от зависания, а не время
# ожидания события.

spec = importlib.util.spec_from_file_location("runs_web", SCRIPT)
assert spec and spec.loader
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)

NEED_INOTIFY = unittest.skipUnless(
    shutil.which("inotifywait"), "нет inotifywait: тест живого слежения пропущен"
)


def make_run(
    results: Path,
    task: str,
    name: str,
    *,
    meta: str | None = None,
    cmd: str | None = None,
    log: str | None = None,
    done: str | None = None,
) -> Path:
    d = results / task / "runs" / name
    d.mkdir(parents=True)
    for fname, text in (
        ("meta.txt", meta),
        ("cmd.sh", cmd),
        ("out.log", log),
        ("done", done),
    ):
        if text is not None:
            (d / fname).write_text(text, encoding="utf-8")
    return d


CMD_SH = """#!/usr/bin/env bash
# Прогон t от tab-run.sh
run_dir=/x/runs/t
printf "cwd: %s\\n" "$PWD" > "$run_dir/meta.txt"
{
echo one
echo two | cat
} 2>&1 | tee "$run_dir/out.log"
rc=${PIPESTATUS[0]}
"""


class ParseTest(unittest.TestCase):
    def test_meta(self) -> None:
        meta = rw.dutylib.parse_meta(
            "cwd: /a b\nstarted_ms: 5\nkind: tests\nмусор\nfinished_ms: 9\ncode: 0\n"
        )
        self.assertEqual(
            meta,
            {
                "cwd": "/a b",
                "started_ms": "5",
                "kind": "tests",
                "finished_ms": "9",
                "code": "0",
            },
        )

    def test_meta_value_with_colon(self) -> None:
        self.assertEqual(rw.dutylib.parse_meta("cwd: C:/x\n")["cwd"], "C:/x")

    def test_cmd(self) -> None:
        self.assertEqual(rw.parse_cmd(CMD_SH), "echo one\necho two | cat")

    def test_cmd_broken(self) -> None:
        self.assertEqual(rw.parse_cmd("echo 1\n"), "")

    def test_sse_frame(self) -> None:
        self.assertEqual(
            rw.sse_frame("log", "a\nb\n"), b"event: log\ndata: a\ndata: b\ndata: \n\n"
        )
        self.assertEqual(rw.sse_frame("done", "0"), b"event: done\ndata: 0\n\n")

    def test_safe_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            make_run(results, "t", "r")
            self.assertIsNotNone(rw.run_dir_for(results, "t", "r"))
            self.assertIsNone(rw.run_dir_for(results, "..", "r"))
            self.assertIsNone(rw.run_dir_for(results, "t", "../r"))
            self.assertIsNone(rw.run_dir_for(results, "t", "нет"))


class ListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)

    def test_list_and_states(self) -> None:
        make_run(
            self.results,
            "a",
            "old",
            meta="cwd: /\nstarted_ms: 10\nkind: build\nfinished_ms: 20\ncode: 1\n",
            cmd=CMD_SH,
            log="x",
            done="1\n",
        )
        make_run(
            self.results,
            "b",
            "live",
            meta="cwd: /\nstarted_ms: 30\n",
            cmd=CMD_SH,
            log="y",
        )
        make_run(self.results, "b", "fresh", cmd=CMD_SH)
        runs = rw.list_runs(self.results)
        self.assertEqual([r["name"] for r in runs][:2], ["live", "old"])
        by = {r["name"]: r for r in runs}
        self.assertEqual(
            (by["old"]["state"], by["old"]["code"], by["old"]["kind"]),
            ("done", 1, "build"),
        )
        self.assertEqual((by["live"]["state"], by["live"]["kind"]), ("running", ""))
        self.assertEqual(by["fresh"]["state"], "pending")
        self.assertEqual(by["old"]["command"], "echo one\necho two | cat")
        self.assertEqual(by["old"]["task"], "a")

    def test_unsafe_names_not_listed(self) -> None:
        make_run(self.results, "a", "плохо")
        make_run(self.results, "a", "ok")
        self.assertEqual([r["name"] for r in rw.list_runs(self.results)], ["ok"])

    def test_empty(self) -> None:
        self.assertEqual(rw.list_runs(self.results), [])

    def test_render_escapes(self) -> None:
        make_run(
            self.results,
            "a",
            "r",
            meta="started_ms: 1\nkind: tests\n",
            cmd="{\necho '<b>x</b>'\n} 2>&1 | tee \"$run_dir/out.log\"\n",
        )
        body = rw.render_index(self.results, [("s", "<i>")]).decode()
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", body)
        self.assertIn("&lt;i&gt;", body)
        self.assertIn("tests", body)
        self.assertIn("/run?task=a&amp;name=r", body)


@NEED_INOTIFY
class FollowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)

    def test_finished_run_sends_all_and_done(self) -> None:
        d = make_run(self.results, "t", "r", log="привет\nмир\n", done="3\n")
        frames: list[bytes] = []
        rw.follow_log(d, frames.append)
        self.assertEqual(
            frames, [rw.sse_frame("log", "привет\nмир\n"), rw.sse_frame("done", "3")]
        )

    def test_big_log_is_tailed(self) -> None:
        d = make_run(
            self.results, "t", "r", log="x" * (rw.TAIL_BYTES + 10) + "\n", done="0\n"
        )
        frames: list[bytes] = []
        rw.follow_log(d, frames.append)
        self.assertTrue(
            frames[0].startswith(
                b"event: log\ndata: [\xd0\xbf\xd1\x80\xd0\xbe\xd0\xbf\xd1\x83\xd1\x89"
            )
        )

    def test_append_then_done(self) -> None:
        d = make_run(self.results, "t", "r", log="first\n")
        got: list[bytes] = []
        seen_first = threading.Event()
        seen_second = threading.Event()

        def send(frame: bytes) -> None:
            got.append(frame)
            if b"first" in frame:
                seen_first.set()
            if b"second" in frame:
                seen_second.set()

        worker = threading.Thread(target=rw.follow_log, args=(d, send))
        worker.start()
        seen_first.wait()
        with open(d / "out.log", "a", encoding="utf-8") as f:
            f.write("second\n")
        seen_second.wait()
        (d / ".done.tmp").write_text("0\n")
        (d / ".done.tmp").rename(d / "done")
        worker.join()
        self.assertFalse(worker.is_alive())
        self.assertEqual(got[-1], rw.sse_frame("done", "0"))
        self.assertEqual(sum(f.count(b"second") for f in got[:-1]), 1)

    def test_client_disconnect_stops_follow(self) -> None:
        d = make_run(self.results, "t", "r", meta="started_ms: 1\n", log="hello\n")
        server_side, client_side = socket.socketpair()
        self.addCleanup(server_side.close)
        self.addCleanup(client_side.close)
        seen = threading.Event()

        def send(frame: bytes) -> None:
            seen.set()

        worker = threading.Thread(target=rw.follow_log, args=(d, send, server_side))
        worker.start()
        seen.wait()
        client_side.close()
        worker.join()
        self.assertFalse(worker.is_alive(), "слежение пережило уход клиента")


@NEED_INOTIFY
class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)
        self.server = rw.make_server(self.results, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_address[1]

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def get(self, path: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read().decode()
        finally:
            conn.close()

    def test_pages(self) -> None:
        make_run(
            self.results,
            "t",
            "r",
            meta="started_ms: 1\nkind: wait\n",
            cmd=CMD_SH,
            log="out\n",
            done="0\n",
        )
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("/run?task=t&amp;name=r", body)
        self.assertIn("dispatch.sh list", body)
        status, body = self.get("/run?task=t&name=r")
        self.assertEqual(status, 200)
        self.assertIn("EventSource", body)
        self.assertIn("echo two | cat", body)
        self.assertEqual(self.get("/run?task=t&name=nope")[0], 404)
        self.assertEqual(self.get("/run?task=..&name=x")[0], 404)
        self.assertEqual(self.get("/other")[0], 404)

    def test_foreign_host_rejected(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            conn.request("GET", "/", headers={"Host": "evil.example"})
            self.assertEqual(conn.getresponse().status, 403)
        finally:
            conn.close()

    def test_stream_finished(self) -> None:
        make_run(self.results, "t", "r", log="hello\n", done="0\n")
        status, body = self.get("/stream?task=t&name=r")
        self.assertEqual(status, 200)
        self.assertEqual(
            body, "event: log\ndata: hello\ndata: \n\nevent: done\ndata: 0\n\n"
        )


class TabRunKindTest(unittest.TestCase):
    """tab-run.sh с поддельным herdr: --kind попадает в cmd.sh, без него поведение прежнее."""

    def run_tab(self, *extra: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "herdr"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'case "$1 $2" in\n'
            '  "tab create") echo \'{"result":{"tab":{"tab_id":"t1"},"root_pane":{"pane_id":"p1"}}}\' ;;\n'
            "esac\n"
        )
        fake.chmod(0o755)
        task = root / "task"
        task.mkdir()
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "w",
            "DUTY_TASK": str(task),
        }
        proc = subprocess.run(
            ["bash", str(TAB_RUN), "r1", *extra, "--", "echo", "hi"],
            env=env,
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return task / "runs" / "r1"

    @unittest.skipUnless(shutil.which("jq"), "нет jq")
    def test_kind_in_meta(self) -> None:
        run_dir = self.run_tab("--kind", "tests")
        meta_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, meta_dir)
        # Выполняем только строки cmd.sh, пишущие meta.txt: сама команда во вкладке не нужна.
        script = "\n".join(
            l
            for l in (run_dir / "cmd.sh").read_text().splitlines()
            if l.startswith(("run_dir=", 'printf "cwd', 'printf "kind'))
        )
        subprocess.run(["bash", "-c", script], check=True, cwd=meta_dir)
        meta = rw.dutylib.parse_meta((run_dir / "meta.txt").read_text())
        self.assertEqual(meta["kind"], "tests")
        self.assertIn("started_ms", meta)

    @unittest.skipUnless(shutil.which("jq"), "нет jq")
    def test_without_kind(self) -> None:
        run_dir = self.run_tab()
        self.assertNotIn("kind", (run_dir / "cmd.sh").read_text())

    @unittest.skipUnless(shutil.which("jq"), "нет jq")
    def test_bad_kind(self) -> None:
        with self.assertRaises(AssertionError):
            self.run_tab("--kind", "Bad Kind")


if __name__ == "__main__":
    unittest.main()
