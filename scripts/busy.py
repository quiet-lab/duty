#!/usr/bin/env python3
"""Реестр занятости общих ресурсов: отметки-файлы с pid держателя.

Один ресурс — один файл `<ресурс>.json` в каталоге реестра. Файл создаётся
атомарно (`O_CREAT|O_EXCL`) и снимается удалением. Отметки советующие:
они не мешают тому, кто их не читает. Без сторонних зависимостей.

Команды: claim, release, list, check. Коды выхода: 0 — успех или свободно,
3 — ресурс занят, 2 — неверное обращение.
"""

import argparse
import errno
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_DIR = "~/work/telecom/axata/ocp-dev-stand/busy"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
EXIT_BUSY = 3
EXIT_USAGE = 2


def registry_dir() -> Path:
    return Path(os.environ.get("DUTY_BUSY_DIR") or os.path.expanduser(DEFAULT_DIR))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но чужой пользователь: считается живым.
        return True
    return True


def mark_path(resource: str) -> Path:
    if not NAME_RE.match(resource):
        raise SystemExit(f"недопустимое имя ресурса: {resource!r}")
    return registry_dir() / f"{resource}.json"


def read_mark(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Повреждённый файл не принадлежит живому держателю.
        return {"resource": path.stem, "holder": "?", "pid": 0, "since_ms": 0, "note": "файл повреждён"}
    return data if isinstance(data, dict) else None


def age_minutes(mark: dict) -> int:
    return max(0, (now_ms() - int(mark.get("since_ms") or 0)) // 60_000)


def describe(mark: dict) -> str:
    since = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(mark.get("since_ms") or 0) / 1000))
    return f"занято: {mark.get('holder')}, {mark.get('note') or 'без заметки'}, с {since}"


def try_create(path: Path, mark: dict) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(mark, f, ensure_ascii=False)
    return True


def cmd_claim(args: argparse.Namespace) -> int:
    # Родитель самого сценария — короткая оболочка Bash вызова: отметка
    # с его pid устарела бы сразу после выхода, поэтому pid задаётся явно.
    if args.pid is None:
        print("claim: нужен --pid долгоживущего держателя; из Bash сессии Claude — "
              '--pid "$PPID"', file=sys.stderr)
        return EXIT_USAGE
    if args.pid <= 0 or not pid_alive(args.pid):
        print(f"claim: процесс --pid {args.pid} не жив, отметка сразу устарела бы", file=sys.stderr)
        return EXIT_USAGE
    path = mark_path(args.resource)
    path.parent.mkdir(parents=True, exist_ok=True)
    mark = {
        "resource": args.resource,
        "holder": args.holder,
        "task": args.task or "",
        "pid": args.pid,
        "since_ms": now_ms(),
        "note": args.note or "",
    }
    for _ in range(3):
        if try_create(path, mark):
            print(f"занято ресурс {args.resource} держателем {args.holder}")
            return 0
        old = read_mark(path)
        if old is None:
            continue  # файл успели снять между попытками
        if pid_alive(int(old.get("pid") or 0)):
            print(describe(old))
            return EXIT_BUSY
        print(f"снята устаревшая отметка {args.resource}: держатель {old.get('holder')}, pid {old.get('pid')} не жив")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print(f"не удалось занять {args.resource}", file=sys.stderr)
    return EXIT_BUSY


def cmd_release(args: argparse.Namespace) -> int:
    path = mark_path(args.resource)
    mark = read_mark(path)
    if mark is None:
        print(f"{args.resource}: отметки нет")
        return 0
    if args.holder and mark.get("holder") != args.holder:
        if not args.force:
            print(f"отметка {args.resource} принадлежит {mark.get('holder')}, а не {args.holder}; "
                  "снять чужую можно с --force", file=sys.stderr)
            return EXIT_BUSY
        print(f"предупреждение: снята чужая отметка {args.resource} держателя {mark.get('holder')}")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    print(f"снято {args.resource}")
    return 0


def all_marks() -> list[dict]:
    d = registry_dir()
    marks = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        m = read_mark(p)
        if m is not None:
            m["resource"] = p.stem
            m["alive"] = pid_alive(int(m.get("pid") or 0))
            m["minutes"] = age_minutes(m)
            marks.append(m)
    return marks


def cmd_list(args: argparse.Namespace) -> int:
    marks = all_marks()
    if args.json:
        print(json.dumps(marks, ensure_ascii=False, indent=2))
        return 0
    if not marks:
        print("отметок нет")
        return 0
    print("| ресурс | держатель | задача | заметка | pid жив | минут |")
    print("|---|---|---|---|---|---|")
    for m in marks:
        print(f"| {m['resource']} | {m.get('holder')} | {m.get('task') or ''} | {m.get('note') or ''} "
              f"| {'да' if m['alive'] else 'нет (устарела)'} | {m['minutes']} |")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    mark = read_mark(mark_path(args.resource))
    if mark is None:
        print(f"свободно: {args.resource}")
        return 0
    if not pid_alive(int(mark.get("pid") or 0)):
        print(f"свободно: {args.resource} (отметка держателя {mark.get('holder')} устарела)")
        return 0
    print(describe(mark))
    return EXIT_BUSY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim", help="занять ресурс")
    c.add_argument("resource")
    c.add_argument("--holder", required=True)
    c.add_argument("--task")
    c.add_argument("--note")
    c.add_argument("--pid", type=int,
                   help='обязателен: процесс держателя; из Bash сессии Claude — --pid "$PPID"')
    c.set_defaults(fn=cmd_claim)
    r = sub.add_parser("release", help="снять свою отметку")
    r.add_argument("resource")
    r.add_argument("--holder")
    r.add_argument("--force", action="store_true", help="снять чужую отметку")
    r.set_defaults(fn=cmd_release)
    ls = sub.add_parser("list", help="перечень отметок")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    k = sub.add_parser("check", help="0 — свободно, 3 — занято")
    k.add_argument("resource")
    k.set_defaults(fn=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
