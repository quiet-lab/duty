# Проба neighbours: соседние навыки при открытом workspace

Выполняет главная сессия, пока открыт workspace пробы overflow-handoff (№ 6) (после первой половины он открыт по правилам).

1. `/duty:cleanup` без `apply`: workspace `overflow-handoff` не предложен к закрытию как завершённый (в `report.md` пометка «передача»); ничего не удалено.
2. `/duty:gates now`: workspace виден в разделе занятости (агент, состояние).
3. Проверка: `bash {{PLUGIN}}/scripts/dispatch.sh list` показывает `overflow-handoff`; `herdr workspace list | jq -r '.result.workspaces[].label'` содержит его; каталог задачи цел.
