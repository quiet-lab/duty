#!/usr/bin/env bash
# Хук SessionStart: отдаёт главной сессии открытые workspace задач, реестр
# занятости и напоминание о /duty:dispatch. В сессии агента (DUTY_ROLE=worker)
# и вне herdr молчит. Любой сбой не должен мешать старту сессии.

[ "${DUTY_ROLE:-}" = worker ] && exit 0
[ "${HERDR_ENV:-}" = 1 ] || exit 0

root=${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
project=${CLAUDE_PROJECT_DIR:-$PWD}
command -v jq > /dev/null || exit 0

text=''
add() { text+="$1"$'\n'; }

if tasks=$(cd "$project" && "$root/scripts/dispatch.sh" list 2> /dev/null); then
    # В выводе заголовок и разделитель занимают две строки; остальное — задачи.
    if [ "$(printf '%s\n' "$tasks" | wc -l)" -gt 2 ]; then
        add "Открытые workspace задач (dispatch.sh list):"
        add "$tasks"
    fi
else
    add "herdr недоступен: перечень workspace задач не получен."
fi

if marks=$(python3 "$root/scripts/busy.py" list 2> /dev/null) && [ "$marks" != 'отметок нет' ] && [ -n "$marks" ]; then
    add "Реестр занятости (busy.py list):"
    add "$marks"
fi

if sigs=$(python3 -I "$root/scripts/signals.py" list --waits 2> /dev/null) && [ "$sigs" != 'ожиданий нет' ] && [ -n "$sigs" ]; then
    add "Ожидания сигналов задачами (signals.py list --waits; все сигналы — signals.py list):"
    add "$sigs"
fi

add 'Крупные задачи — `/duty:dispatch <имя> <модель>` (критерий и лесенка моделей — `skills/dispatch/SKILL.md` плагина); имя этой сессии для `--main` — из `ListAgents`.'

jq -n --arg ctx "$text" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
exit 0
