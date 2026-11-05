#!/usr/bin/env bash
# Хук SessionStart: отдаёт главной сессии открытые workspace задач с шагом
# из state.md, задачи, которые можно продолжить, очередь вопросов агентов,
# реестр занятости, ожидания сигналов (с пометкой «⚠ просрочено +N мин» у
# ожиданий и отметок дольше срока) и напоминание о /duty:dispatch.
# Находки проверки схемы конфигурации (duty_config.py check) и подсказка
# про общий режим (нет .duty/) дописываются после перечней.
# В сессии агента (DUTY_ROLE=worker) отдаёт содержимое
# <корень проекта>/.duty/worker-context.md, если файл есть, иначе молчит;
# вне herdr сессия главной не получает ничего. Любой сбой не должен мешать
# старту сессии.

root=${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
project=${CLAUDE_PROJECT_DIR:-$PWD}
config_py="$root/scripts/duty_config.py"

if [ "${DUTY_ROLE:-}" = worker ]; then
    command -v jq > /dev/null || exit 0
    proj=${DUTY_PROJECT:-$(cd "$project" && python3 -I "$config_py" root 2> /dev/null)} || exit 0
    context="$proj/.duty/worker-context.md"
    [ -n "$proj" ] && [ -s "$context" ] || exit 0
    jq -n --arg ctx "Контекст проекта (.duty/worker-context.md):"$'\n'"$(< "$context")" \
        '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
    exit 0
fi

[ "${HERDR_ENV:-}" = 1 ] || exit 0
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

if stalled=$(cd "$project" && "$root/scripts/dispatch.sh" stalled 2> /dev/null) \
    && [ "$(printf '%s\n' "$stalled" | wc -l)" -gt 2 ]; then
    add "Задачи без живого агента с незавершённым state.md (dispatch.sh stalled):"
    add "$(printf '%s\n' "$stalled" | head -12)"
    add 'Продолжить задачу <имя>: `bash '"$root"'/scripts/dispatch.sh <имя> <модель> --resume` — новый агент читает brief.md и state.md и продолжает с записанного шага; завершённую задачу отметить строкой «Шаг: завершено» в её state.md.'
fi

if questions=$(python3 -I "$root/scripts/ask.py" list --open 2> /dev/null) && [ "$questions" != 'вопросов нет' ] && [ -n "$questions" ]; then
    add "Вопросы агентов без ответа (ask.py list --open):"
    add "$questions"
    add 'Ответить со ссылкой на запись решения, спецификацию или ответ пользователя: `python3 -I '"$root"'/scripts/ask.py answer <задача> <NN> '"'"'<ответ>'"'"' --ref '"'"'<ссылка>'"'"'`. Ответа в записях нет — передать пользователю по одному через AskUserQuestion с шапкой «<символ> <задача> · <модель>: цель, шаг, что зависит от ответа» и записать его ответ с `--ref '"'"'ответ пользователя'"'"'`; решение шире задачи — сразу `/duty:decision`.'
fi

if marks=$(python3 "$root/scripts/busy.py" list 2> /dev/null) && [ "$marks" != 'отметок нет' ] && [ -n "$marks" ]; then
    add "Реестр занятости (busy.py list):"
    add "$marks"
fi

if sigs=$(python3 -I "$root/scripts/signals.py" list --waits 2> /dev/null) && [ "$sigs" != 'ожиданий нет' ] && [ -n "$sigs" ]; then
    add "Ожидания сигналов задачами (signals.py list --waits; все сигналы — signals.py list):"
    add "$sigs"
fi

if findings=$(cd "$project" && python3 -I "$config_py" check 2>&1) && [ -z "$findings" ]; then
    :
elif [ -n "$findings" ]; then
    add "Конфигурация .duty/duty.toml: $(printf '%s' "$findings" | paste -sd';' - | sed 's/;/; /g')"
fi

if mode=$(cd "$project" && python3 -I "$config_py" json 2> /dev/null | jq -r '._mode') && [ "$mode" = general ]; then
    add "Проекта .duty/ нет: плагин в общем режиме; создать — python3 -I $root/scripts/duty_config.py init"
fi

if printf '%s' "$text" | grep -q '⚠ просрочено\|⚠ без срока'; then
    add 'Есть просроченные ожидания (⚠): задача ждёт дольше срока. Спросить агента, что ждёт и почему (`SendMessage` или `herdr pane read`), не продлевать молча; причина — зависшая операция или сигнал, который не придёт, — решается по правилу починки падающего набора (скилл dispatch, «Дальше»).'
fi

add 'Крупные задачи — `/duty:dispatch <имя> <модель>` (критерий и лесенка моделей — `skills/dispatch/SKILL.md` плагина); имя этой сессии для `--main` — из `ListAgents`.'

jq -n --arg ctx "$text" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
exit 0
