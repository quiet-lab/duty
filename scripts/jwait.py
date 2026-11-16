"""Ожидание конца сборки Jenkins по событию и итог в файлы.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Вызывается из `jwait.sh`. Источник событий — команда Jenkins CLI
`console <задание> <номер> -f` по WebSocket (`/cli/ws`) прямо на контроллере:
сервер шлёт журнал сборки по мере записи и кадр EXIT, когда сборка
закончилась, поэтому ожидание — одно чтение сокета, без опроса. Прокси
перед Jenkins может не пропускать заголовки Upgrade, а режим `-http` CLI
через него отвечать 500; тогда CLI идёт на адрес контроллера:
`jenkins.cli_url` конфигурации проекта (пусто — `JENKINS_URL` из файла
доступа), перекрытие — переменная `JENKINS_CLI_URL`.

Адрес Jenkins и учётные данные — файл `jenkins.env_file` (`JENKINS_URL`,
`JENKINS_USER`, `JENKINS_TOKEN`), проверка сертификата выключается
`jenkins.insecure_tls`; без раздела `[jenkins]` в `.duty/duty.toml` итог —
report.md с причиной и код 2. Корень проекта — `--root` или `duty_config.py`.

Итог: `<каталог>/console.log` (журнал сборки), `<каталог>/report.md`
(итог, длительность, этапы `wfapi/describe`, хвост журнала упавшего этапа)
и последним — `<каталог>/done` с кодом: 0 — SUCCESS, 1 — сборка
закончилась с другим итогом, 2 — ожидание не удалось (нет сборки,
нет связи, отказ в доступе).

Протокол CLI по WebSocket (PlainCLIProtocol Jenkins): одно двоичное
сообщение — один кадр «байт операции + данные»; клиент шлёт ARG (0)
со строками в writeUTF, ENCODING (2), LOCALE (1), START (3); сервер —
STDOUT (7), STDERR (8), EXIT (4) с кодом int32.

Сборки ещё нет (номер больше последнего или `last` у задания без сборок) —
её появление ждётся по событиям плагина Jenkins sse-gateway на том же
адресе контроллера, что и CLI: `connect?clientId=` (cookie сессии
сохраняется, в ней живёт диспетчер), поток `listen/<clientId>`
(`text/event-stream`, первое событие `open` с `dispatcherId`),
`configure?batchId=` с подпиской на канал `job` по полному имени задания,
подтверждение — событие `configure`. После подтверждения — одна сверка по
REST (сборка могла появиться до подписки); событие `reload` (сервер
потерял события) — та же сверка. События очереди `job_run_queue_*`
печатаются в stderr; `job_run_started` с нужным номером (для `last` —
любой) — сборка появилась, дальше ожидание конца через CLI; старт сборки
с номером больше нужного — сборки не будет, код 2. Сервер закрывает поток
по своему сроку — переподключение тем же clientId и cookie, пропущенные
события сервер досылает. Плагина нет (connect отвечает 404) или поток
недоступен — код 2 и report.md с причиной: jwait запускается повторно
после появления сборки. Срока ожидания появления нет: общий предел —
у вызывающего (wait-done.sh, вкладка).
"""

import argparse
import base64
import html
import http.client
import importlib.util
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модуль подключается по пути.
_spec = importlib.util.spec_from_file_location(
    "dutylib", Path(__file__).with_name("dutylib.py")
)
dutylib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dutylib)
duty_config = dutylib.duty_config
config = dutylib.config


def env_path() -> Path:
    try:
        return dutylib.access_file("jenkins", "env_file", "Jenkins")
    except dutylib.NotConfigured as error:
        raise WaitError(str(error)) from error


def ssl_context() -> ssl.SSLContext:
    return dutylib.ssl_context(config())


OP_ARG, OP_LOCALE, OP_ENCODING, OP_START, OP_EXIT = 0, 1, 2, 3, 4
OP_STDOUT, OP_STDERR = 7, 8

