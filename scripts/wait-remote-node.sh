# Часть wait-remote.sh, которая выполняется на узле: сценарий приходит
# в bash узла на стандартный ввод ssh.
#
#   bash -s -- <журнал> <pid> <метка> <первая запись|first>
#
# Журнал на узле читает наблюдатель — отдельный процесс (setsid), который
# живёт, пока жив процесс <pid>, и переживает обрывы ssh. Он следит
# за каталогом журнала по событиям inotify (inotifywait -m: modify, create,
# moved_to, moved_from, delete), держит журнал открытым и пишет записи
# в спул — файл, который только дописывается:
#
#   o <строка>  прежняя строка журнала (последние 20 при запуске наблюдателя)
#   l <строка>  новая строка журнала
#   t           журнал усечён или заменён, дальше — с его первой строки
#   a           журнала при запуске наблюдателя нет (ожидается появление)
#   w           наблюдатель запущен заново, строки могут повториться
#   x <текст>   inotifywait не смог следить за каталогом; наблюдатель вышел
#   p <строка>  последняя строка журнала без перевода строки (после конца
#               процесса)
#   e           процесс <pid> завершился, журнал дочитан
#
# Усечение — событие modify, после которого размер журнала меньше
# прочитанного; замена — create или moved_to имени журнала с другим inode:
# прежний журнал дочитывается по открытому дескриптору, затем — запись t
# и новый журнал с первой строки. Конец процесса ждёт `tail --pid`.
#
# Подключение печатает шапку «wait-remote <метка> <первая запись> <записей
# в спуле> <время изменения журнала> <время узла> <секунд от запуска
# процесса>» и спул с первой записи (first — с последних 20) до конца работы
# наблюдателя; спул, дочитанный до записи e, удаляется. Нет нужных
# программ — шапка «wait-remote <метка> missing <программы>».
# Спул и замки — в ${TMPDIR:-/tmp}/wait-remote-<uid>/<pid>-<cksum журнала>/.
#
# Ограничения. Наблюдатель живёт, пока жив процесс, и после конца ожидания
# (timeout, stall, потеря связи) — так перезапуск ожидания продолжает тот же
# спул; спул без записи e остаётся на узле до следующего подключения.
# Усечение, за которым журнал дорос дальше прочитанного раньше, чем
# наблюдатель обработал событие (события modify inotify сливаются), неотличимо
# от дописывания — тот же предел у tail -F. Журнал — обычный файл: запись
# в цель символической ссылки событий в каталоге ссылки не даёт.

# Запись в спул: каждый аргумент — одна запись.
rec() { printf '%s\n' "$@" >> "$spool"; }

# Открыть журнал в дескрипторе 3; при неудаче — have=0.
log_open() {
    exec 3<&-
    have=0
    { exec 3< "$log"; } 2> /dev/null || return 1
    have=1
    off=0
    pend=
    ino=$(stat -L -c %i /dev/fd/3)
}

