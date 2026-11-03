#!/usr/bin/env bash
# Длинная команда во вкладке своего workspace herdr с итогом в файлы.
#
#   tab-run.sh <имя> [--cwd <каталог>] [--kind <вид>] -- <команда…>
#
# Вкладка создаётся в workspace вызывающего ($HERDR_WORKSPACE_ID) в каталоге
# --cwd (по умолчанию текущий), команда идёт в текущем каталоге вкладки.
# Один аргумент после -- — строка для bash (конвейеры, перенаправления),
# несколько — команда с аргументами, каждый экранируется. Команда пишется
# файлом runs/<имя>/cmd.sh, в терминал уходит одна короткая строка без
# кавычек пользователя. Каталог итогов — runs/<имя>/ в $DUTY_TASK (без
# него — в текущем каталоге), он должен быть новым: прежняя отметка done
# сразу отпустила бы ожидающего wait-done.sh.
#
# Необязательный --kind (например tests, build, wait) пишется строкой kind в
# meta.txt и показывается в duty-runs-web (runs-web.py); без него строки kind нет.
#
# Итог: out.log (stdout и stderr вместе, они же на экране вкладки),
# meta.txt (каталог, started_ms, finished_ms, код) и последним done с кодом
# завершения — под временным именем с переименованием, поэтому ожидающий
# видит его целиком. При HERDR_ENV=1 вкладка показывает уведомление herdr
# (звук done, при ненулевом коде request) и печатает «DUTY-DONE <имя>
# rc=<код>» для herdr pane wait-output. В ответ — JSON с tab_id, pane_id
# и run_dir, log (путь к out.log: его же строка «log:» в meta.txt; агент
# передаёт путь сторожу прогресса — wait-done.sh --log <log> --stall-s <секунды>).
# Вкладка закрывается сама последним действием после записи done
# (tab_id лежит в runs/<имя>/tab); у агента остаётся чтение out.log и done
# (правило — agents/worker.md, п. 4).
#
# Вместе с done вкладка будит агента задачи: push-agent.sh отправляет в его
# панель строку «done rc=<код> <каталог прогона>» (итог отправки — push.log
# в каталоге прогона). Без DUTY_TASK отправки нет. Закрытие вкладки посреди
# прогона даёт done с кодом 129, строка «pid:» в meta.txt — признак живого
# прогона для хука Stop (hooks/task-guard.py).
#
# Переменные DUTY_TASK, DUTY_NAME, DUTY_ROLE, DUTY_SYMBOL и DUTY_MODEL вызывающего (если заданы)
# экспортируются в начале cmd.sh и передаются herdr tab create --env: workspace
# create задаёт окружение только корневой панели, новая вкладка его не
# наследует, и команда (например, jwait.sh) иначе пишет итог в путь по
# умолчанию. Основной путь — экспорт в cmd.sh.

set -euo pipefail

die() { printf 'tab-run.sh: %s\n' "$*" >&2; exit 2; }
usage='вызов: tab-run.sh <имя> [--cwd <каталог>] [--kind <вид>] -- <команда…>'

[ $# -ge 3 ] || die "$usage"
name=$1
shift
cwd=$PWD
kind=
here=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
while [ $# -gt 0 ] && [ "$1" != -- ]; do
    case $1 in
        --cwd) cwd=${2:?--cwd без значения}; shift 2 ;;
        --kind) kind=${2:?--kind без значения}; shift 2 ;;
        *) die "неизвестный ключ $1; $usage" ;;
    esac