# Контроллер шлёт ping WebSocket каждые 30 с (замер на 2.528.3: интервал
# 30 с на `wait-node-offline`). Тишина втрое дольше значит, что соединение
# оборвано без FIN: это срок обнаружения обрыва, а не опрос.
SILENCE_S = 90
# Обрыв соединения при идущей сборке — событие для переподключения; подряд
# неудачных подключений больше этого — ожидание прекращается с кодом 2.
MAX_RECONNECTS = 5
TAIL_LINES = 40


class WaitError(Exception):
    pass


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_path().read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def auth_header(e: dict[str, str]) -> str:
    raw = f"{e['JENKINS_USER']}:{e['JENKINS_TOKEN']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def job_path(job: str) -> str:
    """`a/b` → `job/a/job/b/`; адрес задания или сборки тоже принимается."""
    if job.startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(job).path.strip("/").split("/")
        names = [
            urllib.parse.unquote(parts[i + 1])
            for i, p in enumerate(parts[:-1])
            if p == "job"
        ]
        job = "/".join(names)
    return "".join(
        f"job/{urllib.parse.quote(n, safe='')}/" for n in job.split("/") if n
    )


def job_name(job: str) -> str:
    path = job_path(job)
    return "/".join(urllib.parse.unquote(p) for p in path.strip("/").split("/")[1::2])


def rest(base: str, path: str, auth: str) -> dict | None:
    req = urllib.request.Request(
        base.rstrip("/") + "/" + path, headers={"Authorization": auth}
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise WaitError(
                f"Jenkins ответил {exc.code} на {path}: учётные данные {env_path()}"
            ) from None
        raise WaitError(f"Jenkins ответил {exc.code} на {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WaitError(f"Jenkins недоступен ({path}): {exc}") from None


def rest_text(base: str, path: str, auth: str) -> str:
    req = urllib.request.Request(
        base.rstrip("/") + "/" + path, headers={"Authorization": auth}
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""


class CliSocket:
    """Клиент WebSocket ровно для одной команды Jenkins CLI."""

    def __init__(self, url: str, auth: str) -> None:
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=30)
        self.sock = (
            ssl_context().wrap_socket(raw, server_hostname=u.hostname)
            if u.scheme == "https"
            else raw
        )
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path.rstrip("/") + "/cli/ws"
        # Origin обязателен: Jenkins сверяет его с адресом из запроса.
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Authorization: {auth}\r\nOrigin: {u.scheme}://{u.netloc}\r\n\r\n"
            ).encode()
        )
        self.rf = self.sock.makefile("rb")
        status = self.rf.readline().decode("latin-1").strip()
        headers = []
        while (line := self.rf.readline()) not in (b"\r\n", b""):
            headers.append(line.decode("latin-1").strip())
        if " 101 " not in f"{status} ":
            why = next((h for h in headers if h.lower().startswith("x-cli-error")), "")
            raise WaitError(f"CLI {url}: {status} {why}".strip())
        self.sock.settimeout(SILENCE_S)

    def send(self, payload: bytes, opcode: int = 2) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def _read(self, n: int) -> bytes:
        data = self.rf.read(n)
        if data is None or len(data) < n:
            raise EOFError
        return data

    def message(self) -> bytes | None:
        """Следующее двоичное сообщение; None — сервер закрыл соединение."""
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read(2)
            opcode, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n) if n else b""
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts)

    def run(self, args: list[str]) -> None:
        for a in args:
            enc = a.encode()
            self.send(bytes([OP_ARG]) + struct.pack(">H", len(enc)) + enc)
        for op, val in ((OP_ENCODING, "UTF-8"), (OP_LOCALE, "en")):
            self.send(bytes([op]) + struct.pack(">H", len(val)) + val.encode())
        self.send(bytes([OP_START]))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def follow(
    cli_url: str, auth: str, name: str, number: int, log_path: Path
) -> int | None:
    """Журнал сборки до кадра EXIT; код команды console или None при обрыве."""
    cli = CliSocket(cli_url, auth)
    try:
        cli.run(["console", name, str(number), "-f"])
        # Журнал переписывается с начала: console -f отдаёт его целиком.
        with log_path.open("wb") as log:
            while True:
                msg = cli.message()
                if msg is None:
                    return None
                op, data = msg[0], msg[1:]
                if op in (OP_STDOUT, OP_STDERR):
                    log.write(data)
                    log.flush()
                elif op == OP_EXIT:
                    return struct.unpack(">i", data)[0]
    except (EOFError, OSError):
        return None
    finally:
        cli.close()


