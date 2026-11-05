#!/usr/bin/env python3
"""Именованные сигналы между задачами: защёлка «событие произошло».

Сигнал — файл `<имя>.json` в каталоге сигналов. Дающий создаёт его
атомарно (временный файл с точкой в имени и `os.replace`), ожидающий
узнаёт о нём по событию inotify, без опроса. Пока `wait` ждёт, он держит
отметку `<имя>.waiting-<кто>`; `list` показывает сигналы и ожидания.
Требуется `inotifywait` (inotify-tools). Без сторонних библиотек.

Каталог сигналов — `paths.signals` конфигурации проекта (`.duty/duty.toml`,
перекрытие — `DUTY_SIGNALS_DIR`).

Команды: signal, wait, list, clear. Коды выхода: 0 — успех, 2 — неверное
обращение или сбой наблюдения, 124 — истёк --timeout-s.
"""

import argparse
import importlib.util
import json
import os
import re
import select
import signal as os_signal
import subprocess
import sys
import time
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модуль подключается по пути.
_spec = importlib.util.spec_from_file_location(
    "dutylib", Path(__file__).with_name("dutylib.py")
)
dutylib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dutylib)
duty_config = dutylib.duty_config


config_path = dutylib.config_path


# Точек в имени нет, поэтому суффикс «.waiting-» отметки ожидания однозначен;
# «_» допущен, чтобы имя задачи из dispatch.sh годилось как имя сигнала и ожидающего.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
NAME_MAX = 100
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
WAITING = ".waiting-"
# Отметка ожидания: <имя>.waiting-<кто>.<pid>; pid делает имя уникальным на процесс.
WAIT_REST_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)\.[0-9]+$")
# Закрытие inotifywait: замер на станции (32 ядра) — 0,010-0,015 с от terminate
# до выхода; на узле Jenkins в 4,5 раза медленнее на ядро: 0,07 с; с двойным
# запасом 0,14 с, округлено вверх до 1 с.
CLOSE_WAIT_S = 1
FATAL_SIGNALS = (os_signal.SIGTERM, os_signal.SIGINT, os_signal.SIGHUP)


def signals_dir() -> Path:
    return config_path("paths.signals")


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
    print(
        f"недопустимое {kind}: {name!r} (латиница в нижнем регистре, цифры, «-» и «_», "
        f"не с дефиса, не длиннее {NAME_MAX} знаков)",
        file=sys.stderr,
    )
    return False


def to_int(value: object) -> int | None:
    """Целое из поля файла; None — не число (строка, null, дробь, bool)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def fmt_utc(ms: object) -> str:
    n = to_int(ms)
    if n is None:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(n / 1000))
    except (OverflowError, ValueError, OSError):
        return "?"


def overdue_mark(minutes: int, ttl_min: int | None) -> str:
    """Пометка просрочки к строке перечня: «⚠ просрочено +N мин»; пусто — срок не истёк."""
    if ttl_min is None or minutes <= ttl_min:
        return ""
    return f"⚠ просрочено +{minutes - ttl_min} мин"


def write_atomic(path: Path, data: dict) -> None:
    # Временное имя начинается с точки: ни ожидающий, ни list его не читают.
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """Объект из файла; None — файла нет; метка corrupt — файл не разбирается как объект."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {
            "corrupt": True,
            "name": path.name.split(".")[0],
            "text": "повреждён",
            "from": "?",
            "task": "",
            "at_ms": 0,
            "pid": 0,
            "since_ms": 0,
            "holder": "?",
        }
    return data


def default_who(explicit: str | None) -> str:
    return (
        explicit or os.environ.get("DUTY_NAME") or os.environ.get("DUTY_HOLDER") or "?"
    )


def signal_path(name: str) -> Path:
    return signals_dir() / f"{name}.json"


def show_signal(name: str, data: dict) -> None:
    print(f"сигнал {name}: {data.get('text') or ''}")
    print(f"от {data.get('from') or '?'}, {fmt_utc(data.get('at_ms'))}")


def cmd_signal(args: argparse.Namespace) -> int:
    if not check_name("имя сигнала", args.name):
        return EXIT_USAGE
    d = signals_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = signal_path(args.name)
    data = {
        "name": args.name,
        "text": args.text,
        "from": default_who(args.who),
        "task": os.environ.get("DUTY_TASK", ""),
        "at_ms": now_ms(),
    }
    if not args.keep:
        write_atomic(path, data)
        print(f"сигнал {args.name} дан")
        return 0
    # os.link создаёт имя только если его нет: проверка и запись неделимы.
    tmp = d / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            old = read_json(path)
            if old is not None and not old.get("corrupt"):
                print(
                    f"сигнал {args.name} уже есть: {old.get('text') or ''} "
                    f"(от {old.get('from') or '?'}, {fmt_utc(old.get('at_ms'))})"
                )
                return 0
            print(
                f"предупреждение: файл сигнала {args.name} повреждён, перезаписан",
                file=sys.stderr,
            )
            os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    print(f"сигнал {args.name} дан")
    return 0


