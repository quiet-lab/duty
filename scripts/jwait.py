"""Ожидание конца сборки Jenkins по событию и итог в файлы.

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
"""

import argparse
import base64
import html
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
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модуль подключается по пути.
_spec = importlib.util.spec_from_file_location('duty_config', Path(__file__).with_name('duty_config.py'))
duty_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(duty_config)

_CFG = None


def config(root: str | None = None):
    """Конфигурация проекта: загружается один раз."""
    global _CFG
    if _CFG is None or root is not None:
        _CFG = duty_config.load(root=root)
    return _CFG


def env_path() -> Path:
    p = Path(config().get('jenkins.env_file', '')).expanduser()
    return p if p.is_absolute() else (config().root or Path.cwd()) / p


def ssl_context() -> ssl.SSLContext:
    """Проверка сертификата выключается только ключом `jenkins.insecure_tls`."""
    if config().get('jenkins.insecure_tls', False):
        return ssl._create_unverified_context()
    return ssl.create_default_context()

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
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def auth_header(e: dict[str, str]) -> str:
    raw = f"{e['JENKINS_USER']}:{e['JENKINS_TOKEN']}".encode()
    return 'Basic ' + base64.b64encode(raw).decode()


def job_path(job: str) -> str:
    """`a/b` → `job/a/job/b/`; адрес задания или сборки тоже принимается."""
    if job.startswith(('http://', 'https://')):
        parts = urllib.parse.urlsplit(job).path.strip('/').split('/')
        names = [urllib.parse.unquote(parts[i + 1]) for i, p in enumerate(parts[:-1])
                 if p == 'job']
        job = '/'.join(names)
    return ''.join(f'job/{urllib.parse.quote(n, safe="")}/' for n in job.split('/') if n)


def job_name(job: str) -> str:
    path = job_path(job)
    return '/'.join(urllib.parse.unquote(p) for p in path.strip('/').split('/')[1::2])


def rest(base: str, path: str, auth: str) -> dict | None:
    req = urllib.request.Request(base.rstrip('/') + '/' + path, headers={'Authorization': auth})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise WaitError(f'Jenkins ответил {exc.code} на {path}: учётные данные '
                            f'{env_path()}') from None
        raise WaitError(f'Jenkins ответил {exc.code} на {path}') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WaitError(f'Jenkins недоступен ({path}): {exc}') from None


def rest_text(base: str, path: str, auth: str) -> str:
    req = urllib.request.Request(base.rstrip('/') + '/' + path, headers={'Authorization': auth})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
            return r.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, TimeoutError, OSError):
        return ''


