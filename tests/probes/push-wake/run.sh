#!/usr/bin/env bash
# Проба толкающего ожидания плагина duty: tab-run.sh будит агента задачи
# сообщением в панель, хуки task-guard.py не пускают субагента в фон и
# сторожат остановку подзадач без итога.
#
# Использование:
#   run.sh <каталог пробы> setup|A|B|C|D|E|F|teardown
#
# Сценарии (по одному вызову, порядок A..F; D и E зависят от состояния сессии):
#   setup     вкладка и агент push-probe (haiku, --agent duty:worker) в текущем
#             workspace ($HERDR_WORKSPACE_ID), brief.md со служебным блоком
#   A         простой агент просыпается по «done rc=0 <прогон>» от tab-run.sh
#   B         работающему агенту строка встаёт в очередь и доходит в транскрипт
#   C         координатор (нет agent_id в хуке) может Bash в фоне
#   D         субагент: Bash в фоне и Monitor отвергнуты; SubagentStop пишет
#             subtasks/s1.stopped и шлёт координатору строку
#   E         Stop блокирует конец хода при остановившейся подзадаче без итога
#   F         агента из служебного блока нет: push.log «не отправлена», прогон 0
#   teardown  закрыть вкладки, записанные в <каталог>/state.env
#
# Вывод: «PASS|FAIL <сценарий>: <детали>», код 1 при любом FAIL; копия строк
# дописывается в <каталог>/result.txt. Чужие workspace не затрагиваются, сна и
# опроса нет: состояния агента ждёт herdr agent wait, файлов — inotifywait/tail --pid.

set -euo pipefail

die() { printf 'run.sh: %s\n' "$*" >&2; exit 2; }

here=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
plugin=$(realpath "$here/../../..")
[ $# -eq 2 ] || die 'вызов: run.sh <каталог пробы> setup|A|B|C|D|E|F|teardown'
mkdir -p "$1"
dir=$(realpath "$1")
step=$2
state=$dir/state.env
[ -n "${HERDR_WORKSPACE_ID:-}" ] || die 'HERDR_WORKSPACE_ID не задан'
ws=$HERDR_WORKSPACE_ID
AGENT=push-probe
# Каталог запуска агента — доверенный корень проекта (как в dispatch.sh): в каталоге пробы встал бы диалог доверия.
repo_root=$(python3 -I "$plugin/scripts/duty_config.py" --cwd "$plugin" root)
fails=0

report() { # PASS|FAIL сценарий детали
    printf '%s %s: %s\n' "$1" "$2" "$3" | tee -a "$dir/result.txt"
    [ "$1" = PASS ] || fails=1
}

# shellcheck disable=SC1090
[ "$step" = setup ] || { [ -f "$state" ] || die 'нет state.env: сначала setup'; . "$state"; }
save() { printf 'TABS=%q\nPANE=%q\nTRANSCRIPT=%q\n' "${TABS:-}" "${PANE:-}" "${TRANSCRIPT:-}" > "$state"; }

# Новые записи транскрипта после отметки $1 (число строк).
mark() { wc -l < "$TRANSCRIPT"; }
since() { tail -n +"$(($1 + 1))" "$TRANSCRIPT"; }
# Непросмотренное окончание хода herdr показывает как done, а не idle: ждём оба.
agent_wait() {
    local until=(--until "$1")
    [ "$1" != idle ] || until+=(--until "done")
    herdr agent wait "$AGENT" "${until[@]}" --timeout "$2" > /dev/null
}
# Запись транскрипта с текстом $1: тип записи и вложения.
kinds() { jq -r --arg t "$1" 'select(tostring | contains($t)) | "\(.type)/\(.attachment.type // .operation // .subtype // "-")"' | sort | uniq -c | tr '\n' ' '; }

close_tab() { herdr tab close "$1" > /dev/null 2>&1 || true; }

run_tab() { # имя команда [каталог задачи] -> JSON tab-run.sh
    DUTY_TASK=${3:-$dir} DUTY_NAME=$AGENT bash "$plugin/scripts/tab-run.sh" "$1" -- "$2"
}
wait_pid() { # run_dir: ждать записи done по событию файловой системы
    # Событий в каталоге несколько (meta.txt, out.log, done): ждём, пока появится done.
    until [ -e "$1/done" ]; do
        inotifywait -qq -t 120 -e moved_to,create "$1" 2> /dev/null || break
    done
    [ -e "$1/done" ] || return 1
    # push.log пишется после done: ждём конца самого cmd.sh.
    local pid
    pid=$(sed -n 's/^pid: //p' "$1/meta.txt")
    [ -z "$pid" ] || tail --pid="$pid" -f /dev/null
}

case $step in
setup)
    : > "$dir/result.txt"
    mkdir -p "$dir/runs" "$dir/subtasks"
    cat > "$dir/brief.md" << BRIEF
# Проба push-wake

## Служебное

