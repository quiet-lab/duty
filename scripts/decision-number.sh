#!/usr/bin/env bash
# Выдаёт следующий номер записи решения: наибольший существующий плюс один.
# Использование: decision-number.sh <каталог docs/decisions> [--peek]
#
# Номер выдаётся атомарно: замок — каталог <каталог>/.number-lock (mkdir
# атомарен), снимается в trap. Выданный номер сразу резервируется пустым
# файлом NNNN-reserved.md, чтобы следующий вызов, пришедший после снятия
# замка, увидел его и выдал другой номер; вызывающий переименовывает файл
# в настоящее имя записи (mv) либо удаляет, если запись не создана.
# С --peek номер только показывается, без замка и резервирования.
# Номера считаются по файлам рабочего дерева и по индексу git.
set -euo pipefail

dir="${1:-}"
peek="${2:-}"
if [[ -z "$dir" || ! -d "$dir" ]]; then
    echo "использование: decision-number.sh <каталог docs/decisions> [--peek]" >&2
    exit 2
fi
dir="$(cd "$dir" && pwd)"

next_number() {
    local max=0 name n
    {
        find "$dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]-*.md' -printf '%f\n'
        git -C "$dir" ls-files --cached -- . 2>/dev/null | sed 's|.*/||' || true
    } | while read -r name; do
        [[ "$name" =~ ^([0-9]{4})-.*\.md$ ]] && echo "${BASH_REMATCH[1]}"
    done | sort -u | tail -n1 | {
        read -r max || max=0
        printf '%04d\n' "$((10#${max:-0} + 1))"
    }
}

if [[ "$peek" == "--peek" ]]; then
    next_number
    exit 0
fi

lock="$dir/.number-lock"
# Ожидание замка — по событию снятия через inotifywait, без опроса;
# без inotifywait замок не ждётся, а вызов завершается кодом 3. Срок 30 с
# в inotifywait — только страховка от замка, оставшегося после аварийного
# завершения держателя (его снимают вручную: rmdir <каталог>/.number-lock).
acquire() {
    mkdir "$lock" 2>/dev/null
}
trap 'rmdir "$lock" 2>/dev/null || true' EXIT
until acquire; do
    if ! command -v inotifywait >/dev/null; then
        echo "замок $lock занят, а inotifywait не установлен" >&2
        trap - EXIT
        exit 3
    fi
    # Замок мог исчезнуть между mkdir и подпиской: событие ловит удаление,
    # а повторная проверка после подписки закрывает гонку.
    inotifywait -qq -e delete_self,delete --timeout 30 "$lock" 2>/dev/null || true
done

number="$(next_number)"
: > "$dir/${number}-reserved.md"
echo "$number"
