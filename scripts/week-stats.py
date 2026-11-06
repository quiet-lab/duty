#!/usr/bin/env python3
"""Неделя в цифрах: показатели работы за период для /duty:inspect.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Показатели: время жизни набора (pull request'ы репозитория наборов
`bitbucket.sets_repo` от создания до слияния), занятость Jenkins и доля случайных падений, вопросов агентов
в день, новых записей решений, воспроизведённых ломающих находок ревью,
доля моделей в коммитах и доля коммитов в код и тесты. Период — `--days`
суток до текущего момента (по умолчанию 7). Порогов и действий нет: только
числа.

Только чтение: git без fetch, REST только GET. Адреса, файлы доступа, классы
путей и каталог записей решений берутся из `.duty/duty.toml` (`[bitbucket]`,
`[jenkins]`, `[stats]`, `[docs]`, `[paths]`); токены читаются из файлов и не
выводятся; ответ 401 или 403 Bitbucket останавливает обращения к нему без
повторов (`gates.py`). Недоступный источник и отсутствующий раздел даёт
в строке «не получено» или «не считалось» с причиной, остальные строки
считаются.
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask
import gates

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
REVIEW_STATS = Path.home() / ".claude" / "stats" / "review-rounds.tsv"
# Форма строки статистики ревью:
# дата, проект, задача, модель исполнителя, уровень, находок всего,
# воспроизведено ломающих, исправлено, тикет.
REVIEW_COLUMNS = 9
MODEL_RE = re.compile(
    r"^Co-Authored-By:\s*Claude\s+(\w+)", re.IGNORECASE | re.MULTILINE
)
BUILD_TREE = (
    "number,url,result,timestamp,duration,building,actions[lastBuiltRevision[SHA1]]"
)


def record_re(decisions: str) -> re.Pattern[str]:
    """Путь записи решения в каталоге `docs.decisions`: `[NN-тема/]NNNN-имя.md`."""
    return re.compile(
        rf"(^|/){re.escape(decisions.strip('/'))}/(\d{{2}}-[^/]+/)?\d{{4}}-[^/]+\.md$"
    )


def run_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else ""


def repos(root: Path) -> list[Path]:
    """Монорепозиторий и выписанные сабмодули из `.gitmodules`."""
    found = [root]
    for line in run_git(
        root, "config", "-f", ".gitmodules", "--get-regexp", r"\.path$"
    ).splitlines():
        path = root / line.split(" ", 1)[1]
        if (path / ".git").exists():
            found.append(path)
    return found


def pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f} %" if whole else "—"


def hours(ms: float) -> str:
    return f"{ms / HOUR_MS:.1f} ч".replace(".", ",")


# --- Наборы ------------------------------------------------------------------


def set_lifetimes(root: Path, since_ms: int, now_ms: int) -> str:
    sets_repo = gates.config().get("bitbucket.sets_repo", "")
    if not sets_repo:
        return "наборов нет: sets_repo не задан"
    url = run_git(root / sets_repo, "remote", "get-url", "origin").strip()
    m = re.search(r"/scm/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return f"не получено: нет origin у {sets_repo}"
    base = (
        f"/rest/api/1.0/projects/{m.group(1).upper()}/repos/{m.group(2)}/pull-requests"
    )
    prs: list[dict] = []
    start = 0
    while True:
        page = gates.bb(f"{base}?state=ALL&order=NEWEST&limit=100&start={start}")
        values = page.get("values", [])
        prs += values
        if page.get("isLastPage", True) or (
            values and values[-1]["updatedDate"] < since_ms
        ):
            break
        start = page["nextPageStart"]
    merged, declined, open_age = [], 0, []
    for pr in prs:
        closed = pr.get("closedDate") or 0
        if pr["state"] == "OPEN":
            open_age.append(now_ms - pr["createdDate"])
        elif closed >= since_ms and pr["state"] == "MERGED":
            merged.append(closed - pr["createdDate"])
        elif closed >= since_ms:
            declined += 1
    if not merged and not open_age and not declined:
        return "наборов нет"
    parts = []
    if merged:
        parts.append(
            f"слито {len(merged)}: медиана {hours(statistics.median(merged))}, "
            f"наибольшее {hours(max(merged))}"
        )
    if declined:
        parts.append(f"отклонено {declined}")
    if open_age:
        parts.append(f"открыто {len(open_age)}, самый старый {hours(max(open_age))}")
    return "; ".join(parts)


# --- Jenkins -----------------------------------------------------------------


def jenkins_builds(env: dict[str, str], since_ms: int) -> list[dict]:
    """Сборки всех заданий с началом в периоде, кроме проверочных (`jenkins.fake_job`)."""
    fake = gates.config().get("jenkins.fake_job", "")
    out: list[dict] = []

    def walk(url: str, path: list[str]) -> None:
        tree = urllib.parse.quote("_class,jobs[name,url]", safe=",[]")
        d = gates.jenkins(url.rstrip("/") + f"/api/json?tree={tree}", env)
        if d is None:
            raise ConnectionError(f"Jenkins не ответил на {url}")
        if d.get("jobs") is not None:
            for job in d["jobs"]:
                walk(job["url"], [*path, job["name"]])
            return
        name = "/".join(path)
        if fake and fake in name:
            return
        tree = urllib.parse.quote(f"allBuilds[{BUILD_TREE}]{{0,200}}", safe=",[]{}")
        b = gates.jenkins(url.rstrip("/") + f"/api/json?tree={tree}", env) or {}
        for build in b.get("allBuilds", []):
            if build["timestamp"] >= since_ms:
                build["job"] = name
                out.append(build)

    if not env.get("JENKINS_URL"):
        raise ConnectionError("в файле доступа Jenkins нет JENKINS_URL")
    walk(env["JENKINS_URL"], [])
    return out


def revision(build: dict) -> str:
    for action in build.get("actions") or []:
        sha = ((action or {}).get("lastBuiltRevision") or {}).get("SHA1")
        if sha:
            return sha
    return ""


def busy_ms(builds: list[dict], since_ms: int, now_ms: int) -> int:
    """Время, когда шла хотя бы одна сборка (объединение отрезков)."""
    spans = sorted(
        (
            max(b["timestamp"], since_ms),
            now_ms
            if b.get("building")
            else min(b["timestamp"] + b["duration"], now_ms),
        )
        for b in builds
    )
    total, end = 0, since_ms
    for start, stop in spans:
        start = max(start, end)
        if stop > start:
            total += stop - start
            end = stop
    return total


def random_failures(builds: list[dict]) -> tuple[int, int]:
    """Падения и из них случайные: на той же ревизии того же задания позже была SUCCESS."""
    ok = {
        (b["job"], revision(b), b["number"])
        for b in builds
        if b["result"] == "SUCCESS" and revision(b)
    }
    failures = [b for b in builds if b["result"] == "FAILURE"]
    rand = sum(
        1
        for b in failures
        if revision(b)
        and any(
            job == b["job"] and sha == revision(b) and n > b["number"]
            for job, sha, n in ok
        )
    )
    return len(failures), rand


FAILURE_CLASSES = [
    (
        "конфигурация заданий и конвейера",
        re.compile(
            r"Invalid branch|canonicalization|Unable to find project|No item named|не пройдены на этапе «Тикет»"
        ),
    ),
    (
        "инфраструктура узла",
        re.compile(
            r"No space left|ENOSPC|Connection refused|Could not resolve|ECONNRESET"
        ),
    ),
    ("конфликт ветви", re.compile(r"not merge cleanly|не сливается с|изменилась")),
    ("следствие ворот участника", re.compile(r"ворота .* не пройдены")),
    ("истёк срок", re.compile(r"timed out|Timeout|истёк срок")),
]


def failure_classes(builds: list[dict], env: dict[str, str]) -> Counter:
    """Классы падений по сообщениям упавших этапов `wfapi/describe`."""
    failures = [b for b in builds if b["result"] == "FAILURE"]

    def classify(build: dict) -> str:
        d = gates.jenkins(build["url"].rstrip("/") + "/wfapi/describe", env) or {}
        text = " ".join(
            f"{s.get('name', '')} {(s.get('error') or {}).get('message', '')}"
            for s in d.get("stages", [])
            if s.get("status") == "FAILED"
        )
        for name, rx in FAILURE_CLASSES:
            if rx.search(text):
                return name
        return "проверки и тесты"

    with ThreadPoolExecutor(8) as pool:
        return Counter(pool.map(classify, failures))


# --- Git ---------------------------------------------------------------------


def classify_path(repo_name: str, path: str) -> str:
    """Класс пути коммита: код, тесты, документация, правила, конвейер."""
    cfg = gates.config()
    rules_paths = tuple(cfg.get("stats.rules_paths", []))
    pipeline_paths = tuple(cfg.get("stats.pipeline_paths", []))
    test_repos = cfg.get("stats.test_repos", [])
    base = path.rsplit("/", 1)[-1]
    if (
        (repo_name != "." and f"{repo_name}/".startswith(rules_paths))
        or path.startswith(rules_paths)
        or base in ("CLAUDE.md", "AGENTS.md")
    ):
        return "правила"
    if (
        base.startswith("Jenkinsfile")
        or base in ("justfile", "Justfile")
        or path.endswith(".just")
        or path.startswith(pipeline_paths)
    ):
        return "конвейер"
    if (
        path.startswith(("docs/", "openspec/"))
        or "/openspec/" in path
        or "/docs/" in path
        or path.endswith(".md")
    ):
        return "документация"
    if (
        repo_name in test_repos
        or re.search(r"(^|/)(tests?|e2e|__tests__|devlab)/", path)
        or re.search(r"(\.spec\.|\.test\.|test_[^/]*\.py$|playwright\.config)", path)
    ):
        return "тесты"
    return "код"


def git_stats(root: Path, since_ms: int) -> dict:
    """Доли моделей и классов по коммитам периода без слияний во всех репозиториях."""
    since = f"@{since_ms // 1000}"
    models: Counter = Counter()
    primary: Counter = Counter()
    seen: set[str] = set()
    for repo in repos(root):
        name = "." if repo == root else str(repo.relative_to(root))
        log = run_git(
            repo,
            "log",
            "--all",
            "--no-merges",
            f"--since={since}",
            "--no-renames",
            "--numstat",
            "--format=%x1e%H%x00%B%x00",
        )
        for chunk in log.split("\x1e")[1:]:
            sha, body, files = (chunk.split("\x00") + ["", ""])[:3]
            if sha in seen:
                continue
            seen.add(sha)
            m = MODEL_RE.search(body)
            models[m.group(1).capitalize() if m else "без подписи Claude"] += 1
            weight: Counter = Counter()
            for line in files.strip().splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    added = 0 if parts[0] == "-" else int(parts[0])
                    removed = 0 if parts[1] == "-" else int(parts[1])
                    # Указатели сабмодулей в корневом репозитории — не код.
                    if name == "." and (root / parts[2] / ".git").exists():
                        weight["указатели"] += 1
                    else:
                        weight[classify_path(name, parts[2])] += added + removed + 1
            primary[weight.most_common(1)[0][0] if weight else "пусто"] += 1
    return {"commits": len(seen), "models": models, "primary": primary}


def new_records(root: Path, since_ms: int) -> int:
    decisions = gates.config().get("docs.decisions", "")
    if not decisions:
        raise ValueError("docs.decisions не задан")
    log = run_git(
        root,
        "log",
        f"--since=@{since_ms // 1000}",
        "--diff-filter=A",
        "-M",
        "--format=",
        "--name-only",
        "--",
        decisions,
    )
    rx = record_re(decisions)
    return sum(1 for line in log.splitlines() if rx.search(line))


# --- Вопросы и ревью ---------------------------------------------------------


def questions_per_day(results: Path, since_ms: int, days: int) -> str:
    rows = [r for r in ask.collect(results) if r["created_ms"] >= since_ms]
    if not rows:
        return "0"
    answered = sum(1 for r in rows if r["answered"])
    return f"{len(rows) / days:.1f} (всего {len(rows)}, отвечено {answered})".replace(
        ".", ",", 1
    )


def review_stats(path: Path, since_ms: int) -> str:
    if not path.is_file():
        return f"не получено: нет {path}"
    since_day = time.strftime("%Y-%m-%d", time.gmtime(since_ms / 1000))
    reviews = found = reproduced = fixed = other_form = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", cols[0]) or cols[0] < since_day:
            continue
        nums = cols[5:8] if len(cols) >= REVIEW_COLUMNS else []
        if len(nums) != 3 or not all(n.strip().isdigit() for n in nums):
            other_form += 1
            continue
        reviews += 1
        found += int(nums[0])
        reproduced += int(nums[1])
        fixed += int(nums[2])
    text = f"ревью {reviews}: находок {found}, воспроизведено ломающих {reproduced}, исправлено {fixed}"
    if other_form:
        text += f"; строк другой формы пропущено: {other_form}"
    return text


# --- Сборка ------------------------------------------------------------------


def collect(args: argparse.Namespace) -> list[tuple[str, str]]:
    now = time.time_ns() // 1_000_000
    since = now - args.days * DAY_MS
    cfg = gates.config()
    root = cfg.root
    rows: list[tuple[str, str]] = []
    absent = lambda section: f"не считалось: в .duty/duty.toml нет раздела [{section}]"

    def guarded(label: str, fn) -> None:
        try:
            rows.append((label, fn()))
        except gates.NotConfigured as e:
            rows.append((label, f"не считалось: {e}"))
        except gates.BitbucketDenied as e:
            rows.append((label, f"не получено: {e}; повтор не выполняется"))
        except (OSError, ValueError, KeyError, ConnectionError) as e:
            rows.append((label, f"не получено: {type(e).__name__}: {e}"[:200]))

    if args.offline:
        rows.append(("Время жизни набора", "не считалось (--offline)"))
        rows.append(("Занятость Jenkins", "не считалось (--offline)"))
    else:
        if cfg.has("bitbucket"):
            guarded("Время жизни набора", lambda: set_lifetimes(root, since, now))
        else:
            rows.append(("Время жизни набора", absent("bitbucket")))
        if not cfg.has("jenkins"):
            rows.append(("Занятость Jenkins", absent("jenkins")))
        else:
            try:
                env = gates.jenkins_env()
                builds = jenkins_builds(env, since)
                busy = busy_ms(builds, since, now)
                failed, rand = random_failures(builds)
                rows.append(
                    (
                        "Занятость Jenkins",
                        f"{hours(busy)} из {hours(now - since)} ({pct(busy, now - since)}), сборок {len(builds)}",
                    )
                )
                classes = failure_classes(builds, env)
                listed = ", ".join(f"{k} {v}" for k, v in classes.most_common())
                rows.append(
                    (
                        "Случайные падения",
                        (
                            f"{rand} из {failed} падений ({pct(rand, failed)}) "
                            f"прошли повтором на той же ревизии; классы: {listed or '—'}"
                        ),
                    )
                )
            except gates.NotConfigured as e:
                rows.append(("Занятость Jenkins", f"не считалось: {e}"))
            except (OSError, ValueError, KeyError, ConnectionError) as e:
                rows.append(
                    ("Занятость Jenkins", f"не получено: {type(e).__name__}: {e}"[:200])
                )
    guarded(
        "Вопросов агентов в день",
        lambda: questions_per_day(ask.results_dir(args.results), since, args.days),
    )
    if cfg.get("docs.decisions", ""):
        guarded("Новых записей решений", lambda: str(new_records(root, since)))
    else:
        rows.append(("Новых записей решений", "не считалось: docs.decisions не задан"))
    guarded("Ревью", lambda: review_stats(Path(args.review_stats), since))
    stats = git_stats(root, since)
    total = stats["commits"]
    rows.append(
        (
            "Доля моделей в коммитах",
            ", ".join(f"{k} {pct(v, total)}" for k, v in stats["models"].most_common())
            or "коммитов нет",
        )
    )
    code = stats["primary"]["код"] + stats["primary"]["тесты"]
    rows.append(
        (
            "Коммиты в код и тесты",
            f"{code} из {total} ({pct(code, total)}); "
            + ", ".join(f"{k} {v}" for k, v in stats["primary"].most_common()),
        )
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--days", type=int, default=7, help="длина периода в сутках до текущего момента"
    )
    p.add_argument(
        "--root",
        help="корень проекта; по умолчанию его находит duty_config.py",
    )
    p.add_argument(
        "--results",
        help="каталог результатов задач (вопросы); по умолчанию как у ask.py",
    )
    p.add_argument(
        "--review-stats", default=str(REVIEW_STATS), help="файл статистики ревью"
    )
    p.add_argument("--offline", action="store_true", help="без Bitbucket и Jenkins")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.days < 1:
        p.error("--days не меньше 1")
    try:
        cfg = gates.config(args.root)
    except gates.duty_config.ConfigError as e:
        print(f"ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    if cfg.root is None:
        print(
            "корень проекта не найден: запустите из репозитория git или задайте --root",
            file=sys.stderr,
        )
        return 2
    rows = collect(args)
    if args.json:
        print(json.dumps(dict(rows), ensure_ascii=False))
        return 0
    print(f"Период: {args.days} сут. до текущего момента.\n")
    print("| Показатель | Значение |\n|---|---|")
    for label, value in rows:
        print(f"| {label} | {gates.cell(value)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
