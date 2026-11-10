#!/usr/bin/env bash
# Ожидание удалённого процесса с показом его хода.
#
#   wait-remote.sh <[пользователь@]узел> <pid> <удалённый журнал>
#                  [--timeout-s <секунды>] [--stall-s <секунды>]
#                  [--reconnect-s <секунды>] [--max-reconnects <число>]
#
# Один ssh печатает на узле строку-шапку «wait-remote <первая строка>
# <строк в журнале> <время изменения журнала> <время узла> <секунд от
# запуска процесса>» и выполняет
# `tail --pid=<pid> -F -n +<первая строка> <журнал>`: при первом подключении
# с последних 20 строк, после обрыва — со строки, следующей за последней
# показанной, поэтому повтора нет. Строки журнала идут на экран вкладки
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
if [[ $rlog == '~/'* ]]; then
    rpath="\"\$HOME\"/$(quote_posix "${rlog#\~/}")"
else
    rpath=$(quote_posix "$rlog")
fi
# Удалённая команда: шапка и tail с первой строки <начало>; «first» — с последних
# 20 строк журнала (первое подключение).
remote_cmd() {
    local from
    if [ "$1" = first ]; then
        # shellcheck disable=SC2016 # выражение раскрывает оболочка узла
        from='$(( n > 20 ? n - 19 : 1 ))'
    else
        from=$1
    fi
    printf '%s' "n=\$(wc -l 2> /dev/null < $rpath) || n=0; s=$from;" \
        " m=\$(stat -c %Y -- $rpath 2> /dev/null) || m=-;" \
        " e=\$(ps -o etimes= -p $pid 2> /dev/null | tr -d ' '); [ -n \"\$e\" ] || e=-;" \
        " printf 'wait-remote %s %s %s %s %s\\n' \"\$s\" \"\$n\" \"\$m\" \"\$(date +%s)\" \"\$e\";" \
        " exec tail --pid=$pid -F -n +\"\$s\" -- $rpath"
}

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
# next — номер следующей строки журнала, old — последняя строка, которая уже
# была в журнале до первого подключения (показывается, но ростом не считается).
next=
old=0
while true; do
    header=1
    exec {events}< <(exec ssh "${ssh_opts[@]}" -- "$host" "$(remote_cmd "${next:-first}")")
    ssh_pid=$!
    while true; do
        read_args=()
        left=
        # Шапка и прежние строки первого подключения приходят сразу: сторож
        # проверяет застой после них — по отсчёту из шапки и с прежними строками
        # на экране; подключение без ответа ограничивают ключи ssh.
        if [ -n "$stall_s" ] && (( ! header )) && { [ -z "$next" ] || (( next > old )); }; then
            left=$(( last + stall_s - SECONDS ))
            (( left > 0 )) || { printf 'stall %s %s\n' "$(( SECONDS - last ))" "$host"; exit 125; }
            read_args=(-t "$left")
        fi
        rc=0
        IFS= read -r "${read_args[@]}" line <&"$events" || rc=$?
        if [ "$rc" -gt 128 ]; then
            continue  # истёк срок read -t: сторож проверит застой в начале цикла
        elif [ "$rc" -ne 0 ]; then
            # Строка без перевода строки при обрыве: после переподключения tail
            # пришлёт её целиком с того же номера.
            [ -z "$line" ] || printf '%(%H:%M:%S)T %s\n' -1 "$line"
            break
        fi
        if (( header )); then
            header=0
            read -r tag h_from h_lines h_mtime h_now h_age <<< "$line"
            if [[ $tag == wait-remote && $h_from =~ ^[0-9]+$ && $h_lines =~ ^[0-9]+$ ]]; then
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
                    old=$h_lines
                    [ -z "$quiet" ] || last=$(( SECONDS - quiet ))
                elif [ -n "$quiet" ] && (( SECONDS - quiet > last )); then
                    last=$(( SECONDS - quiet ))  # журнал рос во время обрыва
                fi
                next=$h_from
                continue
            fi
        fi
        # Связь считается восстановленной и журнал выросшим, когда пришла новая
        # строка, а не одна из прежних строк первого подключения; без шапки
        # номер строки неизвестен, и каждая строка считается новой.
        if [ -z "$next" ] || (( next > old )); then
            last=$SECONDS
            attempt=0
        fi
        [ -z "$next" ] || next=$(( next + 1 ))
        printf '%(%H:%M:%S)T %s\n' -1 "$line"
    done
    exec {events}<&-
    ssh_pid=
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
        printf 'remote done pid=%s %s за %s мин\n' "$pid" "$host" "$(( (SECONDS - started + 30) / 60 ))"
        exit 0
    fi
    attempt=$(( attempt + 1 ))
    if (( attempt > max_reconnects )); then
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
