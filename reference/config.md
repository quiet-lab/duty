# Конфигурация проекта

Документ-владелец схемы `.duty/duty.toml`: слои, поиск корня, общий
режим, ключи, контракты расширений и команды `scripts/duty_config.py`.
Всё, что зависит от проекта, плагин берёт отсюда; сам плагин имён
проекта не содержит.

## Каталог `.duty/`

| Файл | Что в нём |
|---|---|
| `duty.toml` | конфигурация проекта, в git |
| `local.toml` | перекрытия одной машины, в `.gitignore` |
| `facts.d/*` | исполняемые расширения фактов `facts.sh` |
| `cleanup.d/*` | исполняемые расширения уборки `cleanup-candidates.py` |

В `.duty/` лежит только то, что читают сценарии, навыки и хуки плагина.
Навыки и хуки самого проекта остаются в `.claude/`.

## Слои

Слои читаются по порядку, позднее перекрывает раннее:

1. встроенные значения по умолчанию (`DEFAULTS` в `duty_config.py`);
2. пользовательский `~/.config/duty/config.toml` (путь перекрывает
   переменная `DUTY_CONFIG_USER`);
3. `<корень>/.duty/duty.toml`;
4. `<корень>/.duty/local.toml`.

Таблицы сливаются по ключам, списки и массивы таблиц заменяются целиком.
Поверх всех слоёв действуют переменные окружения:

| Переменная | Что перекрывает |
|---|---|
| `DUTY_RESULTS_DIR` | `paths.results` (и `paths.sandbox`, если он не задан) |
| `DUTY_BUSY_DIR` | `paths.busy` |
| `DUTY_SIGNALS_DIR` | `paths.signals` |
| `DUTY_SYMBOLS_FILE` | `symbols.tasks`: файл с символом на строку |

`schema = 1` обязателен в `duty.toml`; в остальных слоях он
необязателен, но если задан, то равен `1`.

## Поиск корня

Корень проекта — первый найденный:

1. ключ `--root`;
2. переменная `DUTY_PROJECT` (её ставит `dispatch.sh` агенту задачи
   и экспортирует `tab-run.sh`, поэтому worktree задачи вне проекта
   не даёт чужого корня);
3. переменная `CLAUDE_PROJECT_DIR` (хуки и навыки);
4. подъём от текущего каталога (`--cwd`) до каталога с `.duty/duty.toml`,
   не выше надпроекта git (`git rev-parse --show-superproject-working-tree`,
   иначе `--show-toplevel`): из сабмодуля поиск доходит до монорепозитория,
   из отдельного репозитория внутри чужого каталога — не выходит за него;
5. `.duty/duty.toml` не найден — корень равен надпроекту или верху git,
   режим общий;
6. вне git корня нет, имя проекта — имя текущего каталога.

## Общий режим

Режим `general` — у корня нет `.duty/duty.toml`. Действуют встроенные
значения и пользовательский слой; каталоги состояния —
`<paths.state>/<project.name>/{results,busy,signals}`, где `paths.state`
задаёт пользовательский слой (по умолчанию `~/.local/state/duty`),
песочница — `<results>/duty-tests/sandbox`. Те же значения по умолчанию
действуют и в режиме `project` для путей, которых `duty.toml` не задаёт.
Навык, которому нужна таблица `[bitbucket]`, `[jenkins]` или `[jira]`,
без неё сообщает об этом одной строкой и заканчивает работу.

## Ключи

Относительные пути `[paths]` отсчитываются от корня проекта, `~`
раскрывается; `get` и `json` выдают их абсолютными. Остальные пути
(`docs.*`, `cleanup.keep_trees`, `*_file`) тоже отсчитываются от корня,
их раскрывает читающий сценарий. Неизвестный ключ или таблица, значение
другого типа и ключ не своего слоя — находки `check`.

