"""Сводка тикетов `tickets.py`: группы, перекрытия, дубли, фильтр, отчёт.

Запуск: `python3 -I scripts/tests/test_tickets.py`. Сети нет: выгрузка —
`tickets-sample.json` рядом (сокращённая выгрузка Jira с двумя
синтетическими дублями), Jira — сервер на 127.0.0.1 внутри теста.
"""

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

SCRIPTS = Path(__file__).resolve().parents[1]
SAMPLE = Path(__file__).with_name("tickets-sample.json")

TICKETS_TOML = """
[tickets]
project = "RP"
labels = ["claude"]
review_label = "claude-review-defects"
unclear_labels = ["to_discuss", "investigate", "needs-tests", "устарело"]
unclear_words = ["Исследовать", "Ресерч"]
check_statuses = ["To Test", "To Fix"]
stale_days = 14
status_order = ["В работе", "To Test", "To Fix", "Backlog"]
order = ["Записи", "Телефония", "Конвейер", "Интерфейс", "Производительность", "Тесты"]

[[tickets.groups]]
name = "Записи"
pattern = 'склейк|merge media|записи экран'

[[tickets.groups]]
name = "Конвейер"
pattern = 'duty|jenkins|оснастка прогона'

[[tickets.groups]]
name = "Тесты"
pattern = 'тест|e2e'

[[tickets.groups]]
name = "Телефония"
pattern = 'звонк|asterisk|studio'

[[tickets.groups]]
name = "Интерфейс"
pattern = 'интерфейс|ui\\b|дашборд'

[[tickets.groups]]
name = "Производительность"
pattern = ''

[tickets.overrides]
"RP-1262" = "Производительность"
"RP-286" = "Прочее"
"RP-2044" = "плагин duty"
"""


def load_module():
    spec = importlib.util.spec_from_file_location("tickets", SCRIPTS / "tickets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / ".duty").mkdir()
        self.env = {
            **os.environ,
            "DUTY_PROJECT": str(self.root),
            "DUTY_CONFIG_USER": str(self.root / "none.toml"),
            "DUTY_RESULTS_DIR": str(self.root / "results"),
            "HOME": str(self.root),
        }

    def write_config(self, jira: str, tickets: str = TICKETS_TOML) -> None:
        (self.root / ".duty" / "duty.toml").write_text(
            "schema = 1\n" + jira + tickets, encoding="utf-8"
        )

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "tickets.py"), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )


