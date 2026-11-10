#!/usr/bin/env bash
# Ожидание удалённого процесса с показом его хода.
#
#   wait-remote.sh <[пользователь@]узел> <pid> <удалённый журнал>
#                  [--timeout-s <секунды>] [--stall-s <секунды>]
#                  [--reconnect-s <секунды>] [--max-reconnects <число>]
#
# Один ssh печатает на узле строку-шапку «wait-remote <метка> <первая строка>
# <строк в журнале> <inode журнала> <время изменения журнала> <время узла>
# <секунд от запуска процесса>», строки журнала от первой строки до конца
# по шапке (sed), строку «wait-remote-tail <метка>» и выполняет
# `tail --pid=<pid> -F -n +<строк+1> <журнал>`. Первая строка — при первом
# подключении последние 20 строк, после обрыва — строка, следующая за последней
# показанной, поэтому повтора нет. Если журнал за время обрыва усечён или
# заменён (другой inode, строк меньше показанных, на месте последней показанной
# строки другая) — с первой строки, и все его строки считаются новыми.
# Усечение и замену внутри подключения сообщает сам tail -F (сообщения tail
# идут в поток, LC_ALL=C): нумерация начинается с первой строки нового журнала.
# Строки потока до шапки (вывод оболочки узла) идут в stderr, журналом
# не считаются; метка подключения отличает шапку от строк журнала.
# Строки журнала идут на экран вкладки
# (при запуске через tab-run.sh — и в out.log) с префиксом времени
# «ЧЧ:ММ:СС» по мере появления; tail сам заканчивается, когда процесс <pid>
# завершился. Опроса нет: ожидание —
# чтение потока ssh. Пользователь и ключ — из ssh (BatchMode, пароли не
# используются).
#
# Коды: 0 — удалённый процесс завершился (его код выхода с чужого узла
# не получить; итог процесса читается из его журнала); 124 — истёк
# --timeout-s (предел через timeout, печатается «timeout»); 125 — журнал
# не рос дольше --stall-s секунд (печатается «stall <секунд> <узел>»;
# срок отсчитывается от более позднего из двух моментов на узле — изменения
# журнала и запуска процесса, — поэтому ни перезапуск ожидания после timeout,
# ни переподключение после обрыва его не сбрасывают, а старый журнал нового
# процесса застоем не считается; затем — от каждой новой строки;
# 20 прежних строк первого подключения ростом не считаются); 2 — ошибка
# вызова; 3 — связь не восстановилась за --max-reconnects попыток (по
# умолчанию 5).
#
# Обрыв ssh при живом процессе — событие: одно обращение
# `ssh <узел> [ -d /proc/<pid> ]` выясняет, жив ли процесс, и при живом
# процессе соединение открывается снова не раньше чем через --reconnect-s
# (по умолчанию 30 с) после обрыва. Это предел частоты реакции на обрыв,
# а не такт опроса. Состояние ssh контролируют ключи keepalive самого ssh.

set -euo pipefail

die() { printf 'wait-remote.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 3 ] || die 'вызов: wait-remote.sh <[пользователь@]узел> <pid> <удалённый журнал> [--timeout-s N] [--stall-s N] [--reconnect-s N] [--max-reconnects N]'
host=$1
pid=$2
rlog=$3
shift 3
timeout_s=
stall_s=
reconnect_s=30
max_reconnects=5
while [ $# -gt 0 ]; do
    case $1 in
        --timeout-s) timeout_s=${2:?--timeout-s без значения}; shift 2 ;;
        --stall-s) stall_s=${2:?--stall-s без значения}; shift 2 ;;
        --reconnect-s) reconnect_s=${2:?--reconnect-s без значения}; shift 2 ;;
        --max-reconnects) max_reconnects=${2:?--max-reconnects без значения}; shift 2 ;;
        *) die "неизвестный ключ $1" ;;
    esac