| Ключ | Тип | По умолчанию | Кто читает |
|---|---|---|---|
| `schema` | целое | — (обязателен) | `duty_config.py check` |
| `project.name` | строка | имя каталога корня | каталог состояния общего режима и `init` (`duty_config.py`); сценарии плагина имя не читают |
| `paths.results` | строка | `<state>/<name>/results` | `dispatch.sh`, `ask.py`, `runs-web.py`, `cleanup-candidates.py`, `jwait.py` |
| `paths.busy` | строка | `<state>/<name>/busy` | `busy.py`, `gates.py`, `facts.sh` и хук `SessionStart` (через `busy.py list`) |
| `paths.signals` | строка | `<state>/<name>/signals` | `signals.py` (его вызывают хуки `SessionStart` и `task-guard.py`) |
| `paths.sandbox` | строка | `<results>/duty-tests/sandbox` | пробы `tests/probes/` |
| `paths.state` | строка | `~/.local/state/duty` | только пользовательский слой: корень каталогов общего режима |
| `git.protected` | список | `["main", "master"]` и ветвь `origin/HEAD` | `hooks/forbid.py` (push), `branches.py`, `cleanup-candidates.py` |
| `git.working` | список | `[]` | `branches.py` (не показываются), `cleanup-candidates.py` (не удаляются) |
| `git.keep_prefixes` | список | `["backup/", "saved/"]` | `branches.py`, `cleanup-candidates.py` |
| `git.task_prefixes` | список | `[]` | `branches.py`, `cleanup-candidates.py`, `facts.sh` |
| `git.named_dev_branches` | логическое | `false` | `branches.py`, `cleanup-candidates.py`: включённый, делает ветвь без префикса и «/» именной ветвью разработчика (в `branches.py` — «оставить»); чужая местная ветвь (автор последнего коммита — не `user.name`) не удаляется и при выключенном ключе |
| `git.default_parent` | строка | ветвь `origin/HEAD`, иначе `master` | `branches.py`, `cleanup-candidates.py` |
| `git.parent_key` | строка | `"parent"` | `cleanup-candidates.py`, `branches.py` |
| `git.forbid_add_all` | логическое | `true` | `hooks/forbid.py` |
| `git.forbid_stash` | логическое | `true` | `hooks/forbid.py` |
| `secrets.dirs` | список | `[]` | `hooks/forbid.py` (вывод содержимого) |
| `forbid.network` | логическое | `true` | `hooks/forbid.py` |
| `forbid.herdr_direct` | логическое | `true` | `hooks/forbid.py` |
| `[[forbid.rules]]` | `words` — список, `reason` — строка | `[]` | `hooks/forbid.py` |
| `bitbucket.url` | строка | нет таблицы — без Bitbucket | `gates.py` (его используют `branches.py` и `week-stats.py`), навык `after-merge` |
| `bitbucket.token_file` | строка | — | те же |
| `bitbucket.user` | строка | — | только навык `gates` (текст; сценарии ключ не читают, `gates.py` берёт `role=AUTHOR` у токена) |
| `bitbucket.sets_repo` | строка | `""` | `week-stats.py` |
| `jenkins.env_file` | строка | нет таблицы — без Jenkins | `jwait.py`, `gates.py`, `facts.sh` |
| `jenkins.cli_url` | строка | `""` (тогда `JENKINS_URL`) | `jwait.py` |
| `jenkins.insecure_tls` | логическое | `false` | `jwait.py`, `gates.py` |
| `jenkins.fake_job` | строка | `""` | `week-stats.py` |
| `jenkins.nodes` | список | `[]` | навыки `inspect` и `dispatch` (текст; сценарии ключ не читают) |
| `jira.url` | строка | нет таблицы — тикеты не ведутся | `facts.sh`, навык `after-merge` |
| `jira.token_file` | строка | — | те же |
| `jira.ticket` | строка | — | `branches.py`, навыки `gates` и `after-merge` |
| `docs.rules` | список | `[]` | `rules-lint.py repeats`; правила проекта в агентах и навыках названы ссылкой на ключ |
| `docs.rules_optional` | список | `[]` | `rules-lint.py repeats` |
| `docs.review` | строка | `""` | `agents/worker.md`, навыки `dispatch` и `docs-sweep` (текст) |
| `docs.decisions` | строка | `""` | навык `decision`, `decision-number.sh`, `week-stats.py` |
| `docs.decision_insert` | строка | `""` | навык `decision`, `decision-number.sh` |
| `docs.decision_index` | строка | `""` | навык `decision` |
| `docs.prose` | строка | `""` | навык `decision` |
| `docs.lint` | строка | `""` | навык `docs-sweep` |
| `docs.sweep_area` | список | `["docs/", "*.md"]` | навык `docs-sweep` |
| `docs.sweep_exclude` | список | `[]` | навык `docs-sweep` |
| `docs.openspec` | список | `[]` | навык `after-merge` |
| `[[docs.post_change]]` | `paths` — список, `note` — строка | `[]` | навык `docs-sweep` |
| `[[busy.resources]]` | `name`, `why`, `until` — строки | `[]` | навыки `inspect` и `dispatch`, `reference/worker-rules.md` (текст); сценарии ключ не читают |
| `cleanup.keep_trees` | список | `[]` | `cleanup-candidates.py` |
| `cleanup.docker_keep_projects` | список | `[]` | `cleanup-candidates.py` |
| `stats.test_repos` | список | `[]` | `week-stats.py` |
| `stats.rules_paths` | список | `[".claude/"]` | `week-stats.py` |
| `stats.pipeline_paths` | список | `["scripts/", ".github/"]` | `week-stats.py` |
| `symbols.main` | строка | `""` | никто (проверяет только `duty_config.py check`): постоянный символ главной сессии, из наборов задач исключается |
| `symbols.tasks` | список | `[]` (тогда `scripts/symbols.txt`) | `dispatch.sh symbol` (`load_symbols`); `DUTY_SYMBOLS_FILE` перекрывает только этот ключ |
| `symbols.outside` | список | `[]` (тогда `scripts/symbols.txt`, как у `symbols.tasks`) | `dispatch.sh symbol` (`load_symbols`); только пользовательский слой: workspace вне репозиториев |