# Разбить кусок журнала на строки: целые — записями <вид>, неоконченная
# остаётся в pend до следующего куска.
log_emit() {
    local text=$pend$2
    if [[ $text == *$'\n'* ]]; then
        pend=${text##*$'\n'}
        printf '%s\n' "${text%$'\n'*}" | sed "s/^/$1 /" >> "$spool"
    else
        pend=$text
    fi
}

# Дочитать открытый журнал от смещения off до конца кусками до CHUNK байт;
# размер меньше прочитанного — усечение: запись t, чтение с начала.
log_sync() {
    (( have )) || return 0
    local size data
    while true; do
        size=$(stat -L -c %s /dev/fd/3) || return 0
        if (( size < off )); then
            [ -z "$pend" ] || rec "l $pend"
            rec t
            off=0
            pend=
        fi
        (( size > off )) || return 0
        (( size - off <= CHUNK )) || size=$(( off + CHUNK ))
        data=$(tail -c +$(( off + 1 )) /dev/fd/3 | head -c $(( size - off )); echo .)
        off=$size
        log_emit l "${data%.}"
    done
}

# Первое чтение: последние 20 целых строк — прежние (o), неоконченная
# строка ждёт продолжения; журнала нет — запись a. Затем сигнал готовности
# подключению, которое запустило наблюдателя (его стандартный вывод).
log_initial() {
    initialized=1
    if log_open; then
        local size k data
        size=$(stat -L -c %s /dev/fd/3)
        k=$(( size < CHUNK ? size : CHUNK ))
        data=$(tail -c +$(( size - k + 1 )) /dev/fd/3 | head -c "$k"; echo .)
        data=${data%.}
        off=$size
        if [[ $data == *$'\n'* ]]; then
            pend=${data##*$'\n'}
            printf '%s\n' "${data%$'\n'*}" | tail -n 20 | sed 's/^/o /' >> "$spool"
        else
            pend=$data
        fi
    else
        rec a
    fi
    printf 'ready\n'
    exec > /dev/null
}

# Имя журнала создано заново или на него переименован файл: тот же inode —
# просто дочитать; другой — дочитать прежний, запись t (если прежний был)
# и новый журнал с первой строки.
log_replace() {
    local new
    new=$(stat -c %i -- "$log" 2> /dev/null) || return 0
    if (( have )) && [ "$new" = "$ino" ]; then
        log_sync
        return 0
    fi
    if (( have )); then
        log_sync
        [ -z "$pend" ] || rec "l $pend"
        rec t
    fi
    log_open && log_sync
}

# Наблюдатель: <каталог спула> <журнал> <pid>.
watch_log() {
    S=$1
    log=$2
    pid=$3
    spool=$S/spool
    CHUNK=1048576
    have=0
    ino=
    off=0
    pend=
    initialized=0
    export LC_ALL=C
    trap '' HUP
    exec 2>> "$S/err" < /dev/null
    printf '%s\n' "$$" > "$S/pid"
    local dir=${log%/*} base=${log##*/} line ev queued=0
    [ -n "$dir" ] || dir=/
    [ ! -s "$spool" ] || rec w
    # Один поток событий: inotifywait и строка конца процесса.
    exec 4< <(
        exec 9>&-
        inotifywait -m -e modify,create,moved_to,moved_from,delete \
            --format '%e %f' "$dir" 2>&1 &
        tail --pid="$pid" -f /dev/null 2> /dev/null
        echo wait-remote-end
        kill "$!" 2> /dev/null
    )
    while IFS= read -r line <&4; do
        if [[ $line =~ ^([A-Z_,]+)\ (.*)$ ]]; then
            ev=,${BASH_REMATCH[1]},
            if [ "${BASH_REMATCH[2]}" != "$base" ]; then
                :  # событие другого файла каталога
            elif [[ $ev == *,CREATE,* || $ev == *,MOVED_TO,* ]]; then
                queued=0
                log_replace
            else
                # Подряд идущие события читаются одним дочитыванием.
                queued=1
            fi
        else
            case $line in
            'Setting up watches.') ;;
            'Watches established.')
                (( initialized )) || log_initial
                ;;
            wait-remote-end)
                (( initialized )) || log_initial
                log_sync
                [ -z "$pend" ] || rec "p $pend"
                rec e
                return 0
                ;;
            *)
                (( initialized )) || log_initial
                rec "x $line"
                # Весь наблюдатель — одна группа процессов (setsid).
                kill -- -"$$"
                ;;
            esac
        fi
        if (( queued )) && ! read -t 0 -u 4; then
            queued=0
            log_sync
        fi
    done
}

# Подключение: <журнал> <pid> <метка> <первая запись|first>.
main() {
    local log=$1 pid=$2 token=$3 start=$4 missing= c
    # Сценарий уже прочитан до вызова main: стандартный ввод больше не нужен.
    exec < /dev/null
    for c in inotifywait flock setsid stat tail ps; do
        command -v "$c" > /dev/null 2>&1 || missing+=" $c"
    done
    if [ -n "$missing" ]; then
        printf 'wait-remote %s missing%s\n' "$token" "$missing"
        return 0
    fi
    case $log in /*) ;; *) log=$PWD/$log ;; esac
    local root key S wpid n last mtime age
    root=${TMPDIR:-/tmp}/wait-remote-$(id -u)
    key=$(printf '%s\n' "$pid $log" | cksum)
    S=$root/$pid-${key%% *}
    mkdir -p -m 700 "$root" "$S"
    touch "$S/spool"
    # Один наблюдатель на пару «процесс, журнал»: замок lock держит живой
    # наблюдатель (дескриптор наследуется при запуске), замок start — запуск.
    exec 8> "$S/start"
    flock 8
    last=$(tail -n 1 -- "$S/spool")
    exec 9>> "$S/lock"
    if [ "$last" != e ] && flock -n 9; then
        exec 6< <(exec setsid bash -c "$(declare -f rec log_open log_emit log_sync log_initial log_replace watch_log); watch_log \"\$@\"" \
            wait-remote-watch "$S" "$log" "$pid" < /dev/null 2> /dev/null 8>&-)
        # Готовность: наблюдатель записал свой PID и первое чтение журнала.
        IFS= read -r _ <&6 || true
        exec 6<&-
    fi
    exec 9>&- 8>&-
    n=$(wc -l < "$S/spool")
    [ "$start" != first ] || start=$(( n > 20 ? n - 19 : 1 ))
    mtime=$(stat -c %Y -- "$log" 2> /dev/null) || mtime=-
    age=$(ps -o etimes= -p "$pid" 2> /dev/null | tr -d ' ')
    [ -n "$age" ] || age=-
    printf 'wait-remote %s %s %s %s %s %s\n' "$token" "$start" "$n" "$mtime" "$(date +%s)" "$age"
    if [ "$last" = e ]; then
        tail -n +"$start" -- "$S/spool"
    elif read -r wpid < "$S/pid" 2> /dev/null && [ -n "$wpid" ]; then
        tail --pid="$wpid" -f -n +"$start" -- "$S/spool"
    else
        # Наблюдатель не запустился: спул как есть, следующее подключение
        # запустит его снова.
        tail -n +"$start" -- "$S/spool"
    fi
    [ "$(tail -n 1 -- "$S/spool")" != e ] || rm -rf -- "$S"
}

main "$@"
