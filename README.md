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
| `/duty:cleanup [apply]` | sonnet | `duty:runner` | кандидаты на уборку с размерами; с `apply` — удаление строго по перечню; файлы контейнеров rootless Docker (uid из `/etc/subuid`) — через `rootlesskit rm -rf` без sudo; кэш сборки Docker — только старше двух суток | только с `apply` |
| `/duty:after-merge <репозиторий>#<номер>` | sonnet | `duty:runner` | ветвь в origin, тикет RP, задачи `tasks.md`, местные worktree и ветвь после слияния | да |
| `/duty:dispatch <имя> <модель> [задача]` | главной сессии; агенту — из аргумента | `duty:worker` в своём workspace | вынос крупной задачи в workspace herdr с запиской | каталог задачи, workspace |
| `/duty:decision <тема> :: <решение>` | sonnet | `duty:runner` | запись решения в `docs/decisions/` по форме записи 0029 с номером из сценария и строкой указателя; без `::` — справка, что по теме уже решено | да, только в режиме записи (коммит двух файлов) |
| `/duty:docs-sweep <правило> [--paths …] [--dry]` | sonnet | `duty:runner` | массовая правка документов по правилу: перечень до правок, замена, вычитка, коммит по репозиториям; с `--dry` — перечень | да, кроме `--dry` |

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
`Agent`, `worker` — все инструменты, включая `Agent`, и без своей
модели: её задаёт `/duty:dispatch`. Ключ `permissionMode` у агентов
плагина не действует.

## Состав

```
.claude-plugin/plugin.json   манифест
skills/<навык>/SKILL.md      навыки
agents/inspector.md          только чтение
agents/runner.md             рутина с правками по списку
agents/worker.md             исполнитель задачи в своём workspace herdr
scripts/gates.py             сводка pull request'ов (Bitbucket, Jenkins), режим now
scripts/branches.py          таблица ветвей origin с предложениями
scripts/facts.sh             факты для /duty:inspect
scripts/busy.py              реестр занятости общих ресурсов
scripts/tests/test_busy.py   проверки реестра
scripts/cleanup-candidates.py перечень кандидатов на уборку
scripts/dispatch.sh          запуск, перечень и закрытие workspace задач
scripts/tab-run.sh           длинная команда в новой вкладке: cmd.sh, out.log, done
scripts/wait-done.sh         ожидание файла done по inotify со сроком
scripts/jwait.sh, jwait.py   ожидание конца сборки Jenkins: report.md, done
scripts/decision-number.sh   выдача номера записи решения
reference/access.md          доступы, REST, репозитории, задания Jenkins
reference/layout.md          раскладка, защищённые ветви, что чужое
reference/report.md          форма итога
reference/brief-template.md  шаблон записки задачи
reference/worker-rules.md    общие запреты для rules.md задачи
```

Пути внутри плагина в тексте навыков — `${CLAUDE_PLUGIN_ROOT}`, корень
проекта — `${CLAUDE_PROJECT_DIR}`: Claude Code подставляет их при
загрузке навыка. В окружении Bash эти переменные не заданы.

## Вынос задачи в workspace

Крупная задача уходит из главной сессии в отдельный workspace herdr
с говорящим именем; там работает своя сессия Claude в роли
`duty:worker`. Решение и отвергнутые варианты — запись
`docs/decisions/0054-duty-workspace-dispatch.md` монорепозитория.

**Когда.** Задача занимает местный стенд, его экземпляр, домен студии
или узел Jenkins, содержит прогон, сборку или ожидание дольше 10 минут,
правит больше одного репозитория или идёт через изменение OpenSpec.
Модель — по стоимости (`sonnet`, `opus`); Fable в workspace — только
после согласия пользователя: главная сессия предлагает его
с обоснованием и лишь тогда передаёт слово `fable`.

**Что делает навык.** `/duty:dispatch` выполняется в главной сессии,
без `context: fork`: записку составляют из того, что знает только она
(цель, сделанное, решения пользователя), а форк этих сведений
не получает. Запуск — детерминированный сценарий `scripts/dispatch.sh`,
одна команда с проверкой каждого шага: каталог задачи с `brief.md`
и `rules.md`; `herdr workspace create --label <имя> --env DUTY_ROLE=worker
--env DUTY_TASK=<каталог> --env DUTY_NAME=<имя> --no-focus`; служебный блок в `brief.md`
(workspace, панель, имя агента, имя главной сессии, каталог плагина);
`herdr agent start <имя> --kind claude -- --agent duty:worker --model
<модель> --name <имя> "<приглашение>"` (первое приглашение — позиционным
аргументом: `herdr agent prompt` на только что запущенном агенте теряет Enter
длинного текста) и проверка `herdr agent wait`, что агент перешёл в `working`
или `blocked`; потерянный Enter досылается один раз. Хуки `SessionStart`
монорепозитория при `DUTY_ROLE=worker` не выдают снимок `todo.local.md`
и указание `/sync-parents`.

**Файлы задачи** — `../ocp-dev-stand/results/<имя>/`:

