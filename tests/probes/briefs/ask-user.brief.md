# Проба ask-user

## Цель

Проверить вопрос главной сессии файлом и сигналом (`ask.py`): шапку вопроса, уведомление herdr, ожидание ответа на необратимом шаге и применение ответа.

## Следующие шаги

1. Прочитать `{{SANDBOX}}/repo/README.md`; `git clone {{SANDBOX}}/repo $DUTY_TASK/work`.
2. Чем дополнить README, не решено; два равноправных способа: (а) короткая строка назначения, (б) таблица модулей `sandbox_pkg`. Для пробы шаг считается необратимым. Задать вопрос: `python3 -I {{PLUGIN}}/scripts/ask.py new readme --title 'Чем дополнить README песочницы' --step '2 — правка README' --depends 'текст коммита в клоне' --option 'строка назначения — одна строка, без таблицы' --option 'таблица модулей — по строке на модуль sandbox_pkg' --recommended 1 --irreversible`. Затем уведомление herdr по п. 5 агента.
3. Ждать ответа во вкладке: `bash {{PLUGIN}}/scripts/tab-run.sh answer -- 'python3 -I {{PLUGIN}}/scripts/ask.py wait 01'`; `tab_id` и PID ожидания — в `state.md`; ждать внутри хода вызовом Bash с таймаутом 600000 мс `bash {{PLUGIN}}/scripts/wait-done.sh "$DUTY_TASK/runs/answer" --timeout-s 590`, вывод `timeout` — запустить снова. Ответ даст главная сессия; самому не отвечать.
4. Прочитать ответ из раздела «Ответ» файла `$DUTY_TASK/questions/01-readme.md`; в клоне выполнить выбранное (правка `README.md`), `git add README.md && git commit -m "Проба ask-user"`. Push не выполнять.
5. Выполнить «Завершение».

## Команды проверки

- `python3 -I {{PLUGIN}}/scripts/ask.py list` — вопрос `ask-user 01` отвечен; `git -C $DUTY_TASK/work log --oneline -2`.

## Принятые решения

- git и код монорепозитория не трогать; Jira, Bitbucket, Confluence, Jenkins не менять; push только в песочный `origin` (`{{SANDBOX}}/origin.git`), если разрешён ниже.
- Песочница `{{SANDBOX}}/` вне проекта: правки только в клонах внутри `$DUTY_TASK/work/`.
- Тесты песочницы: `{{SANDBOX}}/venv/bin/python -m pytest -q` (в системном `python3` pytest нет). Один тест падает намеренно: `tests/test_pricing.py::test_threshold_is_inclusive`.
- Единственный вопрос — об этом выборе, через `ask.py`; `AskUserQuestion` не вызывать.
- Push запрещён.
- Workspace закрыть самому.

## Ожидание в report.md

Файл вопроса с шапкой (символ, задача, модель, цель, шаг, что зависит, необратимо, рекомендуемый), уведомление herdr (если не в фокусе), ожидание во вкладке, выбранный в ответе вариант, коммит в клоне.

## Завершение

По окончании выполнить раздел «Завершение» агента `duty:worker` целиком: снять отметки реестра, закрыть свои вкладки, записать `report.md` и последний `state.md`, `SendMessage` главной сессии (три строки и путь к `report.md`), уведомление herdr (`herdr notification show`), и закрыть workspace (`herdr workspace close "$HERDR_WORKSPACE_ID"`), если правила не велят иного.

## Ссылки

- Правила задачи — `rules.md` рядом; устройство плагина — `{{PLUGIN}}/agents/worker.md`.