class Watch:
    """Построчное чтение вывода inotifywait с пределом по времени."""

    def __init__(self, directory: Path) -> None:
        # stderr объединён со stdout: «Watches established.» inotifywait пишет в stderr.
        # Сигнал смерти родителя снимает наблюдателя и при SIGKILL ожидающего.
        self.proc = subprocess.Popen(
            [
                "setpriv",
                "--pdeathsig",
                "TERM",
                "inotifywait",
                "-m",
                "-e",
                "moved_to",
                "-e",
                "create",
                "-e",
                "close_write",
                "--format",
                "%e %f",
                str(directory),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Родитель блокирует сигналы на время запуска; наблюдателю маска не нужна.
            preexec_fn=lambda: os_signal.pthread_sigmask(os_signal.SIG_SETMASK, []),  # noqa: PLW1509 — между fork и exec только сброс маски сигналов (pthread_sigmask), другого кода нет
        )
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
            self.proc.wait(timeout=CLOSE_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


def _raise_exit(signum: int, _frame: object) -> None:
    # SystemExit пробегает через finally и снимает отметку ожидания.
    raise SystemExit(128 + signum)


def corrupt_signal(name: str) -> int:
    print(
        f"wait: файл сигнала {name} повреждён: не объект JSON; снять — signals.py clear {name}",
        file=sys.stderr,
    )
    return EXIT_USAGE


def cmd_wait(args: argparse.Namespace) -> int:
    if not check_name("имя сигнала", args.name):
        return EXIT_USAGE
    holder = (
        args.holder
        or os.environ.get("DUTY_NAME")
        or os.environ.get("DUTY_HOLDER")
        or f"pid{os.getpid()}"
    )
    if not check_name("имя ожидающего", holder):
        return EXIT_USAGE
    d = signals_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = signal_path(args.name)
    # Уже данный сигнал отдаётся без наблюдения и без срока.
    data = read_json(path)
    if data is not None:
        if data.get("corrupt"):
            return corrupt_signal(args.name)
        show_signal(args.name, data)
        return 0
    for sig in FATAL_SIGNALS:
        os_signal.signal(sig, _raise_exit)
    # Сигнал во время запуска наблюдателя отложен: иначе процесс inotifywait
    # мог бы остаться без владельца.
    os_signal.pthread_sigmask(os_signal.SIG_BLOCK, FATAL_SIGNALS)
    mark = d / f"{args.name}{WAITING}{holder}.{os.getpid()}"
    watch: Watch | None = None
    created = False
    try:
        try:
            watch = Watch(d)
        except FileNotFoundError:
            print("wait: нет inotifywait: pacman -S inotify-tools", file=sys.stderr)
            return EXIT_USAGE
        os_signal.pthread_sigmask(os_signal.SIG_UNBLOCK, FATAL_SIGNALS)
        # Наблюдение ставится до повторной проверки файла: сигнал, пришедший
        # между проверкой и началом наблюдения, не теряется. Срок отсчитывается
        # после установки наблюдения, а не от запуска inotifywait.
        try:
            first = watch.line(None)
            if first is not None and "Setting up watches." in first:
                first = watch.line(None)
            if first is None or "Watches established." not in first:
                print(f"wait: наблюдение не установлено: {first}", file=sys.stderr)
                return EXIT_USAGE
            deadline = time.monotonic() + args.timeout_s
            data = read_json(path)
            if data is None:
                write_atomic(
                    mark,
                    {
                        "name": args.name,
                        "holder": holder,
                        "task": os.environ.get("DUTY_TASK", ""),
                        "pid": os.getpid(),
                        "since_ms": now_ms(),
                        "timeout_s": args.timeout_s,
                    },
                )
                created = True
                print(
                    f"wait: {args.name}: ждёт сигнал, отметка {mark.name}",
                    file=sys.stderr,
                    flush=True,
                )
            while data is None:
                line = watch.line(deadline)
                if line is None:
                    return _timeout(args, holder)
                event, _, fname = line.partition(" ")
                if fname != path.name:
                    continue
                data = read_json(path)
                # Файл, созданный пустым и ещё не дописанный (echo > x.json),
                # не завершает ожидание: дописанный придёт событием close_write.
                if data is not None and data.get("corrupt") and event == "CREATE":
                    data = None
        except EOFError:
            print(
                f"wait: наблюдение за {d} оборвалось без сигнала {args.name}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if data.get("corrupt"):
            return corrupt_signal(args.name)
        show_signal(args.name, data)
        return 0
    finally:
        # Повторный сигнал не должен прервать уборку.
        os_signal.pthread_sigmask(os_signal.SIG_BLOCK, FATAL_SIGNALS)
        if watch is not None:
            watch.close()
        if created:
            try:
                mark.unlink()
            except FileNotFoundError:
                pass


def _timeout(args: argparse.Namespace, holder: str) -> int:
    print(
        f"wait {args.name}: срок {args.timeout_s} с истёк, сигнала нет", file=sys.stderr
    )
    # Запись в state.md задачи: по ней агент и главная сессия видят просрочку
    # (agents/worker.md, п. 8); без DUTY_TASK или state.md записывать некуда.
    state = Path(os.environ.get("DUTY_TASK", "")) / "state.md"
    if os.environ.get("DUTY_TASK") and state.is_file():
        hhmm = time.strftime("%H:%M", time.gmtime())
        try:
            with state.open("a", encoding="utf-8") as f:
                f.write(
                    f"\nПросрочено: ожидание сигнала {args.name} ({holder}), срок "
                    f"{max(1, -(-args.timeout_s // 60))} мин истёк в {hhmm} UTC; сигнала нет\n"
                )
        except OSError as e:
            print(f"wait: запись в {state} не удалась: {e}", file=sys.stderr)
    return EXIT_TIMEOUT


def collect() -> tuple[list[dict], list[dict]]:
    sigs: list[dict] = []
    waits: list[dict] = []
    d = signals_dir()
    if not d.is_dir():
        return sigs, waits
    now = now_ms()
    names = set()
    for p in sorted(d.iterdir()):
        if p.name.startswith("."):
            continue
        if p.name.endswith(".json") and valid_name(p.name[:-5]):
            data = read_json(p)
            if data is None:
                continue
            data["name"] = p.name[:-5]
            at = to_int(data.get("at_ms"))
            if at is None:
                data["corrupt"] = True
                data["text"] = "повреждён"
            data["minutes"] = 0 if at is None else max(0, (now - at) // 60_000)
            sigs.append(data)
            names.add(data["name"])
        elif WAITING in p.name:
            name, _, rest = p.name.partition(WAITING)
            m = WAIT_REST_RE.match(rest)
            if not (valid_name(name) and m and valid_name(m.group(1))):
                continue
            data = read_json(p)
            if data is None:
                continue
            data["name"] = name
            data["holder"] = data.get("holder") or m.group(1)
            data["file"] = p.name
            pid = to_int(data.get("pid"))
            since = to_int(data.get("since_ms"))
            if pid is None or since is None:
                data["corrupt"] = True
                data["task"] = "повреждён"
            data["alive"] = pid_alive(pid or 0)
            data["minutes"] = 0 if since is None else max(0, (now - since) // 60_000)
            t_s = to_int(data.get("timeout_s"))
            data["ttl_min"] = None if t_s is None else max(1, -(-t_s // 60))
            data["overdue"] = overdue_mark(data["minutes"], data["ttl_min"])
            waits.append(data)
    for w in waits:
        w["given"] = w["name"] in names
    return sigs, waits


def cell(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("|", "\\|")


def print_waits(waits: list[dict]) -> None:
    print("| ждёт сигнал | кто | задача | pid жив | минут | срок, мин | сигнал дан |")
    print("|---|---|---|---|---|---|---|")
    for w in waits:
        print(
            f"| {w['name']} | {w['holder']} | {cell(w.get('task'))} "
            f"| {'да' if w['alive'] else 'нет (устарело)'} | {w['minutes']} "
            f"| {'—' if w['ttl_min'] is None else w['ttl_min']} {w['overdue']} "
            f"| {'да' if w['given'] else 'нет'} |"
        )


def cmd_list(args: argparse.Namespace) -> int:
    sigs, waits = collect()
    if args.waits:
        sigs = []
    if args.json:
        out = {"waits": waits} if args.waits else {"signals": sigs, "waits": waits}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not sigs and not waits:
        print("ожиданий нет" if args.waits else "сигналов и ожиданий нет")
        return 0
    if sigs:
        print("| сигнал | от кого | текст | минут назад |")
        print("|---|---|---|---|")
        for s in sigs:
            print(
                f"| {s['name']} | {cell(s.get('from'))} | {cell(s.get('text'))} | {s['minutes']} |"
            )
    if waits:
        if sigs:
            print()
        print_waits(waits)
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
                (signals_dir() / w["file"]).unlink()
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
    s.add_argument(
        "--from", dest="who", help="кто даёт; по умолчанию DUTY_NAME или DUTY_HOLDER"
    )
    s.add_argument(
        "--keep", action="store_true", help="существующий сигнал не перезаписывать"
    )
    s.set_defaults(fn=cmd_signal)
    w = sub.add_parser("wait", help="ждать сигнал по событию")
    w.add_argument("name")
    w.add_argument(
        "--timeout-s",
        type=int,
        required=True,
        help="срок ожидания, с (обязателен): 2× от замера или оценки; по истечении код 124 и запись в state.md",
    )
    w.add_argument(
        "--holder", help="кто ждёт; по умолчанию DUTY_NAME, DUTY_HOLDER или pid<pid>"
    )
    w.set_defaults(fn=cmd_wait)
    ls = sub.add_parser("list", help="сигналы и ожидания")
    ls.add_argument("--json", action="store_true")
    ls.add_argument(
        "--waits", action="store_true", help="только ожидания, с отметкой «сигнал дан»"
    )
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
