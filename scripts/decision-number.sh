#!/usr/bin/env bash
# Номер и вставка записи решения в тематический каталог docs/decisions/<NN>-<тема>/.
#
#   decision-number.sh <каталог записей>
#       печатает номер, который получит запись, вставленная последней
#       в каталог: последний номер каталога плюс один; в пустом каталоге —
#       последний номер предыдущих каталогов плюс один. Ничего не меняет.
#   decision-number.sh <каталог записей> --insert <файл> [--after <номер>]
#       вставляет запись сценарием scripts/renumber-decisions.py монорепозитория
#       (insert --catalog <NN> --last или insert --after <номер>): последующие
#       записи сдвигаются, ссылки на них переписываются; затем указатель
#       docs/decisions/README.md порождается заново (--write-index).
#       Печатает путь записи. Имя файла — тема латиницей через дефис
#       (<тема>.md или NNNN-<тема>.md), номер ставит сценарий.
#
# Нумерация сквозная и повествовательная: номер — место записи в порядке
# каталогов, а не её постоянный идентификатор. Ветви, вставившие записи
# параллельно, сводятся при слиянии перенумерацией renumber-decisions.py.
# Сценарий перенумерации — переменная DUTY_RENUMBER (по умолчанию
# scripts/renumber-decisions.py корня репозитория каталога).
set -euo pipefail

die() { printf 'decision-number.sh: %s\n' "$*" >&2; exit 2; }

catalog=${1:-}
[[ -n $catalog && -d $catalog ]] || die 'вызов: decision-number.sh <каталог docs/decisions/NN-тема> [--insert <файл> [--after <номер>]]'
shift
catalog=$(cd "$catalog" && pwd)
decisions=$(dirname "$catalog")
nn=$(basename "$catalog")
[[ $nn =~ ^([0-9]{2})-[a-z0-9-]+$ ]] || die "$catalog — не тематический каталог <NN>-<тема>"
nn=${BASH_REMATCH[1]}

insert='' after=''
while [ $# -gt 0 ]; do
    case $1 in
        --insert) insert=${2:?--insert без файла}; shift 2 ;;
        --after) after=${2:?--after без номера}; shift 2 ;;
        *) die "неизвестный ключ $1" ;;
    esac
done
[ -z "$after" ] || [ -n "$insert" ] || die '--after только вместе с --insert'
[[ -z $after || $after =~ ^[0-9]{1,4}$ ]] || die "--after $after: номер записи"

# Номера записей по порядку каталогов: «<NN> <NNNN>» на строку. Источники
# объединения <NNNN>.merge/ лежат глубже и не учитываются.
numbers() {
    local file c n
    for file in "$decisions"/[0-9][0-9]-*/[0-9][0-9][0-9][0-9]-*.md; do
        [ -f "$file" ] || continue
        c=$(basename "$(dirname "$file")"); n=$(basename "$file")
        printf '%s %s\n' "${c:0:2}" "${n:0:4}"
    done | sort
}

next_number() {
    local last
    last=$(numbers | awk -v c="$nn" '$1 <= c { n = $2 } END { print n + 0 }')
    printf '%04d\n' "$((10#$last + 1))"
}

if [ -z "$insert" ]; then
    next_number
    exit 0
fi

[ -f "$insert" ] || die "файла $insert нет"
insert=$(realpath "$insert")
slug=$(basename "$insert" .md)
[[ $slug =~ ^([0-9]{4}-)?([a-z0-9][a-z0-9-]*)$ ]] || die "имя $slug: тема латиницей через дефис"
slug=${BASH_REMATCH[2]}
root=$(git -C "$decisions" rev-parse --show-toplevel 2> /dev/null) || die "$decisions не в git-репозитории"
renumber=${DUTY_RENUMBER:-$root/scripts/renumber-decisions.py}
[ -f "$renumber" ] || die "нет сценария перенумерации $renumber"

