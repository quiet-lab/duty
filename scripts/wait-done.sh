#!/usr/bin/env bash
# Ожидание файла done в каталоге прогона по событию inotify, без опроса.
#
# Использование: wait-done.sh <каталог прогона>
#
# Наблюдение ставится до проверки наличия файла, поэтому отметка, появившаяся
# между проверкой и началом наблюдения, не теряется. Печатает код завершения
# команды из done и выходит с кодом 0; ошибка наблюдения — код 2.

set -euo pipefail

dir=${1:?нужен каталог прогона}
[ -d "$dir" ] || { printf 'wait-done.sh: каталога %s нет\n' "$dir" >&2; exit 2; }
command -v inotifywait > /dev/null || { printf 'wait-done.sh: нет inotifywait\n' >&2; exit 2; }

exec {events}< <(exec inotifywait -m -e moved_to -e close_write --format '%f' "$dir" 2>&1)
watcher=$!

# inotifywait сообщает о готовности строкой «Watches established.».
while IFS= read -r line <&"$events"; do
    case $line in
        *'Watches established.'*) break ;;
        *'Setting up watches.'*) ;;
        *) printf 'wait-done.sh: %s\n' "$line" >&2; kill "$watcher" 2> /dev/null; exit 2 ;;
    esac
done

if [ ! -f "$dir/done" ]; then
    while IFS= read -r file <&"$events"; do
        [ "$file" = done ] && break
    done
fi

# Наблюдатель запущен этим сценарием, его PID известен.
kill "$watcher" 2> /dev/null || true
[ -f "$dir/done" ] || { printf 'wait-done.sh: наблюдение оборвалось без done\n' >&2; exit 2; }
printf 'done %s rc=%s\n' "$dir" "$(cat "$dir/done")"
