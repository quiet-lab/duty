# Пробы flow плагина duty

Регрессионный набор для `scripts/dispatch.sh`, `scripts/tab-run.sh`,
`scripts/wait-done.sh`, `scripts/busy.py` и агента `agents/worker.md`.
Каждая проба — записка и правила для `dispatch.sh`; исход проверяет
главная сессия командами из таблицы. Пробы идут в живом herdr, расходуют
токены агентов и меняют только песочницу и каталоги задач.

## Песочница

`make-sandbox.sh <каталог>` создаёт её с нуля:

- `repo/` — ветвь `master`, пакет `sandbox_pkg` из двух модулей и тесты,
  один тест падает намеренно (`test_threshold_is_inclusive`: порог `>`
  вместо `>=`);
- `origin.git` — голый репозиторий, `origin` для `repo`;
- `marker/` — каталог файлов-отметок (пробы 2 и 3);
- `venv/` — окружение с pytest (в системном `python3` его нет).

Песочница по умолчанию — `../ocp-dev-stand/results/duty-tests/sandbox`
от корня монорепозитория. Каталог уже есть и не пуст — сценарий
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
`dispatch.sh`. Порядок запуска: 3, 2, 6, 1, 7, 5, 4.

В таблице `$SB` — песочница, `$TASK` — каталог задачи
`../ocp-dev-stand/results/<имя>/`, `$DISPATCH` — `scripts/dispatch.sh`.

| № | Имя задачи | Модель | Что проверяет | Ожидаемый исход | Проверка главной сессией |
|---|---|---|---|---|---|
| 3 | `tab-running` | haiku | вкладка `tab-run.sh`, ожидание `wait-done.sh`, закрытие вкладки агентом | после отметки `done rc=0`, вкладка и workspace закрыты | `touch $SB/marker/go`; `cat $TASK/runs/waiter/done`; `$DISPATCH list` |
| 2 | `rules-midway` | haiku | перечитывание `rules.md` перед push | push не выполнен, в отчёте строка запрета | запрет дописать в `$TASK/rules.md` до отметки; отметку создать после появления `runs/tests` и записи `state.md`: `touch $SB/marker/rules-go`; `git -C $SB/origin.git branch` (нет `probe/rules-midway`); `grep -n push $TASK/report.md` |
| 6 | `overflow-handoff` | haiku | передача при переполнении контекста | `brief.md` с «Передача», `state.md`, `report.md` с «передача», workspace открыт; `second-step.txt` нет | `grep Передача $TASK/brief.md`; `grep -i передача $TASK/report.md`; `$DISPATCH list`; `ls $TASK/second-step.txt` (нет) |
| 6б | `overflow-handoff` (продолжение) | haiku | продолжение с записанного шага | `second-step.txt` со строкой «второй шаг выполнен», workspace закрыт | `herdr agent prompt overflow-handoff "<текст>"` (текст — `briefs/overflow-handoff.resume.txt`; печатает `run-probe.sh 6b`); `cat $TASK/second-step.txt` |
| 1 | `ask-user` | haiku | `AskUserQuestion` с уведомлением herdr, запись ответа | один вопрос с двумя вариантами, уведомление, строка ответа в `rules.md` | ответить в окне workspace; `tail -3 $TASK/rules.md`; `git -C $TASK/work log --oneline -2` |
| 7 | `neighbours` | — | `/duty:cleanup`, `/duty:gates now` при открытом workspace пробы 6 | workspace виден в занятости, не закрыт | команды в `briefs/neighbours.md`; `$DISPATCH list` |
| 5 | `jwait-probe` | haiku | `jwait.sh` во вкладке | номер сборки, результат, длительность; либо «сборок в ходу нет» | `cat $TASK/runs/jenkins/done`; `cat $TASK/runs/jenkins/meta.txt`; `head $TASK/report.md` |
| 4 | `opus-subagents` | opus | субагент sonnet с явной моделью, таблица в отчёте | тесты зелёные, ветвь в `origin.git` | `git -C $SB/origin.git branch` (есть `probe/fix-pricing`); `cat $TASK/runs/tests2/done`; `grep -i sonnet $TASK/report.md` |

Записки и правила — `briefs/<имя>.brief.md`, `briefs/<имя>.rules.txt`.
Имя для `dispatch.sh` совпадает с именем задачи в таблице, цифрового
префикса нет.

## Уборка после проб

1. Закрыть оставшиеся workspace: `dispatch.sh close <имя>`.
2. Удалить каталоги задач проб в `../ocp-dev-stand/results/`: `tab-running`,
   `rules-midway`, `overflow-handoff`, `ask-user`, `jwait-probe`,
   `opus-subagents`.
3. Вернуть песочницу к исходному состоянию: `make-sandbox.sh <каталог>
   --reset` — удаляет ветви `probe/*` в `origin.git` и в `repo`, чистит
   `marker/` и `.render/`, восстанавливает рабочее дерево `repo` до `master`.
