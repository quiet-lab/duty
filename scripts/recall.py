#!/usr/bin/env python3
"""Сбор кандидатов для /duty:recall: что по теме уже записано и сказано.

    recall.py <тема> --keyword <слово> [--keyword …] [--root <корень>]
              [--transcripts <каталог>]

Только чтение, вывод — Markdown на stdout, разделы: «Записи решений»
(`docs.decisions`), «Спецификации» (`docs.openspec`: `specs/*/spec.md`,
`changes/*/{proposal.md,design.md,specs/**}`, без `changes/archive/`),
«Правила» (`docs.rules`), «Документы» (`docs.recall`), «История git»
(`git log -S<слово> --oneline -20`), «Реплики пользователя» (транскрипты
`~/.claude/projects/<корень с заменой знаков на ->/*.jsonl`, строки
`"type":"user"` с текстовым `content`, до 20 новых). Поиск — без учёта
регистра, по любому из слов. По файлу: путь и до трёх строк совпадений
с номером и ближайшим заголовком выше; отсутствующий файл пропускается.

Каталог транскриптов — `--transcripts`, затем переменная
`DUTY_TRANSCRIPTS_DIR`, затем `~/.claude/projects/<имя>`. Транскрипты
читаются построчно; stdin не читается.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dutylib

MAX_LINES = 3
MAX_FILES = 40
MAX_REPLIES = 20
QUOTE = 300


def md_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*.md") if p.is_file())
    return []


def expand(root: Path, entry: str) -> list[Path]:
    """Файл, каталог или маска из конфигурации → файлы Markdown."""
    base = Path(os.path.expanduser(entry))
    if not base.is_absolute():
        base = root / entry
    if any(c in entry for c in "*?["):
        out: list[Path] = []
        for hit in sorted(glob.glob(str(base), recursive=True)):
            out += md_files(Path(hit))
        return out
    return md_files(base)


def openspec_files(root: Path, entry: str) -> list[Path]:
    base = Path(os.path.expanduser(entry))
    if not base.is_absolute():
        base = root / entry
    out = sorted(base.glob("specs/*/spec.md"))
    changes = base / "changes"
    if changes.is_dir():
        for ch in sorted(changes.iterdir()):
            if ch.name == "archive" or not ch.is_dir():
                continue
            out += [p for p in (ch / "proposal.md", ch / "design.md") if p.is_file()]
            out += sorted(p for p in (ch / "specs").rglob("*.md") if p.is_file())
    return out


def matches(path: Path, words: list[str]) -> list[str]:
    """До трёх строк совпадений: `номер (заголовок): текст`."""
    low = [w.lower() for w in words]
    heading = ""
    found: list[str] = []
    total = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                text = line.rstrip("\n")
                if text.startswith("#"):
                    heading = text.lstrip("# ").strip()
                if any(w in text.lower() for w in low):
                    total += 1
                    if len(found) < MAX_LINES:
                        where = f" ({heading})" if heading else ""
                        found.append(f"{number}{where}: {text.strip()[:200]}")
    except OSError:
        return []
    if total > MAX_LINES:
        found.append(f"… ещё {total - MAX_LINES}")
    return found


def section(
    title: str, files: list[Path], words: list[str], root: Path, seen: set[Path]
) -> str:
    lines = [f"## {title}", ""]
    count = 0
    hits = 0
    for path in files:
        real = path.resolve()
        if real in seen:
            continue
        found = matches(path, words)
        if not found:
            continue
        seen.add(real)
        hits += 1
        if hits > MAX_FILES:
            count += 1
            continue
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        lines.append(f"- `{shown}`")
        lines += [f"  - {item}" for item in found]
    if hits == 0:
        lines.append("Совпадений нет.")
    elif count:
        lines.append(f"… ещё файлов: {count}")
    lines.append("")
    return "\n".join(lines)


def git_section(root: Path, words: list[str]) -> str:
    lines = ["## История git", ""]
    for word in words:
        out = dutylib.sh("git", "-C", str(root), "log", f"-S{word}", "--oneline", "-20")
        lines.append(f"### `{word}`")
        lines.append("")
        lines += [f"- {row}" for row in out.splitlines()] if out else ["Совпадений нет."]
        lines.append("")
    return "\n".join(lines)


def transcripts_dir(root: Path, override: str | None) -> Path:
    given = override or os.environ.get("DUTY_TRANSCRIPTS_DIR")
    if given:
        return Path(given)
    projects = Path.home() / ".claude" / "projects"
    plain = projects / str(root).replace("/", "-")
    if plain.is_dir():
        return plain
    return projects / re.sub(r"[^A-Za-z0-9-]", "-", str(root))


def quote(text: str, words: list[str]) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    pos = min((i for i in (low.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, pos - QUOTE // 2)
    return flat[start : start + QUOTE]


def replies(directory: Path, words: list[str]) -> list[tuple[str, str, str]]:
    low = [w.lower() for w in words]
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"user"' not in line:
                        continue
                    lowered = line.lower()
                    if not any(w in lowered for w in low):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user":
                        continue
                    message = obj.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, str):
                        continue
                    if not any(w in content.lower() for w in low):
                        continue
                    text = quote(content, words)
                    if text in seen:
                        continue
                    seen.add(text)
                    stamp = str(obj.get("timestamp", ""))
                    found.append((stamp, text, path.name))
        except OSError:
            continue
    found.sort(key=lambda r: r[0], reverse=True)
    return found[:MAX_REPLIES]


def replies_section(directory: Path, words: list[str]) -> str:
    lines = ["## Реплики пользователя", ""]
    if not directory.is_dir():
        lines.append(f"Каталога транскриптов нет: `{directory}`.")
    else:
        rows = replies(directory, words)
        if not rows:
            lines.append("Совпадений нет.")
        for stamp, text, name in rows:
            lines.append(f"- {stamp[:10] or '?'} — «{text}» (`{name}`)")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("topic", help="тема")
    parser.add_argument("--keyword", action="append", default=[], help="ключевое слово")
    parser.add_argument("--root", help="корень проекта")
    parser.add_argument("--transcripts", help="каталог транскриптов")
    args = parser.parse_args(argv)
    try:
        cfg = dutylib.config(args.root)
    except dutylib.duty_config.ConfigError as error:
        sys.exit(f"recall.py: ошибка конфигурации: {error}")
    root = Path(args.root).resolve() if args.root else (cfg.root or Path.cwd())
    words = [w for w in args.keyword if w.strip()] or [args.topic]
    seen: set[Path] = set()
    out = [f"# Справка по теме: {args.topic}", "", "Слова: " + ", ".join(f"`{w}`" for w in words), ""]

    decisions = cfg.get("docs.decisions", "")
    files = expand(root, decisions) if isinstance(decisions, str) and decisions else []
    out.append(section("Записи решений", files, words, root, seen))

    files = []
    for entry in cfg.get("docs.openspec", []):
        files += openspec_files(root, entry)
    out.append(section("Спецификации", files, words, root, seen))

    files = []
    for entry in cfg.get("docs.rules", []):
        files += expand(root, entry)
    out.append(section("Правила", files, words, root, seen))

    files = []
    for entry in cfg.get("docs.recall", ["docs/"]):
        files += expand(root, entry)
    out.append(section("Документы", files, words, root, seen))

    out.append(git_section(root, words))
    out.append(replies_section(transcripts_dir(root, args.transcripts), words))
    sys.stdout.write("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