У массивов таблиц обязательны все поля. Символ — ровно одна кодовая
точка (`len == 1`): флаг из нескольких точек вроде «🏳️‍🌈» и символ
с селектором варианта не подходят. Символ не повторяется ни внутри
набора, ни между `main`, `tasks` и `outside`.

## Расширения

`dutylib.run_extensions(root, kind, timeout_s)` запускает исполняемые
файлы `<корень>/.duty/<kind>.d/*` по порядку имён; скрытые файлы
и каталоги пропускаются. Текущий каталог — корень проекта, окружение
дополнено переменными:

| Переменная | Значение |
|---|---|
| `DUTY_PROJECT` | корень проекта |
| `DUTY_CONFIG` | `<корень>/.duty/duty.toml` |
| `DUTY_PLUGIN` | каталог плагина |

Срок одного запуска — `timeout_s`; по его истечении группа процессов
расширения получает `SIGKILL`. Ненулевой код выхода, истёкший срок
и файл без права исполнения дают строку ошибки в итоге, а не исключение;
остальные расширения выполняются.

- `facts.d` — stdout из разделов Markdown, каждый с заголовка `## …`;
  `facts.sh` выводит их после своих разделов.
- `cleanup.d` — stdout из строк JSON, по объекту на строку, ключи
  `kind`, `sub`, `parent`, `what`, `size`, `verdict`, `cmd`; строки
  добавляются в таблицу `cleanup-candidates.py` как есть.

## Команды `duty_config.py`

Общие ключи: `--root <каталог>`, `--cwd <каталог>`. Запуск —
`python3 -I scripts/duty_config.py <команда>`.