class SseUnavailable(Exception):
    """Поток событий sse-gateway недоступен: плагина нет или нет связи."""


def parse_sse(read1) -> Iterator[tuple[str, str]]:
    """События потока text/event-stream: пары (имя, данные).

    `read1()` отдаёт очередную порцию байтов, пустая — конец потока.
    Разбор по спецификации HTML (раздел server-sent events): конец строки —
    CRLF, LF или CR; строка с `:` в начале — комментарий; `data` из
    нескольких строк склеивается через LF; пустая строка отправляет
    событие; поля `id` и `retry` не нужны и пропускаются; незаконченное
    событие в конце потока отбрасывается. Ошибка чтения и тишина дольше
    срока сокета — тоже конец потока.
    """
    buf = b""
    event, data = "", []
    eof = False
    while True:
        m = re.search(rb"\r\n|\r|\n", buf)
        # CR в конце порции может оказаться началом CRLF: ждём следующую.
        if m is None or (m.group() == b"\r" and m.end() == len(buf) and not eof):
            if eof:
                return
            try:
                chunk = read1()
            except (OSError, http.client.HTTPException, ValueError):
                chunk = b""
            if not chunk:
                eof = True
            buf += chunk
            continue
        line = buf[: m.start()].decode("utf-8", errors="replace")
        buf = buf[m.end() :]
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


class SseStream:
    """Клиент sse-gateway Jenkins: connect, listen и configure в одной сессии.

    Диспетчер событий живёт в HTTP-сессии контроллера, поэтому cookie из
    `Set-Cookie` (`JSESSIONID…`) идут во всех следующих запросах вместе
    с Basic-авторизацией.
    """

    def __init__(self, base: str, auth: str, client_id: str) -> None:
        u = urllib.parse.urlsplit(base)
        self.https = u.scheme == "https"
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/") + "/sse-gateway/"
        self.auth = auth
        self.client_id = client_id
        self.cookies: dict[str, str] = {}
        self.batch = 0
        self.conn: http.client.HTTPConnection | None = None

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.https:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=ssl_context()
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": self.auth}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        h.update(extra or {})
        return h

    def _keep_cookies(self, resp: http.client.HTTPResponse) -> None:
        for raw in resp.headers.get_all("Set-Cookie") or []:
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.cookies[k.strip()] = v.strip()

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        extra: dict[str, str] | None = None,
    ) -> int:
        conn = self._connection(30)
        try:
            conn.request(
                method, self.prefix + path, body=body, headers=self._headers(extra)
            )
            resp = conn.getresponse()
            resp.read()
            self._keep_cookies(resp)
            return resp.status
        finally:
            conn.close()

    def connect(self) -> None:
        try:
            status = self._request(
                "GET", "connect?clientId=" + urllib.parse.quote(self.client_id, safe="")
            )
        except (OSError, http.client.HTTPException) as exc:
            raise SseUnavailable(
                f"нет связи с {self.host}:{self.port}: {exc}"
            ) from None
        if status == 404:
            raise SseUnavailable("connect ответил 404 — плагин не установлен")
        if status != 200:
            raise SseUnavailable(f"connect ответил {status}")

    def listen(self) -> Iterator[tuple[str, str]]:
        """Подключение к потоку; отказ — SseUnavailable или OSError."""
        self.close()
        # Срок сокета — обнаружение мёртвого соединения (SILENCE_S), не опрос.
        self.conn = self._connection(SILENCE_S)
        self.conn.request(
            "GET",
            self.prefix + "listen/" + urllib.parse.quote(self.client_id, safe=""),
            headers=self._headers({"Accept": "text/event-stream"}),
        )
        resp = self.conn.getresponse()
        self._keep_cookies(resp)
        if resp.status != 200:
            resp.read()
            raise SseUnavailable(f"listen ответил {resp.status}")
        return parse_sse(lambda: resp.read1(65536))

    def configure(self, dispatcher: object, name: str) -> int:
        """Подписка на события задания; возврат — batchId для подтверждения."""
        self.batch += 1
        body = json.dumps(
            {
                "dispatcherId": dispatcher,
                "subscribe": [{"jenkins_channel": "job", "job_name": name}],
                "unsubscribe": [],
            }
        ).encode()
        try:
            status = self._request(
                "POST",
                f"configure?batchId={self.batch}",
                body,
                {"Content-Type": "application/json"},
            )
        except (OSError, http.client.HTTPException) as exc:
            raise SseUnavailable(f"configure: {exc}") from None
        if status != 200:
            raise SseUnavailable(f"configure ответил {status}")
        return self.batch

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


