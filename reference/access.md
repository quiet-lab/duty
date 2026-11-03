# Доступы, REST и задания Jenkins

Значения токенов в вывод, отчёты и файлы не попадают: команды читают их
из файлов в момент вызова.

## Откуда берутся доступы

| Система | Базовый адрес | Файл | Как подставляется |
|---|---|---|---|
| Bitbucket Server 7.21 | `https://git.service-desk.site/rest/api/1.0`, статусы сборок — `https://git.service-desk.site/rest/build-status/1.0` | `~/.config/axata/bitbucket-http-token` | `-H "Authorization: Bearer $(cat ~/.config/axata/bitbucket-http-token)"` |
| Jenkins 2.528 | `$JENKINS_URL` (`https://jen.service-desk.site/`) | `~/.config/axata/jenkins-credentials.env`: `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN` | `set -a; . ~/.config/axata/jenkins-credentials.env; set +a`, затем `curl -sk -g -u "$JENKINS_USER:$JENKINS_TOKEN"` |
| Jenkins CLI (WebSocket `/cli/ws`, только `jwait.sh`) | `$JENKINS_CLI_URL`, по умолчанию `http://10.200.100.8:8080/` — контроллер напрямую: внешний nginx не передаёт `Upgrade` | тот же файл | Basic с теми же `JENKINS_USER`, `JENKINS_TOKEN` |
| Jira | `https://jira.service-desk.site/rest/api/2` | `~/.config/axata/jira-pat` | `-H "Authorization: Bearer $(cat ~/.config/axata/jira-pat)"` |

Учётная запись Bitbucket — `a.pinchuk`. **Ответ 401 или 403 от Bitbucket —
остановка без повтора**: четыре неверных входа блокируют учётную запись
до ввода CAPTCHA. Сценарии `scripts/` плагина останавливаются так же.

## Репозитории Bitbucket

Ключ проекта и слаг — из `url` в `.gitmodules` (`scm/<проект>/<слаг>.git`,
ключ — прописными); родитель — ключ `parent`, без него — `branch`.

