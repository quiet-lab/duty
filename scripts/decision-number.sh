#!/usr/bin/env bash
# Номер и вставка записи решения в тематический каталог docs/decisions/<NN>-<тема>/.
#
#   decision-number.sh <каталог записей>
#       печатает номер, который получит запись, вставленная последней
#       в каталог: последний номер каталога плюс один; в пустом каталоге —
#       последний номер предыдущих каталогов плюс один. Ничего не меняет.
#   decision-number.sh <каталог записей> --insert <файл> [--after <номер>] [--root <корень>]
#       вставляет запись командой проекта docs.decision_insert из .duty/duty.toml
#       (к ней дописывается --catalog <NN> --last <файл> или --after <номер> <файл>):
#       последующие записи сдвигаются, ссылки на них переписываются, указатель
#       порождается заново — одной транзакцией команды вставки, которая сама
#       откатывает свои шаги при сбое или сигнале. Команда выполняется в корне
#       проекта: надпроект git каталога записей или --root. Ключ не задан —
#       отказ с подсказкой (reference/config.md).
#       Печатает путь записи. Имя файла — тема латиницей через дефис
#       (<тема>.md или NNNN-<тема>.md), номер ставит сценарий.
#
# Нумерация сквозная и повествовательная: номер — место записи в порядке
# каталогов, а не её постоянный идентификатор. Ветви, вставившие записи
# параллельно, сводятся при слиянии перенумерацией renumber-decisions.py.
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

insert='' after='' project=''
while [ $# -gt 0 ]; do
    case $1 in
        --insert) insert=${2:?--insert без файла}; shift 2 ;;
        --after) after=${2:?--after без номера}; shift 2 ;;
        --root) project=${2:?--root без каталога}; shift 2 ;;
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

# Команда вставки — из конфигурации проекта (reference/config.md).
if [ -z "$project" ]; then
    project=$(git -C "$decisions" rev-parse --show-superproject-working-tree 2> /dev/null || true)
    [ -n "$project" ] || project=$root
fi
config_get() { python3 -I "$(dirname "${BASH_SOURCE[0]}")/duty_config.py" get "$1" --root "$project" --default ''; }
insert_cmd=$(config_get docs.decision_insert) || die "не прочитана конфигурация проекта $project"
[ -n "$insert_cmd" ] || die 'задайте docs.decision_insert в .duty/duty.toml'
eval "insert_argv=($insert_cmd)"

# До начала вставки сигнал просто прерывает сценарий: дерево ещё не менялось.
for sig in INT TERM HUP; do
    # shellcheck disable=SC2064 # имя сигнала подставляется при установке ловушки
    trap "die 'получен сигнал $sig, вставка не начата'" "$sig"
done

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

# Команда вставки получает копию черновика в каталоге git репозитория,
# исходный файл удаляется после успешной вставки: процесс, убитый без
# возможности отката (SIGKILL), или незавершённый откат не уносят
# единственный экземпляр записи. Файл с другой файловой системы (scratchpad
# на tmpfs) команда вставки переносит копированием.
gitdir=$(git -C "$decisions" rev-parse --path-format=absolute --git-dir)
source_file=$insert
copy_dir=$(mktemp -d "$gitdir/decision-insert-src.XXXXXX")
trap 'rmdir "$lock" 2> /dev/null || true; rm -rf "$copy_dir"' EXIT
insert=$copy_dir/$(basename "$source_file")
cp -- "$source_file" "$insert"

if [ -n "$after" ]; then
    where=(--after "$((10#$after))")
else
    where=(--catalog "$nn" --last)
fi

# Откат вставки целиком на журнале команды вставки (renumber-decisions.py
# apply_all): она возвращает ровно то, что изменила сама, и не трогает ни
# правки, сделанные до вставки, ни параллельные. Сценарий своего отката
# не ведёт и состояние дерева по git status не разбирает.
#
# Сигнал сценарию передаётся команде вставки как TERM, чтобы она откатила
# свои шаги. Команда запускается в фоне: встроенная wait прерывается
# пойманным сигналом, а ожидание переднего плана отложило бы ловушку до
# конца вставки. Фоновый процесс неинтерактивной оболочки игнорирует INT,
# поэтому и INT передаётся как TERM.
signal_name='' signals=0 child=''
forward() {
    signal_name=$1
    signals=$((signals + 1))
    [ -z "$child" ] || kill -s TERM "$child" 2> /dev/null || true
}
trap 'forward INT' INT
trap 'forward TERM' TERM
trap 'forward HUP' HUP
(cd "$project" && exec "${insert_argv[@]}" "${where[@]}" "$insert") >&2 &
child=$!
[ -z "$signal_name" ] || kill -s TERM "$child" 2> /dev/null || true
while :; do
    seen=$signals code=0
    wait "$child" || code=$?
    # Процесс жив — wait прервал сигнал, ждём дальше.
    if kill -0 "$child" 2> /dev/null; then
        continue
    fi
    # Сигнал пришёл во время wait, а процесс успел завершиться: его код
    # ещё не получен. Повторная wait возвращает его, а для уже полученного
    # кода отвечает 127, и тогда остаётся прежний.
    if [ "$signals" != "$seen" ]; then
        again=0
        wait "$child" || again=$?
        [ "$again" = 127 ] || code=$again
    fi
    break
done
child=''
trap '' INT TERM HUP

reason='вставка оборвалась'
[ -z "$signal_name" ] || reason="получен сигнал $signal_name"
if [ "$code" != 0 ]; then
    # Итог отката сообщила команда вставки выше; сценарий не утверждает,
    # что дерево возвращено, а проверяет только своё: цел ли черновик.
    if [ -e "$insert" ]; then
        die "$reason; вставка не выполнена (код $code), черновик цел; итог отката — в сообщении команды вставки выше"
    fi
    die "$reason; вставка не выполнена (код $code), копии черновика на месте нет: дерево требует ручной проверки (git status), исходный файл сохранён: $source_file"
fi
[ -z "$signal_name" ] || printf 'decision-number.sh: %s после завершения вставки, запись вставлена\n' "$reason" >&2
rm -f -- "$source_file"

found=$(find "$target" -maxdepth 1 -type f -name "[0-9][0-9][0-9][0-9]-$slug.md" | head -1)
[ -n "$found" ] || die "запись $slug после вставки не найдена"
printf '%s\n' "$found"
