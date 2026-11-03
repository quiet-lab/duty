"""Сводка открытых pull request'ов пользователя: сборки, ворота Jenkins, готовность к слиянию.

Без аргументов — таблица pull request'ов. Аргумент `now` — что идёт сейчас:
исполнители Jenkins с очередью ожидания и агенты, сессии и отметки занятости
на станции.

Только чтение. Токены читаются из файлов ~/.config/axata/ и никуда не выводятся.
Ответ 401 или 403 от Bitbucket останавливает работу без повторов: четыре
неверных входа блокируют учётную запись до ввода CAPTCHA.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BB = "https://git.service-desk.site"
CFG = Path.home() / ".config" / "axata"
# Jenkins отдаёт сертификат внутреннего центра, поэтому проверка выключена,
# как и в curl -k из docs/rollout.md.
INSECURE = ssl._create_unverified_context()


class BitbucketDenied(Exception):
    pass


def bb(path: str) -> dict:
    token = (CFG / "bitbucket-http-token").read_text().strip()
    req = urllib.request.Request(BB + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BitbucketDenied(f"Bitbucket ответил {e.code} на {path}") from None
        raise


def jenkins_env() -> dict[str, str]:
    env = {}
    for line in (CFG / "jenkins-credentials.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def jenkins(url: str, env: dict[str, str]) -> dict | None:
    auth = base64.b64encode(f"{env['JENKINS_USER']}:{env['JENKINS_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=INSECURE) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def is_feed(status: dict) -> bool:
    """Статус «Лента сборок PR-…» (ключ `<префикс>pr-feed/<задание>-<номер>`).

    Лента показывает ход всех этапов и не решает о слиянии; со статусами ворот
    её смешивать нельзя: она новее и перекрыла бы итог ворот.
    """
    return "pr-feed/" in (status.get("key") or "") or (status.get("name") or "").startswith("Лента сборок PR-")


def gate_summary(statuses: list[dict], env: dict[str, str]) -> tuple[str, str]:
    statuses = [s for s in statuses if not is_feed(s)]
    if not statuses:
        return "нет сборок", "—"
    builds = ", ".join(f"{s['key']}: {s['state']}" for s in statuses)
    # Последняя по времени сборка — та, что решает о слиянии.
    last = max(statuses, key=lambda s: s.get("dateAdded", 0))
    url = last.get("url", "").removesuffix("display/redirect")
    if not url.startswith("http"):
        return builds, "—"
    d = jenkins(url.rstrip("/") + "/wfapi/describe", env)
    if d is None:
        return builds, f"[{last['key']}]({url}) недоступна"
    stages = " → ".join(f"{s['name']}:{s['status']}" for s in d.get("stages", []))
    return builds, f"[{d.get('name', '?')}]({url}) {d.get('status')}; {stages}"


def human(ms: float) -> str:
    """Длительность в миллисекундах: «3 ч 05 мин», «12 мин», «40 с»."""
    s = max(int(ms // 1000), 0)
    if s < 60:
        return f"{s} с"
    m = s // 60
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"


def cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip() or "—"


# Узлы Jenkins с исполнителями; сборка pipeline на узле видна как
# PlaceholderExecutable, у которого есть адрес запуска, но нет причин запуска,
# поэтому причины читаются у самой сборки.
EXEC_TREE = "idle,currentExecutable[url,fullDisplayName,timestamp]"


def jenkins_get(base: str, path: str, env: dict[str, str]) -> dict | None:
    return jenkins(base.rstrip("/") + path, env)


def run_info(url: str, base: str, env: dict[str, str]) -> tuple[str, str]:
    """Имя сборки и кто запустил: пользователь или причина запуска."""
    tree = "fullDisplayName,actions[causes[shortDescription,userId]]"
    d = jenkins(url.rstrip("/") + f"/api/json?tree={tree}", env) or {}
    causes = [c for a in d.get("actions", []) for c in a.get("causes", [])]
    who = "; ".join(dict.fromkeys(c.get("userId") or c.get("shortDescription", "?") for c in causes)) or "?"
    return d.get("fullDisplayName", ""), who


def jenkins_now(env: dict[str, str], now_ms: int) -> int:
    base = env.get("JENKINS_URL", "https://jen.service-desk.site/")
    comp = jenkins_get(
        base,
        "/computer/api/json?tree=computer[displayName,offline,temporarilyOffline,offlineCauseReason,"
        f"numExecutors,executors[{EXEC_TREE}],oneOffExecutors[{EXEC_TREE}]]",
        env,
    )
    print("## Исполнители Jenkins\n")
    if comp is None:
        print("Jenkins недоступен (computer/api/json не ответил).")
        return 1
    print("| Узел | Состояние | Задание и сборка | Идёт | Кто запустил |")
    print("|---|---|---|---|---|")
    for c in comp["computer"]:
        name = c["displayName"]
        busy = [e["currentExecutable"] for e in c["executors"] if e.get("currentExecutable")]
        flies = [e["currentExecutable"] for e in c["oneOffExecutors"] if e.get("currentExecutable")]
        if c["offline"]:
            why = c.get("offlineCauseReason") or "причина не названа"
            print(f"| {cell(name)} | не в сети: {cell(why)} | — | — | — |")
            continue
        if c["numExecutors"] == 0 and not flies:
            continue
        total = c["numExecutors"]
        if not busy and not flies:
            print(f"| {cell(name)} | свободен (0/{total}) | — | — | — |")
            continue
        state = f"занят ({len(busy)}/{total})"
        for ex, tag in [(b, "") for b in busy] + [(f, " (оркестрация)") for f in flies]:
            url = ex.get("url", "")
            full, who = run_info(url, base, env) if url.startswith("http") else ("", "?")
            title = full or ex.get("fullDisplayName") or "?"
            ts = ex.get("timestamp")
            took = human(now_ms - ts) if ts else "?"
            print(f"| {cell(name)} | {state if not tag else 'оркестрация'} | [{cell(title)}]({url}) | {took} | {cell(who)} |")
    q = jenkins_get(base, "/queue/api/json?tree=items[inQueueSince,why,task[name]]", env)
    if q is None:
        print("| Очередь ожидания | недоступна | — | — | — |")
    elif not q["items"]:
        print("| Очередь ожидания | пусто | — | — | — |")
    else:
        parts = [
            f"{i['task']['name']} ({human(now_ms - i['inQueueSince'])}: {cell(i.get('why', ''))})"
            for i in q["items"]
        ]
        print(f"| Очередь ожидания | {len(parts)} в очереди | {cell('; '.join(parts))} | — | — |")
    return 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def station_now(now_ms: int) -> None:
    print("\n## Агенты, сессии и занятость на станции\n")
    print("| Вид | Имя | Каталог | Статус | Идёт |")
    print("|---|---|---|---|---|")
    home = str(Path.home())
    short = lambda p: cell(str(p).replace(home, "~"))  # noqa: E731
    herdr: dict[str, dict] = {}
    herdr_note = ""
    if os.environ.get("HERDR_ENV") == "1":
        try:
            r = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=15)
            for a in json.loads(r.stdout)["result"]["agents"]:
                herdr[a.get("agent_session", {}).get("value", a["pane_id"])] = a
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            herdr_note = "herdr agent list не ответил"
    seen = set()
    sessions = []
    for f in sorted((Path.home() / ".claude" / "sessions").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and pid_alive(d.get("pid")):
            sessions.append(d)
    for d in sorted(sessions, key=lambda d: d.get("startedAt", 0)):
        sid = d.get("sessionId")
        h = herdr.get(sid)
        seen.add(sid)
        kind = f"сессия, herdr {h['workspace_id']}" if h else "сессия"
        name = d.get("name") or (h or {}).get("terminal_title_stripped") or sid
        status = (h or {}).get("agent_status") or d.get("status", "?")
        took = human(now_ms - d["startedAt"]) if d.get("startedAt") else "?"
        print(f"| {kind} | {cell(name)} | {short(d.get('cwd', '?'))} | {cell(status)} | {took} |")
    for sid, a in herdr.items():
        if sid in seen:
            continue
        name = a.get("name") or a.get("terminal_title_stripped") or a["pane_id"]
        print(f"| агент herdr {a['workspace_id']} | {cell(name)} | {short(a.get('cwd', '?'))} | {cell(a.get('agent_status'))} | ? |")
    busy_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve().parent / "ocp-dev-stand" / "busy"
    if not busy_dir.is_dir():
        busy_dir = Path.home() / "work/telecom/axata/ocp-dev-stand/busy"
    for f in sorted(busy_dir.glob("*.json")) if busy_dir.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        alive = pid_alive(d.get("pid"))
        state = "держится" if alive else "устарела (процесс не жив)"
        took = human(now_ms - d["since_ms"]) if d.get("since_ms") else "?"
        print(f"| занятость | {cell(d.get('resource', f.stem))}: {cell(d.get('holder'))} | {cell(d.get('note') or '')} | {state} | {took} |")
    if herdr_note:
        print(f"\nНе проверено: {herdr_note}.")
    elif os.environ.get("HERDR_ENV") != "1":
        print("\nНе проверено: herdr (HERDR_ENV не 1), показаны только сессии Claude Code.")


def now_main() -> int:
    now_ms = int(time.time() * 1000)
    rc = jenkins_now(jenkins_env(), now_ms)
    station_now(now_ms)
    return rc


def main() -> int:
    if sys.argv[1:2] == ["now"]:
        return now_main()
    env = jenkins_env()
    try:
        prs = bb("/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=AUTHOR&limit=100")["values"]
        now = time.time()
        print("| Репозиторий | PR | Ветвь → цель | Сборки коммита | Ворота Jenkins (итог; этапы) | Слияние | Возраст, ч |")
        print("|---|---|---|---|---|---|---|")
        for p in sorted(prs, key=lambda p: p["createdDate"]):
            repo = p["toRef"]["repository"]
            key, slug = repo["project"]["key"], repo["slug"]
            sha = p["fromRef"]["latestCommit"]
            statuses = bb(f"/rest/build-status/1.0/commits/{sha}")["values"]
            builds, gate = gate_summary(statuses, env)
            m = bb(f"/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests/{p['id']}/merge")
            if m.get("canMerge"):
                merge = "можно"
            elif m.get("conflicted"):
                merge = "конфликт"
            else:
                merge = "; ".join(v["summaryMessage"] for v in m.get("vetoes", [])) or "нельзя"
            link = p["links"]["self"][0]["href"]
            age = round((now - p["createdDate"] / 1000) / 3600, 1)
            print(
                f"| {key}/{slug} | [#{p['id']}]({link}) | {p['fromRef']['displayId']} → "
                f"{p['toRef']['displayId']} | {builds} | {gate} | {merge} | {age} |"
            )
        print(f"\nВсего открытых: {len(prs)}")
    except BitbucketDenied as e:
        print(f"ОСТАНОВКА: {e}. Повторять нельзя: четыре неверных входа блокируют учётную запись.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
