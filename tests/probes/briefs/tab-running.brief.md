# Проба tab-running

## Цель

Проверить вкладку `tab-run.sh` с долгой командой и ожидание `wait-done.sh`: вкладка идёт до отметки главной сессии, агент закрывает её сам.

## Следующие шаги

1. Запустить вкладку: `bash {{PLUGIN}}/scripts/tab-run.sh waiter --cwd {{SANDBOX}} -- 'inotifywait -e create -e moved_to {{SANDBOX}}/marker'`. В ответе JSON с `tab_id`.
2. Записать `$DUTY_TASK/state.md`: шаг 2, вкладка `waiter`, `tab_id`, следующий шаг — ждать `done`.
3. Ждать внутри хода вызовом Bash с таймаутом 600000 мс: `bash {{PLUGIN}}/scripts/wait-done.sh "$DUTY_TASK/runs/waiter" --timeout-s 590`, код 124 — запустить снова. Файл-отметку в `{{SANDBOX}}/marker/` создаст главная сессия; самому её не создавать, не опрашивать, не использовать `sleep`.
4. По `done rc=0` закрыть вкладку: `herdr tab close <tab_id>`; обновить `state.md`.
5. Выполнить «Завершение».

## Команды проверки

- `cat $DUTY_TASK/runs/waiter/done` — `0`; `ls {{SANDBOX}}/marker` — файл есть.

## Принятые решения

- git и код монорепозитория не трогать; Jira, Bitbucket, Confluence, Jenkins не менять; push только в песочный `origin` (`{{SANDBOX}}/origin.git`), если разрешён ниже.
- Песочница `{{SANDBOX}}/` вне проекта: правки только в клонах внутри `$DUTY_TASK/work/`.
- Тесты песочницы: `{{SANDBOX}}/venv/bin/python -m pytest -q` (в системном `python3` pytest нет). Один тест падает намеренно: `tests/test_pricing.py::test_threshold_is_inclusive`.
- Вопросов не задавать (`ask.py` не вызывать).
- Workspace закрыть самому.

## Ожидание в report.md

Строка «done rc=0», `tab_id` закрытой вкладки, длительность из `runs/waiter/meta.txt`; вкладка закрыта агентом.

## Завершение

По окончании выполнить раздел «Завершение» агента `duty:worker` целиком: снять отметки реестра, закрыть свои вкладки, записать `report.md` и последний `state.md`, `SendMessage` главной сессии (три строки и путь к `report.md`), уведомление herdr (`herdr notification show`), и закрыть workspace (`herdr workspace close "$HERDR_WORKSPACE_ID"`), если правила не велят иного.

## Ссылки

- Правила задачи — `rules.md` рядом; устройство плагина — `{{PLUGIN}}/agents/worker.md`.
