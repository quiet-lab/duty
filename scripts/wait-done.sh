#!/usr/bin/env bash
# Ожидание файла-отметки done в каталоге прогона по событию inotify.
#
#   wait-done.sh <каталог прогона> [--timeout-s <секунды>]
#                [--log <файл> --stall-s <секунды>]
#
# Наблюдение (inotifywait из inotify-tools) ставится до проверки наличия
# done, поэтому отметка, появившаяся между проверкой и началом наблюдения,
# не теряется. Каталог создаёт tab-run.sh до возврата, поэтому ожидание
# запускается после него. Печатает «done rc=<код> <каталог>» и выходит
# с кодом из done; 124 — истёк --timeout-s (предел ожидания, а не такт
# опроса), 125 — сторож прогресса: журнал --log (файл в каталоге прогона,
# обычно out.log) не рос дольше --stall-s секунд; печатается «stall <секунд>
# <каталог>». Рост журнала — событие inotify modify, такта нет; срок
# журнала отсчитывается от последнего роста (до первого — от mtime
# журнала, а без журнала — от запуска ожидания). 2 — ошибка вызова или
# наблюдения. Порог --stall-s — из замера самой тихой операции, не меньше
# 300 с (agents/worker.md, п. 4). Один вызов Bash ограничен 600 с:
# агент ждёт с --timeout-s 590 и таймаутом вызова 600000 мс, а по строке timeout
# запускает ожидание снова (agents/worker.md, п. 4).

set -euo pipefail

die() { printf 'wait-done.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 1 ] || die 'вызов: wait-done.sh <каталог прогона> [--timeout-s <секунды>] [--log <файл> --stall-s <секунды>]'
dir=$1
shift
timeout_s=
log=
stall_s=
while [ $# -gt 0 ]; do
    case $1 in
        --timeout-s) timeout_s=${2:?--timeout-s без значения}; shift 2 ;;
        --log) log=${2:?--log без значения}; shift 2 ;;
        --stall-s) stall_s=${2:?--stall-s без значения}; shift 2 ;;
        *) die "неизвестный ключ $1" ;;
    esac
done
[[ -z $stall_s || $stall_s =~ ^[1-9][0-9]*$ ]] || die "--stall-s $stall_s: нужно целое число секунд больше нуля"
[ -z "$log" ] || [ -n "$stall_s" ] || die '--log без --stall-s'
[ -z "$stall_s" ] || [ -n "$log" ] || die '--stall-s без --log'
[[ -z $timeout_s || $timeout_s =~ ^[0-9]+$ ]] || die "--timeout-s $timeout_s: нужно целое число секунд"
[ -d "$dir" ] || die "каталога $dir нет"
logname=
if [ -n "$log" ]; then
    logname=$(basename "$log")
    [ "$(realpath -m "$(dirname "$log")")" = "$(realpath "$dir")" ] || die "журнал $log лежит не в каталоге прогона $dir"
fi
command -v inotifywait > /dev/null || die 'нет inotifywait: pacman -S inotify-tools'
command -v setpriv > /dev/null || die 'нет setpriv: pacman -S util-linux'

finish() {
    local code
    code=$(tr -d '[:space:]' < "$dir/done")
    [[ $code =~ ^[0-9]+$ ]] || die "в $dir/done не код: $code"
    printf 'done rc=%s %s\n' "$code" "$dir"
    exit "$code"
}

# Наблюдатель с -m сам не заканчивается: при обычном выходе и SIGTERM его
# снимает ловушка EXIT, при SIGKILL этого сценария — сигнал смерти родителя.
exec {events}< <(exec setpriv --pdeathsig TERM inotifywait -m -e moved_to -e close_write ${log:+-e modify} --format '%f' "$dir" 2>&1)
watcher=$!
# Наблюдатель запущен этим сценарием, его PID известен.
trap 'kill "$watcher" 2> /dev/null || true' EXIT

# Первые строки inotifywait — «Setting up watches.» и «Watches
# established.»; после второй наблюдение действует, иное — ошибка.
while IFS= read -r line <&"$events"; do
    case $line in
        *'Watches established.'*) break ;;
        *'Setting up watches.'*) ;;
        *) die "$line" ;;
    esac
done

[ -f "$dir/done" ] && finish

deadline=$(( SECONDS + ${timeout_s:-0} ))
# Последний рост журнала: от mtime журнала, если он есть, иначе от запуска.
last=$SECONDS
if [ -n "$log" ] && [ -e "$log" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$log") ))
    (( age > 0 )) && last=$(( SECONDS - age ))
fi
while true; do
    read_args=()
    left=
    if [ -n "$timeout_s" ]; then
        left=$(( deadline - SECONDS ))
        (( left > 0 )) || { echo timeout; exit 124; }
    fi
    if [ -n "$stall_s" ]; then
        sleft=$(( last + stall_s - SECONDS ))
        (( sleft > 0 )) || { printf 'stall %s %s\n' "$(( SECONDS - last ))" "$dir"; exit 125; }
        if [ -z "$left" ] || (( sleft < left )); then left=$sleft; stalling=1; else stalling=; fi
    fi
    [ -z "$left" ] || read_args=(-t "$left")
    rc=0
    IFS= read -r "${read_args[@]}" file <&"$events" || rc=$?
    if [ "$rc" -ne 0 ]; then
        [ -f "$dir/done" ] && finish
        # Код больше 128 — истёк read -t; иначе поток закончился.
        if [ "$rc" -gt 128 ]; then
            [ -n "${stalling:-}" ] && continue
            echo timeout
            exit 124
        fi
        die "наблюдение за $dir оборвалось без done"
    fi
    [ "$file" = "done" ] && finish
    [ -z "$logname" ] || [ "$file" != "$logname" ] || last=$SECONDS
done