| Путь | Проект/слаг | Родитель | Задание ворот `dev2` |
|---|---|---|---|
| `.` (монорепозиторий) | `QM/monorepo-ocp` | `master` | — |
| `legacy/queue-manager-service` | `QM/queue-manager-service` | `dev2` | `dev2/qms` |
| `legacy/queue-manager-ui` | `QM/queue-manager-ui` | `dev2` | `dev2/qms-ui` |
| `legacy/proxyappback` | `SOFTPHONE-API-DEMO/proxyappback` | `master` (pull request'ы линии — в `dev2`) | `dev2/proxyappback` |
| `legacy/softphone-api-demo` | `SOFTPHONE-API-DEMO/softphone-api-demo` | `master` | `dev2/softphone-api-demo` |
| `legacy/tests` | `PT/tests` | `dev2` (без `new_ocp`) | `dev2/pt-tests` |
| `legacy/release-sets` | `QM/release-sets` | `dev2` | `dev2/release-sets` |
| `legacy/mergemedia` | `MER/mergemedia` | `dev2` | `dev2/mergemedia` |
| `legacy/phone-rotator` | `QM/phone-rotator` | `dev2` | `dev2/phone-rotator` |
| `legacy/jssip-phone` | `JSSIP/jssip-phone` | `master` | — |
| `legacy/screen-backend` | `SCREEN/backend` | `master` | — |
| `legacy/soft-phone-api-demo-test` | `SADT/soft-phone-api-demo-test` | `dev` | — |
| `legacy/adservice` | `AD/adservice` | `master` | — |
| `legacy/ocp-docs-frontend` | `ODS/ocp-docs-frontend` | `master` | — |
| `legacy/kvstore` | `QM/kvstore` | `master` | — |
| `legacy/superset-datasets` | `SDS/main` | `master` | — |
| `claude-plugins/duty` | `QM/duty` | `master` | — |
| вне монорепозитория | `JEN/jenkins-shared-lib` | `master` | — |

Сборку pull request'а находят не по имени задания, а по статусу сборки
коммита: поле `url` в `build-status` ведёт прямо на сборку Jenkins.

## Задания Jenkins

- Ворота pull request'ов в `dev2` — многоветвевые задания из таблицы выше,
  сборка `PR-<номер>`, этапы «Ворота» → «Проверки» (запись 0039
  монорепозитория).
- Бинарники, образы и `Promote` идут шагом `dev2Binaries` в сборке
  pull request'а (`dev2/qms/PR-<номер>`) и в сборке ветви `dev2`; прогон на
  зонах — общее задание `dev2/e2e`; ярусы сборки — запись 0040.
- Ветвь `dev`: сборка `dev` тех же заданий (`dev2/qms`, `dev2/qms-ui`,
  `dev2/proxyappback`, `dev2/mergemedia`, `dev2/pt-tests`) и задание `dev2/e2e`
  с `LINE=dev`.
- Узлы: `BinaryBuilder` (4 исполнителя), `TEST_NODE` (2), `QMS_DEV`,
  `QMS_TEST`, `QMS_FEATURE`, `QMS_PROD`, `test-zone-1`, `test-zone-2`.
- Запуск заданий меняет контур и навыкам плагина запрещён.

## Готовые команды (только чтение)

Каждая команда самостоятельна: состояние оболочки между вызовами
не сохраняется.

```sh
# Сводка открытых pull request'ов пользователя (/duty:gates): таблица Markdown.
python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/gates.py"

# Сырые факты для /duty:inspect: git, worktree, pull request'ы, Jenkins, машина,
# todo.local.md, OpenSpec, отложенные вопросы, Jira.
bash "${CLAUDE_PLUGIN_ROOT}/scripts/facts.sh" "${CLAUDE_PROJECT_DIR}"

# Перечень кандидатов на уборку (/duty:cleanup).
python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/cleanup-candidates.py" "${CLAUDE_PROJECT_DIR}"

# Один pull request: состояние, ветви, версия.
curl -s -H "Authorization: Bearer $(cat ~/.config/axata/bitbucket-http-token)" \
  "https://git.service-desk.site/rest/api/1.0/projects/<КЛЮЧ>/repos/<слаг>/pull-requests/<номер>" |
  python3 -I -c 'import json,sys; p=json.load(sys.stdin); print(p["state"], p["fromRef"]["displayId"], p["toRef"]["displayId"], p["version"], p["title"])'

# Готовность к слиянию: canMerge, conflicted, vetoes.
curl -s -H "Authorization: Bearer $(cat ~/.config/axata/bitbucket-http-token)" \
  "https://git.service-desk.site/rest/api/1.0/projects/<КЛЮЧ>/repos/<слаг>/pull-requests/<номер>/merge"

# Статусы сборок коммита (ключ — задание/ветвь, url — сборка Jenkins).
curl -s -H "Authorization: Bearer $(cat ~/.config/axata/bitbucket-http-token)" \
  "https://git.service-desk.site/rest/build-status/1.0/commits/<sha>"

# Этапы сборки Jenkins по её адресу из build-status (без display/redirect).
set -a; . ~/.config/axata/jenkins-credentials.env; set +a
curl -sk -g -u "$JENKINS_USER:$JENKINS_TOKEN" "<адрес сборки>/wfapi/describe" |
  python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], " → ".join(s["name"]+":"+s["status"] for s in d["stages"]))'

# Тикет Jira: статус и доступные переходы.
curl -s -H "Authorization: Bearer $(cat ~/.config/axata/jira-pat)" \
  "https://jira.service-desk.site/rest/api/2/issue/RP-<номер>/transitions" |
  python3 -I -c 'import json,sys; [print(t["id"], t["name"], "->", t["to"]["name"], t["to"]["statusCategory"]["key"]) for t in json.load(sys.stdin)["transitions"]]'
```

## Команды с изменениями (только `/duty:after-merge`)

```sh
# Удалить ветвь в origin (не из перечня защищённых, reference/layout.md).
git -C <путь сабмодуля> push origin --delete <ветвь>

# Перевод тикета: id перехода, у которого to.statusCategory.key == "done"
# (в проекте RP это «Done» → «Готово»).
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(cat ~/.config/axata/jira-pat)" \
  -d '{"transition":{"id":"<id>"}}' \
  "https://jira.service-desk.site/rest/api/2/issue/RP-<номер>/transitions"   # ожидается 204

# Комментарий в тикет (разметка Jira wiki).
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(cat ~/.config/axata/jira-pat)" \
  -d '{"body":"Слит pull request [<КЛЮЧ>/<слаг>#<номер>|<ссылка>]."}' \
  "https://jira.service-desk.site/rest/api/2/issue/RP-<номер>/comment"      # ожидается 201
```
