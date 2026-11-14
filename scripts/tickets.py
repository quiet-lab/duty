#!/usr/bin/env python3
"""Сводка открытых тикетов Jira по группам функционала для обсуждения очереди.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

    tickets.py [all | <фильтр>] [--root <корень>] [--from-json <выгрузка>] [--today ГГГГ-ММ-ДД]

Без аргумента — выборка `tickets.jql` (пусто — тикеты, назначенные
на текущего пользователя, и тикеты без назначенного, созданные им или
с метками `tickets.labels`); `all` — все нерешённые тикеты проекта
`tickets.project`; иное слово — фильтр по группе, ключу или заголовку
(подстрока без учёта регистра) поверх выборки по умолчанию.

Только чтение: Jira REST `GET /rest/api/2/search` из раздела `[jira]`
(`url`, `token_file`), токен читается из файла и не выводится. Без
`[jira]` или файла токена — одна строка об этом. Ответ 401/403 —
остановка без повторов. Группы, пороги, метки и статусы — раздел
`[tickets]` (`reference/config.md`). В stdout — краткая сводка и путь
полного отчёта `<paths.results>/tickets/<ГГГГММДД-ЧЧММ>.md`; рядом —
выгрузка `.json` того же имени (её принимает `--from-json`).
"""

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модули подключаются по пути.
_spec = importlib.util.spec_from_file_location(
    "dutylib", Path(__file__).with_name("dutylib.py")
)
dutylib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dutylib)
duty_config = dutylib.duty_config
config = dutylib.config
NotConfigured = dutylib.NotConfigured

FIELDS = (
    "summary,status,issuetype,priority,labels,assignee,reporter,"
    "created,updated,description,issuelinks,parent"
)
EXPAND = "changelog"
PAGE = 100
DESC_KEEP = 1000
SHORT_WORDS = 12


class JiraDenied(Exception):
    pass


# --- Выборка -------------------------------------------------------------------


def quote_list(items: list[str]) -> str:
    return ", ".join('"' + i.replace('"', '\\"') + '"' for i in items)


def default_jql(cfg) -> str:
    jql = cfg.get("tickets.jql", "")
    if jql:
        return jql
    mine = "reporter = currentUser()"
    labels = cfg.get("tickets.labels", [])
    if labels:
        mine = f"({mine} OR labels in ({quote_list(labels)}))"
    jql = (
        "resolution = Unresolved AND (assignee = currentUser()"
        f" OR (assignee is EMPTY AND {mine}))"
    )
    project = cfg.get("tickets.project", "")
    return f'project = "{project}" AND {jql}' if project else jql


def all_jql(cfg) -> str:
    project = cfg.get("tickets.project", "")
    if not project:
        raise NotConfigured("[tickets]: project не задан — выборка all не проверено")
    return f'project = "{project}" AND resolution = Unresolved'


def status_since(issue: dict) -> str:
    """Дата последнего перехода в статус по changelog Jira; пусто — переходов нет."""
    since = ""
    for h in (issue.get("changelog") or {}).get("histories") or []:
        if any(i.get("field") == "status" for i in h.get("items") or []):
            since = max(since, (h.get("created") or "")[:10])
    return since


def normalize(issue: dict, duplicate_types: list[str]) -> dict:
    f = issue["fields"]
    links, dups = [], []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if not other:
            continue
        links.append(other["key"])
        if (link.get("type") or {}).get("name") in duplicate_types:
            dups.append(other["key"])
    desc = f.get("description") or ""
    created = (f.get("created") or "")[:10]
    return {
        "key": issue["key"],
        "summary": f.get("summary") or "",
        "status": (f.get("status") or {}).get("name", ""),
        "type": (f.get("issuetype") or {}).get("name", ""),
        "prio": (f.get("priority") or {}).get("name"),
        "labels": f.get("labels") or [],
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "reporter": (f.get("reporter") or {}).get("displayName"),
        "created": created,
        "updated": (f.get("updated") or "")[:10],
        "status_since": status_since(issue) or created,
        "desc": desc if isinstance(desc, str) else "",
        "links": links,
        "dup_links": dups,
        "parent": (f.get("parent") or {}).get("key"),
    }


