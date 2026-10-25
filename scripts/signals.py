#!/usr/bin/env python3
"""Именованные сигналы между задачами: защёлка «событие произошло».

Сигнал — файл `<имя>.json` в каталоге сигналов. Дающий создаёт его
атомарно (временный файл с точкой в имени и `os.replace`), ожидающий
узнаёт о нём по событию inotify, без опроса. Пока `wait` ждёт, он держит
отметку `<имя>.waiting-<кто>`; `list` показывает сигналы и ожидания.
Требуется `inotifywait` (inotify-tools). Без сторонних библиотек.

Команды: signal, wait, list, clear. Коды выхода: 0 — успех, 2 — неверное
обращение или сбой наблюдения, 124 — истёк --timeout-s.
"""

import argparse
import json
import os
import re
import select
import signal as os_signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DIR = "~/work/telecom/axata/ocp-dev-stand/signals"
# Точек в имени нет, поэтому суффикс «.waiting-» отметки ожидания однозначен.
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 100
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
WAITING = ".waiting-"


def signals_dir() -> Path:
    return Path(os.environ.get("DUTY_SIGNALS_DIR") or os.path.expanduser(DEFAULT_DIR))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но чужой пользователь: считается живым.
        return True
    return True


def valid_name(name: str) -> bool:
    return len(name) <= NAME_MAX and bool(NAME_RE.match(name))


def check_name(kind: str, name: str) -> bool:
    if valid_name(name):
        return True
    print(f"недопустимое {kind}: {name!r} (латиница в нижнем регистре, цифры и одиночные дефисы, "
          f"не длиннее {NAME_MAX} знаков)", file=sys.stderr)
    return False


def fmt_utc(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ms / 1000))


def write_atomic(path: Path, data: dict) -> None:
    # Временное имя начинается с точки: ни ожидающий, ни list его не читают.
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Повреждённый файл показывается как есть, а не скрывается.
        return {"name": path.name.split(".")[0], "text": "файл повреждён", "from": "?",
                "task": "", "at_ms": 0, "pid": 0, "since_ms": 0, "holder": "?"}
    return data if isinstance(data, dict) else None


def default_who(explicit: str | None) -> str:
    return explicit or os.environ.get("DUTY_NAME") or os.environ.get("DUTY_HOLDER") or "?"


def signal_path(name: str) -> Path:
    return signals_dir() / f"{name}.json"


def show_signal(name: str, data: dict) -> None:
    print(f"сигнал {name}: {data.get('text') or ''}")
    print(f"от {data.get('from') or '?'}, {fmt_utc(int(data.get('at_ms') or 0))}")


def cmd_signal(args: argparse.Namespace) -> int:
    if not check_name("имя сигнала", args.name):
        return EXIT_USAGE
    d = signals_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = signal_path(args.name)
    if args.keep:
        old = read_json(path)
        if old is not None:
            print(f"сигнал {args.name} уже есть: {old.get('text') or ''} "
                  f"(от {old.get('from') or '?'}, {fmt_utc(int(old.get('at_ms') or 0))})")
            return 0
    data = {"name": args.name, "text": args.text, "from": default_who(args.who),
            "task": os.environ.get("DUTY_TASK", ""), "at_ms": now_ms()}
    write_atomic(path, data)
    print(f"сигнал {args.name} дан")
    return 0


class Watch:
    """Построчное чтение вывода inotifywait с пределом по времени."""

    def __init__(self, directory: Path) -> None:
        # stderr объединён со stdout: «Watches established.» inotifywait пишет в stderr.
        self.proc = subprocess.Popen(
            ["inotifywait", "-m", "-e", "moved_to", "-e", "close_write", "--format", "%f", str(directory)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert self.proc.stdout is not None
        self.fd = self.proc.stdout.fileno()
        self.buf = b""

    def line(self, deadline: float | None) -> str | None:
        """Следующая строка; None — срок истёк; EOFError — наблюдение оборвалось."""
        while b"\n" not in self.buf:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], left)
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise EOFError
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return raw.decode("utf-8", "replace")

    def close(self) -> None:
        # Процесс свой, известный по Popen.
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


def _raise_exit(signum: int, _frame: object) -> None:
    # SystemExit пробегает через finally и снимает отметку ожидания.
    raise SystemExit(128 + signum)


