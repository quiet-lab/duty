#!/usr/bin/env python3
"""duty-runs-web: местный веб-просмотр прогонов агентов (tab-run.sh).

Страницы строятся при запросе, а не по таймеру: сводка (`dispatch.sh list`,
`busy.py list`, `signals.py list --waits`), перечень прогонов
`<results>/*/runs/<имя>/` и страница прогона с живым `out.log`. Дозапись
идёт через Server-Sent Events; файл отслеживается через `inotifywait`
(inotify-tools), без опроса и таймеров. Только стандартная библиотека.

Запуск: `python3 -I scripts/runs-web.py [--results <каталог>] [--port 8765]`.
"""

import argparse
import codecs
import html
import json
import os
import re
import select
import shutil
import signal as os_signal
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_RESULTS = "~/work/telecom/axata/ocp-dev-stand/results"
DEFAULT_PORT = 8765
# Имена каталогов задачи и прогона: tab-run.sh и dispatch.sh допускают только
# такие, поэтому прочие значения запроса отвергаются (выход из каталога результатов).
SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Предохранитель от зависшей команды сводки, а не ожидание события: сводка —
# разовые запросы к herdr, каждый заметно короче этого предела.
SUMMARY_LIMIT_S = 60
# Начальная порция out.log на страницу: хвост этого размера, чтобы многогигабайтный
# журнал не уходил браузеру целиком.
TAIL_BYTES = 1 << 20
CHUNK = 65536
PAGE_STYLE = (
    "body{font:14px/1.4 system-ui,sans-serif;margin:1.5em}table{border-collapse:collapse}"
    "td,th{border:1px solid #888;padding:2px 8px;text-align:left}"
    "pre{background:#1e1e1e;color:#ddd;padding:8px;overflow:auto;white-space:pre-wrap}"
    ".run{color:#b80}.ok{color:#080}.fail{color:#c00}"
)


def parse_meta(text: str) -> dict[str, str]:
    """Строки `ключ: значение` из meta.txt; прочие строки пропускаются."""
    meta: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if sep and key:
            meta[key] = value.strip()
    return meta


def parse_cmd(text: str) -> str:
    """Команда пользователя из cmd.sh, записанного tab-run.sh: между `{` и `} 2>&1 | tee`."""
    lines = text.splitlines()
    try:
        start = lines.index("{")
        end = next(i for i in range(len(lines) - 1, start, -1) if lines[i].startswith("} 2>&1 | tee"))
    except (ValueError, StopIteration):
        return ""
    return "\n".join(lines[start + 1:end])


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_state(run_dir: Path) -> tuple[str, int | None]:
    """(`done` | `running` | `pending`, код выхода). done пишется tab-run.sh последним и целиком."""
    try:
        # Одно чтение вместо exists и read: между ними файл мог появиться.
        done = (run_dir / "done").read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        done = None
    if done is not None:
        try:
            return "done", int(done)
        except ValueError:
            return "done", None
    if (run_dir / "meta.txt").exists():
        return "running", None
    return "pending", None


def describe_run(run_dir: Path) -> dict[str, object]:
    meta = parse_meta(read_text(run_dir / "meta.txt"))
    state, code = run_state(run_dir)
    try:
        mtime = (run_dir / "out.log").stat().st_mtime_ns // 1_000_000
    except OSError:
        mtime = 0
    return {
        "task": run_dir.parent.parent.name,
        "name": run_dir.name,
        "kind": meta.get("kind", ""),
        "command": parse_cmd(read_text(run_dir / "cmd.sh")),
        "meta": meta,
        "state": state,
        "code": code,
        "log_mtime_ms": mtime,
    }


def list_runs(results: Path) -> list[dict[str, object]]:
    """Прогоны всех задач, новые (по started_ms, затем по out.log) выше."""
    runs = [describe_run(d) for d in results.glob("*/runs/*") if d.is_dir()]

    def started(r: dict[str, object]) -> tuple[int, int]:
        meta = r["meta"]
        assert isinstance(meta, dict)
        try:
            ms = int(meta.get("started_ms", "0"))
        except ValueError:
            ms = 0
        mtime = r["log_mtime_ms"]
        assert isinstance(mtime, int)
        return ms, mtime

    runs.sort(key=started, reverse=True)
    return runs


