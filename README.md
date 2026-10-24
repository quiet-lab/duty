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
scripts/cleanup-candidates.py перечень кандидатов на уборку
scripts/tabrun.sh            обёртка длинной команды во вкладке: log.txt, done
scripts/waitdone.sh          ожидание файла done по inotify
scripts/jwait.sh, jwait.py   ожидание конца сборки Jenkins: report.md, done
reference/access.md          доступы, REST, репозитории, задания Jenkins
reference/layout.md          раскладка, защищённые ветви, что чужое
reference/report.md          форма итога
```

Пути внутри плагина в тексте навыков — `${CLAUDE_PLUGIN_ROOT}`, корень
проекта — `${CLAUDE_PROJECT_DIR}`: Claude Code подставляет их при
загрузке навыка. В окружении Bash эти переменные не заданы.

## Вкладки: длинные команды

Длинная команда идёт не в Bash агента и не в субагенте, а во вкладке
workspace herdr (`herdr pane run <панель> …`); агент ждёт её файла-отметки
`done` одним вызовом `waitdone.sh`. Во вкладку идут:

- местный прогон `just dev-stand::pipeline …` (свой `done` рядом
  с `summary.md` и отметки мест в `../ocp-dev-stand/busy/` пишет сам
  `pipeline.py`, `docs/dev-stand.md` монорепозитория);
- ожидание сборки Jenkins — `jwait.sh`;
- `scripts/ci-checks.sh`, `just test-compiled-all`, `just check-release`,
  `pipeline.py --src`, `run-checks-light.sh`, `run-shards.sh`,
  `npx playwright test <файл>`, `pytest tests/blackbox`, наборы
  `e2e/voip-*` — через `tabrun.sh`;
- замеры (`bench.py`, покрытие) — через `tabrun.sh` с отметкой `station`
  в реестре занятости;
- сборки стендов (`just stands::build`, `codegen/run.sh`,
  `checks/run-all.sh`) и наблюдение дев-контура
  (`ssh … docker events --filter event=die`).

Размышляющие подзадачи — инструмент Agent внутри агента workspace,
не вкладки.

```sh
# Во вкладке: команда в текущем каталоге вкладки, итог — в $DUTY_TASK/runs/<имя>/.
"${CLAUDE_PLUGIN_ROOT}/scripts/tabrun.sh" checks -- just test-compiled-all

# Во вкладке: дождаться сборки Jenkins (номер или last) и записать итог.
"${CLAUDE_PLUGIN_ROOT}/scripts/jwait.sh" qms-checks/PR-2581 last

# У агента: одним вызовом дождаться done, код — в выводе и коде выхода.
"${CLAUDE_PLUGIN_ROOT}/scripts/waitdone.sh" "$DUTY_TASK/runs/checks" --timeout-s 590
```

| Сценарий | Каталог итога | Файлы |
|---|---|---|
| `tabrun.sh <имя> -- <команда…>` | `runs/<имя>/` каталога задачи (`$DUTY_TASK`, без него — текущий каталог); каталог должен быть новым | `cmd.txt` (каталог, команда, `started_ms`, `finished_ms`, код), `log.txt` (stdout и stderr вместе, они же на экране вкладки), `done` с кодом команды; при `HERDR_ENV=1` — уведомление `herdr notification show` (звук `done`, при ненулевом коде `request`) |
| `jwait.sh <задание> <номер\|last> [--out <каталог>]` | `--out`; иначе `$DUTY_TASK/runs/jenkins-<задание>-<номер>/`; без `DUTY_TASK` — `../ocp-dev-stand/results/wait/<задание>-<номер>/` (`last` заменяется номером; `/` в имени задания — `-`) | `console.log` (журнал сборки), `report.md` (итог, длительность, этапы `wfapi/describe`, шаги упавших этапов с хвостом журнала, хвост журнала сборки), `done`: 0 — SUCCESS, 1 — другой итог, 2 — ожидание не удалось |
| `waitdone.sh <каталог> [--timeout-s <с>]` | — | ничего не пишет; печатает код из `done` и выходит с ним, 124 — истёк срок, 2 — ошибка вызова |

Отметка `done` появляется переименованием и последней, поэтому её
содержимое всегда целое, а остальные файлы к этому моменту дописаны.

Ожидание везде по событиям: `waitdone.sh` — `inotifywait` (пакет
`inotify-tools`, на станции есть; наблюдение ставится до проверки, есть
ли `done`); `jwait.sh` — поток журнала команды Jenkins CLI
`console <задание> <номер> -f` по WebSocket, конец сборки — кадр EXIT
(запись 0056 монорепозитория). Внешний nginx `jen.service-desk.site`
WebSocket не пропускает, поэтому CLI идёт прямо на контроллер
`JENKINS_CLI_URL` (по умолчанию `http://10.200.100.8:8080/`; переменная
окружения или строка в `jenkins-credentials.env`), REST — на `JENKINS_URL`.
Обрыв соединения при идущей сборке — переподключение (не больше пяти
подряд), тишина дольше 90 с (три интервала ping контроллера) считается
обрывом. Сборок `jwait.sh` не запускает.

`--timeout-s` у `waitdone.sh` — предел ожидания, а не такт опроса:
вызов из Bash агента ограничен 10 минутами, поэтому для долгой команды
агент ставит `--timeout-s 590` и повторяет вызов либо запускает
`waitdone.sh` фоном (`run_in_background`) и получает уведомление
о его выходе.

Вкладка после `done` остаётся открытой для просмотра; закрывает её
агент workspace вместе с остальными ресурсами задачи, когда пишет отчёт.

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
python3 -I claude-plugins/duty/scripts/cleanup-candidates.py "$PWD"   # сухой перечень
claude-plugins/duty/scripts/jwait.sh qms-checks/PR-2581 last   # завершённая сборка: done 0
claude -p '/duty:gates' --model haiku --output-format json < /dev/null
```

В выводе последней команды `modelUsage` содержит только
`claude-haiku-*`, `result` — одна таблица; `git status` монорепозитория
и сабмодулей до и после совпадает.