- панель агента: \`pending\`
- агент herdr и сессия Claude: \`$AGENT\`, модель \`haiku\`
BRIEF
    tab=$(herdr tab create --workspace "$ws" --cwd "$repo_root" --label "signals-push · haiku · probe-agent" --no-focus \
        --env DUTY_ROLE=worker --env "DUTY_TASK=$dir" --env "DUTY_NAME=$AGENT")
    TABS=$(jq -er '.result.tab.tab_id' <<< "$tab")
    PANE=$(jq -er '.result.root_pane.pane_id' <<< "$tab")
    sed -i "s|панель агента: \`pending\`|панель агента: \`$PANE\`|" "$dir/brief.md"
    TRANSCRIPT=
    save
    prompt='Это проба. Не выполняй порядок работы worker и записку не читай. Выполняй только сообщения этой сессии дословно. Файлы репозитория не правь. Сейчас ответь словом готов и закончи ход.'
    if ! out=$(herdr agent start "$AGENT" --kind claude --pane "$PANE" --timeout 30000 -- \
        --agent duty:worker --model haiku --plugin-dir "$plugin" --name "$AGENT" "$prompt" 2>&1); then
        case $out in
            *'"timeout"'* | *agent_not_ready*)
                # Как dispatch.sh: имя закрепляем за панелью сами, agent_launch_pending — не отказ.
                herdr agent rename "$PANE" "$AGENT" > /dev/null 2>&1 || true ;;
            *) die "агент не запущен: $out" ;;
        esac
    fi
    # Приглашение передано аргументом: агент занят с первой секунды.
    agent_wait idle 120000
    id=$(herdr agent get "$AGENT" | jq -er '.result.agent.agent_session.value')
    TRANSCRIPT=$(find "$HOME/.claude/projects" -name "$id.jsonl" -print -quit)
    [ -n "$TRANSCRIPT" ] || die "транскрипт сессии $id не найден"
    save
    report PASS setup "панель $PANE, сессия $id, транскрипт $TRANSCRIPT"
    ;;

A)
    m=$(mark)
    mkdir -p "$dir/waitA"
    out=$(run_tab r1 "inotifywait -qq -t 60 -e create $dir/waitA 2>/dev/null; exit 0")
    tab=$(jq -r .tab_id <<< "$out")
    # Простой агент: working наступает только после толчка в панель.
    if agent_wait working 90000 && agent_wait idle 120000 \
        && since "$m" | jq -e --arg t "done rc=0 $dir/runs/r1" 'select(.type == "user") | select(tostring | contains($t))' > /dev/null; then
        report PASS A "простой агент проснулся: user-запись «done rc=0 $dir/runs/r1»"
    else
        report FAIL A "нет user-записи «done rc=0 $dir/runs/r1»; push.log: $(cat "$dir/runs/r1/push.log" 2> /dev/null || echo нет)"
    fi
    close_tab "$tab"
    ;;

B)
    m=$(mark)
    rm -rf "$dir/runs/r2"
    mkdir -p "$dir/waitB"
    herdr agent prompt "$AGENT" "Выполни Bash с таймаутом 60000: inotifywait -qq -t 40 -e create $dir/waitB; затем ответь словом сделано." > /dev/null
    agent_wait working 60000
    out=$(run_tab r2 true)
    tab=$(jq -r .tab_id <<< "$out")
    wait_pid "$dir/runs/r2"
    agent_wait idle 180000
    new=$(since "$m")
    kinds=$(kinds "done rc=0 $dir/runs/r2" <<< "$new")
    if [ -n "$kinds" ]; then
        report PASS B "строка в очереди дошла до транскрипта; записи (число тип/вид): $kinds; push.log: $(tr '\n' ' ' < "$dir/runs/r2/push.log")"
    else
        report FAIL B "«done rc=0 $dir/runs/r2» нет в транскрипте; push.log: $(cat "$dir/runs/r2/push.log" 2> /dev/null || echo нет)"
    fi
    close_tab "$tab"
    ;;

C)
    m=$(mark)
    herdr agent prompt "$AGENT" 'Вызови Bash с run_in_background true: echo фон. Ответь разрешено или отказано, закончи ход.' > /dev/null
    agent_wait working 60000 || true
    agent_wait idle 120000
    new=$(since "$m")
    if grep -qF 'Субагент не ждёт в фоне' <<< "$new"; then
        report FAIL C "координатору фон отвергнут (в хуке есть agent_id при --agent?)"
    elif jq -e 'select(.type == "user") | .message.content | arrays | .[] | select(.type == "tool_result")' > /dev/null <<< "$new"; then
        report PASS C "tool_result без «Субагент не ждёт в фоне»"
    else
        report FAIL C "tool_result вызова не найден: агент не вызвал Bash"
    fi
    ;;

D)
    m=$(mark)
    rm -f "$dir/subtasks/s1.stopped" "$dir/subtasks/s1.done"
    herdr agent prompt "$AGENT" 'Запусти инструментом Agent с model haiku, subagent_type general-purpose приглашение: Подзадача: s1 (отдельная строка) и текст: Вызови Bash с run_in_background true командой echo фон. Затем ToolSearch select:Monitor и вызови Monitor с любой короткой командой. Отметку итога не пиши. Перескажи ответы инструментов и закончи ход. Сам после запуска ничего не делай и закончи ход.' > /dev/null
    agent_wait working 60000 > /dev/null 2>&1 || true
    [ -e "$dir/subtasks/s1.stopped" ] || inotifywait -qq -t 180 -e create "$dir/subtasks" 2> /dev/null || true
    agent_wait idle 240000 || true
    sess=${TRANSCRIPT%.jsonl}
    sub=$(find "$sess/subagents" -name 'agent-*.jsonl' -newer "$dir/brief.md" -exec ls -t {} + 2> /dev/null | head -n 1)
    if [ -n "$sub" ]; then
        bg=$(jq -c 'select(.type == "user") | .message.content | arrays | .[] | select(.type == "tool_result") | tostring' "$sub" | grep -cF 'Субагент не ждёт в фоне' || true)
        mon=$(jq -c 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use" and .name == "Monitor")' "$sub" | wc -l)
        if [ "$bg" -ge 1 ] && { [ "$mon" -eq 0 ] || [ "$bg" -ge 2 ]; }; then
            report PASS D1 "в $(basename "$sub") отказов «Субагент не ждёт в фоне»: $bg, вызовов Monitor: $mon"
        else
            report FAIL D1 "отказов: $bg, вызовов Monitor: $mon, транскрипт $sub"
        fi
    else
        report FAIL D1 "транскрипт субагента не найден в $sess/subagents"
    fi
    # grep -q под pipefail дал бы SIGPIPE у tail: считаем строки.
    seen=$(since "$m" | grep -cF 'субагент s1 остановился без итога' || true)
    if [ -e "$dir/subtasks/s1.stopped" ] && [ "$seen" -ge 1 ]; then
        report PASS D2 "s1.stopped: $(cat "$dir/subtasks/s1.stopped"); строка дошла до координатора; push.log: $(tr '\n' ' ' < "$dir/subtasks/push.log" 2> /dev/null)"
    else
        report FAIL D2 "s1.stopped: $([ -e "$dir/subtasks/s1.stopped" ] && echo есть || echo нет); строка в транскрипте: $seen; push.log: $(cat "$dir/subtasks/push.log" 2> /dev/null || echo нет)"
    fi
    ;;

E)
    # Естественный итог D: что сделал координатор, получив строку и закончив ход.
    natural=$(grep -cF 'Подзадачи остановились без итога' "$TRANSCRIPT" || true)
    echo "INFO E: блокировок Stop в транскрипте после D: $natural; s1.done: $([ -e "$dir/subtasks/s1.done" ] && echo есть || echo нет)" | tee -a "$dir/result.txt"
    rm -f "$dir/subtasks/s1.done"
    printf '{"agent_id":"x"}' > "$dir/subtasks/s1.stopped"
    m=$(mark)
    herdr agent prompt "$AGENT" 'Ответь словом стоп и закончи ход.' > /dev/null
    agent_wait working 60000 > /dev/null 2>&1 || true
    agent_wait idle 180000
    new=$(since "$m")
    if grep -qF 'Подзадачи остановились без итога' <<< "$new"; then
        report PASS E "блокировка Stop в транскрипте: $(kinds 'Подзадачи остановились без итога' <<< "$new")"
    else
        report FAIL E "«Подзадачи остановились без итога» нет в записях после запроса; s1.stopped: $([ -e "$dir/subtasks/s1.stopped" ] && echo есть || echo нет)"
    fi
    printf 'проба\n' > "$dir/subtasks/s1.done"
    ;;

F)
    fake=$dir/fake
    rm -rf "$fake/runs"
    mkdir -p "$fake"
    cat > "$fake/brief.md" << BRIEF
## Служебное

- панель агента: \`p-ghost-0\`
- агент herdr и сессия Claude: \`push-ghost-none\`, модель \`haiku\`
BRIEF
    out=$(run_tab f1 true "$fake")
    tab=$(jq -r .tab_id <<< "$out")
    wait_pid "$fake/runs/f1"
    rc=$(cat "$fake/runs/f1/done" 2> /dev/null || echo нет)
    log=$(cat "$fake/runs/f1/push.log" 2> /dev/null || true)
    if [ "$rc" = 0 ] && grep -qF 'не отправлена' <<< "$log"; then
        report PASS F "done=0, push.log: $log"
    else
        report FAIL F "done=$rc, push.log: ${log:-пуст}"
    fi
    close_tab "$tab"
    ;;

teardown)
    for t in ${TABS:-}; do close_tab "$t"; done
    report PASS teardown "закрыты вкладки: ${TABS:-нет}"
    ;;
*) die "неизвестный шаг $step" ;;
esac
exit "$fails"