def sse_frame(event: str, data: str) -> bytes:
    """Кадр SSE; перевод строки в data — несколько строк `data:`, клиент склеивает их через \\n."""
    lines = [f"event: {event}"]
    lines += [f"data: {part}" for part in data.split("\n")]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def run_dir_for(results: Path, task: str, name: str) -> Path | None:
    if not (SAFE_RE.match(task) and SAFE_RE.match(name)):
        return None
    run_dir = results / task / "runs" / name
    return run_dir if run_dir.is_dir() else None


def client_gone(sock: socket.socket) -> bool:
    """Клиент закрыл соединение: сокет читается и отдаёт конец потока.

    Лишние байты клиента снимаются с сокета, иначе он оставался бы читаемым
    и ожидание превращалось бы в холостой цикл.
    """
    try:
        return sock.recv(4096) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def follow_log(run_dir: Path, send, sock: socket.socket | None = None) -> None:
    """Отдаёт содержимое out.log и дозапись через `send(bytes)`; завершает кадром `done`.

    Наблюдение ставится до чтения файла, поэтому запись между чтением и
    ожиданием не теряется. Выход: прогон завершён (`done`), клиент ушёл
    (EOF на `sock`) или запись не удалась.
    """
    log = run_dir / "out.log"
    watch = subprocess.Popen(
        ["inotifywait", "-m", "-e", "modify", "-e", "create", "-e", "moved_to",
         "-e", "close_write", "--format", "%f", str(run_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Сигналы сервера (Ctrl-C) наблюдателю не нужны: он закрывается ниже.
        start_new_session=True)
    assert watch.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    offset = 0

    def pump() -> None:
        nonlocal offset
        try:
            with open(log, "rb") as f:
                if offset == 0:
                    size = os.fstat(f.fileno()).st_size
                    offset = max(0, size - TAIL_BYTES)
                f.seek(offset)
                while chunk := f.read(CHUNK):
                    offset += len(chunk)
                    text = decoder.decode(chunk)
                    if text:
                        send(sse_frame("log", text))
        except FileNotFoundError:
            pass

    try:
        # Чтение файла начинается только после «Watches established.»: запись между
        # чтением и установкой наблюдения иначе была бы потеряна.
        ready_buf = b""
        while b"Watches established" not in ready_buf:
            chunk = os.read(watch.stdout.fileno(), 4096)
            if not chunk:
                send(sse_frame("fail", "inotifywait завершился"))
                return
            ready_buf += chunk
        while True:
            pump()
            state, code = run_state(run_dir)
            if state == "done":
                pump()
                send(sse_frame("done", "" if code is None else str(code)))
                return
            fds = [watch.stdout]
            if sock is not None:
                fds.append(sock)  # type: ignore[arg-type]
            ready, _, _ = select.select(fds, [], [])
            if sock is not None and sock in ready and client_gone(sock):
                return
            if watch.stdout in ready:
                chunk = os.read(watch.stdout.fileno(), 4096)
                if not chunk:
                    send(sse_frame("fail", "inotifywait завершился"))
                    return
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        # Процесс свой, известный по Popen.
        if watch.poll() is None:
            watch.terminate()
        watch.wait()
        watch.stdout.close()


def run_summary_cmd(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                              timeout=SUMMARY_LIMIT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"не выполнено: {exc}"
    out = proc.stdout + proc.stderr
    return out if proc.returncode == 0 else f"{out}\n(код выхода {proc.returncode})"


def summary_sections(results: Path) -> list[tuple[str, str]]:
    py = sys.executable
    return [
        ("dispatch.sh list", run_summary_cmd(["bash", str(SCRIPTS / "dispatch.sh"), "list", "--results", str(results)])),
        ("busy.py list", run_summary_cmd([py, "-I", str(SCRIPTS / "busy.py"), "list"])),
        ("signals.py list --waits", run_summary_cmd([py, "-I", str(SCRIPTS / "signals.py"), "list", "--waits"])),
    ]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str, head_extra: str = "") -> bytes:
    return (f"<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>{esc(title)}</title>"
            f"<style>{PAGE_STYLE}</style>{head_extra}<body>{body}</body></html>").encode("utf-8")


def state_label(run: dict[str, object]) -> str:
    if run["state"] == "done":
        code = run["code"]
        cls = "ok" if code == 0 else "fail"
        return f"<span class=\"{cls}\">завершён, код {esc('?' if code is None else code)}</span>"
    if run["state"] == "running":
        return "<span class=\"run\">идёт</span>"
    return "<span class=\"run\">ожидает запуска</span>"


def render_index(results: Path, sections: list[tuple[str, str]]) -> bytes:
    parts = ["<h1>Прогоны агентов</h1>", "<h2>Сводка</h2>"]
    for title, text in sections:
        parts.append(f"<h3>{esc(title)}</h3><pre>{esc(text)}</pre>")
    parts.append(f"<h2>Прогоны</h2><p>Каталог: {esc(results)}</p>")
    rows = []
    for run in list_runs(results):
        link = f"/run?task={quote(str(run['task']))}&amp;name={quote(str(run['name']))}"
        rows.append(f"<tr><td>{esc(run['task'])}</td><td><a href=\"{link}\">{esc(run['name'])}</a></td>"
                    f"<td>{esc(run['kind'])}</td><td>{state_label(run)}</td>"
                    f"<td><code>{esc(str(run['command'])[:200])}</code></td></tr>")
    parts.append("<table><tr><th>Задача</th><th>Прогон</th><th>Вид</th><th>Состояние</th><th>Команда</th></tr>"
                 + "".join(rows) + "</table>")
    return page("Прогоны агентов", "".join(parts))


# Минимум скрипта: EventSource дописывает кадры `log` в pre; `done` закрывает поток,
# иначе браузер переподключался бы к уже завершённому прогону.
RUN_SCRIPT = """<script>
const log = document.getElementById('log'), st = document.getElementById('state');
const es = new EventSource('/stream?task=' + encodeURIComponent(TASK) + '&name=' + encodeURIComponent(NAME));
es.addEventListener('log', e => { const bottom = innerHeight + scrollY >= document.body.scrollHeight - 40;
  log.appendChild(document.createTextNode(e.data)); if (bottom) scrollTo(0, document.body.scrollHeight); });
es.addEventListener('done', e => { st.textContent = 'завершён, код ' + (e.data || '?'); es.close(); });
es.addEventListener('fail', e => { st.textContent = 'наблюдение оборвалось: ' + e.data; es.close(); });
</script>"""


def render_run(run_dir: Path) -> bytes:
    run = describe_run(run_dir)
    meta = run["meta"]
    assert isinstance(meta, dict)
    meta_rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in meta.items())
    task, name = str(run["task"]), str(run["name"])
    body = (f"<p><a href=\"/\">к перечню</a></p><h1>{esc(task)} / {esc(name)}</h1>"
            f"<p>Вид: {esc(run['kind'] or '—')}. Состояние: <span id=\"state\">{state_label(run)}</span></p>"
            f"<table>{meta_rows}</table><h3>Команда</h3><pre>{esc(run['command'])}</pre>"
            f"<h3>out.log</h3><pre id=\"log\"></pre>"
            f"<script>const TASK={json.dumps(task)}, NAME={json.dumps(name)};</script>{RUN_SCRIPT}")
    return page(f"{task}/{name}", body)


class Handler(BaseHTTPRequestHandler):
    results: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def reply(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            self.reply(200, render_index(self.results, summary_sections(self.results)))
            return
        if url.path in ("/run", "/stream"):
            run_dir = run_dir_for(self.results, query.get("task", [""])[0], query.get("name", [""])[0])
            if run_dir is None:
                self.reply(404, page("Нет прогона", "<p>Прогон не найден.</p>"))
            elif url.path == "/run":
                self.reply(200, render_run(run_dir))
            else:
                self.stream(run_dir)
            return
        self.reply(404, page("Нет страницы", "<p>Нет такой страницы.</p>"))

    def stream(self, run_dir: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(frame: bytes) -> None:
            self.wfile.write(frame)
            self.wfile.flush()

        follow_log(run_dir, send, self.connection)


def make_server(results: Path, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"results": results})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--results", default=os.environ.get("DUTY_RESULTS_DIR") or DEFAULT_RESULTS,
                   help="каталог результатов задач (в нём <задача>/runs/<прогон>/)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    if shutil.which("inotifywait") is None:
        print("нет inotifywait: pacman -S inotify-tools", file=sys.stderr)
        return 2
    server = make_server(Path(os.path.expanduser(args.results)), args.host, args.port)
    # SIGTERM закрывает сервер так же, как Ctrl-C.
    os_signal.signal(os_signal.SIGTERM, raise_interrupt)
    print(f"duty-runs-web: http://{args.host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
