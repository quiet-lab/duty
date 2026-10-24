# Проба ask-user

## Цель

Проверить вопрос пользователю через `AskUserQuestion` с уведомлением herdr и запись ответа в `rules.md`.

## Следующие шаги

1. Прочитать `{{SANDBOX}}/repo/README.md`.
2. Чем дополнить README, не решено; два равноправных способа: (а) короткая строка назначения, (б) таблица модулей `sandbox_pkg`. Выбор за пользователем: задать один вопрос через `AskUserQuestion` с двумя вариантами и последствием каждого. Перед вопросом, если workspace не в фокусе, — `herdr notification show "Workspace <метка> ждёт ответа" --sound request` (проверка фокуса — как в п. 5 агента).
3. Ответ дописать строкой в раздел «Переданные» файла `$DUTY_TASK/rules.md`.
4. `git clone {{SANDBOX}}/repo $DUTY_TASK/work`; в клоне выполнить выбранное (правка `README.md`), `git add README.md && git commit -m "Проба ask-user"`. Push не выполнять.
5. Выполнить «Завершение».

## Команды проверки

- `tail -3 $DUTY_TASK/rules.md` — строка ответа; `git -C $DUTY_TASK/work log --oneline -2`.

## Принятые решения

- git и код монорепозитория не трогать; Jira, Bitbucket, Confluence, Jenkins не менять; push только в песочный `origin` (`{{SANDBOX}}/origin.git`), если разрешён ниже.
- Песочница `{{SANDBOX}}/` вне проекта: правки только в клонах внутри `$DUTY_TASK/work/`.
- Тесты песочницы: `{{SANDBOX}}/venv/bin/python -m pytest -q` (в системном `python3` pytest нет). Один тест падает намеренно: `tests/test_pricing.py::test_threshold_is_inclusive`.
- Единственный разрешённый вопрос пользователю — об этом выборе.
- Push запрещён.
- Workspace закрыть самому.

## Ожидание в report.md

Один вопрос с двумя вариантами, уведомление herdr (если не в фокусе), выбранный вариант, строка ответа в `rules.md`, коммит в клоне.

## Завершение

По окончании выполнить раздел «Завершение» агента `duty:worker` целиком: снять отметки реестра, закрыть свои вкладки, записать `report.md` и последний `state.md`, `SendMessage` главной сессии (три строки и путь к `report.md`), уведомление herdr (`herdr notification show`), и закрыть workspace (`herdr workspace close "$HERDR_WORKSPACE_ID"`), если правила не велят иного.

## Ссылки

- Правила задачи — `rules.md` рядом; устройство плагина — `{{PLUGIN}}/agents/worker.md`.
