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
| `/duty:gates [now \| фильтр]` | haiku | `duty:inspector` | открытые pull request'ы пользователя: сборки, ворота Jenkins с этапами, готовность к слиянию, возраст; с `now` — что идёт на узлах Jenkins и чем заняты агенты | ничего |
| `/duty:branches [all \| репозиторий] [--mine]` | haiku | `duty:inspector` | таблица ветвей в `origin` с предложением по каждой: слить, удалить, оставить, решить | ничего |
| `/duty:cleanup [apply]` | sonnet | `duty:runner` | кандидаты на уборку с размерами; с `apply` — удаление строго по перечню | только с `apply` |
| `/duty:after-merge <репозиторий>#<номер>` | sonnet | `duty:runner` | ветвь в origin, тикет RP, задачи `tasks.md`, местные worktree и ветвь после слияния | да |

`/duty:gates now` отвечает на вопрос «что сейчас идёт и чем заняты
агенты»: таблица исполнителей Jenkins (узел, задание и сборка, сколько
идёт, кто запустил; очередь ожидания — отдельной строкой) и таблица
станции (сессии Claude Code из `~/.claude/sessions`, агенты `herdr`,
отметки реестра занятости из `../ocp-dev-stand/busy/` с проверкой
`kill -0`). `/duty:branches` заменяет вопросы по одной ветви таблицей:
данные берутся из местных `refs/remotes` без `fetch`, в конце печатается
дата последнего `fetch`; защищённые `dev`, `dev2`, `master` и рабочая
`new_ocp` не показываются, чужие ветви получают предложение «решить»
и не предлагаются к удалению. Вся логика обоих режимов — в сценариях,
навыки на `haiku` только запускают их.

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
scripts/gates.py             сводка pull request'ов (Bitbucket, Jenkins), режим now
scripts/branches.py          таблица ветвей origin с предложениями
scripts/facts.sh             факты для /duty:inspect
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
python3 -I claude-plugins/duty/scripts/gates.py now    # узлы Jenkins, агенты, занятость
python3 -I claude-plugins/duty/scripts/branches.py all --mine   # ветви origin
python3 -I claude-plugins/duty/scripts/cleanup-candidates.py "$PWD"   # сухой перечень
claude -p '/duty:gates' --model haiku --output-format json < /dev/null
```

В выводе последней команды `modelUsage` содержит только
`claude-haiku-*`, `result` — одна таблица; `git status` монорепозитория
и сабмодулей до и после совпадает.