done
[[ $pid =~ ^[1-9][0-9]*$ ]] || die "pid $pid: нужно целое число"
[[ $host =~ ^[A-Za-z0-9._@-]+$ && $host != -* ]] || die "узел $host: недопустимое имя"
[[ -z $stall_s || $stall_s =~ ^[1-9][0-9]*$ ]] || die "--stall-s $stall_s: нужно целое число секунд больше нуля"
[[ -z $timeout_s || $timeout_s =~ ^[1-9][0-9]*$ ]] || die "--timeout-s $timeout_s: нужно целое число секунд больше нуля"
[[ $reconnect_s =~ ^[0-9]+$ ]] || die "--reconnect-s $reconnect_s: нужно целое число секунд"
[[ $max_reconnects =~ ^[0-9]+$ ]] || die "--max-reconnects $max_reconnects: нужно целое число"

# Срок --timeout-s: сценарий перезапускается под timeout, 124 — истёк срок.
if [ -n "$timeout_s" ] && [ -z "${WAIT_REMOTE_INNER:-}" ]; then
    inner=(bash "$0" "$host" "$pid" "$rlog" --reconnect-s "$reconnect_s" --max-reconnects "$max_reconnects")
    [ -z "$stall_s" ] || inner+=(--stall-s "$stall_s")
    # timeout идёт в фоне, обёртка ждёт его через wait: ловушка TERM срабатывает
    # сразу и передаёт сигнал дальше, сирот не остаётся.
    WAIT_REMOTE_INNER=1 timeout "$timeout_s" "${inner[@]}" &
    timeout_pid=$!
    # Процесс запущен этим сценарием, его PID известен.
    trap 'kill -TERM "$timeout_pid" 2> /dev/null || true' TERM INT
    rc=0
    wait "$timeout_pid" || rc=$?
    # Ловушка прервала wait: дождаться, пока завершится сам процесс.
    if kill -0 "$timeout_pid" 2> /dev/null; then
        wait "$timeout_pid" || rc=$?
    fi
    [ "$rc" -ne 124 ] || echo timeout
    exit "$rc"
