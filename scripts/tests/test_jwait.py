"""Проверки jwait.py на двойнике Jenkins в процессе теста.

Запуск: `python3 -I scripts/tests/test_jwait.py` или через pytest.

Двойник — ThreadingHTTPServer: REST сборки и задания, consoleText,
sse-gateway (connect, listen, configure) с cookie сессии и управляемой
лентой событий. Ход двойника задаётся элементами ленты: событие SSE,
закрытие потока, действие над состоянием и ожидание события
threading.Event (например, «jwait сверился по REST после подписки»).
Сроков ожидания и sleep в проверках нет: зависание ловит только внешний
предел прогона целиком (~/.claude/CLAUDE.md, «Устройство кода»).

К моменту `follow` сборка двойника уже не идёт (`building: false`),
поэтому цикл ожидания конца через CLI WebSocket не входит в проверку.
"""

import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
JWAIT = SCRIPTS / "jwait.py"
JOB = "folder/app"
JOB_PREFIX = "/job/folder/job/app/"
COOKIE = "JSESSIONID.twin=sess-1"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def load_jwait():
    spec = importlib.util.spec_from_file_location("jwait_under_test", JWAIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def job_event(kind: str, **fields) -> tuple:
    return ("ev", "job", {"jenkins_event": kind, "job_name": JOB, **fields})


def started(number: int) -> tuple:
    return job_event(
        "job_run_started",
        jenkins_object_id=str(number),
        jenkins_object_name=f"#{number}",
        job_run_status="RUNNING",
        job_run_queueId=7,
    )


class Twin:
    """Состояние двойника Jenkins и лента событий sse-gateway."""

    def __init__(self) -> None:
        self.builds: dict[int, str] = {}
        self.next_number = 1
        self.sse = True
        self.feed: queue.Queue = queue.Queue()
        self.on_configure: list[tuple] = []
        self.before_confirm: list = []
        # Ход после второй и следующих подписок (новый диспетчер).
        self.on_reconfigure: list[tuple] = []
        self.on_listen: dict[int, list[tuple]] = {}
        self.connects: list[str] = []
        self.listens: list[tuple[str, str]] = []
        self.configures: list[dict] = []
        self.configured = threading.Event()
        # Ответ на сверку сборки по REST отправлен после подписки.
        self.checked = threading.Event()
        self.lock = threading.Lock()
        # Сессия и диспетчер меняются при «перезапуске» контроллера.
        self.session = "sess-1"
        self.dispatcher = "disp-1"
        self.job_exists = True
        # Число отказов listen (403) подряд для проверки предела.
        self.refuse_listen = 0

    def cookie(self) -> str:
        return f"JSESSIONID.twin={self.session}"

    def restart(self) -> None:
        """Потеря сессии: прежний cookie больше не действует."""
        self.session = "sess-2"
        self.dispatcher = "disp-2"
        self.checked.clear()

    def add_build(self, number: int, result: str = "SUCCESS") -> None:
        with self.lock:
            self.builds[number] = result
            self.next_number = max(self.next_number, number + 1)


class Handler(BaseHTTPRequestHandler):
    server: "TwinServer"

    def log_message(self, *args) -> None:
        pass

    def send_json(self, code: int, body: object, extra: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        if not self.headers.get("Authorization", "").startswith("Basic "):
            self.send_json(401, {})
            return False
        return True

    def session_ok(self) -> bool:
        if self.server.twin.cookie() not in self.headers.get("Cookie", ""):
            self.send_json(403, {"error": "нет сессии"})
            return False
        return True

    def do_GET(self) -> None:
        twin = self.server.twin
        if not self.authorized():
            return
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/sse-gateway/connect":
            if not twin.sse:
                self.send_json(404, {})
                return
            twin.connects.append(query["clientId"][0])
            self.send_json(
                200,
                {"jsessionid": twin.session},
                {"Set-Cookie": f"{twin.cookie()}; Path=/; HttpOnly"},
            )
            return
        if url.path.startswith("/sse-gateway/listen/"):
            self.listen(urllib.parse.unquote(url.path.rsplit("/", 1)[1]))
            return
        if url.path.startswith(JOB_PREFIX):
            self.job(url.path[len(JOB_PREFIX) :], query)
            return
        self.send_json(404, {})

    def job(self, rest: str, query: dict) -> None:
        twin = self.server.twin
        with twin.lock:
            builds = dict(twin.builds)
            nxt = twin.next_number
        check = False
        if not twin.job_exists:
            self.send_json(404, {})
        elif rest == "api/json":
            self.send_json(200, {"nextBuildNumber": nxt})
        elif rest == "lastBuild/api/json":
            check = True
            if builds:
                self.send_json(200, {"number": max(builds)})
            else:
                self.send_json(404, {})
        else:
            number, _, tail = rest.partition("/")
            result = builds.get(int(number)) if number.isdigit() else None
            if result is None:
                check = tail == "api/json"
                self.send_json(404, {})
            elif tail == "api/json":
                check = query.get("tree") == ["building"]
                self.send_json(
                    200,
                    {
                        "building": False,
                        "result": result,
                        "duration": 60000,
                        "url": f"http://twin{JOB_PREFIX}{number}/",
                        "timestamp": 1,
                    },
                )
            elif tail == "wfapi/describe":
                self.send_json(200, {"stages": []})
            elif tail == "consoleText":
                raw = f"журнал сборки {number}\nFinished: {result}\n".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_json(404, {})
        if check and twin.configured.is_set():
            twin.checked.set()

    def write_event(self, name: str, data: dict, crlf: bool = False) -> None:
        # Комментарий и data из нескольких строк — проверка разбора SSE.
        nl = "\r\n" if crlf else "\n"
        lines = [": комментарий", f"event: {name}"]
        lines += [f"data: {part}" for part in json.dumps(data, indent=1).splitlines()]
        self.wfile.write((nl.join(lines) + nl + nl).encode())
        self.wfile.flush()

    def listen(self, client_id: str) -> None:
        twin = self.server.twin
        if twin.listens and twin.refuse_listen:
            twin.refuse_listen -= 1
            self.send_json(403, {})
            return
        if not self.session_ok():
            return
        twin.listens.append((client_id, self.headers.get("Cookie", "")))
        n = len(twin.listens)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.write_event(
                "open", {"dispatcherId": twin.dispatcher, "dispatcherInst": 1}
            )
            for item in twin.on_listen.get(n, []):
                twin.feed.put(item)
            while True:
                item = twin.feed.get()
                if item[0] == "close":
                    return
                if item[0] == "wait":
                    item[1].wait()
                elif item[0] == "do":
                    item[1]()
                else:
                    self.write_event(item[1], item[2], crlf=n % 2 == 0)
        except OSError:
            return

    def do_POST(self) -> None:
        twin = self.server.twin
        if not self.authorized():
            return
        url = urllib.parse.urlsplit(self.path)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if url.path != "/sse-gateway/configure" or not self.session_ok():
            self.send_json(404, {})
            return
        batch = urllib.parse.parse_qs(url.query)["batchId"][0]
        twin.configures.append(body)
        for action in twin.before_confirm:
            action()
        self.send_json(200, {})
        twin.configured.set()
        twin.feed.put(
            (
                "ev",
                "configure",
                {"batchId": batch, "dispatcherId": body["dispatcherId"]},
            )
        )
        items = twin.on_configure if len(twin.configures) == 1 else twin.on_reconfigure
        for item in items:
            twin.feed.put(item)


class TwinServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, twin: Twin) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.twin = twin


class ParseSseTest(unittest.TestCase):
    def parse(self, chunks: list[bytes]) -> list[tuple[str, str]]:
        it = iter(chunks)
        return list(load_jwait().parse_sse(lambda: next(it, b"")))

    def test_spec_cases(self) -> None:
        got = self.parse(
            [
                b": c\nevent: a\ndata: 1\ndata:2\n\n",
                b"data: x\r",
                b"\ndata: y\r\r",
                b"event: b\rdata: z\r\n\r\nid: 5\nretry: 1\n\n",
                b"event: lost\ndata: tail",
            ]
        )
        self.assertEqual(got, [("a", "1\n2"), ("message", "x\ny"), ("b", "z")])


class JwaitTest(unittest.TestCase):
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
        (self.root / ".duty" / "duty.toml").write_text(
            'schema = 1\n[jenkins]\nenv_file = "jenkins.env"\n', encoding="utf-8"
        )
        self.twin = Twin()
        self.server = TwinServer(self.twin)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.twin.feed.put, ("close",))
        port = self.server.server_address[1]
        (self.root / "jenkins.env").write_text(
            f"JENKINS_URL=http://127.0.0.1:{port}\nJENKINS_USER=u\nJENKINS_TOKEN=t\n",
            encoding="utf-8",
        )
        self.out = self.root / "out"

    def run_jwait(self, build: str) -> subprocess.CompletedProcess[str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("JENKINS_CLI_URL", "DUTY_TASK")
        }
        env.update(GIT_ENV)
        env.update(
            {
                "DUTY_PROJECT": str(self.root),
                "DUTY_CONFIG_USER": str(self.root / "none.toml"),
                "HOME": str(self.root),
            }
        )
        return subprocess.run(
            [sys.executable, "-I", str(JWAIT), JOB, build, "--out", str(self.out)],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def report(self) -> str:
        return (self.out / "report.md").read_text(encoding="utf-8")

    def done(self) -> str:
        return (self.out / "done").read_text(encoding="utf-8").strip()

    # (a) сборка уже есть и закончилась — поток событий не нужен.
    def test_a_existing_build(self) -> None:
        for result, code in (("SUCCESS", 0), ("FAILURE", 1)):
            with self.subTest(result=result):
                self.out = self.root / f"out-{result}"
                self.twin.add_build(3 + code, result)
                r = self.run_jwait(str(3 + code))
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertEqual(self.done(), str(code))
                self.assertIn(f"итог: **{result}**", self.report())
                self.assertIn(
                    f"журнал сборки {3 + code}", (self.out / "console.log").read_text()
                )
        self.assertEqual(self.twin.connects, [])

    # (b) сборки нет: очередь, старт N, итог по сборке.
    def test_b_queue_then_started(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            job_event(
                "job_run_queue_enter", job_run_queueId=7, job_run_status="QUEUED"
            ),
            job_event("job_run_queue_buildable", job_run_queueId=7),
            job_event(
                "job_run_queue_left", job_run_queueId=7, job_run_status="ALLOCATED"
            ),
            ("do", lambda: self.twin.add_build(5, "SUCCESS")),
            started(5),
        ]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.done(), "0")
        for text in (
            "в очереди",
            "ждёт исполнителя",
            "получила исполнителя",
            "стартовала",
        ):
            self.assertIn(text, r.stderr)
        self.assertLess(r.stderr.index("в очереди"), r.stderr.index("стартовала"))
        report = self.report()
        self.assertIn("итог: **SUCCESS**", report)
        self.assertRegex(
            report, r"ожидание: появления ждал \d+\.\d с \(событий очереди: 3"
        )
        self.assertEqual(
            self.twin.configures,
            [
                {
                    "dispatcherId": "disp-1",
                    "subscribe": [{"jenkins_channel": "job", "job_name": JOB}],
                    "unsubscribe": [],
                }
            ],
        )

    # (c) стартовала N+1, а N нет — сборки N не будет.
    def test_c_later_build_started(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            ("do", lambda: self.twin.add_build(6)),
            started(6),
        ]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.done(), "2")
        self.assertIn("#5 не будет: стартовала #6", self.report())

    # (d) плагина sse-gateway нет — прежнее поведение с понятной причиной.
    def test_d_no_plugin(self) -> None:
        self.twin.sse = False
        self.twin.next_number = 5
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 2, r.stderr)
        report = self.report()
        self.assertIn("сборки folder/app #5 ещё нет", report)
        self.assertIn("плагин sse-gateway", report)
        self.assertIn("404", report)
        self.assertIn("запусти jwait повторно", report)

    # (e) сервер закрыл поток после подтверждения, старт — в следующем listen.
    def test_e_reconnect_same_client_and_cookie(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [("wait", self.twin.checked), ("close",)]
        self.twin.on_listen = {
            2: [("do", lambda: self.twin.add_build(5, "FAILURE")), started(5)]
        }
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertEqual(len(self.twin.listens), 2)
        (id1, cookie1), (id2, cookie2) = self.twin.listens
        self.assertEqual(id1, id2)
        self.assertEqual(self.twin.connects, [id1])
        self.assertIn(COOKIE, cookie1)
        self.assertEqual(cookie1, cookie2)
        self.assertEqual(len(self.twin.configures), 1)
        self.assertIn("переподключений потока: 1", self.report())

    # (f) сборка появилась между первой проверкой и подпиской.
    def test_f_appeared_before_subscription(self) -> None:
        self.twin.next_number = 5
        # Сборка создаётся до того, как в ленту попадёт подтверждение.
        self.twin.before_confirm = [lambda: self.twin.add_build(5)]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("появилась до подписки", r.stderr)
        self.assertIn("итог: **SUCCESS**", self.report())

    # (g) last у задания без сборок — ждёт первого старта.
    def test_g_last_without_builds(self) -> None:
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            job_event(
                "job_run_queue_enter", job_run_queueId=1, job_run_status="QUEUED"
            ),
            ("do", lambda: self.twin.add_build(1)),
            started(1),
        ]
        r = self.run_jwait("last")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("у задания folder/app ещё нет сборок", r.stderr)
        self.assertIn("# Сборка `folder/app` #1", self.report())

    # Номер меньше следующего, а сборки нет — она не появится; поток не нужен.
    def test_number_already_passed(self) -> None:
        self.twin.next_number = 9
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("нет и не будет: следующий номер задания — 9", self.report())
        self.assertEqual(self.twin.connects, [])

    # Задания нет — код 2 сразу, как до потока событий.
    def test_missing_job(self) -> None:
        self.twin.job_exists = False
        for build in ("5", "last"):
            with self.subTest(build=build):
                self.out = self.root / f"out-{build}"
                r = self.run_jwait(build)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("задания folder/app нет", self.report())
        self.assertEqual(self.twin.connects, [])

    # Старт N+1, а событие старта N пропало: сверка находит N.
    def test_later_started_but_build_exists(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            ("do", lambda: (self.twin.add_build(5), self.twin.add_build(6))),
            started(6),
        ]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("найдена сверкой", r.stderr)
        self.assertIn("# Сборка `folder/app` #5", self.report())

    # Событие reload — сверка по REST.
    def test_reload_checks_rest(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            ("do", lambda: self.twin.add_build(5)),
            ("ev", "reload", {}),
        ]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("потерял события", r.stderr)

    # Сессия потеряна (перезапуск): listen отказывает, новая сессия,
    # новый диспетчер получает подписку заново.
    def test_lost_session_resubscribes(self) -> None:
        self.twin.next_number = 5
        self.twin.on_configure = [
            ("wait", self.twin.checked),
            ("do", self.twin.restart),
            ("close",),
        ]
        self.twin.on_reconfigure = [
            ("wait", self.twin.checked),
            ("do", lambda: self.twin.add_build(5)),
            started(5),
        ]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(len(self.twin.connects), 2)
        self.assertEqual(
            [c["dispatcherId"] for c in self.twin.configures], ["disp-1", "disp-2"]
        )
        self.assertIn("sess-2", self.twin.listens[-1][1])

    # Отказы listen подряд сверх предела — код 2.
    def test_listen_refused_limit(self) -> None:
        self.twin.next_number = 5
        self.twin.refuse_listen = 100
        self.twin.on_configure = [("wait", self.twin.checked), ("close",)]
        r = self.run_jwait("5")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("обрывался 6 раз подряд", self.report())
        self.assertIn("listen", self.report())


if __name__ == "__main__":
    unittest.main()
