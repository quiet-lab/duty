---
description: После слияния pull request'а: удалить ветвь в origin, перевести тикет RP в «Готово» со ссылкой, отметить задачи tasks.md, убрать местные worktree и ветвь. todo.local.md не трогает.
argument-hint: "<репозиторий>#<номер> | <URL pull request'а>"
context: fork
model: sonnet
agent: duty:runner
allowed-tools: Bash, Read, Edit
---

Цель: после слияния одного pull request'а привести в порядок ветви,
тикет и задачи OpenSpec, не трогая чужого.

Переданные аргументы вызова (подставлены при запуске, пустая строка —
аргументов нет): «$ARGUMENTS».

Допустимые значения: `<путь сабмодуля или слаг>#<номер>` (например
`legacy/queue-manager-service#2536`, `tests#647`) или ссылка
`https://git.service-desk.site/projects/<КЛЮЧ>/repos/<слаг>/pull-requests/<номер>`.
Пусто или не разбирается — итог из одной строки об этом.

Доступы и команды — `${CLAUDE_PLUGIN_ROOT}/reference/access.md`,
защищённые ветви — `${CLAUDE_PLUGIN_ROOT}/README.md`, раздел «Раскладка».
Корень монорепозитория — `${CLAUDE_PROJECT_DIR}`.

## Шаги

1. **Найти pull request.** Ключ проекта и слаг — по таблице
   «Репозитории Bitbucket» в `access.md`. `GET …/pull-requests/<номер>`:
   нужны `state`, `fromRef.displayId` (ветвь), `toRef.displayId`,
   `title`, `links.self[0].href`. Готово, если `state` = `MERGED`;
   иначе остановка: «pull request не слит (state)», остальные шаги
   не выполняются.
2. **Ветвь в origin.** Защищённая или чужая (README плагина, «Раскладка») — не трогать,
   в «Решения за пользователем». Иначе:
   `git -C <сабмодуль> push origin --delete <ветвь>`. Готово:
   `git -C <сабмодуль> ls-remote --heads origin <ветвь>` пуст. Ветвь
   уже удалена — отметить «уже удалена».
3. **Тикет.** Номер `RP-<номер>` — из имени ветви, иначе из заголовка.
   Нет — шаг пропускается с пометкой. Если тикет ведут ещё ветви
   с открытыми pull request'ами (`GET /rest/api/1.0/dashboard/pull-requests?state=OPEN&role=AUTHOR`,
   ветви с тем же `RP-<номер>`) — тикет не переводить, перечислить их
   в «Решения за пользователем». Иначе: `GET …/transitions`, выбрать
   переход с `to.statusCategory.key` = `done`; нет такого перехода —
   в «Решения за пользователем» со списком переходов. Комментарий
   со ссылкой на pull request (`201`), затем переход (`204`). Готово:
   `GET /rest/api/2/issue/RP-<номер>?fields=status` даёт статус
   категории `done`.
4. **OpenSpec.** Найти задачи, где упомянуты тикет или ветвь:

   ```sh
   grep -n -e 'RP-<номер>' -e '<ветвь>' \
     "${CLAUDE_PROJECT_DIR}"/openspec/changes/*/tasks.md \
     "${CLAUDE_PROJECT_DIR}"/legacy/queue-manager-service/openspec/changes/*/tasks.md
   ```

   Отметить `- [x]` только строки `- [ ]`, чей текст целиком выполнен
   этим слиянием (ворота, слияние в `dev2` по этому тикету). Сомнение —
   не отмечать, строку в «Решения за пользователем». Файлы сабмодуля
   QMS коммитить в сабмодуле по пути (`git add <файл>`) коммитом
   по-русски с `Co-Authored-By` модели; в монорепозитории правки
   не коммитить и не отправлять — перечислить в итоге. Готово:
   `grep -c '\- \[ \]'` по файлу уменьшился на число отметок.
5. **Местное.** Worktree с этой ветвью:
   `git -C <сабмодуль> worktree list --porcelain`. Для каждого: дерево
   чистое (`git -C <путь> status --porcelain` пуст) и без процесса
   внутри — `git -C <сабмодуль> worktree remove <путь>`; иначе
   оставить с причиной. Затем `git -C <сабмодуль> branch -d <ветвь>`
   (не `-D`; отказ — оставить с причиной). Готово: `git worktree list`
   и `git branch --list <ветвь>` её не показывают.

## Итог

По `${CLAUDE_PLUGIN_ROOT}/reference/report.md`. Таблица:

| Шаг | Объект | Результат | Проверка |

Затем «Решения за пользователем» и «Что не проверено».

Запрещено: сливать pull request'ы; `todo.local.md` (его ведёт `/handoff`);
`/sync-parents`; коммит и отправка монорепозитория; `--force`;
удаление защищённых и чужих ветвей; завершение процессов; повтор
после 401/403 от Bitbucket.