def fetch(cfg, jql: str) -> dict[str, dict]:
    url = cfg.get("jira.url", "")
    if not isinstance(url, str) or not url.strip():
        raise NotConfigured("[jira]: url не задан — тикеты не проверено")
    token = dutylib.access_file("jira", "token_file", "тикеты").read_text().strip()
    base = url.rstrip("/") + "/rest/api/2/search?"
    dup_types = cfg.get("tickets.duplicate_links", [])
    check = cfg.get("tickets.check_statuses", [])

    def pages(query_jql: str, expand: str):
        """Страницы выдачи Jira: по одной на запрос."""
        start = 0
        while True:
            params = {
                "jql": query_jql,
                "fields": FIELDS,
                "maxResults": PAGE,
                "startAt": start,
            }
            if expand:
                params["expand"] = expand
            req = urllib.request.Request(
                base + urllib.parse.urlencode(params),
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=30, context=dutylib.ssl_context(cfg)
                ) as r:
                    page = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise JiraDenied(f"Jira ответил {e.code}") from None
                raise
            issues = page.get("issues") or []
            yield issues
            start += len(issues)
            if not issues or start >= page.get("total", 0):
                return

    out: dict[str, dict] = {}
    for issues in pages(jql, ""):
        for issue in issues:
            t = normalize(issue, dup_types)
            t["desc"] = t["desc"][:DESC_KEEP]
            out[t["key"]] = t
    # История переходов нужна только для возраста в статусе проверки:
    # запрашивается по ключам тикетов в этих статусах.
    waiting = [k for k, t in out.items() if t["status"] in check]

    def history(keys: list[str]) -> bool:
        """Записывает `status_since` по истории переходов тикетов `keys`; False — 400."""
        try:
            for issues in pages(f"key in ({', '.join(keys)})", EXPAND):
                for issue in issues:
                    t = out.get(issue["key"])
                    if t is not None:
                        t["status_since"] = status_since(issue) or t["created"]
        except urllib.error.HTTPError as e:
            # 401/403 сюда не пускает: они останавливают отчёт.
            if e.code != 400:
                raise
            return False
        return True

    def batch_history(keys: list[str]) -> None:
        """История пакета; при 400 — повтор по одному ключу с остановкой."""
        if history(keys):
            return
        names = ", ".join(keys)
        # 400 на пакет (например, ключ удалённого тикета): без повтора все
        # тикеты пакета остались бы с запасным `created`, уже записанным
        # `normalize`. Повтор по одному ключу оставляет `created` только тому,
        # на кого 400 и по одному.
        if len(keys) == 1:
            print(
                f"tickets: Jira ответила 400 на историю {names}: "
                "возраст в статусе — по дате создания",
                file=sys.stderr,
            )
            return
        print(
            f"tickets: Jira ответила 400 на историю пакета ({names}): "
            "повтор по одному ключу",
            file=sys.stderr,
        )
        bad: list[str] = []
        for key in keys:
            if history([key]):
                bad.clear()
                continue
            bad.append(key)
            # Два 400 подряд на одиночные ключи: причина в самом запросе
            # (expand, длина JQL), а не в ключе; повтор остальных не поможет.
            if len(bad) == 2:
                print(
                    "tickets: Jira ответила 400 на одиночные ключи подряд "
                    f"({', '.join(bad)}): повтор прекращён, остальные ключи "
                    "пакета и эти — с возрастом в статусе по дате создания",
                    file=sys.stderr,
                )
                return
        if bad:
            print(
                f"tickets: Jira ответила 400 на историю {', '.join(bad)}: "
                "возраст в статусе — по дате создания",
                file=sys.stderr,
            )

    for i in range(0, len(waiting), PAGE):
        batch_history(waiting[i : i + PAGE])
    return out


# --- Разбор --------------------------------------------------------------------


def key_num(key: str) -> tuple[str, int]:
    prefix, _, num = key.rpartition("-")
    return (prefix, int(num) if num.isdigit() else 0)


