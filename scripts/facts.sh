#!/usr/bin/env bash
# Сырые факты для /duty:inspect: короткие разделы, каждый обрезан head.
# Только чтение: git без fetch, REST только GET; токены не выводятся.
# Аргумент — корень монорепозитория (по умолчанию текущий каталог).

set -uo pipefail
root=${1:-$PWD}
cfg=$HOME/.config/axata
plugin=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

section() { printf '\n## %s\n' "$1"; }

section 'Деревья (git status)'
git -C "$root" status --short --branch | head -15
git -C "$root" submodule foreach --quiet \
    'printf "%s: %s\n" "$sm_path" "$(git status --short --branch | head -6 | tr "\n" ";")"'

section 'Worktree сабмодулей (кроме основного)'
git -C "$root" submodule foreach --quiet \
    'git worktree list | tail -n +2 | sed "s|^|$sm_path: |" | head -12'

section 'Свои ветви в origin (dev2-RP-*, feature/*), без fetch'
git -C "$root" submodule foreach --quiet \
    'git for-each-ref --format="%(refname:short) %(committerdate:relative)" "refs/remotes/origin/dev2-RP-*" "refs/remotes/origin/feature/*" | sed "s|^|$sm_path: |" | head -10'

section 'Открытые pull request'"'"'ы (Bitbucket, Jenkins)'
python3 -I "$plugin/scripts/gates.py" | head -40

section 'Jenkins: очередь и занятые исполнители'
set -a; . "$cfg/jenkins-credentials.env"; set +a
J() { curl -sk -g --max-time 20 -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/$1"; }
J 'queue/api/json?tree=items[task[name],why,inQueueSince]' | python3 -I -c '
import json, sys
items = json.load(sys.stdin)["items"]
print(f"в очереди: {len(items)}")
for i in items[:10]:
    print("-", i["task"].get("name"), "|", (i.get("why") or "")[:100])'
J 'computer/api/json?tree=computer[displayName,offline,numExecutors,executors[currentExecutable[url]],oneOffExecutors[currentExecutable[url]]]' | python3 -I -c '
import json, sys
for c in json.load(sys.stdin)["computer"]:
    busy = [e["currentExecutable"]["url"] for e in c["executors"] + c["oneOffExecutors"] if e.get("currentExecutable")]
    n = c["numExecutors"]
    state = "вне сети" if c["offline"] else f"занято {len(busy)}/{n}"
    print("-", c["displayName"], state, " ".join(u.split("/job/", 1)[-1].replace("/job/", "/") for u in busy)[:200])'

section 'Машина'
cat /proc/loadavg; nproc; free -g | head -2; df -h /tmp | tail -1

section 'Занятость'
python3 -I "$plugin/scripts/busy.py" list | head -30

section 'Вопросы агентов без ответа'
python3 -I "$plugin/scripts/ask.py" list --open | head -20

section 'Текущая работа (todo.local.md)'
# Снимок очереди — местный файл машины (в .gitignore).
todo="$root/todo.local.md"
if [ -f "$todo" ]; then sed -n '/^## Текущая работа/,/^## /p' "$todo" | head -120; else echo 'снимка нет'; fi

section 'Открытые задачи OpenSpec'
for f in "$root"/openspec/changes/*/tasks.md "$root"/legacy/queue-manager-service/openspec/changes/*/tasks.md; do
    [ -f "$f" ] || continue
    n=$(grep -c '^\s*- \[ \]' "$f")
    [ "$n" -gt 0 ] && printf '%s: открыто %s; первая: %s\n' "${f#$root/}" "$n" \
        "$(grep -m1 '^\s*- \[ \]' "$f" | cut -c1-120)"
done

section 'Отложенные вопросы: условия пересмотра'
sed -n '/^| Файл/,/^$/p' "$root/docs/open-questions/README.md" | cut -c1-260 | head -30

section 'Jira: тикеты на пользователе в работе'
curl -s --max-time 20 -H "Authorization: Bearer $(cat "$cfg/jira-pat")" \
    'https://jira.service-desk.site/rest/api/2/search?jql=assignee%3DcurrentUser()%20AND%20statusCategory%3D%22In%20Progress%22&fields=summary,status&maxResults=30' |
    python3 -I -c '
import json, sys
d = json.load(sys.stdin)
for i in d.get("issues", []):
    print("-", i["key"], i["fields"]["status"]["name"], "|", i["fields"]["summary"][:90])
print("всего:", d.get("total"))'

section 'Неделя в цифрах'
python3 -I "$plugin/scripts/week-stats.py" --root "$root" | head -20