| Файл | Кто пишет | Что в нём |
|---|---|---|
| `brief.md` | главная сессия по `reference/brief-template.md`, служебный блок — `dispatch.sh` | цель, сделанное, где остановились, следующие шаги, команды проверки, решения, ссылки; раздел «Передача» — агент при переполнении контекста |
| `rules.md` | `dispatch.sh` из `reference/worker-rules.md`, главная сессия дописывает «Переданные» | действующие запреты и решения; агент перечитывает перед каждым внешним действием |
| `state.md` | агент после каждого шага | снимок: шаг, хеши, вкладки, занятые ресурсы |
| `report.md` | агент в конце | итог по `reference/report.md` |
| `runs/<вкладка>/` | `scripts/tab-run.sh` | `cmd.sh`, `out.log`, `meta.txt`, `done` с кодом завершения |

**Долгие команды** идут во вкладках своего workspace
(`scripts/tab-run.sh`: `herdr tab create --no-focus`, `herdr pane run`),
ожидание — `scripts/wait-done.sh` (inotify) фоновым вызовом Bash или
`herdr pane wait-output --match 'DUTY-DONE <вкладка>'`, без опроса.
Размышляющие подзадачи — инструментом Agent внутри агента. Общие
ресурсы — `scripts/busy.py claim|release` с `--holder <имя>` и
`--pid "$PPID"` (процесс сессии Claude).

**Вопросы** агент задаёт пользователю сам в своём workspace через
`AskUserQuestion` с вариантами, после всего, что от ответа не зависит;
если workspace не в фокусе (`herdr workspace list` → `focused: false`),
перед вопросом — `herdr notification show "Workspace <имя> ждёт ответа"
--sound request`. Главная сессия вопросы не пересылает, а новые
решения дописывает в `rules.md`.

**Итог и закрытие.** Агент по порядку: снимает свои отметки реестра,
удаляет свои временные деревья (scratchpad, `runs/*/tmp`), закрывает
созданные им вкладки (корневую оставляет), пишет `report.md`
и `state.md`, отправляет главной сессии `SendMessage` (три строки
и путь к `report.md`), показывает `herdr notification show … --sound
done` и последним действием закрывает свой workspace — если `rules.md`
не велит оставить его открытым. Тогда закрывает главная сессия:

```sh
bash claude-plugins/duty/scripts/dispatch.sh list           # открытые workspace задач: агент, report.md
bash claude-plugins/duty/scripts/dispatch.sh close <имя>    # агент idle/done, report.md есть
```

`close` снимает забытые отметки держателя `<имя>` через `busy.py`
с предупреждением, закрывает вкладки кроме корневой и workspace;
перечнем `list` позже воспользуется `/duty:cleanup`.

## Вкладки: длинные команды

Длинная команда идёт не в Bash агента и не в субагенте, а во вкладке
workspace herdr (`tab-run.sh`: `herdr tab create`, `herdr pane run`);
агент ждёт её файла-отметки `done` одним вызовом `wait-done.sh`. Во вкладку идут:

- местный прогон `just dev-stand::pipeline …` (свой `done` рядом
  с `summary.md` и отметки мест в `../ocp-dev-stand/busy/` пишет сам
  `pipeline.py`, `docs/dev-stand.md` монорепозитория);
- ожидание сборки Jenkins — `jwait.sh` через `tab-run.sh`;
- `scripts/ci-checks.sh`, `just test-compiled-all`, `just check-release`,
  `pipeline.py --src`, `run-checks-light.sh`, `run-shards.sh`,
  `npx playwright test <файл>`, `pytest tests/blackbox`, наборы
  `e2e/voip-*` — через `tab-run.sh`;
- замеры (`bench.py`, покрытие) — через `tab-run.sh` с отметкой `station`
  в реестре занятости;
- сборки стендов (`just stands::build`, `codegen/run.sh`,
  `checks/run-all.sh`) и наблюдение дев-контура
  (`ssh … docker events --filter event=die`).

Размышляющие подзадачи — инструмент Agent внутри агента workspace,
не вкладки.

```sh
# Новая вкладка в каталоге --cwd (по умолчанию текущий), итог — в $DUTY_TASK/runs/<имя>/.
"${CLAUDE_PLUGIN_ROOT}/scripts/tab-run.sh" checks --cwd "$PWD" -- just test-compiled-all
# Строка для bash — одним аргументом: кавычки уходят в cmd.sh, а не в терминал.
"${CLAUDE_PLUGIN_ROOT}/scripts/tab-run.sh" grep-log -- 'grep -c "FAILED" out.txt | tee n.txt'

# Ожидание сборки Jenkins (номер или last) — тоже во вкладке.
"${CLAUDE_PLUGIN_ROOT}/scripts/tab-run.sh" pr-2581 -- "${CLAUDE_PLUGIN_ROOT}/scripts/jwait.sh" qms-checks/PR-2581 last

# У агента: одним вызовом дождаться done, код — в выводе и коде выхода.
"${CLAUDE_PLUGIN_ROOT}/scripts/wait-done.sh" "$DUTY_TASK/runs/checks" --timeout-s 590
```

