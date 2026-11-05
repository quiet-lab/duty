#!/usr/bin/env python3
"""Проверка файлов правил плагина duty и записок задач.

Подкоманды:

- `repeats` (по умолчанию) — повторы предложений между файлами правил:
  одно правило должно жить в одном файле-владельце, остальные места
  ссылаются на него. Набор: постоянные файлы плагина (`PLUGIN_FILES`;
  у `hooks/task-guard.py` берётся только докстринг модуля), затем из
  конфигурации проекта (`reference/config.md`) `docs.rules` (глобы от корня
  проекта, `~` допустим), `<корень>/.duty/*.md` и `docs.rules_optional`
  (отсутствие файла — примечание в stderr, не находка). Без корня проекта
  или в общем режиме проверяются только файлы плагина;
- `brief <файл>` — обязательные разделы записки задачи (`-` — стандартный
  ввод); та же проверка, что в `dispatch.sh` и в хуке `task-guard.py`
  (`scripts/brief_check.py`).

Находки печатаются строками `файл:строка: сообщение` и итогом «N находок»;
код возврата 1 при находках, 0 без них, 2 при ошибке настройки.
Запуск: `python3 -I scripts/rules-lint.py [repeats [--root КАТАЛОГ]]`; без `--root`
корень ищет `duty_config.py`.
Внешних зависимостей нет (Python 3.11+).
"""

from __future__ import annotations

import argparse
import ast
import glob
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brief_check
import duty_config

PLUGIN = Path(__file__).resolve().parent.parent
# Порог повтора: нормализованное предложение не короче обоих значений.
MIN_WORDS = 8
MIN_CHARS = 50
# Постоянные файлы плагина от его каталога; у task-guard.py — только докстринг.
PLUGIN_FILES = (
    "README.md",
    "agents/worker.md",
    "reference/worker-rules.md",
    "reference/brief-template.md",
    "reference/report.md",
    "reference/config.md",
    "skills/dispatch/SKILL.md",
    "hooks/task-guard.py",
)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^\s*#{1,6}\s")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")
SHOWN_CHARS = 80


class ConfigError(Exception):
    """Ошибка настройки."""


@dataclass(frozen=True)
class Sentence:
    file: str
    line: int
    text: str


def normalize(text: str) -> str:
    """Регистр, ё, обратные кавычки, разметка и пунктуация убираются."""
    lowered = text.casefold().replace("ё", "е").replace("`", "")
    return " ".join(re.sub(r"[^\w]+", " ", lowered).split())


def units(lines: list[str], first_line: int = 1) -> list[tuple[int, str]]:
    """Единицы текста: абзац, пункт списка, ячейка таблицы; блоки кода исключены."""
    result: list[tuple[int, str]] = []
    current: list[str] = []
    start = first_line
    fenced = False

    def flush() -> None:
        if current:
            result.append((start, " ".join(current)))
            current.clear()

    for offset, raw in enumerate(lines):
        number = first_line + offset
        if FENCE_RE.match(raw):
            flush()
            fenced = not fenced
            continue
        stripped = raw.strip()
        if fenced:
            continue
        if not stripped or HEADING_RE.match(raw):
            flush()
            if stripped:
                result.append((number, stripped.lstrip("# ")))
        elif stripped.startswith("|"):
            flush()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                result.extend((number, c) for c in cells if c)
        elif LIST_RE.match(raw):
            flush()
            start = number
            current.append(LIST_RE.sub("", raw, count=1).strip())
        else:
            if not current:
                start = number
            current.append(stripped)
    flush()
    return result


def docstring_lines(text: str) -> tuple[list[str], int]:
    """Докстринг модуля Python и номер его первой строки."""
    tree = ast.parse(text)
    first = tree.body[0] if tree.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        value = first.value.value
        if isinstance(value, str):
            return value.splitlines(), first.lineno
    return [], 1


