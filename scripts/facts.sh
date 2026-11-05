#!/usr/bin/env bash
# Сырые факты для /duty:inspect: короткие разделы, каждый обрезан head.
# Только чтение: git без fetch, REST только GET; токены не выводятся.
# Аргумент — корень проекта (по умолчанию его находит duty_config.py).
# Разделы Jenkins, Jira и pull request'ов выводятся при наличии одноимённых
# таблиц в .duty/duty.toml; проектные разделы выдают расширения
# .duty/facts.d/* (reference/config.md).

set -uo pipefail
plugin=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cfgpy="$plugin/scripts/duty_config.py"
root=${1:-$(python3 -I "$cfgpy" root)}
if [ -z "$root" ]; then
    echo 'корень проекта не найден: запустите из репозитория git или передайте корень аргументом' >&2
    exit 2
fi
# Остальные сценарии находят ту же конфигурацию по этой переменной.
export DUTY_PROJECT="$root"

section() { printf '\n## %s\n' "$1"; }
cfgget() { python3 -I "$cfgpy" get "$1" --root "$root" 2>/dev/null; }
# Путь файла из конфигурации: ~ раскрывается, относительный — от корня.
cfgfile() {
    local f
    f=$(cfgget "$1")
    [ -n "$f" ] || return 1
    f=${f/#\~/$HOME}
    case $f in /*) printf '%s\n' "$f" ;; *) printf '%s\n' "$root/$f" ;; esac
}
absent() { echo "раздела [$1] в .duty/duty.toml нет: не проверено"; }

section 'Деревья (git status)'
git -C "$root" status --short --branch | head -15
git -C "$root" submodule foreach --quiet \
    'printf "%s: %s\n" "$sm_path" "$(git status --short --branch | head -6 | tr "\n" ";")"'

section 'Worktree сабмодулей (кроме основного)'
git -C "$root" submodule foreach --quiet \
    'git worktree list | tail -n +2 | sed "s|^|$sm_path: |" | head -12'

prefixes=$(python3 -I -c '
import json, sys
print("\n".join(json.loads(sys.argv[1] or "[]")))' "$(cfgget git.task_prefixes)")
shown=$(printf '%s' "$prefixes" | paste -sd' ' -)
section "Свои ветви в origin (${shown:-git.task_prefixes не задан}), без fetch"
if [ -n "$prefixes" ]; then
    # Образцы идут по одному на строку и передаются аргументами xargs: слова
    # не разбиваются, `*` раскрывает только git for-each-ref.
    patterns=$(printf '%s\n' "$prefixes" | sed 's|^|refs/remotes/origin/|; s|$|*|')
    export patterns
    git -C "$root" submodule foreach --quiet \
        'printf "%s\n" "$patterns" | xargs -d "\n" git for-each-ref --format="%(refname:short) %(committerdate:relative)" | sed "s|^|$sm_path: |" | head -10'
fi

section 'Открытые pull request'"'"'ы (Bitbucket, Jenkins)'
python3 -I "$plugin/scripts/gates.py" --root "$root" | head -40

section 'Jenkins: очередь и занятые исполнители'
if jenv=$(cfgfile jenkins.env_file); then
    set -a; . "$jenv"; set +a
    k=()
    [ "$(cfgget jenkins.insecure_tls)" = true ] && k=(-k)
    J() { curl -s "${k[@]}" -g --max-time 20 -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/$1"; }
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
else
    absent jenkins
fi

section 'Машина'
cat /proc/loadavg; nproc; free -g | head -2; df -h /tmp | tail -1

section 'Занятость'
python3 -I "$plugin/scripts/busy.py" list | head -30

section 'Вопросы агентов без ответа'
python3 -I "$plugin/scripts/ask.py" list --open | head -20

# Проектные разделы (очередь работы, спецификации, отложенные вопросы) выдают
# расширения .duty/facts.d/*: stdout как есть, сбой — строкой об ошибке.
python3 -I - "$plugin/scripts" "$root" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import dutylib

for name, out, error in dutylib.run_extensions(Path(sys.argv[2]), "facts", 60):
    if error:
        print(f"\n## {name}\nне получено: {error}")
    else:
        sys.stdout.write(out if out.startswith("\n") else "\n" + out)
PY

section 'Jira: тикеты на пользователе в работе'
if jurl=$(cfgget jira.url) && [ -n "$jurl" ] && jtok=$(cfgfile jira.token_file); then
    curl -s --max-time 20 -H "Authorization: Bearer $(cat "$jtok")" \
        "$jurl/rest/api/2/search?jql=assignee%3DcurrentUser()%20AND%20statusCategory%3D%22In%20Progress%22&fields=summary,status&maxResults=30" |
        python3 -I -c '
import json, sys
d = json.load(sys.stdin)
for i in d.get("issues", []):
    print("-", i["key"], i["fields"]["status"]["name"], "|", i["fields"]["summary"][:90])
print("всего:", d.get("total"))'
else
    absent jira
fi

section 'Повторы правил'
python3 -I "$plugin/scripts/rules-lint.py" repeats --root "$root" | tail -5

section 'Неделя в цифрах'
python3 -I "$plugin/scripts/week-stats.py" --root "$root" | head -20
