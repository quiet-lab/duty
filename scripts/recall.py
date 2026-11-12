#!/usr/bin/env python3
"""Сбор кандидатов для /duty:recall: что по теме уже записано и сказано.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

    recall.py [scan] <тема> --keyword <слово> [--keyword …] [--root <корень>]
              [--transcripts <каталог>]
    recall.py save <тема> [--root <корень>]      # Markdown итога — со stdin

`save` пишет итог в `<paths.results>/recall/<тема-латиницей>-<ГГГГММДД-ЧЧММ>.md`
(кириллица транслитерируется, пустая латиница — `topic`) и печатает путь.

Только чтение, вывод — Markdown на stdout, разделы: «Записи решений»
(`docs.decisions`), «Спецификации» (`docs.openspec`: `specs/*/spec.md`,
`changes/*/{proposal.md,design.md,specs/**}`, без `changes/archive/`),
«Правила» (`docs.rules`), «Документы» (`docs.recall`), «История git»
(`git log -S<слово> --oneline -20 --since=365.days.ago`), «Реплики пользователя» (транскрипты
`~/.claude/projects/<корень с заменой знаков на ->/*.jsonl`, строки
`"type":"user"` с текстовым `content`, до 20 новых). Поиск — без учёта
регистра, по любому из слов. По файлу: путь и до трёх строк совпадений
с номером и ближайшим заголовком выше; отсутствующий файл пропускается.
Файлов раздела показывается не больше 40: сначала с большим числом
разных слов и строк совпадений, остальные — счётчиком «… ещё файлов».
Файл, подходящий нескольким разделам, показывается в каждом.

Каталог транскриптов — `--transcripts`, затем переменная
`DUTY_TRANSCRIPTS_DIR`, затем `~/.claude/projects/<имя корня>` и каталоги
с этим именем в начале (сеансы из подкаталогов и worktree корня): такой
каталог берётся, если рабочий каталог его сеанса — корень или внутри
корня. Транскрипты читаются построчно; stdin читает только `save`.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dutylib

MAX_LINES = 3
MAX_FILES = 40
GIT_SINCE = "365.days.ago"
MAX_REPLIES = 20
QUOTE = 300
MAX_REPLY_CHARS = 4000
TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        [
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "e",
            "zh",
            "z",
            "i",
            "y",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "kh",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ],
    )
)


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


def matches(path: Path, words: list[str]) -> tuple[list[str], int, int]:
    """До трёх строк совпадений `номер (заголовок): текст`, число строк совпадений и разных слов."""
    low = [w.lower() for w in words]
    heading = ""
    found: list[str] = []
    total = 0
    hit_words: set[str] = set()
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                text = line.rstrip("\n")
                if text.startswith("#"):
                    heading = text.lstrip("# ").strip()
                lowered = text.lower()
                here = [w for w in low if w in lowered]
                if here:
                    total += 1
                    hit_words.update(here)
                    if len(found) < MAX_LINES:
                        where = f" ({heading})" if heading else ""
                        found.append(f"{number}{where}: {text.strip()[:200]}")
    except OSError:
        return [], 0, 0
    if total > MAX_LINES:
        found.append(f"… ещё {total - MAX_LINES}")
    return found, total, len(hit_words)


def section(title: str, files: list[Path], words: list[str], root: Path) -> str:
    """Раздел справки: файл показывается один раз в разделе, порядок — по числу разных слов и строк."""
    lines = [f"## {title}", ""]
    seen: set[Path] = set()
    hits: list[tuple[int, int, int, Path, list[str]]] = []
    for path in files:
        real = path.resolve()
        if real in seen:
            continue
        seen.add(real)
        found, total, distinct = matches(path, words)
        if found:
            hits.append((-distinct, -total, len(hits), path, found))
    hits.sort(key=lambda h: h[:3])
    for *_, path, found in hits[:MAX_FILES]:
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        lines.append(f"- `{shown}`")
        lines += [f"  - {item}" for item in found]
    if not hits:
        lines.append("Совпадений нет.")
    elif len(hits) > MAX_FILES:
        lines.append(f"… ещё файлов: {len(hits) - MAX_FILES}")
    lines.append("")
    return "\n".join(lines)


def git_section(root: Path, words: list[str]) -> str:
    lines = ["## История git", ""]
    for word in words:
        lines.append(f"### `{word}` (за год)")
        lines.append("")
        try:
            done = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "log",
                    f"-S{word}",
                    "--oneline",
                    "-20",
                    f"--since={GIT_SINCE}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            code, out, err = done.returncode, done.stdout.strip(), done.stderr
        except OSError as error:
            code, out, err = 1, "", str(error)
        if code != 0:
            first = (err.strip().splitlines() or ["код " + str(code)])[0]
            lines.append(f"git log не выполнен: {first}")
        else:
            lines += (
                [f"- {row}" for row in out.splitlines()] if out else ["Совпадений нет."]
            )
        lines.append("")
    return "\n".join(lines)


def session_cwd(directory: Path) -> str:
    """Рабочий каталог сеанса из первой записи с `cwd` самого нового транскрипта каталога."""
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for path in reversed(files):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(50), fh):
                    if '"cwd"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd") if isinstance(obj, dict) else None
                    if isinstance(cwd, str):
                        return cwd
        except OSError:
            continue
    return ""


def transcripts_dirs(root: Path, override: str | None) -> list[Path]:
    """Каталоги транскриптов проекта: заданный явно либо по имени корня и его префиксным подкаталогам."""
    given = override or os.environ.get("DUTY_TRANSCRIPTS_DIR")
    if given:
        return [Path(given)]
    projects = Path.home() / ".claude" / "projects"
    plain = projects / str(root).replace("/", "-")
    name = re.sub(r"[^A-Za-z0-9-]", "-", str(root))
    found = [d for d in (plain, projects / name) if d.is_dir()]
    found = list(dict.fromkeys(found))
    if projects.is_dir():
        for d in sorted(projects.iterdir()):
            if d in found or not d.is_dir():
                continue
            if not (
                d.name.startswith(plain.name + "-") or d.name.startswith(name + "-")
            ):
                continue
            # Имя с общим началом бывает у соседнего проекта (`proj` и `proj-x`):
            # решает рабочий каталог сеанса.
            cwd = session_cwd(d)
            if cwd == str(root) or cwd.startswith(str(root) + "/"):
                found.append(d)
    return found or [plain]


# Служебные записи `type:user`: отчёты субагентов, вывод команд, напоминания.
SERVICE_RE = re.compile(
    r"\s*<(?:task-notification|local-command-stdout|local-command-stderr"
    r"|system-reminder|command-name|command-message|command-args"
    r"|cross-session-message|bash-input|bash-stdout)\b"
    # Строки push-agent.sh (herdr agent prompt) приходят без тегов:
    # «done rc=<код> <каталог прогона>» от tab-run.sh и «субагент <имя>
    # остановился без итога: …» от хука SubagentStop.
    r"|done rc=\d+ "
    r"|субагент \S+ остановился без итога"
)


def quote(text: str, words: list[str]) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    pos = min((i for i in (low.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, pos - QUOTE // 2)
    return flat[start : start + QUOTE]


def replies(directories: list[Path], words: list[str]) -> list[tuple[str, str, str]]:
    low = [w.lower() for w in words]
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    paths = sorted(p for d in directories for p in d.glob("*.jsonl"))
    for path in paths:
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
                    if obj.get("isMeta") or obj.get("isSidechain"):
                        continue
                    message = obj.get("message")
                    content = (
                        message.get("content") if isinstance(message, dict) else None
                    )
                    if not isinstance(content, str):
                        continue
                    if len(content) > MAX_REPLY_CHARS:
                        continue
                    if SERVICE_RE.match(content) or "<command-name>" in content:
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


def replies_section(directories: list[Path], words: list[str]) -> str:
    lines = ["## Реплики пользователя", ""]
    existing = [d for d in directories if d.is_dir()]
    if not existing:
        lines.append(f"Каталога транскриптов нет: `{directories[0]}`.")
    else:
        rows = replies(existing, words)
        if not rows:
            lines.append("Совпадений нет.")
        for stamp, text, name in rows:
            lines.append(f"- {stamp[:10] or '?'} — «{text}» (`{name}`)")
    lines.append("")
    return "\n".join(lines)


def slug(topic: str) -> str:
    latin = "".join(TRANSLIT.get(c, c) for c in topic.lower())
    return re.sub(r"[^a-z0-9]+", "-", latin).strip("-") or "topic"


def save(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="recall.py save")
    parser.add_argument("topic", help="тема")
    parser.add_argument("--root", help="корень проекта")
    args = parser.parse_args(argv)
    try:
        cfg = dutylib.config(args.root)
    except dutylib.duty_config.ConfigError as error:
        sys.exit(f"recall.py: ошибка конфигурации: {error}")
    text = sys.stdin.read()
    if not text.strip():
        sys.exit("recall.py save: итог на stdin пуст")
    results = cfg.get("paths.results", "")
    if not results:
        sys.exit("recall.py save: paths.results не задан в конфигурации")
    directory = Path(results) / "recall"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug(args.topic)}-{time.strftime('%Y%m%d-%H%M')}.md"
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "save":
        return save(argv[1:])
    if argv and argv[0] == "scan":
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    out = [
        f"# Справка по теме: {args.topic}",
        "",
        "Слова: " + ", ".join(f"`{w}`" for w in words),
        "",
    ]

    decisions = cfg.get("docs.decisions", "")
    files = expand(root, decisions) if isinstance(decisions, str) and decisions else []
    out.append(section("Записи решений", files, words, root))

    files = []
    for entry in cfg.get("docs.openspec", []):
        files += openspec_files(root, entry)
    out.append(section("Спецификации", files, words, root))

    files = []
    for entry in cfg.get("docs.rules", []):
        files += expand(root, entry)
    out.append(section("Правила", files, words, root))

    files = []
    for entry in cfg.get("docs.recall", dutylib.duty_config.DEFAULTS["docs"]["recall"]):
        files += expand(root, entry)
    out.append(section("Документы", files, words, root))

    out.append(git_section(root, words))
    out.append(replies_section(transcripts_dirs(root, args.transcripts), words))
    sys.stdout.write("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
