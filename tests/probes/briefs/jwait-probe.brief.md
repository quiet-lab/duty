# Проба jwait-probe

## Цель

Проверить `jwait.sh`: ожидание идущей сборки Jenkins по событию во вкладке и итог в файлы.

## Критерий готовности

Все шаги раздела «Следующие шаги» выполнены, команды раздела «Команды проверки» дают ожидаемое, итоговый отчёт есть: `test -s "$DUTY_TASK/report.md"`.

## Условие остановки

Шаг не выполняется или проверка падает и не чинится правкой за один подход — остановка и запись причины в `report.md`; вопрос вне пробы не задаётся.

## Следующие шаги

1. Запустить вкладку: `bash {{PLUGIN}}/scripts/tab-run.sh jenkins -- '{{PLUGIN}}/scripts/jwait.sh dev2/e2e last'`; `tab_id` записать в `state.md`. Формат — `jwait.sh <задание> <номер|last>`; итог пишется в `$DUTY_TASK/runs/jenkins-dev2-e2e-last/` (`console.log`, `report.md`, `done`).
2. Если сборок в ходу нет и вкладка сразу завершилась (`done` уже есть) — записать это в отчёт и перейти к «Завершение».
3. Иначе ждать внутри хода вызовом Bash с таймаутом 600000 мс: `bash {{PLUGIN}}/scripts/wait-done.sh "$DUTY_TASK/runs/jenkins" --timeout-s 590`, вывод `timeout` — запустить снова.
4. В отчёт: номер сборки и результат (из `runs/jenkins-dev2-e2e-last/report.md`), длительность ожидания из `runs/jenkins/meta.txt` (`finished_ms - started_ms`), код из `done`.
5. Закрыть вкладку; выполнить «Завершение».

## Команды проверки

- `cat $DUTY_TASK/runs/jenkins/done`; `head -20 $DUTY_TASK/runs/jenkins-dev2-e2e-last/report.md`.

## Принятые решения

- git и код монорепозитория не трогать; Jira, Bitbucket, Confluence, Jenkins не менять; push только в песочный `origin` (`{{SANDBOX}}/origin.git`), если разрешён ниже.
- Песочница `{{SANDBOX}}/` вне проекта: правки только в клонах внутри `$DUTY_TASK/work/`.
- Тесты песочницы: `{{SANDBOX}}/venv/bin/python -m pytest -q` (в системном `python3` pytest нет). Один тест падает намеренно: `tests/test_pricing.py::test_threshold_is_inclusive`.
- Вопросов не задавать (`ask.py` не вызывать).
- Сборки Jenkins не запускать и не останавливать; только ждать.
- Workspace закрыть самому.

## Ожидание в report.md

Номер сборки, результат, длительность ожидания; либо «сборок в ходу нет».

## Завершение

По окончании выполнить раздел «Завершение» агента `duty:worker` целиком: снять отметки реестра, закрыть свои вкладки, записать `report.md` и последний `state.md`, `SendMessage` главной сессии (три строки и путь к `report.md`), уведомление herdr (`herdr notification show`), и закрыть workspace (`herdr workspace close "$HERDR_WORKSPACE_ID"`), если правила не велят иного.

## Ссылки

- Правила задачи — `rules.md` рядом; устройство плагина — `{{PLUGIN}}/agents/worker.md`.
- Прочитанное при постановке: `{{PLUGIN}}/agents/worker.md`, `{{PLUGIN}}/reference/worker-rules.md`.
