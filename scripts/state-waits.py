#!/usr/bin/env python3
"""Просроченные ожидания из раздела «Ожидания» файла state.md задачи.

Строка раздела: `- <что ждём>: pid <PID> <comm>, с <UTC ЧЧ:ММ>, срок <N> мин`
(agents/worker.md, п. 8). Печатает через «; » пометки «⚠ просрочено +N мин
<что ждём>» по строкам, чей срок истёк; нет просроченных — пусто. Строка
без срока (старый формат) помечается «⚠ без срока <что ждём>». Время в
строке — только ЧЧ:ММ UTC: начало берётся как ближайшее такое время
не позже текущего. Код выхода всегда 0: перечень не должен ломать list.
"""

import re
import sys
import time
from pathlib import Path

LINE_RE = re.compile(
    r"^\s*-\s+(?P<what>.+?):\s+pid\s+\d+\s+\S+"
    r"(?:,\s+с\s+(?P<hh>\d{1,2}):(?P<mm>\d{2}))?(?:,\s+срок\s+(?P<ttl>\d+)\s+мин)?\s*$"
)


def section(text: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Ожидания"
        elif inside:
            lines.append(line)
    return lines


def elapsed_min(hh: int, mm: int, now: float) -> int:
    """Минуты от последнего наступления ЧЧ:ММ UTC до now."""
    t = time.gmtime(now)
    cur = t.tm_hour * 60 + t.tm_min
    return (cur - (hh * 60 + mm)) % (24 * 60)


def marks(text: str, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    out: list[str] = [
        f"⚠ {line.strip()}"
        for line in text.splitlines()
        if line.startswith("Просрочено:")
    ]  # запись signals.py wait
    for line in section(text):
        m = LINE_RE.match(line)
        if not m:
            continue
        if m["ttl"] is None or m["hh"] is None:
            out.append(f"⚠ без срока: {m['what']}")
            continue
        over = elapsed_min(int(m["hh"]), int(m["mm"]), now) - int(m["ttl"])
        if over > 0:
            out.append(f"⚠ просрочено +{over} мин: {m['what']}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("вызов: state-waits.py <state.md>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[1]).read_text(encoding="utf-8")
    except OSError:
        return 0
    print("; ".join(marks(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
