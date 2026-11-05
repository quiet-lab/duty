# Проба opus-subagents

## Цель

Проверить, что агент на opus поручает правку субагенту на sonnet с явной моделью и путями, а не правит сам, и отчитывается таблицей с моделями.

## Критерий готовности

Все шаги раздела «Следующие шаги» выполнены, команды раздела «Команды проверки» дают ожидаемое, итоговый отчёт есть: `test -s "$DUTY_TASK/report.md"`.

## Условие остановки

Шаг не выполняется или проверка падает и не чинится правкой за один подход — остановка и запись причины в `report.md`; вопрос вне пробы не задаётся.

## Следующие шаги

1. `git clone {{SANDBOX}}/repo $DUTY_TASK/work`; в клоне `git switch -c probe/fix-pricing`.
2. Вкладкой прогнать тесты: `bash {{PLUGIN}}/scripts/tab-run.sh tests1 --cwd $DUTY_TASK/work -- '{{SANDBOX}}/venv/bin/python -m pytest -q'`; ждать внутри хода вызовом Bash с таймаутом 600000 мс `wait-done.sh "$DUTY_TASK/runs/tests1" --timeout-s 590`, вывод `timeout` — запустить снова; `tab_id` в `state.md`. Найти причину единственного падения.
3. Исправление кода и, если нужно, правку теста поручить инструментом `Agent` с `model: sonnet` (назвать явно). В поручении — пути `$DUTY_TASK/brief.md`, `$DUTY_TASK/rules.md`, клона `$DUTY_TASK/work` и причина падения; без пересказа записки. Самому код не править.
4. Повторный прогон вкладкой `tests2` (новое имя), ждать `done`; ожидаем `rc=0`.
5. `git add` только изменённых файлов по путям; коммит «Исправление порога скидки».
6. Перед push перечитать `rules.md`; `git push origin probe/fix-pricing` (разрешён).
7. Выполнить «Завершение».

## Команды проверки

- `git -C {{SANDBOX}}/origin.git branch` — есть `probe/fix-pricing`.
- `cat $DUTY_TASK/runs/tests2/done` — `0`.

## Принятые решения

- репозиторий проекта не трогать; Jira, Bitbucket, Confluence, Jenkins не менять; push только в песочный `origin` (`{{SANDBOX}}/origin.git`), если разрешён ниже.
- Песочница `{{SANDBOX}}/` вне проекта: правки только в клонах внутри `$DUTY_TASK/work/`.
- Тесты песочницы: `{{SANDBOX}}/venv/bin/python -m pytest -q` (в системном `python3` pytest нет). Один тест падает намеренно: `tests/test_pricing.py::test_threshold_is_inclusive`.
- Вопросов не задавать (`ask.py` не вызывать).
- Push ветви `probe/fix-pricing` в песочный `origin` разрешён.
- Субагентов запускать на `sonnet`; Fable не использовать.
- Workspace закрыть самому.

## Ожидание в report.md

Таблица шагов (шаг, исполнитель, результат), модель каждого субагента, строка «Доля субагентов по моделям», модель этой задачи (opus) и признак лесенки, по которому она выбрана (разбор причин — opus, правка по найденной причине — sonnet). Тесты зелёные, ветвь в `origin.git`.

## Завершение

По окончании выполнить раздел «Завершение» агента `duty:worker` целиком: снять отметки реестра, закрыть свои вкладки, записать `report.md` и последний `state.md`, `SendMessage` главной сессии (три строки и путь к `report.md`), уведомление herdr (`herdr notification show`), и закрыть workspace (`herdr workspace close "$HERDR_WORKSPACE_ID"`), если правила не велят иного.

## Ссылки

- Правила задачи — `rules.md` рядом; устройство плагина — `{{PLUGIN}}/agents/worker.md`.
- Прочитанное при постановке: `{{PLUGIN}}/agents/worker.md`, `{{PLUGIN}}/reference/worker-rules.md`.
