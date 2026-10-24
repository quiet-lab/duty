#!/usr/bin/env bash
# Дождаться файла-отметки done в каталоге прогона и напечатать код.
#
#   waitdone.sh <каталог> [--timeout-s <секунды>]
#
# Ожидание — по уведомлению inotify (inotifywait из inotify-tools), без
# опроса: наблюдение ставится до проверки, есть ли done, поэтому отметка,
# появившаяся между ними, не теряется. Каталога ещё нет — он создаётся,
# чтобы наблюдение встало до запуска вкладки. Код выхода — код из done;
# 124 — истёк --timeout-s (предел ожидания, а не такт опроса), 2 — ошибка
# вызова.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "вызов: waitdone.sh <каталог> [--timeout-s <секунды>]" >&2
    exit 2
fi
dir=$1
shift
timeout_s=
while [[ $# -gt 0 ]]; do
    case $1 in
        --timeout-s) timeout_s=${2:?--timeout-s без значения}; shift 2 ;;
        *) echo "неизвестный ключ $1" >&2; exit 2 ;;
    esac
done
command -v inotifywait >/dev/null || {
    echo "нет inotifywait: pacman -S inotify-tools" >&2
    exit 2
}
mkdir -p "$dir"

finish() {
    local code
    code=$(tr -d '[:space:]' < "$dir/done")
    echo "$code"
    [[ $code =~ ^[0-9]+$ ]] || exit 2
    exit "$code"
}

exec {events}< <(exec inotifywait -m -e close_write -e moved_to --format '%f' "$dir" 2>&1)
watcher=$!
trap 'kill "$watcher" 2>/dev/null || true' EXIT
# Первые строки inotifywait — в stderr: «Setting up watches.» и
# «Watches established.»; после второй наблюдение действует.
while IFS= read -r line <&"$events"; do
    [[ $line == "Watches established." ]] && break
done
[[ -f $dir/done ]] && finish

read_args=()
[[ -n $timeout_s ]] && read_args=(-t "$timeout_s")
deadline=$(( $(date +%s) + ${timeout_s:-0} ))
while true; do
    if [[ -n $timeout_s ]]; then
        left=$(( deadline - $(date +%s) ))
        (( left > 0 )) || { echo timeout; exit 124; }
        read_args=(-t "$left")
    fi
    if ! IFS= read -r "${read_args[@]}" f <&"$events"; then
        [[ -f $dir/done ]] && finish
        if [[ -n $timeout_s ]]; then echo timeout; exit 124; fi
        echo "наблюдение inotify за $dir оборвалось" >&2
        exit 2
    fi
    [[ $f == done ]] && finish
done
