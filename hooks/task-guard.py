#!/usr/bin/env python3
"""Хуки плагина duty: ворота постановки, сторожа сессии агента задачи.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Ворота действуют во всех сессиях с плагином: `pre-tool` для `Agent`/`Task`
отклоняет приглашение без строк «Критерий готовности», «Условие остановки»,
«Приоритеты» и «Справка»; `AskUserQuestion` — вопрос без фразы «По документам:» (проверка —
`scripts/brief_check.py`); приглашение пропускается для типов только для чтения,
`duty:runner` и вызовов из навыка (по транскрипту сессии). Остальное ниже — только
в сессии агента задачи (`DUTY_ROLE=worker`).

Причина: субагент ставил наблюдение и заканчивал ход, уведомление к нему
не приходило, а координатор считал его работающим. Поэтому:

- `pre-tool` отвергает `Monitor` и `Bash` с `run_in_background` в субагенте
  (поле `agent_id` есть только там) и снимает отметки подзадачи при её
  перезапуске (`Agent`: `SubagentStop` субагента на переднем плане пишет
  `.stopped` раньше `PostToolUse`, поэтому снятие — до вызова);
- `pre-tool` при запуске подзадачи пишет отметку `subtasks/<имя>.started`;
  `.started` без `.done` — запущена и ещё без итога (`launched_without_result`),
  `Stop` называет такие подзадачи в причине блокировки, а `SubagentStop`
  помечает `registered: false` в `.stopped`, если запуска не видел;
- `post-tool` снимает отметку остановки после успешного `SendMessage`
  (возобновление): неудачная отправка отметку не теряет;
- `subagent-stop` по метке `Подзадача: <имя>` в приглашении субагента проверяет
  отметку итога `subtasks/<имя>.done`; её нет — пишет `subtasks/<имя>.stopped`
  и шлёт координатору строку «возобнови или перезапусти»;
- `stop` блокирует конец хода координатора, пока есть остановившаяся подзадача
  без итога, для которой нет ожидания, начатого после её остановки: прогона
  во вкладке без `done` (`started_ms` в `meta.txt`), ожидания `signals.py wait`
  (`since_ms`). Ожидание, начатое раньше, к остановке подзадачи отношения
  не имеет и ход не отпускает. Повторная
  остановка (`stop_hook_active`) пропускается с уведомлением herdr: условие
  может не разрешиться, а цикл блокировок расходует ход за ходом.

Вне сессии агента задачи (`DUTY_ROLE` не `worker`) сторожа молчат. Сбой хука не должен
мешать сессии: любая ошибка идёт в stderr с кодом 0.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN / "scripts"
TAG_RE = re.compile(r"^Подзадача: ([a-z0-9][a-z0-9_.-]{0,63})\s*$", re.MULTILINE)
TAG_LINE_RE = re.compile(r"^Подзадача:", re.MULTILINE)
SUBAGENT_TOOLS = {"Agent", "Task"}
# Эти субагенты только читают и отвечают сразу: итог виден в их ответе.
READ_ONLY_TYPES = {"Explore", "Plan", "claude-code-guide", "duty:inspector"}
# Рутина навыков /duty:* идёт на runner: под ворота постановки не попадает.
GATE_FREE_TYPES = READ_ONLY_TYPES | {"duty:runner"}
MESSAGE_LIMIT = 200
# Хвост транскрипта сессии для распознавания запуска из навыка. Ход со всеми
# вызовами и выводами инструментов обычно короче; больший ход читается
# частично и считается «не из навыка»: отказ ворот безопаснее пропуска.
# Чтение хвоста 2 МБ — единицы миллисекунд, предел хука 10 с.
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
# Подпроцесс signals.py: 0,1 с на станции, на узлах в 4,5 раза дольше,
# с двойным запасом 0,9 с; предел 5 с взят с округлением вверх на запуск
# интерпретатора; у herdr — ответ сервера, тот же порядок.
SUBPROCESS_LIMIT_S = 5

DENY_WAIT = (
    "Субагент не ждёт в фоне: после конца хода уведомление к нему не придёт, "
    "и подзадача встанет. Жди внутри хода: `bash <плагин>/scripts/wait-done.sh "
    "<каталог прогона> --timeout-s 590` с таймаутом Bash 600000 мс, вывод timeout — "
    "запустить снова. "
    "Долгий прогон — забота координатора: верни ему итог и напиши, что запустить."
)
UNTAGGED = (
    "Подзадача без строки «Подзадача: <имя>» в приглашении: её остановка без итога "
    "не отслеживается (agents/worker.md, п. 3)."
)
BAD_TAG = (
    "Строка «Подзадача:» в приглашении есть, но имя не подходит: допустима форма "
    "[a-z0-9][a-z0-9_.-]* (до 64 знаков, строчные латинские буквы), например "
    "«Подзадача: fx-duty». Остановка без итога не отслеживается "
    "(agents/worker.md, п. 3)."
)


GATE_HINT = (
    "Задача субагенту ставится с критерием готовности, условием остановки, "
    "приоритетами и справкой. В приглашении не хватает: {missing}. Добавьте строки "
    "(или заголовки с текстом под ними): "
    "«Критерий готовности: <команда и ожидаемый результат>»; "
    "«Условие остановки: <сигнал, при котором работа прекращается и исполнитель "
    "отчитывается>»; "
    "«Приоритеты: <порядок предпочтений на развилках>» или "
    "«Приоритеты: развилок нет — <причина>»; "
    "«Справка: <путь к итогу /duty:recall в <paths.results>/recall/>» или "
    "«Справка: не нужна — <причина>». Форма — "
    "reference/brief-template.md плагина duty, раздел «Раздел для субагентов»."
)
ASK_TOOL = "AskUserQuestion"
ASK_HINT = (
    "Вопрос пользователю задаётся после справки по документам: сначала "
    "/duty:recall <тема>; в вопрос — строку «По документам: <что найдено или "
    "записей нет>». Нет строки в вопросах: {missing}."
)


def last_turn_skill(transcript: object) -> str | None:
    """Имя навыка, из которого идёт текущий ход; None — ход начат не навыком.

    Ход начинается последним настоящим сообщением пользователя: запись
    `user` без `tool_result` и без `isMeta` (развёрнутый текст навыка Claude Code
    пишет как `isMeta`, он ходом не считается). Навык — slash-команда
    (`<command-name>` в сообщении) либо вызов инструмента `Skill` после него.
    Любая ошибка чтения — None.
    """
    try:
        path = Path(str(transcript))
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            raw = stream.read()
    except (OSError, ValueError):
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    skill: str | None = None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # обрезанная первая строка хвоста и посторонние записи
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if entry.get("type") == "assistant":
            for block in blocks:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Skill"
                    and skill is None
                ):
                    raw_input = block.get("input")
                    name = (
                        raw_input.get("skill") if isinstance(raw_input, dict) else None
                    )
                    skill = str(name or "Skill")
            continue
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
            continue
        text = (
            content
            if isinstance(content, str)
            else "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))
        )
        found = COMMAND_RE.search(text)
        return found.group(1) if found else skill
    return None


def project_root() -> Path | None:
    """Корень проекта по конфигурации плагина; None — `brief_check` берёт cwd."""
    sys.path.insert(0, str(SCRIPTS))
    import duty_config

    return duty_config.find_root()


def gate_question(data: dict[str, object]) -> bool:
    """Ворота вопроса пользователю: True — вопрос отклонён."""
    raw_input = data.get("tool_input")
    questions = raw_input.get("questions") if isinstance(raw_input, dict) else None
    sys.path.insert(0, str(SCRIPTS))
    import brief_check

    bad = [
        str(index)
        for index, item in enumerate(
            questions if isinstance(questions, list) else [], 1
        )
        if not brief_check.check_question(
            str(item.get("question", "")) if isinstance(item, dict) else ""
        )
    ]
    if not bad:
        return False
    pre_tool_output(
        permissionDecision="deny",
        permissionDecisionReason=ASK_HINT.format(missing=", ".join(bad)),
    )
    return True


def gate(data: dict[str, object]) -> bool:
    """Ворота постановки: True — поручение отклонено."""
    raw_input = data.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if tool_input.get("subagent_type") in GATE_FREE_TYPES:
        return False
    sys.path.insert(0, str(SCRIPTS))
    import brief_check

    missing = brief_check.check_prompt(
        str(tool_input.get("prompt", "")), root=project_root(), fresh=False
    )
    if not missing:
        return False
    if last_turn_skill(data.get("transcript_path")) is not None:
        # Субагентов навыков (/code-review, /simplify, deep-research) ставит
        # текст навыка, а не модель: строк в нём нет и править его нельзя.
        # Сообщения не печатаем: в сессии агента задачи следом работает
        # pre_tool, и второй JSON в stdout сломал бы разбор ответа хука.
        return False
    pre_tool_output(
        permissionDecision="deny",
        permissionDecisionReason=GATE_HINT.format(missing=", ".join(missing)),
    )
    return True


def task_dir() -> Path | None:
    raw = os.environ.get("DUTY_TASK")
    return Path(raw) if raw else None


def subtasks(task: Path) -> Path:
    return task / "subtasks"


def one_line(text: object, limit: int = MESSAGE_LIMIT) -> str:
    return " ".join(str(text or "").split())[:limit]


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def pre_tool_output(**fields: str) -> None:
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", **fields}})


def forget_stopped(task: Path, target: str) -> None:
    """Возобновлённая подзадача снова работает: отметка остановки снимается."""
    directory = subtasks(task)
    if not directory.is_dir():
        return
    for path in directory.glob("*.stopped"):
        record = read_record(path)
        if target in (path.stem, record.get("agent_id")):
            path.unlink(missing_ok=True)


def read_record(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pre_tool(data: dict[str, object]) -> None:
    tool = data.get("tool_name")
    raw_input = data.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if data.get("agent_id"):
        if tool == "Monitor" or (
            tool == "Bash" and tool_input.get("run_in_background")
        ):
            pre_tool_output(
                permissionDecision="deny", permissionDecisionReason=DENY_WAIT
            )
        return
    task = task_dir()
    if task is None:
        return
    if tool in SUBAGENT_TOOLS:
        tag = TAG_RE.search(str(tool_input.get("prompt", "")))
        if tag:
            # Перезапуск под тем же именем: прежние отметки к нему не относятся.
            for suffix in (".stopped", ".done"):
                (subtasks(task) / f"{tag.group(1)}{suffix}").unlink(missing_ok=True)
            mark_started(task, tag.group(1))
        elif tool_input.get("subagent_type") not in READ_ONLY_TYPES:
            has_line = TAG_LINE_RE.search(str(tool_input.get("prompt", "")))
            pre_tool_output(additionalContext=BAD_TAG if has_line else UNTAGGED)


def mark_started(task: Path, name: str) -> None:
    """Запуск подзадачи регистрируется отметкой `<имя>.started` со временем (мс)."""
    directory = subtasks(task)
    directory.mkdir(parents=True, exist_ok=True)
    record = {"at_ms": time.time_ns() // 1_000_000}
    (directory / f"{name}.started").write_text(json.dumps(record), encoding="utf-8")


def launched_without_result(task: Path) -> list[str]:
    """Запущенные подзадачи без итога `.done` (сверка `.started` с `.done`)."""
    directory = subtasks(task)
    if not directory.is_dir():
        return []
    return [
        path.stem
        for path in sorted(directory.glob("*.started"))
        if not (directory / f"{path.stem}.done").exists()
    ]


def post_tool(data: dict[str, object]) -> None:
    """Успешный `SendMessage` возобновил подзадачу: отметка остановки снимается."""
    if data.get("agent_id") or data.get("tool_name") != "SendMessage":
        return
    task = task_dir()
    if task is None:
        return
    response = data.get("tool_response")
    # Ответ SendMessage — объект с полем success; ложное значение — отправка
    # не удалась, подзадача стоит, отметка остаётся.
    if not isinstance(response, dict) or response.get("success") is not True:
        return
    raw_input = data.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    forget_stopped(task, str(tool_input.get("to", "")))


def first_prompt(transcript: object) -> str:
    """Приглашение субагента — первая запись его транскрипта."""
    if not transcript:
        return ""
    try:
        with Path(str(transcript)).expanduser().open(encoding="utf-8") as stream:
            content = (
                json.loads(stream.readline()).get("message", {}).get("content", "")
            )
    except (OSError, ValueError, AttributeError):
        return ""
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content)


def push(task: Path, text: str) -> None:
    subtasks(task).mkdir(parents=True, exist_ok=True)
    with (subtasks(task) / "push.log").open("a", encoding="utf-8") as log:
        subprocess.run(
            ["bash", str(SCRIPTS / "push-agent.sh"), str(task), text, "--no-wait"],
            stdout=log,
            stderr=log,
            timeout=SUBPROCESS_LIMIT_S,
            check=False,
        )


def subagent_stop(data: dict[str, object]) -> None:
    task = task_dir()
    if task is None:
        return
    tag = TAG_RE.search(first_prompt(data.get("agent_transcript_path")))
    if not tag:
        return
    name = tag.group(1)
    directory = subtasks(task)
    if (directory / f"{name}.done").exists():
        (directory / f"{name}.stopped").unlink(missing_ok=True)
        return
    agent_id = str(data.get("agent_id", ""))
    last = one_line(data.get("last_assistant_message"))
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "agent_id": agent_id,
        "last": last,
        "at_ms": time.time_ns() // 1_000_000,
        # Остановка подзадачи, запуск которой хук не видел (нет `.started`).
        "registered": (directory / f"{name}.started").exists(),
    }
    (directory / f"{name}.stopped").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    push(
        task,
        f"субагент {name} остановился без итога: возобнови (SendMessage to={agent_id}) "
        f"или перезапусти; его последние слова: {last}",
    )


def stopped_without_result(task: Path) -> list[tuple[str, str, int]]:
    """Подзадачи с `.stopped` без `.done`: имя, agent_id, время остановки (мс)."""
    directory = subtasks(task)
    if not directory.is_dir():
        return []
    pending = []
    for path in sorted(directory.glob("*.stopped")):
        if (directory / f"{path.stem}.done").exists():
            continue
        record = read_record(path)
        at_ms = record.get("at_ms")
        if not isinstance(at_ms, int):
            # Отметка без времени: остановка не позже изменения файла.
            try:
                at_ms = path.stat().st_mtime_ns // 1_000_000
            except OSError:
                at_ms = 0
        pending.append((path.stem, str(record.get("agent_id", "?")), at_ms))
    return pending


def dutylib_module():
    """Модуль scripts/dutylib.py плагина: прогоны tab-run.sh и их meta.txt."""
    sys.path.insert(0, str(SCRIPTS))
    import dutylib

    return dutylib


def json_lines(argv: list[str]) -> object:
    try:
        done = subprocess.run(
            [sys.executable, "-I", *argv],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_LIMIT_S,
            check=True,
        )
        return json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"task-guard.py: {argv[0]}: {error}", file=sys.stderr)
        return None


def run_waits(task: Path) -> list[tuple[str, int]]:
    """Живые прогоны tab-run.sh задачи: (что, начало в мс)."""
    runs = task / "runs"
    if not runs.is_dir():
        return []
    dutylib = dutylib_module()
    found: list[tuple[str, int]] = []
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        if (run_dir / "done").exists():
            continue  # завершённый прогон: meta.txt не читается
        # meta.txt читается один раз на прогон: и для PID, и для начала.
        meta = dutylib.read_meta(run_dir)
        if dutylib.tab_run_alive(run_dir.resolve(), meta):
            found.append(
                (f"прогон {run_dir.name}", dutylib.run_started_ms(run_dir, meta))
            )
    return found


def signal_waits(task: Path) -> list[tuple[str, int]]:
    """Живые ожидания сигналов этой задачи в signals.py: (что, начало в мс)."""
    found: list[tuple[str, int]] = []
    holder = os.environ.get("DUTY_NAME") or task.name
    waits = json_lines([str(SCRIPTS / "signals.py"), "list", "--waits", "--json"])
    if isinstance(waits, dict):
        for wait in waits.get("waits", []):
            since = wait.get("since_ms")
            if (
                wait.get("holder") == holder
                and wait.get("alive")
                and not wait.get("given")
            ):
                found.append(
                    (
                        f"сигнал {wait.get('name')}",
                        since if isinstance(since, int) else 0,
                    )
                )
    return found


def uncovered(task: Path) -> list[tuple[str, str, int]]:
    """Остановившиеся подзадачи, после остановки которых не начато ни одно ожидание.

    Ожидания, которые разбудят координатора сами, — живые прогоны и сигналы;
    signals.py вызывается, только если прогоны покрыли не все остановки.
    """
    pending = stopped_without_result(task)
    for source in (run_waits, signal_waits):
        if not pending:
            break
        starts = [start for _, start in source(task)]
        pending = [item for item in pending if not any(s >= item[2] for s in starts)]
    return pending


def stop(data: dict[str, object]) -> None:
    task = task_dir()
    if task is None:
        return
    pending = uncovered(task)
    if not pending:
        return
    names = ", ".join(name for name, _, _ in pending)
    if data.get("stop_hook_active"):
        subprocess.run(
            [
                "herdr",
                "notification",
                "show",
                f"{os.environ.get('DUTY_NAME', task.name)}: застой",
                "--body",
                f"ход окончен, подзадачи без итога: {names}",
                "--sound",
                "request",
            ],
            capture_output=True,
            timeout=SUBPROCESS_LIMIT_S,
            check=False,
        )
        return
    lines = "; ".join(f"{name} (agent_id {agent})" for name, agent, _ in pending)
    stopped = {name for name, _, _ in pending}
    running = [n for n in launched_without_result(task) if n not in stopped]
    if running:
        lines += f". Запущены и ещё без итога: {', '.join(running)}"
    emit(
        {
            "decision": "block",
            "reason": (
                f"Подзадачи остановились без итога, и после их остановки не начато ни одно "
                f"ожидание: {lines}. Каждую возобнови "
                "(SendMessage), перезапусти или, если итог уже снят или подзадача снята, "
                f"запиши отметку {subtasks(task)}/<имя>.done с одной строкой итога."
            ),
        }
    )


HANDLERS = {
    "pre-tool": pre_tool,
    "post-tool": post_tool,
    "subagent-stop": subagent_stop,
    "stop": stop,
}


def main(argv: list[str]) -> int:
    worker = os.environ.get("DUTY_ROLE") == "worker"
    if not worker and argv[1:2] != ["pre-tool"]:
        return 0
    try:
        handler = HANDLERS[argv[1]]
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            gated = argv[1] == "pre-tool" and data.get("tool_name") in SUBAGENT_TOOLS
            if gated and gate(data):
                return 0
            if (
                argv[1] == "pre-tool"
                and data.get("tool_name") == ASK_TOOL
                and gate_question(data)
            ):
                return 0
            if worker:
                handler(data)
    except Exception as error:  # noqa: BLE001 - сбой хука не должен мешать сессии
        print(f"task-guard.py: {error!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
