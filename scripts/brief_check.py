"""Проверка формы записки задачи и приглашения субагента.

Единственный код проверки: его вызывают `scripts/rules-lint.py` (а через него
и `dispatch.sh`) и хук `hooks/task-guard.py`. Зависимостей нет.

- `check_brief` — файл записки `brief.md` по шаблону `reference/brief-template.md`;
- `check_prompt` — приглашение `Agent`: строки «Критерий готовности» и
  «Условие остановки».
"""

from __future__ import annotations

import re

SECTIONS = ("Цель", "Критерий готовности", "Условие остановки", "Ссылки")
READ_NAME = "Прочитанное при постановке"
DONE_NAME = "Критерий готовности"
STOP_NAME = "Условие остановки"
LINKS_READ_VIOLATION = f"Ссылки / {READ_NAME}"
DONE_COMMAND_VIOLATION = f"{DONE_NAME} (не названа команда)"
PROMPT_NAMES = (DONE_NAME, STOP_NAME)

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
DASHES = {"", "—", "–", "-", "--"}


def is_placeholder(text: str) -> bool:
    """Пустое значение, прочерк или незаполненный заполнитель."""
    value = text.strip().strip("*_ ").strip()
    if value in DASHES:
        return True
    return bool(re.fullmatch(r"\{\{.*\}\}", value) or re.fullmatch(r"<[^<>]*>", value))


def is_empty_line(line: str) -> bool:
    """Строка без содержания: пустая, прочерк, заполнитель (с маркером списка)."""
    return is_placeholder(BULLET_RE.sub("", line, count=1))


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
    return any(not is_empty_line(line) for line in body if not FENCE_RE.match(line))


def has_command(body: list[str]) -> bool:
    """Команда названа: фрагмент в обратных кавычках или непустой блок кода."""
    fenced = False
    for line in body:
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced and not is_empty_line(line):
            return True
    text = "\n".join(line for line in body if not FENCE_RE.match(line))
    return any(
        not is_placeholder(fragment) for fragment in INLINE_CODE_RE.findall(text)
    )


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
    pattern = re.compile(
        rf"^\s*(?:[-*+]\s+)?(?:\*\*|__)?{READ_NAME}(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$",
        re.IGNORECASE,
    )
    for position, line in enumerate(body):
        match = pattern.match(line)
        if not match:
            continue
        if not is_placeholder(match.group(1)):
            return True
        # Значение на следующих строках того же пункта (продолжение с отступом).
        for follow in body[position + 1 :]:
            if (
                not follow.strip()
                or BULLET_RE.match(follow)
                or not follow[:1].isspace()
            ):
                break
            if not is_placeholder(follow):
                return True
    return False


def check_brief(text: str) -> list[str]:
    """Имена разделов записки с нарушением; пусто — записка годна."""
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
        elif name == "Ссылки" and not has_read(body, lines, headings, head.index + 1):
            problems.append(LINKS_READ_VIOLATION)
    return problems


def prompt_has(lines: list[str], headings: list[Heading], name: str) -> bool:
    key = name.casefold()
    for head in headings:
        if head.name == key and has_content(
            body_of(lines, headings, head, max_level=6)
        ):
            return True
    pattern = re.compile(
        rf"^\s*(?:[-*+]\s+)?(?:\*\*|__)?{re.escape(name)}(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$",
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.match(line)
        if match and not is_placeholder(match.group(1)):
            return True
    return False


def check_prompt(text: str) -> list[str]:
    """Недостающие в приглашении строки: «Критерий готовности», «Условие остановки»."""
    lines = text.splitlines()
    headings = scan(lines, skip_fences=False)
    return [name for name in PROMPT_NAMES if not prompt_has(lines, headings, name)]
