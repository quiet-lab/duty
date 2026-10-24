# duty — дежурство по монорепозиторию

Плагин Claude Code для монорепозитория `monorepo-ocp`. Назначение —
экономить токены основной сессии: инспекция и рутина выполняются
форком (`context: fork`) на самой дешёвой модели, которой достаточно
для задачи, а в основную сессию возвращается только короткий итог —
одна таблица по форме [`reference/report.md`](reference/report.md).
Навыки не читают деревья целиком: факты собирают сценарии `scripts/`
короткими командами и запросами REST, у каждого шага — критерий
готовности, проверяемый командой.

Проектные навыки `/handoff` и `/sync-parents` остаются в
`.claude/skills/` монорепозитория; плагин их не повторяет и не заменяет.

## Навыки

| Навык | Модель | Агент | Что делает | Меняет |
|---|---|---|---|---|
| `/duty:inspect [фокус]` | sonnet | `duty:inspector` | таблица задач: что трогают, с чем пересекаются, какой моделью, можно ли сейчас; перечень уборки | ничего |
| `/duty:gates [фильтр]` | haiku | `duty:inspector` | открытые pull request'ы пользователя: сборки, ворота Jenkins с этапами, готовность к слиянию, возраст | ничего |
| `/duty:cleanup [apply]` | sonnet | `duty:runner` | кандидаты на уборку с размерами; с `apply` — удаление строго по перечню | только с `apply` |
| `/duty:after-merge <репозиторий>#<номер>` | sonnet | `duty:runner` | ветвь в origin, тикет RP, задачи `tasks.md`, местные worktree и ветвь после слияния | да |

Модель задаёт ключ `model` навыка; он перекрывает модель агента
и основной сессии. Набор инструментов задаёт агент: `inspector` —
`Bash, Read, Grep, Glob` без правок, `runner` — с `Edit, Write`, без
`Agent`. Ключ `permissionMode` у агентов плагина не действует.

## Состав

```
.claude-plugin/plugin.json   манифест
skills/<навык>/SKILL.md      навыки
agents/inspector.md          только чтение
agents/runner.md             рутина с правками по списку
scripts/gates.py             сводка pull request'ов (Bitbucket, Jenkins)
scripts/facts.sh             факты для /duty:inspect
scripts/busy.py              реестр занятости общих ресурсов
scripts/tests/test_busy.py   проверки реестра
scripts/cleanup-candidates.py перечень кандидатов на уборку
reference/access.md          доступы, REST, репозитории, задания Jenkins
reference/layout.md          раскладка, защищённые ветви, что чужое
reference/report.md          форма итога
```

Пути внутри плагина в тексте навыков — `${CLAUDE_PLUGIN_ROOT}`, корень
проекта — `${CLAUDE_PROJECT_DIR}`: Claude Code подставляет их при
загрузке навыка. В окружении Bash эти переменные не заданы.

## Подключение

Плагин — сабмодуль `claude-plugins/duty` монорепозитория
(`QM/duty` в Bitbucket). Каталог `.claude-plugin/marketplace.json`
в корне монорепозитория объявляет marketplace `ocp` с плагином `duty`
(источник `./claude-plugins/duty`), `.claude/settings.json` —
`extraKnownMarketplaces.ocp` (источник `directory`, путь `.`)
и `enabledPlugins["duty@ocp"]`. Интерактивная сессия при первом
запуске предлагает доверить marketplace и установить плагин;
для `claude -p` и скриптов установка выполняется один раз:

```sh
claude plugin install duty@ocp --scope project
```

Плагин читается прямо из каталога сабмодуля (`claude plugin list`
показывает `Read from: …/claude-plugins/duty`), правки действуют
со следующего запуска сессии без переустановки.

Первый прогон `/duty:cleanup apply` состоялся; действуют исключения:
ветви, выписанные в любую worktree, и ветви под `--exclude`
не удаляются, слитость — по `origin/<родитель>`, scratchpad с грязным
деревом или невыгруженными коммитами остаётся.

## Реестр занятости

Несколько агентов и сессий делят стенд, станцию, узлы Jenkins, очередь
pull request'ов и дерево `docs/`. Чтобы они не мешали друг другу, ресурс
помечается файлом в `~/work/telecom/axata/ocp-dev-stand/busy/`
(переменная `DUTY_BUSY_DIR` меняет каталог). Отметки советующие:
принудительной блокировки нет, их читает тот, кто спрашивает.

Файл `<ресурс>.json` создаётся атомарно (`O_CREAT|O_EXCL`), снимается
удалением держателем; время — целые миллисекунды Unix UTC:

```json
{"resource": "dev-stand", "holder": "<workspace или сессия>",
 "task": "<путь к записке или описание>", "pid": 12345,
 "since_ms": 1792900000000, "note": "<что идёт>"}
```

Имена ресурсов: `dev-stand` (стенд целиком), `dev-stand-<N>` (экземпляр),
`station` (вся станция, замеры), `jenkins-binarybuilder`,
`jenkins-test-node`, `pr-train` (очередь pull request'ов dev2),
`docs-tree` (каталог `docs/`), `studio-<домен>`.

```sh
busy.py claim <ресурс> --holder <имя> [--task <путь>] [--note <текст>] [--pid N]
busy.py release <ресурс> [--holder <имя>] [--force]
busy.py list [--json]
busy.py check <ресурс>      # 0 — свободно, 3 — занято
```

`claim` при занятом живым держателем печатает «занято: держатель, заметка,
с какого времени» и возвращает 3. `pid` по умолчанию — родительский процесс
вызывающего, то есть сессия агента: отметка живёт, пока жива сессия.
`release` с `--holder` снимает только свою отметку, чужую — с `--force`
и предупреждением.

Отметку ставят: сценарии стенда — сами, записью файла по этому контракту,
от плагина они не зависят; агенты и навыки — `busy.py`. Отметка устарела,
если процесс `pid` не жив (`kill -0`): `claim` снимает её сам с сообщением,
`list` показывает её как устаревшую, `/duty:inspect` относит её к уборке.
Раздел «Занятость» в выводе `facts.sh` и столбец «Можно сейчас» навыка
`/duty:inspect` опираются на реестр. Решение — запись 0055 в
`docs/decisions/` монорепозитория. Проверка:
`python3 -I scripts/tests/test_busy.py`.

## Доступы

Токены не хранятся в плагине: сценарии читают
`~/.config/axata/bitbucket-http-token`, `~/.config/axata/jenkins-credentials.env`
и `~/.config/axata/jira-pat` в момент вызова
([`reference/access.md`](reference/access.md)). Ответ 401 или 403
от Bitbucket останавливает работу без повтора.

## Проверка

Из корня монорепозитория:

```sh
claude plugin list | grep -A4 duty@ocp                # установлен и включён
claude --plugin-dir claude-plugins/duty plugin details duty   # инвентарь и токены
python3 -I claude-plugins/duty/scripts/gates.py        # таблица pull request'ов
python3 -I claude-plugins/duty/scripts/cleanup-candidates.py "$PWD"   # сухой перечень
claude -p '/duty:gates' --model haiku --output-format json < /dev/null
```

В выводе последней команды `modelUsage` содержит только
`claude-haiku-*`, `result` — одна таблица; `git status` монорепозитория
и сабмодулей до и после совпадает.