| Сценарий | Каталог итога | Файлы |
|---|---|---|
| `tab-run.sh <имя> [--cwd <каталог>] -- <команда…>` | `runs/<имя>/` каталога задачи (`$DUTY_TASK`, без него — текущий каталог); каталог должен быть новым, иначе отказ с кодом 2 | `cmd.sh` (команда: один аргумент — строка для bash, несколько — команда с экранированными аргументами), `out.log` (stdout и stderr вместе, они же на экране вкладки), `meta.txt` (каталог, `started_ms`, `finished_ms`, код), `done` с кодом команды; при `HERDR_ENV=1` — уведомление `herdr notification show` (звук `done`, при ненулевом коде `request`) и строка `DUTY-DONE <имя> rc=<код>`; в ответ — JSON с `tab_id`, `pane_id`, `run_dir` |
| `jwait.sh <задание> <номер\|last> [--out <каталог>]` | `--out`; иначе `$DUTY_TASK/runs/jenkins-<задание>-<номер>/`; без `DUTY_TASK` — `../ocp-dev-stand/results/wait/<задание>-<номер>/` (`last` заменяется номером; `/` в имени задания — `-`) | `console.log` (журнал сборки), `report.md` (итог, длительность, этапы `wfapi/describe`, шаги упавших этапов с хвостом журнала, хвост журнала сборки), `done`: 0 — SUCCESS, 1 — другой итог, 2 — ожидание не удалось |
| `wait-done.sh <каталог> [--timeout-s <с>]` | — | ничего не пишет; печатает `done rc=<код> <каталог>` и выходит с кодом из `done`, 124 — истёк срок, 2 — ошибка вызова или каталога нет (его создаёт `tab-run.sh` до возврата) |

Отметка `done` появляется переименованием и последней, поэтому её
содержимое всегда целое, а остальные файлы к этому моменту дописаны.

Ожидание везде по событиям: `wait-done.sh` — `inotifywait` (пакет
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

`--timeout-s` у `wait-done.sh` — предел ожидания, а не такт опроса:
вызов из Bash агента ограничен 10 минутами, поэтому для долгой команды
агент ставит `--timeout-s 590` и повторяет вызов либо запускает
`wait-done.sh` фоном (`run_in_background`) и получает уведомление
о его выходе.

Вкладка после `done` остаётся открытой для просмотра; закрывает её
агент workspace вместе с остальными ресурсами задачи, когда пишет отчёт.

## Номера записей решений

Номер записи `docs/decisions/` выдаёт только
`scripts/decision-number.sh <каталог>`: под замком (`mkdir` каталога
`.number-lock`, снимается в `trap`) берётся наибольший номер среди файлов
рабочего дерева и индекса git, плюс один, и сразу резервируется пустой
файл `NNNN-reserved.md`, который навык переименовывает в запись. Так
параллельные агенты не получают один номер (коллизии 0065, 0077, 0078,
0107). Ожидание замка идёт по событию (`inotifywait`), без опроса;
`--peek` показывает следующий номер без резерва.

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
busy.py claim <ресурс> --holder <имя> --pid <pid> [--task <путь>] [--note <текст>]
busy.py release <ресурс> [--holder <имя>] [--force]
busy.py list [--json]
busy.py check <ресурс>      # 0 — свободно, 3 — занято
```

`claim` при занятом живым держателем печатает «занято: держатель, заметка,
с какого времени» и возвращает 3. `--pid` обязателен: родитель самого
сценария — короткая оболочка Bash вызова, отметка с её pid сразу
устарела бы. Из Bash сессии Claude — `--pid "$PPID"` (процесс сессии):
отметка живёт, пока жива сессия; без `--pid` или с неживым процессом
`claim` возвращает 2 с подсказкой.
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
python3 -I claude-plugins/duty/scripts/gates.py now    # узлы Jenkins, агенты, занятость
python3 -I claude-plugins/duty/scripts/branches.py all --mine   # ветви origin
python3 -I claude-plugins/duty/scripts/cleanup-candidates.py "$PWD"   # сухой перечень
claude-plugins/duty/scripts/jwait.sh qms-checks/PR-2581 last   # завершённая сборка: done 0
claude -p '/duty:gates' --model haiku --output-format json < /dev/null
bash -n claude-plugins/duty/scripts/dispatch.sh       # разбор сценария выноса
bash claude-plugins/duty/scripts/dispatch.sh list     # перечень workspace задач
```

Пробный вынос (в herdr; workspace закрывает сам агент, каталог задачи
удаляется после проверки):

```sh
bash claude-plugins/duty/scripts/dispatch.sh dispatch-probe haiku \
  --goal 'Напиши в $DUTY_TASK/report.md слово «готово» и заверши задачу.'
cat ../ocp-dev-stand/results/dispatch-probe/report.md && rm -r ../ocp-dev-stand/results/dispatch-probe
```

В выводе последней команды `modelUsage` содержит только
`claude-haiku-*`, `result` — одна таблица; `git status` монорепозитория
и сабмодулей до и после совпадает.