def sentences_of(path: Path, shown: str) -> list[Sentence]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        lines, first = docstring_lines(text)
    else:
        lines, first = text.splitlines(), 1
    found: list[Sentence] = []
    for line, unit in units(lines, first):
        for sentence in SENTENCE_END_RE.split(unit):
            if sentence.strip():
                found.append(Sentence(shown, line, sentence.strip()))
    return found


def shown_name(path: Path, root: Path) -> str:
    """Путь от корня проекта; файлы плагина вне корня — «плагин/…»."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        pass
    try:
        return f"плагин/{path.relative_to(PLUGIN)}"
    except ValueError:
        return str(path)


def expand(entry: str, root: Path) -> list[Path]:
    pattern = str(Path(entry).expanduser())
    if not Path(pattern).is_absolute():
        pattern = str(root / pattern)
    return sorted(Path(p) for p in glob.glob(pattern) if Path(p).is_file())


def run_repeats(root_arg: Path | None) -> list[str]:
    try:
        cfg = duty_config.load(root_arg)
    except duty_config.ConfigError as error:
        raise ConfigError(str(error)) from error
    root = cfg.root
    base = root or PLUGIN
    findings: list[str] = []
    # Записи набора: (образец или путь, обязателен ли, от какого каталога,
    # источник записи для сообщения о пропавшем файле).
    entries: list[tuple[str, bool, Path, str]] = [
        (f, True, PLUGIN, f"{f}:1: в плагине нет файла") for f in PLUGIN_FILES
    ]
    if root is not None:
        for key, required in (("docs.rules", True), ("docs.rules_optional", False)):
            value = cfg.get(key, [])
            entries += [
                (str(e), required, root, "duty.toml:1: в наборе нет файла для")
                for e in value
            ]
        if cfg.mode == "project":
            for path in sorted((root / ".duty").glob("*.md")):
                entries.append((str(path), True, root, f"{path}:1: нет файла"))
    index: dict[str, list[Sentence]] = {}
    seen: set[Path] = set()
    for entry, required, origin, source in entries:
        paths = expand(entry, origin)
        if not paths and not required:
            print(
                f"rules-lint.py: примечание: необязательный файл «{entry}» "
                "не найден, не проверен",
                file=sys.stderr,
            )
            continue
        if not paths:
            findings.append(f"{source} «{entry}»")
            continue
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            for sentence in sentences_of(path, shown_name(path, base)):
                key = normalize(sentence.text)
                if len(key.split()) < MIN_WORDS or len(key) < MIN_CHARS:
                    continue
                index.setdefault(key, []).append(sentence)
    for group in index.values():
        first = group[0]
        for other in group[1:]:
            if other.file == first.file:
                continue
            findings.append(
                f"{first.file}:{first.line}: повтор в {other.file}:{other.line}: "
                f"{first.text[:SHOWN_CHARS]}"
            )
    return sorted(set(findings))


def run_brief(source: str) -> list[str]:
    if source == "-":
        text, shown = sys.stdin.read(), "записка"
    else:
        try:
            text, shown = Path(source).read_text(encoding="utf-8"), source
        except OSError as error:
            raise ConfigError(f"{source}: {error}") from error
    return [
        f"{shown}: нет или не заполнен раздел: {name}"
        for name in brief_check.check_brief(text)
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    repeats = sub.add_parser("repeats", help="повторы предложений между файлами правил")
    repeats.add_argument(
        "--root",
        type=Path,
        default=None,
        help="корень проекта (иначе ищет duty_config.py)",
    )
    brief = sub.add_parser("brief", help="обязательные разделы записки")
    brief.add_argument("file", help="файл записки или - (стандартный ввод)")
    return parser


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["repeats", *argv]
    args = build_parser().parse_args(argv)
    try:
        if args.command == "brief":
            findings = run_brief(args.file)
        else:
            findings = run_repeats(args.root)
    except ConfigError as error:
        print(f"rules-lint.py: {error}", file=sys.stderr)
        return 2
    for line in findings:
        print(line)
    print(f"{len(findings)} находок")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