class Rules:
    """Правила `[tickets]`: группы, перекрытия, метки и пороги."""

    def __init__(self, cfg) -> None:
        get = cfg.get
        # Шаблоны ищутся по тексту в нижнем регистре, поэтому без учёта регистра:
        # «QMS|Jenkins» в конфигурации находит «qms».
        self.groups = []
        for g in get("tickets.groups", []):
            try:
                pattern = (
                    re.compile(g["pattern"], re.IGNORECASE) if g["pattern"] else None
                )
            except re.error as e:
                raise NotConfigured(
                    f"[tickets]: группа «{g['name']}»: pattern: {e} — тикеты не проверено"
                ) from None
            self.groups.append((g["name"], pattern))
        self.overrides: dict[str, str] = get("tickets.overrides", {})
        self.other = get("tickets.other_group", "Прочее")
        self.review_label = get("tickets.review_label", "")
        self.review_group = (
            f"Замечания ревью ({self.review_label})" if self.review_label else ""
        )
        self.unclear_labels = get("tickets.unclear_labels", [])
        self.unclear_words = get("tickets.unclear_words", [])
        self.check_statuses = get("tickets.check_statuses", [])
        self.stale_days = get("tickets.stale_days", 14)
        self.status_order = get("tickets.status_order", [])
        self.order = get("tickets.order", [])
        self.desc_chars = get("tickets.desc_chars", 150)

    def match(self, t: dict) -> str:
        text = (t["summary"] + " " + t["desc"][: self.desc_chars]).lower()
        for name, pattern in self.groups:
            if pattern is not None and pattern.search(text):
                return name
        return self.other

    def classify(self, t: dict) -> None:
        """Группа `g` и функционал находок `a` (только у тикетов ревью)."""
        override = self.overrides.get(t["key"])
        if self.review_label and self.review_label in t["labels"]:
            t["g"] = self.review_group
            t["a"] = override or self.match(t)
        else:
            t["g"] = override or self.match(t)
            t["a"] = ""

    def group_order(self, present: list[str]) -> list[str]:
        names = list(self.order) or list(dict.fromkeys(n for n, _ in self.groups))
        tail = [self.other, self.review_group]
        order = [n for n in names if n not in tail]
        order += sorted(n for n in present if n not in order and n not in tail)
        order += [n for n in tail if n and n not in order]
        return [n for n in order if n in present]

    def status_rank(self, status: str) -> int:
        if status in self.status_order:
            return self.status_order.index(status)
        return len(self.status_order)

    def unclear_reason(self, t: dict) -> str:
        labels = [x for x in t["labels"] if x in self.unclear_labels]
        if labels:
            return ", ".join(labels)
        words = t["summary"].split()
        if words and words[0] in self.unclear_words:
            return "исследование/проверка"
        if t["g"] == self.other:
            return "расплывчатая постановка"
        return ""


def norm_title(s: str) -> str:
    return " ".join(re.findall(r"\w+", s.lower()))


def duplicates(tickets: dict[str, dict]) -> list[tuple[list[str], str]]:
    """Вероятные дубли: одинаковые нормализованные заголовки и связи типа «дубль»."""
    found: list[tuple[list[str], str]] = []
    by_title: dict[str, list[str]] = {}
    for k, t in tickets.items():
        by_title.setdefault(norm_title(t["summary"]), []).append(k)
    for keys in by_title.values():
        if len(keys) > 1:
            found.append((sorted(keys, key=key_num), "одинаковый заголовок"))
    # Пара, уже найденная по заголовку, второй раз по связи не считается.
    pairs = {
        tuple(sorted((k, other), key=key_num))
        for k, t in tickets.items()
        for other in t.get("dup_links", [])
        if other in tickets
        and norm_title(t["summary"]) != norm_title(tickets[other]["summary"])
    }
    found += [(list(p), "связь «дубль»") for p in sorted(pairs)]
    return sorted(found, key=lambda x: key_num(x[0][0]))


def cell(s: str) -> str:
    """Текст ячейки таблицы Markdown: «|» и переводы строк ломают строку таблицы."""
    return " ".join(s.replace("|", "/").split())


def short(s: str) -> str:
    w = cell(s).split()
    return " ".join(w[:SHORT_WORDS]) + ("…" if len(w) > SHORT_WORDS else "")


def stale(t: dict, rules: Rules, today: dt.date) -> int | None:
    """Дни в статусе проверки, если не меньше порога: от даты входа в статус.

    В выгрузке без `status_since` (прежний формат) берётся дата обновления.
    """
    if t["status"] not in rules.check_statuses:
        return None
    try:
        days = (
            today - dt.date.fromisoformat(t.get("status_since") or t["updated"])
        ).days
    except ValueError:
        return None
    return days if days >= rules.stale_days else None


# --- Отчёт ---------------------------------------------------------------------


