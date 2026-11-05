#!/usr/bin/env python3
"""Вопросы агентов задач главной сессии: файл вопроса и сигнал.

Агент workspace не спрашивает пользователя сам: вопрос пишется файлом
`<каталог задачи>/questions/<NN>-<slug>.md` с шапкой (символ workspace,
задача, модель, цель, шаг, что зависит от ответа, необратимо ли,
рекомендуемый вариант) и даётся сигнал `question-<задача>-<NN>`. Главная
сессия отвечает командой `answer`: ответ дописывается в раздел `## Ответ`
того же файла, даётся сигнал `answer-<задача>-<NN>`. Обратимую работу агент
продолжает по рекомендуемому варианту; необратимое ждёт ответа `wait`.

Команды: new, answer, list, wait. Коды выхода: 0 — успех, 2 — неверное
обращение, у `wait` — коды `signals.py wait`. Без сторонних зависимостей.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модуль подключается по пути.
_spec = importlib.util.spec_from_file_location(
    "duty_config", Path(__file__).with_name("duty_config.py")
)
duty_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(duty_config)


def config_path(key: str) -> Path:
    """Путь `[paths]` из конфигурации проекта (с перекрытием переменными окружения)."""
    try:
        path = duty_config.load().path(key)
    except duty_config.ConfigError as error:
        raise SystemExit(f"ошибка конфигурации: {error}")
    assert path is not None
    return path


SIGNALS = Path(__file__).resolve().parent / "signals.py"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
FILE_RE = re.compile(r"^(\d{2})-([a-z0-9-]+)\.md$")
FIELD_RE = re.compile(r"^- ([^:]+): (.*)$")
ANSWER = "## Ответ"
EXIT_USAGE = 2

FIELDS = {
    "symbol": "символ",
    "task": "задача",
    "model": "модель",
    "goal": "цель задачи",
    "step": "шаг",
    "depends": "от ответа зависит",
    "irreversible": "необратимо",
    "recommended": "рекомендуемый вариант",
    "created": "создан",
}


def results_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(os.path.expanduser(explicit))
    return config_path("paths.results")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def fmt_utc(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def fail(text: str) -> int:
    print(f"ask.py: {text}", file=sys.stderr)
    return EXIT_USAGE


def service_value(brief: str, key: str) -> str:
    """Значение из служебного блока brief.md; последний блок главнее (продолжение задачи)."""
    found = re.findall(rf"^- {re.escape(key)}: `([^`]*)`", brief, re.MULTILINE)
    return found[-1] if found else ""


def brief_goal(brief: str) -> str:
    """Первая непустая строка раздела «Цель» записки."""
    lines = brief.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Цель":
            for rest in lines[i + 1 :]:
                if rest.startswith("## "):
                    break
                if rest.strip():
                    return rest.strip()
    return "—"


def signal(name: str, text: str) -> bool:
    done = subprocess.run(
        [sys.executable, "-I", str(SIGNALS), "signal", name, text],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print(f"ask.py: сигнал {name} не дан: {done.stderr.strip()}", file=sys.stderr)
    return done.returncode == 0


def parse(path: Path) -> dict:
    """Поля шапки, варианты и ответ файла вопроса."""
    text = path.read_text(encoding="utf-8")
    head, _, answer = text.partition(ANSWER)
    m = FILE_RE.match(path.name)
    data: dict = {
        "nn": m.group(1) if m else "?",
        "file": str(path),
        "title": "",
        "options": [],
    }
    reverse = {v: k for k, v in FIELDS.items()}
    section = ""
    for line in head.splitlines():
        if line.startswith("# "):
            data["title"] = line.split(":", 1)[-1].strip()
        elif line.startswith("## "):
            section = line[3:].strip()
        elif section == "Варианты" and re.match(r"^\d+\. ", line):
            data["options"].append(line.split(". ", 1)[1])
        elif not section and (f := FIELD_RE.match(line)) and f.group(1) in reverse:
            data[reverse[f.group(1)]] = f.group(2).strip()
    created = re.search(r"created_ms=(\d+)", data.get("created", ""))
    data["created_ms"] = int(created.group(1)) if created else 0
    data["answer"] = answer.strip()
    data["answered"] = bool(data["answer"])
    return data


def cmd_new(args: argparse.Namespace) -> int:
    task_dir = os.environ.get("DUTY_TASK")
    if not task_dir or not Path(task_dir).is_dir():
        return fail(
            "DUTY_TASK не задан или каталога нет: ask.py new вызывается из задачи"
        )
    if not SLUG_RE.match(args.slug):
        return fail(
            f"slug {args.slug!r}: строчная латиница, цифры и дефис, до 40 знаков"
        )
    if len(args.option) < 2:
        return fail("нужно не меньше двух --option")
    if not 1 <= args.recommended <= len(args.option):
        return fail(
            f"--recommended {args.recommended}: номер одного из {len(args.option)} вариантов"
        )
    task = Path(task_dir)
    brief = (
        (task / "brief.md").read_text(encoding="utf-8")
        if (task / "brief.md").is_file()
        else ""
    )
    name = os.environ.get("DUTY_NAME") or task.name
    symbol = (
        os.environ.get("DUTY_SYMBOL") or service_value(brief, "символ workspace") or "—"
    )
    models = re.findall(r"модель `([^`]*)`", brief)
    model = os.environ.get("DUTY_MODEL") or (models[-1] if models else "?")
    created = now_ms()
    questions = task / "questions"
    questions.mkdir(exist_ok=True)
    taken = [
        int(m.group(1)) for p in questions.iterdir() if (m := FILE_RE.match(p.name))
    ]
    number = max(taken, default=0) + 1
    while True:
        nn = f"{number:02d}"
        path = questions / f"{nn}-{args.slug}.md"
        options = "\n".join(f"{i}. {o}" for i, o in enumerate(args.option, start=1))
        body = (
            f"# Вопрос {nn}: {args.title}\n\n"
            f"- {FIELDS['symbol']}: {symbol}\n"
            f"- {FIELDS['task']}: {name}\n"
            f"- {FIELDS['model']}: {model}\n"
            f"- {FIELDS['goal']}: {brief_goal(brief)}\n"
            f"- {FIELDS['step']}: {args.step}\n"
            f"- {FIELDS['depends']}: {args.depends}\n"
            f"- {FIELDS['irreversible']}: {'да' if args.irreversible else 'нет'}\n"
            f"- {FIELDS['recommended']}: {args.recommended}\n"
            f"- {FIELDS['created']}: {fmt_utc(created)} (`created_ms={created}`)\n\n"
            f"## Варианты\n\n{options}\n\n{ANSWER}\n"
        )
        # Создание с O_EXCL: два агента одной задачи не займут один номер.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            number += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        break
    # Сигналы прежнего вопроса с тем же номером (каталог задачи пересоздан
    # или файл удалён) — защёлки: без снятия ask.py wait отпустился бы сразу.
    for kind in ("question", "answer"):
        subprocess.run(
            [sys.executable, "-I", str(SIGNALS), "clear", f"{kind}-{name}-{nn}"],
            capture_output=True,
            check=False,
        )
    signal(f"question-{name}-{nn}", args.title)
    print(f"вопрос {nn}: {path}")
    print(
        f"ответ ждать: ask.py wait {nn} --timeout-s <секунды>"
        if args.irreversible
        else "продолжать по рекомендуемому варианту с пометкой в state.md"
    )
    return 0


def find_question(task: str, nn: str, results: Path) -> Path | None:
    found = sorted((results / task / "questions").glob(f"{nn}-*.md"))
    return found[0] if found else None


def cmd_answer(args: argparse.Namespace) -> int:
    nn = f"{int(args.nn):02d}" if args.nn.isdigit() else args.nn
    path = find_question(args.task, nn, results_dir(args.results))
    if path is None:
        return fail(f"вопроса {args.task} {nn} нет")
    if parse(path)["answered"]:
        return fail(f"на вопрос {args.task} {nn} уже ответили: {path}")
    stamp = now_ms()
    ref = args.ref or "ответ пользователя"
    with path.open("a", encoding="utf-8") as f:
        f.write(
            f"\n{args.text.strip()}\n\n- ссылка: {ref}\n"
            f"- время: {fmt_utc(stamp)} (`answered_ms={stamp}`)\n"
        )
    print(f"ответ записан: {path}")
    if not signal(f"answer-{args.task}-{nn}", args.text.strip()[:200]):
        return fail(
            f"ответ записан, но агент не разбужен: повторить сигнал answer-{args.task}-{nn}"
        )
    return 0


def collect(results: Path) -> list[dict]:
    rows = []
    for path in sorted(results.glob("*/questions/*.md")):
        if not FILE_RE.match(path.name):
            continue
        data = parse(path)
        data.setdefault("task", path.parent.parent.name)
        rows.append(data)
    rows.sort(key=lambda r: (r["created_ms"], r["task"], r["nn"]))
    return rows


def cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ").strip() or "—"


def cmd_list(args: argparse.Namespace) -> int:
    rows = collect(results_dir(args.results))
    if args.open:
        rows = [r for r in rows if not r["answered"]]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("вопросов нет")
        return 0
    print(
        "| символ | задача | NN | суть | необратимо | рекомендуемый | отвечен | файл |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        rec = r.get("recommended", "?")
        opts = r["options"]
        if rec.isdigit() and 1 <= int(rec) <= len(opts):
            rec = f"{rec}. {opts[int(rec) - 1]}"
        print(
            f"| {cell(r.get('symbol', '—'))} | {cell(r['task'])} | {r['nn']} | {cell(r['title'])} "
            f"| {cell(r.get('irreversible', '?'))} | {cell(rec)} | {'да' if r['answered'] else 'нет'} "
            f"| `{r['file']}` |"
        )
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    name = os.environ.get("DUTY_NAME")
    task_dir = os.environ.get("DUTY_TASK")
    if not name or not task_dir:
        return fail("DUTY_NAME и DUTY_TASK не заданы: ask.py wait вызывается из задачи")
    nn = f"{int(args.nn):02d}" if args.nn.isdigit() else args.nn
    path = find_question(name, nn, Path(task_dir).parent)
    if path is None:
        return fail(f"вопроса {nn} в {task_dir}/questions нет")
    argv = [
        sys.executable,
        "-I",
        str(SIGNALS),
        "wait",
        f"answer-{name}-{nn}",
        "--holder",
        name,
    ]
    argv += ["--timeout-s", str(args.timeout_s)]
    code = subprocess.run(argv, check=False).returncode
    if code != 0:
        return code
    answer = parse(path)["answer"]
    if not answer:
        return fail(f"сигнал ответа на вопрос {nn} есть, а раздел ответа в {path} пуст")
    print(answer)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--results",
        help="каталог результатов; по умолчанию paths.results конфигурации проекта",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser(
        "new", help="задать вопрос: файл в $DUTY_TASK/questions и сигнал"
    )
    n.add_argument("slug")
    n.add_argument("--title", required=True, help="суть вопроса одной строкой")
    n.add_argument("--step", required=True, help="шаг задачи")
    n.add_argument("--depends", required=True, help="что зависит от ответа")
    n.add_argument(
        "--option",
        action="append",
        default=[],
        help="«вариант — последствие»; не меньше двух",
    )
    n.add_argument(
        "--recommended", type=int, required=True, help="номер рекомендуемого варианта"
    )
    n.add_argument(
        "--irreversible", action="store_true", help="шаг необратим: агент ждёт ответа"
    )
    a = sub.add_parser("answer", help="ответить: раздел «Ответ» и сигнал")
    a.add_argument("task")
    a.add_argument("nn")
    a.add_argument("text")
    a.add_argument(
        "--ref", help="запись решения, спецификация или «ответ пользователя»"
    )
    ls = sub.add_parser("list", help="очередь вопросов всех задач")
    ls.add_argument("--open", action="store_true", help="только без ответа")
    ls.add_argument("--json", action="store_true")
    w = sub.add_parser("wait", help="ждать ответа на свой вопрос по сигналу")
    w.add_argument("nn")
    w.add_argument(
        "--timeout-s",
        type=int,
        required=True,
        help="срок ожидания ответа, с (обязателен): оценка главной сессии, не меньше 2× ожидаемого",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "new": cmd_new,
        "answer": cmd_answer,
        "list": cmd_list,
        "wait": cmd_wait,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