QUEUE_TEXT = {
    "job_run_queue_enter": "в очереди",
    "job_run_queue_buildable": "ждёт исполнителя",
    "job_run_queue_blocked": "заблокирована в очереди",
}
LEFT_TEXT = {
    "ALLOCATED": "получила исполнителя",
    "CANCELLED": "снята с очереди",
}


def build_number(payload: dict) -> int | None:
    raw = str(payload.get("jenkins_object_id") or "")
    if not raw.isdigit():
        raw = str(payload.get("jenkins_object_name") or "").lstrip("#")
    return int(raw) if raw.isdigit() else None


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def wait_appear(
    cli_url: str, base: str, auth: str, name: str, jp: str, number: int | None
) -> tuple[int, str]:
    """Ожидание появления сборки по событиям sse-gateway; возврат — номер и замер."""
    started = time.time()
    if number is None:
        head = f"у задания {name} ещё нет сборок"
    else:
        head = f"сборки {name} #{number} ещё нет"

    def unavailable(why: str) -> WaitError:
        return WaitError(
            f"{head}; поток событий Jenkins (плагин sse-gateway) недоступен: {why}"
            " — запусти jwait повторно после появления сборки"
        )

    def check() -> int | None:
        """Сверка по REST: номер появившейся сборки или None."""
        if number is None:
            last = rest(base, jp + "lastBuild/api/json?tree=number", auth)
            return int(last["number"]) if last else None
        # Следующий номер читается до сборки: сборка, созданная между двумя
        # запросами, не принимается за пропущенную.
        job = rest(base, jp + "api/json?tree=nextBuildNumber", auth) or {}
        if rest(base, f"{jp}{number}/api/json?tree=building", auth) is not None:
            return number
        nxt = job.get("nextBuildNumber")
        if isinstance(nxt, int) and nxt > number:
            raise WaitError(
                f"сборки {name} #{number} нет и не будет: следующий номер задания — {nxt}"
            )
        return None

    def measured(found: int, how: str) -> tuple[int, str]:
        say(f"{name} #{found}: {how}")
        return found, (
            f"появления ждал {time.time() - started:.1f} с"
            f" (событий очереди: {queued}, переподключений потока: {reconnects})"
        )

    queued = reconnects = 0
    found = check()
    if found is not None:
        return measured(found, "сборка уже есть")
    stream = SseStream(cli_url, auth, f"jwait-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        stream.connect()
    except SseUnavailable as exc:
        raise unavailable(str(exc)) from None
    say(f"{head}: жду появления по событиям Jenkins (sse-gateway, задание {name})")
    dispatcher: object = None
    batch = 0
    failures = 0
    why = ""
    try:
        while True:
            got_event = False
            try:
                events = stream.listen()
            except (SseUnavailable, OSError, http.client.HTTPException) as exc:
                why = str(exc)
                if dispatcher is None:
                    raise unavailable(f"listen: {why}") from None
                events = iter(())
            for ev, data in events:
                got_event = True
                try:
                    payload = json.loads(data) if data else {}
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if ev == "open":
                    current = payload.get("dispatcherId")
                    if current is None:
                        raise unavailable("событие open без dispatcherId")
                    if current != dispatcher:
                        # Первое подключение или новый диспетчер (сессия
                        # потеряна): подписка заново, подтверждение — событием.
                        try:
                            batch = stream.configure(current, name)
                        except SseUnavailable as exc:
                            if dispatcher is None:
                                raise unavailable(str(exc)) from None
                            raise WaitError(f"поток событий: {exc}") from None
                        dispatcher = current
                    continue
                if ev == "configure" and str(payload.get("batchId")) == str(batch):
                    say("подписка на события задания подтверждена, сверка по REST")
                    found = check()
                    if found is not None:
                        return measured(found, "сборка появилась до подписки")
                    continue
                if ev == "reload":
                    say("сервер событий потерял события, сверка по REST")
                    found = check()
                    if found is not None:
                        return measured(found, "сборка найдена сверкой")
                    continue
                if ev != "job" or payload.get("job_name") != name:
                    continue
                kind = str(payload.get("jenkins_event") or "")
                queue_id = payload.get("job_run_queueId", "?")
                if kind.startswith("job_run_queue_"):
                    queued += 1
                    if kind == "job_run_queue_left":
                        status = str(payload.get("job_run_status") or "")
                        text = LEFT_TEXT.get(status, f"вышла из очереди ({status})")
                    else:
                        text = QUEUE_TEXT.get(kind, kind)
                    say(f"{name}: сборка {text} (очередь {queue_id})")
                elif kind == "job_run_started":
                    n = build_number(payload)
                    if n is None:
                        continue
                    if number is None or n == number:
                        return measured(n, "сборка стартовала")
                    if n > number:
                        raise WaitError(
                            f"сборки {name} #{number} не будет: стартовала #{n}"
                        )
            # Поток закрыт сервером (его срок) или оборван: переподключение
            # тем же clientId и cookie. Подключение без единого события
            # (сервер шлёт `open` на каждое) считается неудачным.
            if got_event:
                failures = 0
            else:
                failures += 1
                if failures > MAX_RECONNECTS:
                    raise WaitError(
                        f"{head}; поток событий обрывался {failures} раз подряд: {why or 'нет событий'}"
                    )
            reconnects += 1
    finally:
        stream.close()


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-n:])