fi

ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
# Путь журнала для оболочки узла: «~/» в начале раскрывает узел, остальное —
# в одинарных кавычках POSIX (чужая оболочка не обязана понимать %q).
quote_posix() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
# shellcheck disable=SC2088 # тильда сравнивается буквально, раскрывает её узел
if [[ $rlog == '~/'* ]]; then
    rpath="\"\$HOME\"/$(quote_posix "${rlog#\~/}")"
else
    rpath=$(quote_posix "$rlog")
fi
# Метка шапки и границы прежних строк: строка журнала с ней не совпадёт.
token="$$-$RANDOM$RANDOM"
# Удалённая команда: шапка, строки журнала с первой строки <начало> до конца
# по шапке, граница и tail со следующей строки; «first» — с последних 20 строк
# журнала (первое подключение). После обрыва: <inode> — из прежней шапки
# («?» — журнал сменился внутри подключения, inode не сверяется), <сумма> —
# cksum последней показанной строки (номер <начало> - 1).
remote_cmd() {
    local start check=
    if [ "$1" = first ]; then
        # shellcheck disable=SC2016 # выражение раскрывает оболочка узла
        start='s=$(( n > 20 ? n - 19 : 1 ));'
    else
        start="s=$1;"
        if (( $1 > 1 )); then
            check=" [ \"\$n\" -ge $(( $1 - 1 )) ] && [ \"\$(sed -n '$(( $1 - 1 ))p' $rpath | cksum)\" = '$3' ]"
            [ "$2" = '?' ] || check+=" && [ \"\$i\" = '$2' ]"
            check+=" || s=1;"
        fi
    fi
    printf '%s' "n=\$(wc -l 2> /dev/null < $rpath) || n=0;" \
        " im=\$(stat -c '%i %Y' -- $rpath 2> /dev/null) || im='- -'; i=\${im% *};" \
        " $start$check" \
        " e=\$(ps -o etimes= -p $pid 2> /dev/null | tr -d ' '); [ -n \"\$e\" ] || e=-;" \
        " printf 'wait-remote $token %s %s %s %s %s\\n' \"\$s\" \"\$n\" \"\$im\" \"\$(date +%s)\" \"\$e\";" \
        " [ \"\$s\" -gt \"\$n\" ] || sed -n \"\$s,\${n}p\" $rpath;" \
        " echo 'wait-remote-tail $token';" \
        " exec env LC_ALL=C tail --pid=$pid -F -n +\$(( n + 1 )) -- $rpath 2>&1"
}

# Сообщения tail -F о смене журнала внутри подключения (LC_ALL=C); перед
# сообщением — обрывок последней строки прежнего журнала, если она была без
# перевода строки.
re_reset='^(.*)tail: (.+(: file truncated| has been replaced;  following new file)|(.+) has appeared;  following new file)$'
re_diag='^(.*)tail: (cannot open .+ for reading: .+|.+ has become inaccessible: .+|.+ giving up on this name)$'

ssh_pid=
sleep_pid=
# Поток ssh и пауза запущены этим сценарием, их PID известны; TERM мог прийти
# между запуском паузы и записью её PID, поэтому снимаются все фоновые задачи.
trap '[ -z "$ssh_pid" ] || kill "$ssh_pid" 2> /dev/null || true
    kill $(jobs -p) 2> /dev/null || true' EXIT
trap 'exit 143' TERM

started=$SECONDS
last=$SECONDS
attempt=0
# next — номер следующей строки журнала, last_line — последняя показанная
# (строка next - 1), ino — inode журнала из последней шапки («?» — журнал
# сменился внутри подключения), fresh — строки до границы растят журнал
# (кроме первого подключения: там это прежние строки), partial — строка
# без перевода строки, на которой кончился вывод tail, final — подключение
# после обрыва, когда процесс уже завершился: дочитать журнал.
next=
last_line=
ino=-
fresh=0
partial=
final=0
show() { printf '%(%H:%M:%S)T %s\n' -1 "$1"; }
# Обрывок последней строки журнала печатается, только если поток закончил сам
# tail (процесс завершился) или истёк срок сторожа; обрывок при обрыве связи
# отбрасывается — после переподключения строка придёт целиком.
flush_partial() { [ -z "$partial" ] || show "$partial"; }
# Строка журнала: показ, счёт; <рост> 1 — строка растит журнал.
log_line() {
    if (( $1 )); then
        last=$SECONDS
        attempt=0
    fi
    next=$(( next + 1 ))
    last_line=$2
    show "$2"
}
while true; do
    phase=header  # header — до шапки, old — до границы, tail — вывод tail
    partial=
    ck=-
    [ -z "$next" ] || (( next < 2 )) || ck=$(printf '%s\n' "$last_line" | cksum)
    exec {events}< <(exec ssh "${ssh_opts[@]}" -- "$host" "$(remote_cmd "${next:-first}" "$ino" "$ck")")
    ssh_pid=$!
    while true; do
        read_args=()
        # Сторож — на выводе tail; шапку и прежние строки ограничивают ключи ssh.
        if [ -n "$stall_s" ] && [ "$phase" = tail ]; then
            left=$(( last + stall_s - SECONDS ))
            (( left > 0 )) || { printf 'stall %s %s\n' "$(( SECONDS - last ))" "$host"; exit 125; }
            read_args=(-t "$left")
        fi
        rc=0
        IFS= read -r "${read_args[@]}" line <&"$events" || rc=$?
        if [ "$rc" -gt 128 ]; then
            # Истёк срок сторожа: read -t сохраняет начатую строку в line.
            partial=$line
            flush_partial
            printf 'stall %s %s\n' "$(( SECONDS - last ))" "$host"
            exit 125
        elif [ "$rc" -ne 0 ]; then
            [ "$phase" != tail ] || partial=$line
            break
        fi
        case $phase in
        header)
            if [[ $line != *"wait-remote $token "* ]]; then
                # Шапку печатает та же команда перед журналом: строки до неё
                # выводит оболочка узла (.bashrc и подобные), это не журнал.
                printf 'wait-remote.sh: вывод оболочки узла: %s\n' "$line" >&2
                continue
            fi
            noise=${line%%"wait-remote $token "*}
            [ -z "$noise" ] || printf 'wait-remote.sh: вывод оболочки узла: %s\n' "$noise" >&2
            read -r h_from _ h_ino h_mtime h_now h_age <<< "${line#*"wait-remote $token "}"
            [[ $h_from =~ ^[0-9]+$ ]] || h_from=1
            [[ $h_ino =~ ^[0-9]+$ ]] || h_ino=-
            ino=$h_ino
            # Тишина — секунд от более позднего из моментов на узле: изменение
            # журнала, запуск процесса (часы узла); неизвестно — пусто.
            quiet=
            if [[ $h_mtime =~ ^[0-9]+$ && $h_now =~ ^[0-9]+$ ]]; then
                quiet=$(( h_now > h_mtime ? h_now - h_mtime : 0 ))
            fi
            if [[ $h_age =~ ^[0-9]+$ ]] && { [ -z "$quiet" ] || (( h_age < quiet )); }; then
                quiet=$h_age
            fi
            if [ -z "$next" ]; then
                [ -z "$quiet" ] || last=$(( SECONDS - quiet ))
            else
                fresh=1
                if (( h_from < next )); then
                    printf 'журнал усечён или заменён, показ с первой строки\n'
                fi
                if [ -n "$quiet" ] && (( SECONDS - quiet > last )); then
                    last=$(( SECONDS - quiet ))  # журнал рос во время обрыва
                fi
            fi
            next=$h_from
            phase=old
            ;;
        old)
            if [ "$line" = "wait-remote-tail $token" ]; then
                phase='tail'
            else
                log_line "$fresh" "$line"
            fi
            ;;
        tail)
            if [[ $line =~ $re_reset ]]; then
                [ -z "${BASH_REMATCH[1]}" ] || show "${BASH_REMATCH[1]}"
                printf 'wait-remote.sh: %s\n' "${BASH_REMATCH[2]}" >&2
                # Новый журнал читается с первой строки, inode сверять не с чем.
                [ -n "${BASH_REMATCH[4]}" ] || printf 'журнал усечён или заменён, показ с первой строки\n'
                next=1
                ino='?'
            elif [[ $line =~ $re_diag ]]; then
                [ -z "${BASH_REMATCH[1]}" ] || show "${BASH_REMATCH[1]}"
                printf 'wait-remote.sh: %s\n' "${BASH_REMATCH[2]}" >&2
            else
                log_line 1 "$line"
            fi
            ;;
        esac
    done
    exec {events}<&-
    stream_rc=0
    wait "$ssh_pid" 2> /dev/null || stream_rc=$?
    ssh_pid=
    # Поток оборвался (ssh вернул не 0): обрывок строки не последний.
    [ "$stream_rc" -eq 0 ] || partial=
    # Поток закончился: одно обращение выясняет, жив ли процесс.
    alive=0
    ssh_rc=0
    ssh "${ssh_opts[@]}" -- "$host" "[ -d /proc/$pid ]" > /dev/null 2>&1 || ssh_rc=$?
    if [ "$ssh_rc" -eq 0 ]; then
        alive=1
    elif [ "$ssh_rc" -ne 255 ]; then
        alive=0  # /proc/<pid> на узле нет
    else
        alive=1  # связи нет, судьба процесса неизвестна
    fi
    if [ "$alive" -eq 0 ]; then
        if [ "$stream_rc" -ne 0 ] && (( ! final )) && [ -n "$next" ]; then
            final=1  # поток оборвался, процесс завершился: дочитать журнал
            continue
        fi
        flush_partial  # последняя строка журнала без перевода строки
        printf 'remote done pid=%s %s за %s мин\n' "$pid" "$host" "$(( (SECONDS - started + 30) / 60 ))"
        exit 0
    fi
    attempt=$(( attempt + 1 ))
    if (( attempt > max_reconnects )); then
        flush_partial
        printf 'связь с %s потеряна, попыток %s\n' "$host" "$max_reconnects"
        exit 3
    fi
    printf 'обрыв связи с %s, повтор %s из %s через %s с\n' "$host" "$attempt" "$max_reconnects" "$reconnect_s"
    # WARNING: пауза без источника событий — предел частоты повторных подключений
    # после обрыва (событие обрыва есть, ожидание чужого узла после него — нет);
    # заменяется, если ssh получит уведомление о возвращении узла.
    # В фоне с wait: TERM не ждёт конца паузы.
    sleep "$reconnect_s" &
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