class Report:
    def __init__(self, tickets: dict[str, dict], rules: Rules, url: str) -> None:
        self.t = tickets
        self.rules = rules
        self.url = url.rstrip("/")
        groups: dict[str, list[dict]] = {}
        for v in tickets.values():
            groups.setdefault(v["g"], []).append(v)
        self.order = rules.group_order(list(groups))
        self.groups = {
            g: sorted(
                groups[g],
                key=lambda v: (rules.status_rank(v["status"]), -key_num(v["key"])[1]),
            )
            for g in self.order
        }

    def link(self, key: str) -> str:
        return f"[{key}]({self.url}/browse/{key})" if self.url else key

    def statuses(self) -> list[tuple[str, int]]:
        c = Counter(v["status"] for v in self.t.values())
        known = [s for s in self.rules.status_order if s in c]
        rest = sorted(s for s in c if s not in known)
        return [(s, c[s]) for s in known + rest]

    def row(self, v: dict, review: bool) -> str:
        who = v["assignee"].split()[0] if v["assignee"] else "не назначен"
        cells = [
            self.link(v["key"]),
            cell(v["type"]),
            cell(v["status"]),
            cell(who),
            short(v["summary"]),
        ]
        if review:
            cells.append(cell(v["a"]))
        return "| " + " | ".join(cells) + " |"

    def unclear(self) -> list[tuple[str, str]]:
        out = []
        for k in sorted(self.t, key=key_num):
            why = self.rules.unclear_reason(self.t[k])
            if why:
                out.append((k, why))
        return out

    def stale(self, today: dt.date) -> list[tuple[str, int]]:
        out = []
        for k in sorted(self.t, key=key_num):
            days = stale(self.t[k], self.rules, today)
            if days is not None:
                out.append((k, days))
        return out

    def markdown(self, title: str, scope: str, today: dt.date) -> str:
        out = [f"# {title}", "", scope, ""]
        for g in self.order:
            review = g == self.rules.review_group
            out += [f"## {g} ({len(self.groups[g])})", ""]
            out.append(
                "| Ключ | Тип | Статус | Назначен | Суть |"
                + (" Функционал находок |" if review else "")
            )
            out.append("|---|---|---|---|---|" + ("---|" if review else ""))
            out += [self.row(v, review) for v in self.groups[g]]
            out.append("")
        assigned = Counter(v["assignee"] or "не назначен" for v in self.t.values())
        out += [
            "## Сводка",
            "",
            f"Всего тикетов: {len(self.t)}.",
            "",
            "По статусам: " + ", ".join(f"{s} — {n}" for s, n in self.statuses()) + ".",
            "",
            "По группам: "
            + "; ".join(f"{g} — {len(self.groups[g])}" for g in self.order)
            + ".",
            "",
            "Назначение: "
            + ", ".join(f"{a} — {n}" for a, n in assigned.most_common())
            + ".",
            "",
            "## Для обсуждения",
            "",
            "### Неясно, что делать дальше (ждут решения, исследования, тесты или устарели)",
            "",
        ]
        out += self.listing(self.unclear()) or ["Нет."]
        out += ["", "### Вероятные дубли", ""]
        dups = duplicates(self.t)
        out += [
            "- "
            + ", ".join(self.link(k) for k in keys)
            + f": {short(self.t[keys[0]]['summary'])} ({why})"
            for keys, why in dups
        ] or ["Нет."]
        statuses = " и ".join(f"«{s}»" for s in self.rules.check_statuses)
        out += [
            "",
            f"### В {statuses or 'статусах проверки'} дольше {self.rules.stale_days} дн.",
            "",
        ]
        out += [
            f"- {self.link(k)} {self.t[k]['status']}, {days} дн.: {short(self.t[k]['summary'])}"
            for k, days in self.stale(today)
        ] or ["Нет."]
        return "\n".join(out) + "\n"

    def listing(self, items: list[tuple[str, str]]) -> list[str]:
        return [
            f"- {self.link(k)} {self.t[k]['status']}: {short(self.t[k]['summary'])} ({why})"
            for k, why in items
        ]

    def summary(self, today: dt.date) -> str:
        statuses = self.statuses()
        names = [s for s, _ in statuses]
        head = "| Группа | Всего | " + " | ".join(names) + " |"
        lines = [head, "|---|---:|" + "---:|" * len(names)]
        for g in self.order:
            c = Counter(v["status"] for v in self.groups[g])
            lines.append(
                f"| {g} | {len(self.groups[g])} | "
                + " | ".join(str(c.get(s, 0)) for s in names)
                + " |"
            )
        lines.append(
            f"| Всего | {len(self.t)} | "
            + " | ".join(str(n) for _, n in statuses)
            + " |"
        )
        lines += [
            "",
            (
                f"Для обсуждения: неясных {len(self.unclear())}, вероятных дублей"
                f" {len(duplicates(self.t))}, в статусах проверки дольше"
                f" {self.rules.stale_days} дн. {len(self.stale(today))}."
            ),
        ]
        return "\n".join(lines)


