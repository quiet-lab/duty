#!/usr/bin/env bash
# Ожидание удалённого процесса с показом его хода.
#
#   wait-remote.sh <[пользователь@]узел> <pid> <удалённый журнал>
#                  [--timeout-s <секунды>] [--stall-s <секунды>]
#                  [--reconnect-s <секунды>] [--max-reconnects <число>]
#
# Журнал на узле читает наблюдатель wait-remote-node.sh: он следит за
# журналом по событиям inotify (inotifywait), живёт, пока жив процесс <pid>,
# переживает обрывы ssh и пишет строки журнала в спул на узле — файл,
# который только дописывается (виды записей — в wait-remote-node.sh).
# Усечение и замену журнала (mv, новый файл) наблюдатель видит по событиям
# и тогда, когда связи нет; на экран выходит «журнал усечён или заменён,
# показ с первой строки» и строки нового журнала с первой.
# Одно подключение ssh передаёт сценарий узла на стандартный ввод bash узла,
# при необходимости запускает наблюдателя и читает спул: при первом
# подключении — с последних 20 записей, после обрыва — с записи, следующей
# за последней принятой, поэтому повтора и потерь нет; обрывок записи при
# обрыве отбрасывается и после переподключения приходит целиком.
# Строки потока до шапки подключения (вывод оболочки узла) идут в stderr,
# журналом не считаются; метка подключения отличает шапку от записей.
# Строки журнала идут на экран вкладки (при запуске через tab-run.sh —
# и в out.log) с префиксом времени «ЧЧ:ММ:СС» по мере появления. Опроса нет:
# ожидание — чтение потока ssh. Пользователь и ключ — из ssh (BatchMode,
# пароли не используются). На узле нужны bash, inotifywait (inotify-tools),
# flock и setsid (util-linux), GNU tail, stat, ps; без них — сообщение
# с перечнем недостающих и код 2.
#
# Коды: 0 — удалённый процесс завершился (его код выхода с чужого узла
# не получить; итог процесса читается из его журнала); 124 — истёк
# --timeout-s (предел через timeout, печатается «timeout»); 125 — журнал
# не рос дольше --stall-s секунд (печатается «stall <секунд> <узел>»;
# срок отсчитывается от более позднего из двух моментов на узле — изменения
# журнала и запуска процесса, — поэтому ни перезапуск ожидания после timeout,
# ни переподключение после обрыва его не сбрасывают, а старый журнал нового
# процесса застоем не считается; затем — от каждой новой строки; прежние
# строки первого подключения ростом не считаются; срок проверяется и при
# каждом обрыве, поэтому обрывы до первой строки кончаются кодом 125,
# а не 3); 2 — ошибка вызова, на узле нет нужных программ или inotifywait
# не смог следить за каталогом журнала; 3 — связь не восстановилась
# за --max-reconnects попыток (по умолчанию 5).
#
# Обрыв ssh — событие: соединение открывается снова не раньше чем через
# --reconnect-s (по умолчанию 30 с) после обрыва. Это предел частоты реакции
# на обрыв, а не такт опроса. Состояние ssh контролируют ключи keepalive
# самого ssh.

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
node_script=$(dirname "${BASH_SOURCE[0]}")/wait-remote-node.sh
[ -r "$node_script" ] || die "нет $node_script"
# Путь журнала для оболочки узла: «~/» в начале раскрывает узел, остальное —
# в одинарных кавычках POSIX (чужая оболочка не обязана понимать %q).
quote_posix() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
# shellcheck disable=SC2088 # тильда сравнивается буквально, раскрывает её узел
if [[ $rlog == '~/'* ]]; then
    rpath="\"\$HOME\"/$(quote_posix "${rlog#\~/}")"
else
    rpath=$(quote_posix "$rlog")
fi
# Метка шапки: строка вывода оболочки узла с ней не совпадёт.
token="$$-$RANDOM$RANDOM"
# Удалённая команда: оболочка узла проверяет bash и передаёт ему сценарий
# узла со стандартного ввода; <начало> — номер первой записи спула или first.
remote_cmd() {
    printf '%s' "command -v bash > /dev/null 2>&1 || { echo 'wait-remote $token missing bash'; exit 0; };" \
        " exec bash -s -- $rpath $pid $token $1"
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
# next — номер следующей записи спула (first — первое подключение), fresh —
# записи до границы растят журнал (кроме первого подключения: там это прежние
# строки), finished — пришла запись e: процесс завершился, журнал дочитан.
next=first
fresh=0
finished=0
show() { printf '%(%H:%M:%S)T %s\n' -1 "$1"; }
stall() {
    printf 'stall %s %s\n' "$(( SECONDS - last ))" "$host"
    exit 125
}
# Запись спула (wait-remote-node.sh): <номер> <запись>.
record() {
    local kind=${2:0:1} text=${2:2}
    case $kind in
    o | p) show "$text" ;;
    l)
        # Новая строка растит журнал, кроме прежних строк первого подключения.
        if (( fresh || $1 > bound )); then
            last=$SECONDS
            attempt=0
        fi
        show "$text"
        ;;
    t) printf 'журнал усечён или заменён, показ с первой строки\n' ;;
    a) printf 'wait-remote.sh: журнала %s на узле %s пока нет\n' "$rlog" "$host" >&2 ;;
    w) printf 'wait-remote.sh: наблюдатель на узле %s запущен заново, строки журнала могут повториться\n' "$host" >&2 ;;
    x)
        printf 'wait-remote.sh: %s: %s\n' "$host" "$text" >&2
        exit 2
        ;;
    e) finished=1 ;;
    *) printf 'wait-remote.sh: неизвестная запись спула: %s\n' "$2" >&2 ;;
    esac
}
while true; do
    phase=header  # header — до шапки, old — до границы, tail — новые записи
    exec {events}< <(exec ssh "${ssh_opts[@]}" -- "$host" "$(remote_cmd "$next")" < "$node_script")
    ssh_pid=$!
    while true; do
        read_args=()
        # Сторож — на новых записях; шапку и прежние записи ограничивают ключи
        # ssh, обрывы до границы — проверка срока при обрыве.
        if [ -n "$stall_s" ] && [ "$phase" = tail ] && (( ! finished )); then
            left=$(( last + stall_s - SECONDS ))
            (( left > 0 )) || stall
            read_args=(-t "$left")
        fi
        rc=0
        IFS= read -r "${read_args[@]}" line <&"$events" || rc=$?
        (( rc <= 128 )) || stall
        # Поток кончился; обрывок записи отбрасывается.
        (( rc == 0 )) || break
        if [ "$phase" = header ]; then
            if [[ $line != *"wait-remote $token "* ]]; then
                # Шапку печатает та же команда перед спулом: строки до неё
                # выводит оболочка узла (.bashrc и подобные), это не журнал.
                printf 'wait-remote.sh: вывод оболочки узла: %s\n' "$line" >&2
                continue
            fi
            noise=${line%%"wait-remote $token "*}
            [ -z "$noise" ] || printf 'wait-remote.sh: вывод оболочки узла: %s\n' "$noise" >&2
            read -r h_from h_count h_mtime h_now h_age <<< "${line#*"wait-remote $token "}"
            if [ "$h_from" = missing ]; then
                printf 'wait-remote.sh: на узле %s нет: %s (inotifywait — пакет inotify-tools)\n' \
                    "$host" "$h_count${h_mtime:+ $h_mtime}${h_now:+ $h_now}${h_age:+ $h_age}" >&2
                exit 2
            fi
            [[ $h_from =~ ^[0-9]+$ ]] || h_from=1
            [[ $h_count =~ ^[0-9]+$ ]] || h_count=0
            # Тишина — секунд от более позднего из моментов на узле: изменение
            # журнала, запуск процесса (часы узла); неизвестно — пусто.
            quiet=
            if [[ $h_mtime =~ ^[0-9]+$ && $h_now =~ ^[0-9]+$ ]]; then
                quiet=$(( h_now > h_mtime ? h_now - h_mtime : 0 ))
            fi
            if [[ $h_age =~ ^[0-9]+$ ]] && { [ -z "$quiet" ] || (( h_age < quiet )); }; then
                quiet=$h_age
            fi
            if [ "$next" = first ]; then
                [ -z "$quiet" ] || last=$(( SECONDS - quiet ))
            else
                fresh=1
                if [ -n "$quiet" ] && (( SECONDS - quiet > last )); then
                    last=$(( SECONDS - quiet ))  # журнал рос во время обрыва
                fi
            fi
            # Записи до границы (номер не больше числа записей спула при
            # подключении) — прежние.
            idx=$(( h_from - 1 ))
            bound=$h_count
            phase=old
            (( idx < bound )) || phase=tail
            continue
        fi
        idx=$(( idx + 1 ))
        next=$(( idx + 1 ))
        (( idx < bound )) || phase=tail
        record "$idx" "$line"
    done
    exec {events}<&-
    wait "$ssh_pid" 2> /dev/null || true
    ssh_pid=
    if (( finished )); then
        printf 'remote done pid=%s %s за %s мин\n' "$pid" "$host" "$(( (SECONDS - started + 30) / 60 ))"
        exit 0
    fi
    # Обрыв: застой проверяется и здесь — обрывы до первой новой записи
    # не продлевают срок сторожа.
    if [ -n "$stall_s" ] && (( SECONDS - last >= stall_s )); then
        stall
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