def cmd_wait(args: argparse.Namespace) -> int:
    if not check_name("имя сигнала", args.name):
        return EXIT_USAGE
    holder = args.holder or os.environ.get("DUTY_NAME") or os.environ.get("DUTY_HOLDER") or f"pid{os.getpid()}"
    if not check_name("имя ожидающего", holder):
        return EXIT_USAGE
    d = signals_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = signal_path(args.name)
    deadline = None if args.timeout_s is None else time.monotonic() + args.timeout_s
    for sig in (os_signal.SIGTERM, os_signal.SIGINT, os_signal.SIGHUP):
        os_signal.signal(sig, _raise_exit)
    try:
        watch = Watch(d)
    except FileNotFoundError:
        print("wait: нет inotifywait: pacman -S inotify-tools", file=sys.stderr)
        return EXIT_USAGE
    mark = d / f"{args.name}{WAITING}{holder}"
    try:
        # Наблюдение ставится до проверки наличия файла: сигнал, пришедший
        # между проверкой и началом наблюдения, не теряется.
        try:
            first = watch.line(deadline)
            if first is not None and "Setting up watches." in first:
                first = watch.line(deadline)
            if first is None:
                return _timeout(args)
            if "Watches established." not in first:
                print(f"wait: наблюдение не установлено: {first}", file=sys.stderr)
                return EXIT_USAGE
            data = read_json(path)
            if data is None:
                write_atomic(mark, {"name": args.name, "holder": holder,
                                    "task": os.environ.get("DUTY_TASK", ""),
                                    "pid": os.getpid(), "since_ms": now_ms()})
                print(f"wait: {args.name}: ждёт сигнал, отметка {mark.name}", file=sys.stderr, flush=True)
            while data is None:
                line = watch.line(deadline)
                if line is None:
                    return _timeout(args)
                if line == path.name:
                    data = read_json(path)
        except EOFError:
            print(f"wait: наблюдение за {d} оборвалось без сигнала {args.name}", file=sys.stderr)
            return EXIT_USAGE
        show_signal(args.name, data)
        return 0
    finally:
        watch.close()
        try:
            mark.unlink()
        except FileNotFoundError:
            pass


def _timeout(args: argparse.Namespace) -> int:
    print(f"wait {args.name}: срок {args.timeout_s} с истёк, сигнала нет", file=sys.stderr)
    return EXIT_TIMEOUT


def collect() -> tuple[list[dict], list[dict]]:
    sigs: list[dict] = []
    waits: list[dict] = []
    d = signals_dir()
    if not d.is_dir():
        return sigs, waits
    now = now_ms()
    for p in sorted(d.iterdir()):
        if p.name.startswith("."):
            continue
        if p.name.endswith(".json") and valid_name(p.name[:-5]):
            data = read_json(p)
            if data is None:
                continue
            data["name"] = p.name[:-5]
            data["minutes"] = max(0, (now - int(data.get("at_ms") or 0)) // 60_000)
            sigs.append(data)
        elif WAITING in p.name:
            name, _, who = p.name.partition(WAITING)
            if not (valid_name(name) and valid_name(who)):
                continue
            data = read_json(p)
            if data is None:
                continue
            data["name"] = name
            data["holder"] = who
            data["alive"] = pid_alive(int(data.get("pid") or 0))
            data["minutes"] = max(0, (now - int(data.get("since_ms") or 0)) // 60_000)
            waits.append(data)
    return sigs, waits


def cell(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("|", "\\|")


def cmd_list(args: argparse.Namespace) -> int:
    sigs, waits = collect()
    if args.json:
        print(json.dumps({"signals": sigs, "waits": waits}, ensure_ascii=False, indent=2))
        return 0
    if not sigs and not waits:
        print("сигналов и ожиданий нет")
        return 0
    if sigs:
        print("| сигнал | от кого | текст | минут назад |")
        print("|---|---|---|---|")
        for s in sigs:
            print(f"| {s['name']} | {cell(s.get('from'))} | {cell(s.get('text'))} | {s['minutes']} |")
    if waits:
        if sigs:
            print()
        print("| ждёт сигнал | кто | задача | pid жив | минут |")
        print("|---|---|---|---|---|")
        for w in waits:
            print(f"| {w['name']} | {w['holder']} | {cell(w.get('task'))} "
                  f"| {'да' if w['alive'] else 'нет (устарело)'} | {w['minutes']} |")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    if not check_name("имя сигнала", args.name):
        return EXIT_USAGE
    try:
        signal_path(args.name).unlink()
        print(f"{args.name}: сигнал снят")
    except FileNotFoundError:
        print(f"{args.name}: сигнала нет")
    _, waits = collect()
    for w in waits:
        if w["name"] == args.name and not w["alive"]:
            try:
                (signals_dir() / f"{args.name}{WAITING}{w['holder']}").unlink()
                print(f"устаревшее ожидание {w['holder']} снято")
            except FileNotFoundError:
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("signal", help="дать сигнал")
    s.add_argument("name")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--from", dest="who", help="кто даёт; по умолчанию DUTY_NAME или DUTY_HOLDER")
    s.add_argument("--keep", action="store_true", help="существующий сигнал не перезаписывать")
    s.set_defaults(fn=cmd_signal)
    w = sub.add_parser("wait", help="ждать сигнал по событию")
    w.add_argument("name")
    w.add_argument("--timeout-s", type=int, help="предохранитель: срок ожидания; по умолчанию без срока")
    w.add_argument("--holder", help="кто ждёт; по умолчанию DUTY_NAME, DUTY_HOLDER или pid<pid>")
    w.set_defaults(fn=cmd_wait)
    ls = sub.add_parser("list", help="сигналы и ожидания")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    c = sub.add_parser("clear", help="снять сигнал и устаревшие ожидания")
    c.add_argument("name")
    c.set_defaults(fn=cmd_clear)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
