#!/usr/bin/env bash
# Ожидание файла-отметки done в каталоге прогона по событию inotify.
#
#   wait-done.sh <каталог прогона> [--timeout-s <секунды>]
#
# Наблюдение (inotifywait из inotify-tools) ставится до проверки наличия
# done, поэтому отметка, появившаяся между проверкой и началом наблюдения,
# не теряется. Каталог создаёт tab-run.sh до возврата, поэтому ожидание
# запускается после него. Печатает «done rc=<код> <каталог>» и выходит
# с кодом из done; 124 — истёк --timeout-s (предел ожидания, а не такт
# опроса), 2 — ошибка вызова или наблюдения. Один вызов Bash ограничен 600 с:
# агент ждёт с --timeout-s 590 и таймаутом вызова 600000 мс, а по строке timeout
# запускает ожидание снова (agents/worker.md, п. 4).

set -euo pipefail

die() { printf 'wait-done.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 1 ] || die 'вызов: wait-done.sh <каталог прогона> [--timeout-s <секунды>]'
dir=$1
shift
timeout_s=
while [ $# -gt 0 ]; do
    case $1 in
        --timeout-s) timeout_s=${2:?--timeout-s без значения}; shift 2 ;;
        *) die "неизвестный ключ $1" ;;
    esac
done
[[ -z $timeout_s || $timeout_s =~ ^[0-9]+$ ]] || die "--timeout-s $timeout_s: нужно целое число секунд"
[ -d "$dir" ] || die "каталога $dir нет"
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
exec {events}< <(exec setpriv --pdeathsig TERM inotifywait -m -e moved_to -e close_write --format '%f' "$dir" 2>&1)
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
while true; do
    read_args=()
    if [ -n "$timeout_s" ]; then
        left=$(( deadline - SECONDS ))
        (( left > 0 )) || { echo timeout; exit 124; }
        read_args=(-t "$left")
    fi
    if ! IFS= read -r "${read_args[@]}" file <&"$events"; then
        [ -f "$dir/done" ] && finish
        if [ -n "$timeout_s" ]; then echo timeout; exit 124; fi
        die "наблюдение за $dir оборвалось без done"
    fi
    [ "$file" = "done" ] && finish
done