class ClassifyTest(Base):
    """Разбор на уровне модуля: конфигурация читается из временного корня."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config('[jira]\nurl = "https://jira.example"\n')
        self.mod = load_module()
        self.mod.dutylib.config.cache_clear()
        self.cfg = self.mod.config(str(self.root))
        self.rules = self.mod.Rules(self.cfg)
        self.t = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for v in self.t.values():
            v.setdefault("dup_links", [])
            self.rules.classify(v)

    def test_groups_by_priority(self) -> None:
        g = {k: v["g"] for k, v in self.t.items()}
        self.assertEqual(g["RP-2203"], "Записи")
        self.assertEqual(g["RP-2136"], "Записи")
        self.assertEqual(g["RP-2102"], "Конвейер")
        self.assertEqual(g["RP-2077"], "Телефония")
        self.assertEqual(g["RP-1684"], "Интерфейс")
        # «тест» в заголовке и «звонк» — первой подходит группа выше по списку.
        self.assertEqual(g["RP-2213"], "Тесты")
        self.assertEqual(g["RP-1602"], "Телефония")
        # В начале описания — «интерфейс»: участвует и описание.
        self.assertEqual(g["RP-1648"], "Интерфейс")

    def test_overrides(self) -> None:
        self.assertEqual(self.t["RP-1262"]["g"], "Производительность")
        self.assertEqual(self.t["RP-286"]["g"], "Прочее")
        review = "Замечания ревью (claude-review-defects)"
        self.assertEqual(self.t["RP-2044"]["g"], review)
        self.assertEqual(self.t["RP-2044"]["a"], "плагин duty")
        self.assertEqual(self.t["RP-2140"]["g"], review)
        self.assertEqual(self.t["RP-2140"]["a"], "Записи")
        self.assertEqual(self.t["RP-2203"]["a"], "")

    def test_group_order(self) -> None:
        order = self.rules.group_order(sorted({v["g"] for v in self.t.values()}))
        self.assertEqual(
            order,
            [
                "Записи",
                "Телефония",
                "Конвейер",
                "Интерфейс",
                "Производительность",
                "Тесты",
                "Прочее",
                "Замечания ревью (claude-review-defects)",
            ],
        )

    def test_duplicates(self) -> None:
        self.assertEqual(
            self.mod.duplicates(self.t),
            [
                (["RP-1602", "RP-3001"], "одинаковый заголовок"),
                (["RP-2084", "RP-2093"], "связь «дубль»"),
            ],
        )

    def test_unclear_reasons(self) -> None:
        reason = {k: self.rules.unclear_reason(v) for k, v in self.t.items()}
        self.assertEqual(reason["RP-1040"], "needs-tests, устарело")
        self.assertEqual(reason["RP-987"], "исследование/проверка")
        self.assertEqual(reason["RP-286"], "расплывчатая постановка")
        self.assertEqual(reason["RP-2203"], "")

    def test_filter(self) -> None:
        f = self.mod.apply_filter
        self.assertEqual(sorted(f(self.t, "rp-1828")), ["RP-1828"])
        self.assertEqual(
            sorted(f(self.t, "записи")),
            ["RP-1828", "RP-2084", "RP-2093", "RP-2136", "RP-2140", "RP-2203"],
        )
        self.assertIn("RP-2044", f(self.t, "плагин duty"))

    def test_stale(self) -> None:
        report = self.mod.Report(self.t, self.rules, "")
        self.assertEqual(
            report.stale(dt.date(2026, 11, 20)),
            [
                ("RP-286", 50),
                ("RP-524", 20),
                ("RP-1389", 21),
                ("RP-1602", 21),
                ("RP-1775", 20),
            ],
        )
        self.assertEqual(report.stale(dt.date(2026, 10, 10)), [])

    def test_stale_age_from_status_entry(self) -> None:
        # RP-286 обновлён 2026-10-31, но в статусе с 2026-10-01.
        report = self.mod.Report(self.t, self.rules, "")
        days = dict(report.stale(dt.date(2026, 11, 20)))
        self.assertEqual(days["RP-286"], 50)
        self.assertEqual(dict(report.stale(dt.date(2026, 11, 9)))["RP-286"], 39)

    def test_default_jql(self) -> None:
        self.assertEqual(
            self.mod.default_jql(self.cfg),
            'project = "RP" AND resolution = Unresolved AND (assignee = currentUser()'
            ' OR (assignee is EMPTY AND (reporter = currentUser() OR labels in ("claude"))))',
        )
        self.assertEqual(
            self.mod.all_jql(self.cfg), 'project = "RP" AND resolution = Unresolved'
        )


class ScriptTest(Base):
    def test_without_jira_section(self) -> None:
        self.write_config("")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout, "в .duty/duty.toml нет раздела [jira]: тикеты не проверено\n"
        )

    def test_without_token_file(self) -> None:
        self.write_config('[jira]\nurl = "http://127.0.0.1:9"\n')
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("[jira]: token_file не задан — тикеты не проверено", r.stdout)

    def test_all_without_project(self) -> None:
        self.write_config('[jira]\nurl = "http://x"\n', "")
        r = self.run_script("all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[tickets]: project не задан", r.stdout)

    def test_report_from_sample(self) -> None:
        self.write_config('[jira]\nurl = "https://jira.example"\n')
        r = self.run_script("--from-json", str(SAMPLE), "--today", "2026-11-20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| Записи | 4 | 1 | 1 | 0 | 2 |", r.stdout)
        self.assertIn("| Всего | 29 |", r.stdout)
        self.assertIn("вероятных дублей 2", r.stdout)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        self.assertEqual(path.parent, self.root / "results" / "tickets")
        self.assertFalse(path.with_suffix(".json").exists())
        text = path.read_text(encoding="utf-8")
        heads = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(
            heads,
            [
                "## Записи (4)",
                "## Телефония (7)",
                "## Конвейер (1)",
                "## Интерфейс (4)",
                "## Производительность (1)",
                "## Тесты (2)",
                "## Прочее (6)",
                "## Замечания ревью (claude-review-defects) (4)",
                "## Сводка",
                "## Для обсуждения",
            ],
        )
        self.assertIn(
            "| [RP-2044](https://jira.example/browse/RP-2044) | Задача | Backlog |",
            text,
        )
        self.assertIn(
            "| Ключ | Тип | Статус | Назначен | Суть | Функционал находок |", text
        )
        self.assertIn("### Вероятные дубли", text)
        self.assertIn("### В «To Test» и «To Fix» дольше 14 дн.", text)
        self.assertIn(
            "RP-1775](https://jira.example/browse/RP-1775) To Fix, 20 дн.", text
        )
        # Внутри группы: по порядку статусов, затем по номеру убыванием.
        rec = text.split("## Записи (4)")[1].split("## Телефония")[0]
        keys = [
            line.split("[")[1].split("]")[0]
            for line in rec.splitlines()
            if line.startswith("| [")
        ]
        self.assertEqual(keys, ["RP-2203", "RP-2136", "RP-2093", "RP-2084"])

    def test_filter_argument(self) -> None:
        self.write_config('[jira]\nurl = "https://jira.example"\n')
        r = self.run_script("склейк", "--from-json", str(SAMPLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| Всего | 3 |", r.stdout)


class ReviewFixesTest(Base):
    """Неверные входы — одна строка без трассировки; отчёт не перезаписывается."""

    def one_ticket(self, **fields) -> Path:
        t = {
            "key": "RP-1",
            "summary": "Склейка | экран",
            "status": "To Test",
            "type": "Задача",
            "labels": [],
            "assignee": None,
            "updated": "2026-11-01",
            "desc": "",
            **fields,
        }
        path = self.root / "one.json"
        path.write_text(json.dumps({"RP-1": t}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_bad_pattern(self) -> None:
        bad = TICKETS_TOML.replace("'склейк|merge media|записи экран'", "'склейк('")
        self.write_config('[jira]\nurl = "https://jira.example"\n', bad)
        r = self.run_script("--from-json", str(SAMPLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("группа «Записи»: pattern", r.stdout)
        r = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "duty_config.py"), "check"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertIn("tickets.groups «Записи»: pattern", r.stdout + r.stderr)

    def test_pattern_ignores_case(self) -> None:
        upper = TICKETS_TOML.replace("'склейк|merge media|записи экран'", "'СКЛЕЙК'")
        self.write_config('[jira]\nurl = "https://jira.example"\n', upper)
        r = self.run_script("--from-json", str(self.one_ticket()))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| Записи | 1 |", r.stdout)

    def test_bad_today_and_export(self) -> None:
        self.write_config('[jira]\nurl = "https://jira.example"\n')
        r = self.run_script("--from-json", str(SAMPLE), "--today", "2026/11/20")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ГГГГ-ММ-ДД", r.stderr)
        bad = self.root / "bad.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        r = self.run_script("--from-json", str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn("выгрузка", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("Jira недоступна", r.stdout)

    def test_cells_escaped_and_no_overwrite(self) -> None:
        self.write_config('[jira]\nurl = "https://jira.example"\n')
        one = self.one_ticket(status="To | Test", assignee="Иванов | Иван")
        paths = []
        for _ in range(2):
            r = self.run_script("--from-json", str(one))
            self.assertEqual(r.returncode, 0, r.stderr)
            paths.append(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        self.assertNotEqual(paths[0], paths[1])
        text = Path(paths[1]).read_text(encoding="utf-8")
        row = next(x for x in text.splitlines() if x.startswith("| [RP-1]"))
        self.assertEqual(row.count("|"), 6, row)

    def test_title_and_link_pair_counted_once(self) -> None:
        mod = load_module()
        t = {
            "RP-1": {"summary": "Один и тот же", "dup_links": ["RP-2"]},
            "RP-2": {"summary": "Один  и тот же!", "dup_links": ["RP-1"]},
        }
        self.assertEqual(
            mod.duplicates(t), [(["RP-1", "RP-2"], "одинаковый заголовок")]
        )


class FakeJira(BaseHTTPRequestHandler):
    pages: ClassVar[list[tuple[int, dict]]] = []
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        FakeJira.seen.append(self.path + " " + self.headers.get("Authorization", ""))
        code, body = FakeJira.pages[min(len(FakeJira.seen), len(FakeJira.pages)) - 1]
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def issue(key: str, summary: str) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": "Backlog"},
            "issuetype": {"name": "Задача"},
            "labels": [],
            "assignee": None,
            "updated": "2026-11-01T10:00:00.000+0300",
            "description": None,
            "issuelinks": [
                {"type": {"name": "Duplicate"}, "outwardIssue": {"key": "RP-2"}}
            ]
            if key == "RP-1"
            else [],
        },
    }


class JiraTest(Base):
    def serve(self, pages: list[tuple[int, dict]]) -> None:
        FakeJira.pages = pages
        FakeJira.seen = []
        server = HTTPServer(("127.0.0.1", 0), FakeJira)
        self.port = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.root / "token").write_text("secret-token\n", encoding="utf-8")
        self.write_config(
            f'[jira]\nurl = "http://127.0.0.1:{server.server_port}"\ntoken_file = "token"\n'
        )

    def test_denied_stops_without_retry(self) -> None:
        self.serve([(401, {})])
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("ОСТАНОВКА: Jira ответил 401", r.stdout)
        self.assertEqual(len(FakeJira.seen), 1)
        self.assertNotIn("secret-token", r.stdout + r.stderr)

    def test_pages_and_export(self) -> None:
        self.serve(
            [
                (200, {"total": 2, "issues": [issue("RP-1", "Первый")]}),
                (200, {"total": 2, "issues": [issue("RP-2", "Второй")]}),
            ]
        )
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeJira.seen), 2)
        self.assertIn("startAt=1", FakeJira.seen[1])
        self.assertTrue(FakeJira.seen[0].endswith("Bearer secret-token"))
        self.assertIn("вероятных дублей 1", r.stdout)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(export), ["RP-1", "RP-2"])
        self.assertEqual(export["RP-1"]["dup_links"], ["RP-2"])
        self.assertNotIn("secret-token", r.stdout + r.stderr)

    def test_duplicate_link_types_from_config(self) -> None:
        item = issue("RP-1", "Первый")
        item["fields"]["issuelinks"][0]["type"]["name"] = "Relates"
        self.serve([(200, {"total": 1, "issues": [item]})])
        self.write_config(
            f'[jira]\nurl = "http://127.0.0.1:{self.port}"\ntoken_file = "token"\n',
            TICKETS_TOML.replace(
                "[tickets]\n", '[tickets]\nduplicate_links = ["Relates"]\n'
            ),
        )
        r = self.run_script("--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-1"]["dup_links"], ["RP-2"])

    def test_default_link_type_ignores_other_types(self) -> None:
        item = issue("RP-1", "Первый")
        item["fields"]["issuelinks"][0]["type"]["name"] = "Relates"
        self.serve([(200, {"total": 1, "issues": [item]})])
        r = self.run_script()
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-1"]["dup_links"], [])

    def test_status_since_from_changelog(self) -> None:
        item = issue("RP-1", "Первый")
        item["fields"]["created"] = "2026-09-01T10:00:00.000+0300"
        item["changelog"] = {
            "histories": [
                {
                    "created": "2026-09-05T10:00:00.000+0300",
                    "items": [{"field": "status"}],
                },
                {
                    "created": "2026-10-10T10:00:00.000+0300",
                    "items": [{"field": "status"}],
                },
                {
                    "created": "2026-10-30T10:00:00.000+0300",
                    "items": [{"field": "labels"}],
                },
            ]
        }
        mod = load_module()
        self.assertEqual(mod.normalize(item, [])["status_since"], "2026-10-10")
        item["changelog"] = {}
        self.assertEqual(mod.normalize(item, [])["status_since"], "2026-09-01")

    def test_changelog_not_requested_without_check_statuses(self) -> None:
        self.serve([(200, {"total": 1, "issues": [issue("RP-1", "Первый")]})])
        self.run_script()
        self.assertEqual(len(FakeJira.seen), 1)
        self.assertNotIn("expand", FakeJira.seen[0])

    def test_history_400_falls_back_to_created(self) -> None:
        """400 на пакет и на одиночный запрос — возраст по `created` и предупреждение."""
        waiting = issue("RP-2", "Второй")
        waiting["fields"]["status"] = {"name": "To Test"}
        waiting["fields"]["created"] = "2026-09-01T10:00:00.000+0300"
        self.serve(
            [
                (200, {"total": 1, "issues": [waiting]}),
                (400, {"errorMessages": ["Issue does not exist"]}),
            ]
        )
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        # основной запрос и пакет из одного ключа с 400: повторять нечего
        self.assertEqual(len(FakeJira.seen), 2)
        self.assertIn("400 на историю RP-2", r.stderr)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-2"]["status_since"], "2026-09-01")

    def test_history_400_batch_retries_per_key(self) -> None:
        """Один ключ пакета даёт 400: остальные получают свою историю, в stderr предупреждение."""

        def waiting(key: str) -> dict:
            item = issue(key, key)
            item["fields"]["status"] = {"name": "To Test"}
            item["fields"]["created"] = "2026-09-01T10:00:00.000+0300"
            return item

        good = waiting("RP-2")
        good["changelog"] = {
            "histories": [
                {
                    "created": "2026-10-10T10:00:00.000+0300",
                    "items": [{"field": "status"}],
                }
            ]
        }
        self.serve(
            [
                (200, {"total": 2, "issues": [waiting("RP-2"), waiting("RP-3")]}),
                (400, {"errorMessages": ["Issue does not exist"]}),
                (200, {"total": 1, "issues": [good]}),
                (400, {"errorMessages": ["Issue does not exist"]}),
            ]
        )
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeJira.seen), 4)
        self.assertIn("RP-2%2C+RP-3", FakeJira.seen[1])
        self.assertIn("RP-2", FakeJira.seen[2])
        self.assertNotIn("RP-3", FakeJira.seen[2])
        self.assertIn("400 на историю пакета (RP-2, RP-3)", r.stderr)
        self.assertIn("400 на историю RP-3", r.stderr)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-2"]["status_since"], "2026-10-10")
        self.assertEqual(export["RP-3"]["status_since"], "2026-09-01")

    def test_history_400_from_request_stops_retry(self) -> None:
        """400 на каждый запрос истории: после двух одиночных повтор прекращается."""
        items = []
        for n in range(2, 6):
            item = issue(f"RP-{n}", f"Т{n}")
            item["fields"]["status"] = {"name": "To Test"}
            item["fields"]["created"] = "2026-09-01T10:00:00.000+0300"
            items.append(item)
        bad = (400, {"errorMessages": ["Bad request"]})
        self.serve([(200, {"total": 4, "issues": items}), bad, bad, bad])
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        # основной запрос, пакет и два одиночных: остальные ключи не запрашиваются
        self.assertEqual(len(FakeJira.seen), 4)
        self.assertIn("повтор прекращён", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 2, r.stderr)
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-5"]["status_since"], "2026-09-01")

    def test_history_403_still_stops(self) -> None:
        waiting = issue("RP-2", "Второй")
        waiting["fields"]["status"] = {"name": "To Test"}
        self.serve([(200, {"total": 1, "issues": [waiting]}), (403, {})])
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("ОСТАНОВКА: Jira ответил 403", r.stdout)

    def test_changelog_only_for_tickets_in_check_statuses(self) -> None:
        # История нужна только для возраста в статусе проверки: основной запрос
        # идёт без expand, история запрашивается по ключам тикетов в статусе
        # из check_statuses.
        waiting = issue("RP-2", "Второй")
        waiting["fields"]["status"] = {"name": "To Test"}
        waiting["fields"]["created"] = "2026-09-01T10:00:00.000+0300"
        with_history = {
            **waiting,
            "changelog": {
                "histories": [
                    {
                        "created": "2026-10-10T10:00:00.000+0300",
                        "items": [{"field": "status"}],
                    }
                ]
            },
        }
        self.serve(
            [
                (200, {"total": 2, "issues": [issue("RP-1", "Первый"), waiting]}),
                (200, {"total": 1, "issues": [with_history]}),
            ]
        )
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeJira.seen), 2)
        self.assertNotIn("expand", FakeJira.seen[0])
        self.assertIn("expand=changelog", FakeJira.seen[1])
        self.assertIn("RP-2", FakeJira.seen[1])
        self.assertNotIn("RP-1", FakeJira.seen[1])
        path = Path(r.stdout.strip().splitlines()[-1].removeprefix("Отчёт: "))
        export = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(export["RP-2"]["status_since"], "2026-10-10")


if __name__ == "__main__":
    unittest.main()
