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
# Номера считаются по файлам каталога, индексу git, деревьям всех местных
# ветвей репозитория и каталогам всех его worktree (с незакоммиченными
# файлами и резервами): задачи в разных worktree не получают один номер.
# Вне git-репозитория — только по файлам каталога.
set -euo pipefail

dir="${1:-}"
peek="${2:-}"
if [[ -z "$dir" || ! -d "$dir" ]]; then
    echo "использование: decision-number.sh <каталог docs/decisions> [--peek]" >&2
    exit 2
fi
dir="$(cd "$dir" && pwd)"

# Имена файлов записей из всех источников; ошибки git не роняют сценарий.
list_names() {
    local prefix ref wt d
    find "$dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]-*.md' -printf '%f\n'
    git -C "$dir" ls-files --cached -- . 2>/dev/null | sed 's|.*/||' || true
    if git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        prefix="$(git -C "$dir" rev-parse --show-prefix 2>/dev/null || true)"
        while read -r ref; do
            [[ -n "$ref" ]] || continue
            if [[ -n "$prefix" ]]; then
                git -C "$dir" ls-tree --full-tree --name-only "$ref" -- "$prefix" 2>/dev/null || true
            else
                git -C "$dir" ls-tree --full-tree --name-only "$ref" 2>/dev/null || true
            fi
        done < <(git -C "$dir" for-each-ref --format='%(refname)' refs/heads 2>/dev/null || true) | sed 's|.*/||' || true
        while read -r wt; do
            d="$wt/$prefix"
            if [[ -d "$d" ]]; then
                find "$d" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]-*.md' -printf '%f\n' 2>/dev/null || true
            fi
        done < <(git -C "$dir" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' || true)
    fi
}

next_number() {
    local max=0 name n
    list_names | while read -r name; do
        # if, а не «&&»: при set -e и pipefail неподходящее имя последней
        # строки сделало бы код выхода всего конвейера ненулевым.
        if [[ "$name" =~ ^([0-9]{4})-.*\.md$ ]]; then
            echo "${BASH_REMATCH[1]}"
        fi
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
