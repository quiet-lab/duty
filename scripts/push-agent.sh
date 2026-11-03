#!/usr/bin/env bash
# Сообщение агенту задачи в его панель herdr: простаивающая сессия просыпается,
# работающая получает строку в очередь.
#
#   push-agent.sh <каталог задачи> <строка> [--no-wait]
#
# Имя агента и панель берутся из служебного блока brief.md в момент отправки
# (последний блок: продолжение --resume дописывает новый), поэтому строка
# находит агента и после перезапуска. herdr agent prompt отвечает
# agent_not_found на панели без агента и в оболочку ничего не набирает.
# Агент ждёт ответа на диалог (agent_blocked) — отправка повторяется после
# смены его состояния по событию herdr agent wait; с --no-wait (хуки, которым
# нельзя ждать) повтора нет. Коды: 0 — строка принята, 3 — некому или
# не принято, 2 — ошибка вызова. Записки с блоком нет — не задача duty,
# код 0 без отправки.

set -uo pipefail

die() { printf 'push-agent.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 2 ] || die 'вызов: push-agent.sh <каталог задачи> <строка> [--no-wait]'
task=$1 text=$2
wait_blocked=1
[ "${3:-}" = --no-wait ] && wait_blocked=0
# Строка уходит как одно сообщение: перевод строки отправил бы её по частям.
text=${text//$'\n'/ }

brief=$task/brief.md
service_value() { # начало строки блока -> значение в первых обратных кавычках
    grep -F -- "- $1: \`" "$brief" 2> /dev/null | tail -n 1 | sed -E 's/^[^`]*`([^`]*)`.*/\1/'
}
name=$(service_value 'агент herdr и сессия Claude')
pane=$(service_value 'панель агента')
[ -n "$name$pane" ] || { printf 'нет служебного блока в %s: отправки нет\n' "$brief"; exit 0; }

target=
for candidate in "$name" "$pane"; do
    if [ -n "$candidate" ] && herdr agent get "$candidate" > /dev/null 2>&1; then
        target=$candidate
        break
    fi
done
[ -n "$target" ] || { printf 'агента %s и панели %s нет: строка не отправлена: %s\n' "$name" "$pane" "$text"; exit 3; }

# Предел попыток — на случай агента, снова и снова встающего на диалог.
for _ in 1 2 3 4 5 6 7 8; do
    if out=$(herdr agent prompt "$target" "$text" 2>&1); then
        printf 'отправлено %s: %s\n' "$target" "$text"
        exit 0
    fi
    case $out in
        *agent_blocked*)
            [ "$wait_blocked" = 1 ] || { printf 'агент %s ждёт ответа на диалог, строка не отправлена: %s\n' "$target" "$text"; exit 3; }
            herdr agent wait "$target" --until idle --until working --until done > /dev/null 2>&1 \
                || { printf 'ожидание агента %s оборвалось: %s\n' "$target" "$text"; exit 3; }
            ;;
        *) printf 'herdr agent prompt %s: %s\n' "$target" "$out"; exit 3 ;;
    esac
done
printf 'агент %s всё время на диалоге, строка не отправлена: %s\n' "$target" "$text"
exit 3