def report(
    out: Path, base: str, auth: str, name: str, number: int, build_path: str, note: str
) -> int:
    info = (
        rest(
            base,
            build_path + "api/json?tree=result,duration,building,url,timestamp",
            auth,
        )
        or {}
    )
    desc = rest(base, build_path + "wfapi/describe", auth) or {}
    result = info.get("result")
    code = 0 if result == "SUCCESS" else 1
    lines = [
        f"# Сборка `{name}` #{number}",
        "",
        (
            f"- итог: **{result or 'не записан'}**"
            f"{' (ещё идёт)' if info.get('building') else ''}"
        ),
        f"- длительность: {info.get('duration', 0) / 60000:.1f} мин",
        f"- начало: `{info.get('timestamp')}` мс Unix UTC",
        f"- адрес: {info.get('url', base + build_path)}",
        f"- журнал: `{out / 'console.log'}`",
    ]
    if note:
        lines.append(f"- ожидание: {note}")
    stages = desc.get("stages", [])
    if stages:
        lines += ["", "| этап | итог | мин |", "|---|---|---|"]
        lines += [
            f"| {s['name']} | {s['status']} | {s.get('durationMillis', 0) / 60000:.1f} |"
            for s in stages
        ]
    failed = [s for s in stages if s.get("status") in ("FAILED", "UNSTABLE", "ABORTED")]
    for s in failed:
        lines += ["", f"## Упавший этап «{s['name']}»", ""]
        sd = rest(base, s["_links"]["self"]["href"].lstrip("/"), auth) or {}
        bad = [
            n
            for n in sd.get("stageFlowNodes", [])
            if n.get("status") in ("FAILED", "UNSTABLE", "ABORTED")
        ]
        if not bad:
            lines.append("Упавших шагов нет: этап пропущен после прежнего падения.")
        for node in bad:
            text = ""
            href = node.get("_links", {}).get("log", {}).get("href")
            if href:
                text = strip_html(
                    (rest(base, href.lstrip("/"), auth) or {}).get("text", "")
                )
            if not text.strip():
                text = node.get("parameterDescription") or ""
            lines += [
                f"Шаг «{node['name']}» ({node['status']}):",
                "",
                "```",
                tail(text) or "(журнал шага пуст)",
                "```",
                "",
            ]
    console = out / "console.log"
    if not console.exists() or console.stat().st_size == 0:
        console.write_text(rest_text(base, build_path + "consoleText", auth))
    if code:
        lines += [
            "",
            f"## Хвост журнала сборки ({TAIL_LINES} строк)",
            "",
            "```",
            tail(console.read_text(errors="replace")),
            "```",
        ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return code


def write_done(out: Path, code: int) -> None:
    # Отметка появляется переименованием: ждущий видит её целиком.
    tmp = out / ".done.tmp"
    tmp.write_text(f"{code}\n")
    tmp.rename(out / "done")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("job", help="задание: папка/задание или адрес задания")
    p.add_argument("build", help="номер сборки или last")
    p.add_argument("--out", default="", help="каталог итога")
    p.add_argument("--root", default=None, help="корень проекта")
    a = p.parse_args()
    try:
        cfg = config(a.root)
    except duty_config.ConfigError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    name = job_name(a.job)
    if not name:
        p.error(f"не разобрано имя задания: {a.job}")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    jp = job_path(a.job)
    e: dict[str, str] = {}
    auth = base = ""
    number: int | None = None
    error = ""
    try:
        if not cfg.has("jenkins"):
            raise WaitError("нет раздела [jenkins] в .duty/duty.toml")
        e = env()
        auth = auth_header(e)
        base = e["JENKINS_URL"]
        if a.build == "last":
            # Сборок нет — номер остаётся пустым, первая ждётся по событиям.
            last = rest(base, jp + "lastBuild/api/json?tree=number", auth)
            number = int(last["number"]) if last else None
        else:
            number = int(a.build)
    except (WaitError, KeyError, ValueError, OSError) as exc:
        error = str(exc)
    tag = f"{slug}-{number if number is not None else a.build}"
    if a.out:
        out = Path(a.out)
    elif os.environ.get("DUTY_TASK"):
        out = Path(os.environ["DUTY_TASK"]) / "runs" / f"jenkins-{tag}"
    else:
        out = cfg.path("paths.results") / "wait" / tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "done").unlink(missing_ok=True)

    code = 2
    try:
        if error:
            raise WaitError(error)
        cli_url = (
            os.environ.get("JENKINS_CLI_URL")
            or e.get("JENKINS_CLI_URL")
            or cfg.get("jenkins.cli_url", "")
            or base
        )
        info = None
        if number is not None:
            info = rest(base, f"{jp}{number}/api/json?tree=building", auth)
        appear = ""
        if info is None:
            number, appear = wait_appear(cli_url, base, auth, name, jp, number)
            info = rest(base, f"{jp}{number}/api/json?tree=building", auth)
            if info is None:
                raise WaitError(f"сборки {name} #{number} нет после события старта")
        bp = f"{jp}{number}/"
        print(f"жду {name} #{number}, итог — {out}", file=sys.stderr, flush=True)
        note, failures, started = appear, 0, time.time()
        while info and info.get("building"):
            got = None
            try:
                got = follow(cli_url, auth, name, number, out / "console.log")
            except WaitError as exc:
                note = str(exc)
            except OSError as exc:
                note = f"CLI {cli_url}: {exc}"
            if got is not None:
                break
            info = rest(base, bp + "api/json?tree=building", auth)
            failures += 1
            if info and info.get("building") and failures >= MAX_RECONNECTS:
                raise WaitError(
                    f"соединение CLI обрывалось {failures} раз подряд: {note}"
                )
        if failures:
            note = (note + "; " if note else "") + f"переподключений: {failures}"
        note = (
            note + "; " if note else ""
        ) + f"ждал {(time.time() - started) / 60:.1f} мин"
        code = report(out, base, auth, name, number, bp, note)
    except (WaitError, KeyError, ValueError, OSError) as exc:
        (out / "report.md").write_text(
            f"# Ожидание сборки `{name}` {a.build}\n\nне удалось: {exc}\n"
        )
        code = 2
    write_done(out, code)
    print((out / "report.md").read_text())
    return code


if __name__ == "__main__":
    sys.exit(main())