# WARNING: замок на вставку в одном рабочем дереве. Почему иначе нельзя:
# вставка переименовывает записи и переписывает ссылки во многих файлах
# не одной атомарной операцией, две вставки в одном дереве теряют правки
# друг друга. Разные worktree замок не делят (каталог git у каждого свой),
# их номера сводит перенумерация при слиянии. Ожидание — событием снятия
# (inotifywait); замок, не снятый за 30 с, считается оставшимся после
# аварии держателя: сценарий завершается с подсказкой rmdir, а не ждёт
# бесконечно. Условие снятия: renumber-decisions.py insert сам проверяет,
# что дерево не изменилось за время вставки, и откатывает её при гонке.
lock="$(git -C "$decisions" rev-parse --path-format=absolute --git-dir)/decision-insert.lock"
announced=0
until mkdir "$lock" 2> /dev/null; do
    command -v inotifywait > /dev/null || die "замок $lock занят, а inotifywait не установлен"
    if [ "$announced" = 0 ]; then
        printf 'decision-number.sh: замок %s занят, ожидание снятия; после аварии снимается rmdir %s\n' "$lock" "$lock" >&2
        announced=1
    fi
    # Замок мог исчезнуть между mkdir и подпиской: событие ловит удаление,
    # а повторный mkdir после подписки закрывает гонку. Код 2 — срок вышел.
    code=0
    inotifywait -qq -e delete_self,delete --timeout 30 "$lock" 2> /dev/null || code=$?
    if [ "$code" = 2 ] && [ -d "$lock" ]; then
        die "замок $lock не снят за 30 с; если вставка не идёт, снять: rmdir $lock"
    fi
done
trap 'rmdir "$lock" 2> /dev/null || true' EXIT

# Каталог, куда ляжет запись: при --after — каталог записи <номер>.
target=$catalog
if [ -n "$after" ]; then
    anchor=$(find "$decisions"/[0-9][0-9]-* -maxdepth 1 -type f -name "$(printf '%04d' "$((10#$after))")-*.md" | head -1)
    [ -n "$anchor" ] || die "записи $after нет"
    target=$(dirname "$anchor")
fi

# Файл записи с другой файловой системы (scratchpad на tmpfs) сценарий
# перенумерации перенести переименованием не может (EXDEV): копия кладётся
# в каталог git репозитория, исходный файл удаляется после успешной вставки.
gitdir=$(git -C "$decisions" rev-parse --path-format=absolute --git-dir)
source_file=''
# Черновик внутри репозитория тоже копируется: откат не должен удалять
# единственный экземпляр, который перенумерация успела переместить.
if [ "$(stat -c %d "$insert")" != "$(stat -c %d "$decisions")" ] || [[ $insert == "$root"/* ]]; then
    source_file=$insert
    copy_dir=$(mktemp -d "$gitdir/decision-insert-src.XXXXXX")
    insert=$copy_dir/$(basename "$source_file")
    cp -- "$source_file" "$insert"
    trap 'rmdir "$lock" 2> /dev/null || true; rm -rf "$copy_dir"' EXIT
fi

# Откат при обрыве вставки: перенумерация переименовывает записи через
# временные имена и при ошибке оставляет каталог разобранным. До вставки
# запоминаются изменённые пути; после ошибки пути, изменённые вставкой,
# возвращаются к состоянию HEAD (отслеживаемые) или удаляются (новые).
# Пути, изменённые до вставки, не трогаются.
changed_paths() { git -C "$root" status --porcelain=v1 -z --no-renames -uall | tr '\0' '\n' | sed 's/^...//' | sort -u; }
before=$(changed_paths)
rollback() {
    local path
    printf 'decision-number.sh: вставка оборвалась, откат изменений\n' >&2
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        grep -qxF -- "$path" <<< "$before" && continue
        if git -C "$root" cat-file -e "HEAD:$path" 2> /dev/null; then
            git -C "$root" checkout -q HEAD -- "$path"
        else
            git -C "$root" rm -q --cached --ignore-unmatch -- "$path" 2> /dev/null || true
            rm -f -- "$root/$path"
        fi
    done < <(changed_paths)
    die 'вставка не выполнена, дерево возвращено в исходное состояние'
}

if [ -n "$after" ]; then
    python3 -I "$renumber" insert --after "$((10#$after))" "$insert" >&2 || rollback
else
    python3 -I "$renumber" insert --catalog "$nn" --last "$insert" >&2 || rollback
fi
python3 -I "$renumber" --write-index >&2 || rollback
[ -z "$source_file" ] || rm -f -- "$source_file"

found=$(find "$target" -maxdepth 1 -type f -name "[0-9][0-9][0-9][0-9]-$slug.md" | head -1)
[ -n "$found" ] || die "запись $slug после вставки не найдена"
printf '%s\n' "$found"