def apply_filter(tickets: dict[str, dict], needle: str) -> dict[str, dict]:
    n = needle.lower()
    return {
        k: v
        for k, v in tickets.items()
        if n in k.lower()
        or n in v["summary"].lower()
        or n in v["g"].lower()
        or n in v["a"].lower()
    }


class BadExport(Exception):
    """Файл `--from-json` не читается как выгрузка тикетов."""


def load_export(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BadExport(str(e)) from None
    if not isinstance(data, dict) or not all(
        isinstance(v, dict) and "summary" in v for v in data.values()
    ):
        raise BadExport("ожидается объект «ключ → тикет» (выгрузка прошлого запуска)")
    for v in data.values():
        v.setdefault("dup_links", [])
    return data


def free_stem(out_dir: Path, name: str) -> Path:
    """Имя отчёта без перезаписи: второй запуск в ту же минуту получает суффикс."""
    stem, n = out_dir / name, 1
    while stem.with_suffix(".md").exists() or stem.with_suffix(".json").exists():
        n += 1
        stem = out_dir / f"{name}-{n}"
    return stem


def build(
    tickets: dict[str, dict], cfg, arg: str, jql: str, today: dt.date, now: dt.datetime
) -> tuple[str, str]:
    """Полный отчёт и краткая сводка по выгрузке."""
    rules = Rules(cfg)
    for v in tickets.values():
        rules.classify(v)
    if arg and arg != "all":
        tickets = apply_filter(tickets, arg)
    project = cfg.get("tickets.project", "")
    title = (
        "Открытые тикеты Jira" + (f" {project}" if project else "") + " по функционалу"
    )
    scope = f"Выборка на {now:%Y-%m-%d %H:%M}: `{jql}`" + (
        f", фильтр «{arg}»." if arg and arg != "all" else "."
    )
    report = Report(tickets, rules, cfg.get("jira.url", "") or "")
    return report.markdown(title, scope, today), report.summary(today)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сводка открытых тикетов Jira по группам функционала."
    )
    ap.add_argument("arg", nargs="?", default="", help="all или фильтр")
    ap.add_argument("--root", help="корень проекта")
    ap.add_argument("--from-json", help="выгрузка прошлого запуска вместо Jira")
    ap.add_argument("--today", help="дата отсчёта застревания ГГГГ-ММ-ДД")
    a = ap.parse_args()
    arg = a.arg.strip()
    try:
        cfg = config(a.root)
    except duty_config.ConfigError as e:
        print(f"ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    if not cfg.has("jira"):
        print("в .duty/duty.toml нет раздела [jira]: тикеты не проверено")
        return 0
    now = dt.datetime.now()
    try:
        today = dt.date.fromisoformat(a.today) if a.today else now.date()
    except ValueError:
        print(f"--today {a.today}: ожидается ГГГГ-ММ-ДД", file=sys.stderr)
        return 2
    try:
        jql = all_jql(cfg) if arg == "all" else default_jql(cfg)
        if a.from_json:
            tickets = load_export(Path(a.from_json))
            jql += f" (выгрузка {a.from_json})"
        else:
            tickets = fetch(cfg, jql)
        full, brief = build(tickets, cfg, arg, jql, today, now)
    except BadExport as e:
        print(f"выгрузка {a.from_json}: {e}", file=sys.stderr)
        return 2
    except NotConfigured as e:
        print(e)
        return 0
    except JiraDenied as e:
        print(
            f"ОСТАНОВКА: {e} — повторять нельзя: неверные входы блокируют учётную запись"
        )
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"Jira недоступна: {e} — тикеты не проверено")
        return 1
    out_dir = Path(cfg.get("paths.results")) / "tickets"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = free_stem(out_dir, f"{now:%Y%m%d-%H%M}")
    if not a.from_json:
        stem.with_suffix(".json").write_text(
            json.dumps(tickets, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    stem.with_suffix(".md").write_text(full, encoding="utf-8")
    print(brief)
    print(f"\nОтчёт: {stem.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