| Команда | Что делает | Коды выхода |
|---|---|---|
| `root` | печатает корень | 0; 3 — корня нет |
| `get <ключ.через.точку> [--default <значение>]` | скаляр строкой (`true`/`false` для логических), список и таблица — JSON, путь `[paths]` — абсолютный | 0; 3 — нет ключа и `--default` |
| `json` | вся слитая конфигурация и служебные ключи `_root`, `_files`, `_mode` | 0 |
| `env [--prefix DUTY_CFG_]` | строки `export <ПРЕФИКС><ТАБЛИЦА>_<КЛЮЧ>=…` для `eval` в bash, плюс `<ПРЕФИКС>ROOT` и `<ПРЕФИКС>MODE`; списки — через запятую, таблицы и массивы таблиц не печатаются | 0 |
| `check` | проверка схемы, строки `файл: сообщение` | 0; 1 — находки |
| `init [--name <имя>]` | создаёт `<корень>/.duty/duty.toml` с `schema`, `[project] name` и закомментированными ключами | 0; 1 — файл уже есть; 3 — корня нет |

Ошибка TOML в любом слое — код 2 и сообщение в stderr у всех команд,
кроме `init`.

Python API (импорт по пути, как `gates` в `branches.py`):
`load(root=None, cwd=None) -> Config`; у `Config` — `root`, `mode`
(`project` или `general`), `name`, `files`, `get("a.b", default)`,
`path("paths.results")`, `has("bitbucket")`. Ошибка TOML —
исключение `ConfigError`.

## Пример: монорепозиторий

```toml
# monorepo-ocp/.duty/duty.toml
schema = 1

[project]
name = "monorepo-ocp"

[paths]
results = "../ocp-dev-stand/results"
busy = "../ocp-dev-stand/busy"
signals = "../ocp-dev-stand/signals"
sandbox = "../ocp-dev-stand/results/duty-tests/sandbox"

[git]
protected = ["dev", "dev2", "master"]
working = ["new_ocp", "dev-local"]
keep_prefixes = ["backup/", "saved/"]
task_prefixes = ["dev2-RP-", "bugfix/RP-", "chore/", "feature/"]
named_dev_branches = true
default_parent = "master"
parent_key = "parent"
forbid_add_all = true
forbid_stash = true

[secrets]
dirs = ["~/.config/axata"]

[forbid]
network = true
herdr_direct = true

[[forbid.rules]]
words = ["instances-down", "all"]
reason = "экземпляры стенда снимаются только своими номерами (legacy/devlab/AGENTS.md)"

[bitbucket]
url = "https://git.service-desk.site"
token_file = "~/.config/axata/bitbucket-http-token"
user = "a.pinchuk"
sets_repo = "legacy/release-sets"

[jenkins]
env_file = "~/.config/axata/jenkins-credentials.env"
cli_url = "http://10.200.100.8:8080/"
insecure_tls = true
fake_job = "z__fake"
nodes = ["HeavyCI_1", "QMS_DEV", "QMS_PROD", "QMS_FEATURE"]

[jira]
url = "https://jira.service-desk.site"
token_file = "~/.config/axata/jira-pat"
ticket = "RP-\\d+"

[docs]
rules = ["CLAUDE.md", ".claude/rules/*.md", "docs/isolation.md",
         "docs/decisions/08-agents-and-docs/0066-*.md",
         "docs/decisions/08-agents-and-docs/0067-*.md"]
rules_optional = ["~/.claude/CLAUDE.md"]
review = ".claude/rules/code-review.md"
decisions = "docs/decisions"
decision_insert = "python3 scripts/renumber-decisions.py insert"
decision_index = "python3 scripts/renumber-decisions.py --write-index"
prose = "docs/russian-prose.md"
lint = "python3 -I scripts/docs-lint.py"
sweep_area = ["docs/", "*.md", ".claude/rules/", "openspec/"]
sweep_exclude = ["openspec/changes/archive/", "docs/decisions/README.md"]
openspec = ["openspec", "legacy/queue-manager-service/openspec"]

[[docs.post_change]]
paths = ["docs/jenkins/"]
note = "just confluence-mirror"

[[busy.resources]]
name = "station"
why = "замер производительности на станции искажается любой соседней нагрузкой"
until = "замеры переезжают на выделенный узел"

[[busy.resources]]
name = "studio-<домен>"
why = "у домена студии Oktell один слушатель событий и одна очередь по набранному номеру"
until = "домен на каждого исполнителя (docs/studio/)"

[[busy.resources]]
name = "dev-stand-<N>"
why = "экземпляр стенда с его базой и портами принадлежит одному прогону"
until = "экземпляр создаётся и удаляется самим прогоном"

[cleanup]
keep_trees = ["../ocp-dev-stand/src/qms", "../ocp-dev-stand/src/ui",
              "../ocp-dev-stand/src/tests", "../ocp-dev-stand/src/jssip-phone"]
docker_keep_projects = ["ocp-stand"]

[stats]
test_repos = ["legacy/tests"]
rules_paths = [".claude/", "claude-plugins/"]
pipeline_paths = ["deploy/", "scripts/", "jenkins/", ".jenkins/"]

[symbols]
main = "🍀"
tasks = ["🌱", "🌲", "🌳", "🌴", "🌵", "🌾", "🌿", "🍃", "🪴", "🍏", "🍐",
         "🥝", "🍈", "🫒", "🥦", "🥬", "🫑", "🫛", "🫝", "🥑", "🌽"]
```