class CliSocket:
    """Клиент WebSocket ровно для одной команды Jenkins CLI."""

    def __init__(self, url: str, auth: str) -> None:
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == 'https' else 80)
        raw = socket.create_connection((u.hostname, port), timeout=30)
        self.sock = ssl_context().wrap_socket(raw, server_hostname=u.hostname) \
            if u.scheme == 'https' else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path.rstrip('/') + '/cli/ws'
        # Origin обязателен: Jenkins сверяет его с адресом из запроса.
        self.sock.sendall((
            f'GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
            f'Authorization: {auth}\r\nOrigin: {u.scheme}://{u.netloc}\r\n\r\n').encode())
        self.rf = self.sock.makefile('rb')
        status = self.rf.readline().decode('latin-1').strip()
        headers = []
        while (line := self.rf.readline()) not in (b'\r\n', b''):
            headers.append(line.decode('latin-1').strip())
        if ' 101 ' not in f'{status} ':
            why = next((h for h in headers if h.lower().startswith('x-cli-error')), '')
            raise WaitError(f'CLI {url}: {status} {why}'.strip())
        self.sock.settimeout(SILENCE_S)

    def send(self, payload: bytes, opcode: int = 2) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('>H', n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('>Q', n)
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
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            data = self._read(n) if n else b''
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            parts.append(data)
            if fin:
                return b''.join(parts)

    def run(self, args: list[str]) -> None:
        for a in args:
            enc = a.encode()
            self.send(bytes([OP_ARG]) + struct.pack('>H', len(enc)) + enc)
        for op, val in ((OP_ENCODING, 'UTF-8'), (OP_LOCALE, 'en')):
            self.send(bytes([op]) + struct.pack('>H', len(val)) + val.encode())
        self.send(bytes([OP_START]))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def follow(cli_url: str, auth: str, name: str, number: int, log_path: Path) -> int | None:
    """Журнал сборки до кадра EXIT; код команды console или None при обрыве."""
    cli = CliSocket(cli_url, auth)
    try:
        cli.run(['console', name, str(number), '-f'])
        # Журнал переписывается с начала: console -f отдаёт его целиком.
        with log_path.open('wb') as log:
            while True:
                msg = cli.message()
                if msg is None:
                    return None
                op, data = msg[0], msg[1:]
                if op in (OP_STDOUT, OP_STDERR):
                    log.write(data)
                    log.flush()
                elif op == OP_EXIT:
                    return struct.unpack('>i', data)[0]
    except (EOFError, OSError):
        return None
    finally:
        cli.close()


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', text))


def tail(text: str, n: int = TAIL_LINES) -> str:
    return '\n'.join(text.rstrip('\n').splitlines()[-n:])


def report(out: Path, base: str, auth: str, name: str, number: int, build_path: str,
           note: str) -> int:
    info = rest(base, build_path + 'api/json?tree=result,duration,building,url,timestamp',
                auth) or {}
    desc = rest(base, build_path + 'wfapi/describe', auth) or {}
    result = info.get('result')
    code = 0 if result == 'SUCCESS' else 1
    lines = [f'# Сборка `{name}` #{number}', '',
             f'- итог: **{result or "не записан"}**'
             f'{" (ещё идёт)" if info.get("building") else ""}',
             f'- длительность: {info.get("duration", 0) / 60000:.1f} мин',
             f'- начало: `{info.get("timestamp")}` мс Unix UTC',
             f'- адрес: {info.get("url", base + build_path)}',
             f'- журнал: `{out / "console.log"}`']
    if note:
        lines.append(f'- ожидание: {note}')
    stages = desc.get('stages', [])
    if stages:
        lines += ['', '| этап | итог | мин |', '|---|---|---|']
        lines += [f'| {s["name"]} | {s["status"]} | {s.get("durationMillis", 0) / 60000:.1f} |'
                  for s in stages]
    failed = [s for s in stages if s.get('status') in ('FAILED', 'UNSTABLE', 'ABORTED')]
    for s in failed:
        lines += ['', f'## Упавший этап «{s["name"]}»', '']
        sd = rest(base, s['_links']['self']['href'].lstrip('/'), auth) or {}
        bad = [n for n in sd.get('stageFlowNodes', [])
               if n.get('status') in ('FAILED', 'UNSTABLE', 'ABORTED')]
        if not bad:
            lines.append('Упавших шагов нет: этап пропущен после прежнего падения.')
        for node in bad:
            text = ''
            href = node.get('_links', {}).get('log', {}).get('href')
            if href:
                text = strip_html((rest(base, href.lstrip('/'), auth) or {}).get('text', ''))
            if not text.strip():
                text = node.get('parameterDescription') or ''
            lines += [f'Шаг «{node["name"]}» ({node["status"]}):', '', '```',
                      tail(text) or '(журнал шага пуст)', '```', '']
    console = out / 'console.log'
    if not console.exists() or console.stat().st_size == 0:
        console.write_text(rest_text(base, build_path + 'consoleText', auth))
    if code:
        lines += ['', f'## Хвост журнала сборки ({TAIL_LINES} строк)', '', '```',
                  tail(console.read_text(errors='replace')), '```']
    (out / 'report.md').write_text('\n'.join(lines) + '\n')
    return code


def write_done(out: Path, code: int) -> None:
    # Отметка появляется переименованием: ждущий видит её целиком.
    tmp = out / '.done.tmp'
    tmp.write_text(f'{code}\n')
    tmp.rename(out / 'done')


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('job', help='задание: папка/задание или адрес задания')
    p.add_argument('build', help='номер сборки или last')
    p.add_argument('--out', default='', help='каталог итога')
    p.add_argument('--root', default=None, help='корень проекта')
    a = p.parse_args()
    try:
        cfg = config(a.root)
    except duty_config.ConfigError as exc:
        print(f'ошибка конфигурации: {exc}', file=sys.stderr)
        return 2

    name = job_name(a.job)
    if not name:
        p.error(f'не разобрано имя задания: {a.job}')
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-')
    jp = job_path(a.job)
    e: dict[str, str] = {}
    auth = base = ''
    number: int | None = None
    error = ''
    try:
        if not cfg.has('jenkins'):
            raise WaitError('нет раздела [jenkins] в .duty/duty.toml')
        e = env()
        auth = auth_header(e)
        base = e['JENKINS_URL']
        if a.build == 'last':
            last = rest(base, jp + 'lastBuild/api/json?tree=number', auth)
            if not last:
                raise WaitError(f'у задания {name} нет сборок')
            number = int(last['number'])
        else:
            number = int(a.build)
    except (WaitError, KeyError, ValueError, OSError) as exc:
        error = str(exc)
    tag = f'{slug}-{number if number is not None else a.build}'
    if a.out:
        out = Path(a.out)
    elif os.environ.get('DUTY_TASK'):
        out = Path(os.environ['DUTY_TASK']) / 'runs' / f'jenkins-{tag}'
    else:
        out = cfg.path('paths.results') / 'wait' / tag
    out.mkdir(parents=True, exist_ok=True)
    (out / 'done').unlink(missing_ok=True)

    code = 2
    try:
        if error or number is None:
            raise WaitError(error)
        cli_url = os.environ.get('JENKINS_CLI_URL') or e.get('JENKINS_CLI_URL') or cfg.get('jenkins.cli_url', '') or base
        bp = f'{jp}{number}/'
        info = rest(base, bp + 'api/json?tree=building', auth)
        if info is None:
            raise WaitError(f'сборки {name} #{number} нет')
        print(f'жду {name} #{number}, итог — {out}', file=sys.stderr, flush=True)
        note, failures, started = '', 0, time.time()
        while info and info.get('building'):
            got = None
            try:
                got = follow(cli_url, auth, name, number, out / 'console.log')
            except WaitError as exc:
                note = str(exc)
            except OSError as exc:
                note = f'CLI {cli_url}: {exc}'
            if got is not None:
                break
            info = rest(base, bp + 'api/json?tree=building', auth)
            failures += 1
            if info and info.get('building') and failures >= MAX_RECONNECTS:
                raise WaitError(f'соединение CLI обрывалось {failures} раз подряд: {note}')
        if failures:
            note = (note + '; ' if note else '') + f'переподключений: {failures}'
        note = (note + '; ' if note else '') + f'ждал {(time.time() - started) / 60:.1f} мин'
        code = report(out, base, auth, name, number, bp, note)
    except (WaitError, KeyError, ValueError, OSError) as exc:
        (out / 'report.md').write_text(f'# Ожидание сборки `{name}` {a.build}\n\n'
                                       f'не удалось: {exc}\n')
        code = 2
    write_done(out, code)
    print((out / 'report.md').read_text())
    return code


if __name__ == '__main__':
    sys.exit(main())
