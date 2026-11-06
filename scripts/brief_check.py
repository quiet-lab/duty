"""Проверка формы записки задачи и приглашения субагента.

Единственный код проверки: его вызывают `scripts/rules-lint.py` (а через него
и `dispatch.sh`) и хук `hooks/task-guard.py`. Зависимостей нет.

- `check_brief` — файл записки `brief.md` по шаблону `reference/brief-template.md`;
- `check_prompt` — приглашение `Agent`: строки «Критерий готовности»,
  «Условие остановки» и «Справка»;
- `check_question` — текст вопроса `AskUserQuestion`: фраза «По документам:».

Строка «Справка: <путь>» называет файл итога `/duty:recall` не старше суток
(путь абсолютный или от корня проекта) либо «Справка: не нужна — <причина>».
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

SECTIONS = ("Цель", "Критерий готовности", "Условие остановки", "Ссылки")
READ_NAME = "Прочитанное при постановке"
DONE_NAME = "Критерий готовности"
STOP_NAME = "Условие остановки"
LINKS_READ_VIOLATION = f"Ссылки / {READ_NAME}"
DONE_COMMAND_VIOLATION = f"{DONE_NAME} (не названа команда)"
PROMPT_NAMES = (DONE_NAME, STOP_NAME)
RECALL_NAME = "Справка"
LINKS_RECALL_VIOLATION = f"Ссылки / {RECALL_NAME}"
RECALL_MAX_AGE_S = 24 * 60 * 60
NOT_NEEDED_RE = re.compile(
    r"^не\s+нужна\b\s*(?:[—–-]+\s*(.*))?$", re.IGNORECASE | re.DOTALL
)
QUESTION_MARK = "По документам:"

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
DASHES = {"", "—", "–", "-", "--"}


TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|<[^<>]*>")
MARKER = r"(?:(?:[-*+]|\d+[.)])\s+)?"
LABEL_RE = re.compile(
    rf"^\s*{MARKER}(?:\*\*|__)?[А-ЯЁA-Z][^:`\n]{{0,50}}:(?:\*\*|__)?(?:\s|$)"
)
PATH_LIKE_RE = re.compile(r"\S*/\S*|\S+\.\w+")


def is_placeholder(text: str) -> bool:
    """Пустое значение, прочерк или заполнитель: без `<...>`, `{{...}}` и кавычек меньше трёх букв."""
    rest = TEMPLATE_RE.sub("", text.replace("`", ""))
    return sum(ch.isalpha() for ch in rest) < 3


def is_template_fragment(fragment: str) -> bool:
    """Фрагмент в обратных кавычках без содержания: пуст или целиком заполнитель."""
    return not TEMPLATE_RE.sub("", fragment).strip()


def looks_like_path(fragment: str) -> bool:
    """Одно слово с `/` или расширением, не начинающееся с `./`: путь или имя файла, не команда."""
    value = fragment.strip()
    return bool(PATH_LIKE_RE.fullmatch(value)) and not value.startswith("./")


def is_empty_line(line: str) -> bool:
    """Строка без содержания: пустая, прочерк, заполнитель (с маркером списка)."""
    return HEADING_RE.match(line) is not None or is_placeholder(
        BULLET_RE.sub("", line, count=1)
    )


def heading_name(title: str) -> str:
    return title.strip().rstrip(":").strip().casefold()


class Heading:
    def __init__(self, index: int, level: int, name: str) -> None:
        self.index = index
        self.level = level
        self.name = name


def scan(lines: list[str], *, skip_fences: bool) -> list[Heading]:
    """Заголовки текста; с `skip_fences` заголовки внутри блоков кода не считаются."""
    found: list[Heading] = []
    fenced = False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced and skip_fences:
            continue
        match = HEADING_RE.match(line)
        if match:
            found.append(
                Heading(index, len(match.group(1)), heading_name(match.group(2)))
            )
    return found


def body_of(
    lines: list[str], headings: list[Heading], head: Heading, *, max_level: int
) -> list[str]:
    """Тело раздела: строки до следующего заголовка уровня не глубже `max_level`."""
    end = len(lines)
    for other in headings:
        if other.index > head.index and other.level <= max_level:
            end = other.index
            break
    return lines[head.index + 1 : end]


def has_content(body: list[str]) -> bool:
    """Есть содержание: строка не заголовок и не заполнитель; в блоке кода — любая непустая."""
    fenced = False
    for line in body:
        if FENCE_RE.match(line):
            fenced = not fenced
        elif fenced:
            if line.strip() and not is_template_fragment(line):
                return True
        elif not is_empty_line(line):
            return True
    return False


def has_command(body: list[str]) -> bool:
    """Команда названа: фрагмент в обратных кавычках или непустой блок кода."""
    fenced = False
    for line in body:
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced and line.strip() and not is_template_fragment(line):
            return True
    text = "\n".join(line for line in body if not FENCE_RE.match(line))
    return any(
        not is_template_fragment(fragment) and not looks_like_path(fragment)
        for fragment in INLINE_CODE_RE.findall(text)
    )


def labeled_values(lines: list[str], name: str) -> list[str]:
    """Значения метки `name`: после двоеточия или на следующей непустой строке."""
    pattern = re.compile(
        rf"^\s*{MARKER}(?:\*\*|__)?{re.escape(name)}(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$",
        re.IGNORECASE,
    )
    values: list[str] = []
    for position, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            nxt = next((x for x in lines[position + 1 :] if x.strip()), "")
            if nxt and not HEADING_RE.match(nxt) and not LABEL_RE.match(nxt):
                value = BULLET_RE.sub("", nxt, count=1).strip()
        values.append(value)
    return values


def has_read(
    body: list[str], lines: list[str], headings: list[Heading], start: int
) -> bool:
    """В «Ссылках» (тело начинается со строки `start`) есть заполненное «Прочитанное при постановке»."""
    end = start + len(body)
    for head in headings:
        if (
            head.name == heading_name(READ_NAME)
            and head.level >= 3
            and start <= head.index < end
            and has_content(body_of(lines, headings, head, max_level=6))
        ):
            return True
    return any(not is_placeholder(v) for v in labeled_values(body, READ_NAME))


def project_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Корень проекта для относительных путей: аргумент, `DUTY_PROJECT`, `CLAUDE_PROJECT_DIR`, cwd."""
    for given in (
        root,
        os.environ.get("DUTY_PROJECT"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
    ):
        if given:
            return Path(given).expanduser()
    return Path.cwd()


def recall_state(
    values: list[str],
    root: str | os.PathLike[str] | None,
    now: float | None,
) -> str | None:
    """Состояние строки «Справка»: None — годна; иначе суффикс нарушения ('', ' (файла нет)', ' (старше суток)')."""
    value = next((v for v in values if not is_placeholder(v)), None)
    if value is None:
        return ""
    plain = value.replace("`", "").strip()
    refusal = NOT_NEEDED_RE.match(plain)
    if refusal:
        return None if not is_placeholder(refusal.group(1) or "") else ""
    fragments = INLINE_CODE_RE.findall(value)
    target = (fragments[0] if fragments else plain).strip()
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = project_root(root) / path
    try:
        if not path.is_file():
            return " (файла нет)"
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return " (файла нет)"
    return " (старше суток)" if age > RECALL_MAX_AGE_S else None


def check_brief(
    text: str,
    root: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> list[str]:
    """Имена разделов записки с нарушением; пусто — записка годна.

    `root` — корень проекта для относительного пути справки, `now` — текущее время
    (секунды с начала эпохи), оба для вызывающих и тестов.
    """
    lines = text.splitlines()
    headings = scan(lines, skip_fences=True)
    problems: list[str] = []
    for name in SECTIONS:
        key = heading_name(name)
        head = next((h for h in headings if h.level == 2 and h.name == key), None)
        if head is None:
            problems.append(name)
            continue
        body = body_of(lines, headings, head, max_level=2)
        if not has_content(body):
            problems.append(name)
        elif name == DONE_NAME and not has_command(body):
            problems.append(DONE_COMMAND_VIOLATION)
        elif name == "Ссылки":
            if not has_read(body, lines, headings, head.index + 1):
                problems.append(LINKS_READ_VIOLATION)
            state = recall_state(labeled_values(body, RECALL_NAME), root, now)
            if state is not None:
                problems.append(LINKS_RECALL_VIOLATION + state)
    return problems


def prompt_has(lines: list[str], headings: list[Heading], name: str) -> bool:
    key = name.casefold()
    for head in headings:
        if head.name == key and has_content(
            body_of(lines, headings, head, max_level=6)
        ):
            return True
    return any(not is_placeholder(v) for v in labeled_values(lines, name))


def check_prompt(
    text: str,
    root: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> list[str]:
    """Недостающие в приглашении строки: «Критерий готовности», «Условие остановки», «Справка»."""
    lines = text.splitlines()
    headings = scan(lines, skip_fences=False)
    problems = [name for name in PROMPT_NAMES if not prompt_has(lines, headings, name)]
    state = recall_state(labeled_values(lines, RECALL_NAME), root, now)
    if state is not None:
        problems.append(RECALL_NAME + state)
    return problems


def check_question(text: str) -> bool:
    """В вопросе есть «По документам:» и за ним предложение (не заполнитель)."""
    index = text.find(QUESTION_MARK)
    if index < 0:
        return False
    return not is_placeholder(text[index + len(QUESTION_MARK) :])