## Пример: dotfiles

```toml
# ~/.local/share/chezmoi/.duty/duty.toml
schema = 1

[project]
name = "dotfiles"

[git]
protected = ["main"]

[symbols]
main = "🏠"
tasks = ["🏡", "🏢", "🏣", "🏤", "🏦", "🏨", "🏩", "🏫", "🏬", "🏭", "🏯",
         "🏰", "💒", "🛖"]
```

Таблицы `[paths]` нет: результаты, реестр и сигналы — в
`~/.local/state/duty/dotfiles/`.

## Пример: duty-herdr

```toml
# ~/work/pets/duty-herdr/.duty/duty.toml
schema = 1

[project]
name = "duty-herdr"

[git]
protected = ["master"]
keep_prefixes = ["backup/", "saved/", "upstream/"]

[symbols]
main = "🦁"
tasks = ["🦄", "🦌", "🦊", "🦇", "🦍", "🦏", "🦒", "🦓", "🦔", "🦘", "🦙",
         "🦛", "🦝", "🦡", "🦣", "🦥", "🦦", "🦧", "🦨", "🦫", "🦬", "🦮",
         "🫎", "🫏", "🐂", "🐃", "🐄", "🐅", "🐆", "🐇", "🐈", "🐎", "🐏",
         "🐐", "🐑", "🐒", "🐕", "🐖", "🐗", "🐘", "🐨", "🐩", "🐪", "🐫",
         "🐭", "🐮", "🐯", "🐰", "🐼", "🐻", "🐺", "🐹", "🐷", "🐶", "🐵",
         "🐴", "🐱", "🐁", "🐀"]
```

## Пример: сам плагин duty

```toml
# claude-plugins/duty/.duty/duty.toml
schema = 1

[project]
name = "duty"

[symbols]
main = "🦑"
tasks = ["🐋", "🐙", "🐚", "🐟", "🐠", "🐡", "🐬", "🐳", "🦀", "🦈", "🦐",
         "🦞", "🦭", "🪼", "🫍"]
```

## Пример: пользовательский слой

```toml
# ~/.config/duty/config.toml
[paths]
state = "~/.local/state/duty"

[symbols]
outside = ["🍺", "🍻", "🍷", "🍸", "🍹", "🍵", "☕", "🍾", "🥂", "🥃", "🥛",
           "🧉", "🧋"]
```
