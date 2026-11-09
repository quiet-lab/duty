# Пробы flow плагина duty

Регрессионный набор для сценариев плагина `dispatch.sh`, `tab-run.sh`,
`wait-done.sh`, `busy.py` и агента `agents/worker.md`.
Каждая проба — записка и правила для `dispatch.sh`; исход проверяет
главная сессия командами из таблицы. Пробы идут в живом herdr, расходуют
токены агентов и меняют только песочницу и каталоги задач.

## Песочница

`make-sandbox.sh <каталог>` создаёт её с нуля:

- `repo/` — ветвь `master`, пакет `sandbox_pkg` из двух модулей и тесты,
  один тест падает намеренно (`test_threshold_is_inclusive`: порог `>`
  вместо `>=`);
- `origin.git` — голый репозиторий, `origin` для `repo`;
- `marker/` — каталог файлов-отметок (пробы 2, 3, 9 и 9б);
- `venv/` — окружение с pytest (в системном `python3` его нет).

Песочница по умолчанию — `paths.sandbox` конфигурации
(`duty_config.py get paths.sandbox`). Каталог уже есть и не пуст — сценарий
отказывает; `--reset` возвращает существующую песочницу к исходному
состоянию (см. «Уборка»).

## Запуск

```sh
tests/probes/run-probe.sh <номер> [--sandbox <каталог>] [--main <имя сессии>] [--dry]
```

Сценарий подставляет в записку и правила `{{SANDBOX}}` (каталог песочницы)
и `{{PLUGIN}}` (каталог плагина) в `<песочница>/.render/`, вызывает
`dispatch.sh <имя> <модель> --brief … --rules … --main …` и печатает
команды проверки. С `--dry` только рендерит и печатает команду
`dispatch.sh`. Порядок запуска: 3, 2, 6, 7, 5, 4, 9, 9б.

В таблице `$SB` — песочница, `$TASK` — каталог задачи
`<results>/<имя>/` (`paths.results`), `$DISPATCH` — `dispatch.sh` плагина.

| № | Имя задачи | Модель | Что проверяет | Ожидаемый исход | Проверка главной сессией |
|---|---|---|---|---|---|
| 3 | `tab-running` | haiku | вкладка `tab-run.sh`, ожидание `wait-done.sh`, самозакрытие вкладки по `done` без закрытия агентом | после отметки `done rc=0`, вкладка закрылась сама, workspace закрыт | `touch $SB/marker/go`; `cat $TASK/runs/waiter/done`; до закрытия workspace агентом — `herdr tab list --workspace <workspace задачи>` без `tab_id` из `$TASK/runs/waiter/tab`; `grep 'закрылась сама' $TASK/report.md`; `$DISPATCH list` |
| 2 | `rules-midway` | haiku | перечитывание `rules.md` перед push | push не выполнен, в отчёте строка запрета | запрет дописать в `$TASK/rules.md` до отметки; отметку создать после появления строки «Watches established.» во вкладке агента (агент дошёл до ожидания): `touch $SB/marker/rules-go`; `git -C $SB/origin.git branch` (нет `probe/rules-midway`); `grep -n push $TASK/report.md` |
| 6 | `overflow-handoff` | haiku | передача при переполнении контекста | `brief.md` с «Передача», `state.md`, `report.md` с «передача», workspace открыт; `second-step.txt` нет | `grep Передача $TASK/brief.md`; `grep -i передача $TASK/report.md`; `$DISPATCH list`; `ls $TASK/second-step.txt` (нет) |
| 6б | `overflow-handoff` (продолжение) | haiku | продолжение с записанного шага | `second-step.txt` со строкой «второй шаг выполнен», workspace закрыт | `herdr agent prompt overflow-handoff "<текст>"` (текст — `briefs/overflow-handoff.resume.txt`; печатает `run-probe.sh 6b`); `cat $TASK/second-step.txt` |
| 7 | `neighbours` | — | `/duty:cleanup`, `/duty:gates now` при открытом workspace пробы 6 | workspace виден в занятости, не закрыт | команды в `briefs/neighbours.md`; `$DISPATCH list` |
| 5 | `jwait-probe` | haiku | `jwait.sh` во вкладке (требует раздел `[jenkins]`) | номер сборки, результат, длительность; либо «сборок в ходу нет» | `cat $TASK/runs/jenkins/done`; `cat $TASK/runs/jenkins/meta.txt`; `head $TASK/report.md` |
| 4 | `opus-subagents` | opus | субагент sonnet с явной моделью, таблица в отчёте | тесты зелёные, ветвь в `origin.git` | `git -C $SB/origin.git branch` (есть `probe/fix-pricing`); `cat $TASK/runs/tests2/done`; `grep -i sonnet $TASK/report.md` |
| 8 | `push-wake` | haiku | толчок `done` в простаивающую и работающую сессию, отказ фона в субагенте, `SubagentStop`, блокировка `Stop`, агент отсутствует | все сценарии PASS, вкладки закрыты | `tests/probes/push-wake/run.sh <каталог> setup\|A\|B\|C\|D\|E\|F\|teardown`: сценарий сам печатает PASS или FAIL |
| 9 | `ask-workspace` | sonnet | вопрос исполнителя пользователю в его workspace (`agents/worker.md`, п. 5); независимые шаги до ответа | до ответа панель `blocked`, диалог с «По документам:», `ask-step1` и `decide-me` есть, `ask-step3` нет, `longrun` идёт, сообщений главной сессии нет; после ответа — `ask-step3`, `longrun-done`, решение по `decide-me`, один вопрос в отчёте | `herdr agent list`; `herdr agent read $PANE --source detection --format text`; `ls $SB/marker/`; `cat $TASK/runs/longrun/done`; ответ — выбор в диалоге клавишами `herdr pane send-keys $PANE <клавиши>`, затем `touch $SB/marker/ask-go`; `grep -A3 'Заданные вопросы' $TASK/report.md`; `$DISPATCH list` |
| 9б | `ask-coordinator` | opus | координатор переносит вопрос субагента пользователю в свой workspace, не ожидая другого субагента | до ответа панель `blocked`, диалог с «По документам:», `coord-b-step1` есть, `coord-a-done` и `done` вкладки `tests-a` нет, сообщений главной сессии нет; после — `coord-a-done`, решение по `coord-decide`, отчёт с вопросом и моделями | то же с `coord-go`; `grep -A3 'Заданные вопросы' $TASK/report.md`; `grep -i 'Доля субагентов' $TASK/report.md`; `$DISPATCH list` |

Записки и правила — `briefs/<имя>.brief.md`, `briefs/<имя>.rules.txt`.
Имя для `dispatch.sh` совпадает с именем задачи в таблице, цифрового
префикса нет.

## Уборка после проб

1. Закрыть оставшиеся workspace: `dispatch.sh close <имя>`.
2. Удалить каталоги задач проб в каталоге результатов (`paths.results`): `tab-running`,
   `rules-midway`, `overflow-handoff`, `jwait-probe`,
   `opus-subagents`, `ask-workspace`, `ask-coordinator`.
3. Вернуть песочницу к исходному состоянию: `make-sandbox.sh <каталог>
   --reset` — удаляет ветви `probe/*` в `origin.git` и в `repo`, чистит
   `marker/` и `.render/`, восстанавливает рабочее дерево `repo` до `master`.
