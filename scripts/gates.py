"""Сводка открытых pull request'ов пользователя: сборки, ворота Jenkins, готовность к слиянию.

Только чтение. Токены читаются из файлов ~/.config/axata/ и никуда не выводятся.
Ответ 401 или 403 от Bitbucket останавливает работу без повторов: четыре
неверных входа блокируют учётную запись до ввода CAPTCHA.
"""

import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BB = "https://git.service-desk.site"
CFG = Path.home() / ".config" / "axata"
# Jenkins отдаёт сертификат внутреннего центра, поэтому проверка выключена,
# как и в curl -k из docs/dev-contour-deploy-check.md.
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


def gate_summary(statuses: list[dict], env: dict[str, str]) -> tuple[str, str]:
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


def main() -> int:
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