done
[ "${1:-}" = -- ] && [ $# -ge 2 ] || die "$usage"
shift

[[ $name =~ ^[a-z0-9][a-z0-9_.-]{0,63}$ ]] || die "имя $name: только строчные латинские буквы, цифры, _ . -"
[ "${HERDR_ENV:-}" = 1 ] || die 'сессия не в herdr (HERDR_ENV не 1)'
[ -n "${HERDR_WORKSPACE_ID:-}" ] || die 'HERDR_WORKSPACE_ID не задан'
[[ -z $kind || $kind =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] || die "вид $kind: только строчные латинские буквы, цифры, _ -"
[ -d "$cwd" ] || die "каталога $cwd нет"
command -v jq > /dev/null || die 'нет jq'

if [ $# -eq 1 ]; then
    command=$1
else
    command=$(printf '%q ' "$@")
fi

base=${DUTY_TASK:-$PWD}
[ -d "$base" ] || die "каталога задачи $base нет"
mkdir -p "$base/runs"
run_dir=$(realpath "$base/runs")/$name
# mkdir без -p отказывает, если каталог уже есть: это и есть проверка
# новизны, без гонки между проверкой и созданием.
mkdir "$run_dir" 2> /dev/null || die "каталог $run_dir уже есть: другое имя или убрать каталог"
cwd=$(realpath "$cwd")

{
    printf '#!/usr/bin/env bash\n'
    printf '# Прогон %s от tab-run.sh; идёт в текущем каталоге вкладки (%s).\n' "$name" "$cwd"
    for var in DUTY_TASK DUTY_NAME DUTY_ROLE DUTY_SYMBOL DUTY_MODEL; do
        [ -z "${!var:-}" ] || printf 'export %s=%q\n' "$var" "${!var}"
    done
    printf 'run_dir=%q\n' "$run_dir"
    printf 'printf "cwd: %%s\\nstarted_ms: %%s\\n" "$PWD" "$(date +%%s%%3N)" > "$run_dir/meta.txt"\n'
    printf 'printf "log: %%s\\n" "$run_dir/out.log" >> "$run_dir/meta.txt"\n'
    [ -z "$kind" ] || printf 'printf "kind: %%s\\n" %q >> "$run_dir/meta.txt"\n' "$kind"
    printf 'push=%q\n' "$here/push-agent.sh"
    printf 'run_name=%q\n' "$name"
    # Закрытие вкладки посреди прогона (SIGHUP) и остановка сигналом тоже
    # записывают done: без него wait-done.sh ждал бы вечно. Обработчик сигнала
    # bash выполняет после конца конвейера, поэтому done не опережает команду.
    cat << 'FINISH'
duty_finish() {
    local rc=$1 sound=done
    trap - EXIT HUP INT TERM
    trap '' HUP INT TERM PIPE
    printf 'finished_ms: %s\ncode: %s\n' "$(date +%s%3N)" "$rc" >> "$run_dir/meta.txt"
    printf '%s\n' "$rc" > "$run_dir/.done.tmp" && mv -f "$run_dir/.done.tmp" "$run_dir/done"
    if [ "${HERDR_ENV:-}" = 1 ]; then
        [ "$rc" = 0 ] || sound=request
        herdr notification show "Вкладка $run_name завершена" --body "$run_dir" --sound "$sound" > /dev/null 2>&1 || true
    fi
    printf 'DUTY-DONE %s rc=%s\n' "$run_name" "$rc" || true
    if [ -n "${DUTY_TASK:-}" ]; then
        bash "$push" "$DUTY_TASK" "done rc=$rc $run_dir" >> "$run_dir/push.log" 2>&1 || true
    fi
    # Последним действием: закрытие вкладки шлёт HUP этому сценарию, но done
    # записан, а ловушки сняты выше.
    if [ "${HERDR_ENV:-}" = 1 ] && [ -f "$run_dir/tab" ]; then
        herdr tab close "$(cat "$run_dir/tab")" > /dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap 'duty_finish 1' EXIT
trap 'duty_finish 129' HUP
trap 'duty_finish 130' INT
trap 'duty_finish 143' TERM
FINISH
    printf 'printf "pid: %%s\\n" "$$" >> "$run_dir/meta.txt"\n'
    printf '{\n%s\n} 2>&1 | tee "$run_dir/out.log"\n' "$command"
    printf 'duty_finish "${PIPESTATUS[0]}"\n'
} > "$run_dir/cmd.sh"
bash -n "$run_dir/cmd.sh" || die "команда не разбирается bash: $run_dir/cmd.sh"

env_args=()
for var in DUTY_TASK DUTY_NAME DUTY_ROLE DUTY_SYMBOL DUTY_MODEL; do
    [ -z "${!var:-}" ] || env_args+=(--env "$var=${!var}")
done
# Подпись вкладки команды — «▸ <имя прогона>», без символа workspace, имени
# задачи и модели: панель агентов herdr группирует вкладки по workspace, и полная
# подпись читалась бы как ещё один агент.
tab_label="▸ $name"
tab=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$cwd" --label "$tab_label" --no-focus ${env_args[@]+"${env_args[@]}"})
tab_id=$(jq -er '.result.tab.tab_id' <<< "$tab") || die "herdr tab create: $tab"
pane_id=$(jq -er '.result.root_pane.pane_id' <<< "$tab") || die "herdr tab create: $tab"
printf '%s\n' "$tab_id" > "$run_dir/tab"

herdr pane run "$pane_id" "bash $(printf '%q' "$run_dir/cmd.sh")" > /dev/null \
    || die "herdr pane run $pane_id не принял команду; вкладка $tab_id остаётся"

jq -n --arg tab "$tab_id" --arg pane "$pane_id" --arg dir "$run_dir" \
    '{tab_id: $tab, pane_id: $pane, run_dir: $dir, log: ($dir + "/out.log")}'
