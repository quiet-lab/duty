#!/usr/bin/env bash
# Вынос задачи в отдельный workspace herdr с агентом duty:worker.
#
# Использование:
#   dispatch.sh <имя> <модель> --brief <файл> [--rules <файл>] ...
#   dispatch.sh <имя> <модель> --goal <цель> --done '<критерий готовности с командой>'
#               --stop '<условие остановки>'
#               --priorities '<порядок предпочтений на развилках | развилок нет — причина>' --read '<прочитанное при постановке>'
#               --recall '<путь итога /duty:recall | не нужна — причина>' ...
#   dispatch.sh <имя> <модель> --resume ...
#   (общие ключи: [--rules <файл>] [--main <имя сессии>] [--root <каталог>]
#               [--results <каталог>] [--plugin-dir <каталог>])
#   dispatch.sh list [--root <каталог>] [--results <каталог>]
#   dispatch.sh symbol [--root <каталог>] [--results <каталог>]   (свободный символ)
#   (корень проекта — --root, иначе scripts/duty_config.py root: DUTY_PROJECT,
#   CLAUDE_PROJECT_DIR, подъём до .duty/duty.toml; каталог результатов — --results,
#   иначе paths.results конфигурации; ключи и слои — reference/config.md)
#   dispatch.sh stalled [--root <каталог>] [--results <каталог>]
#   dispatch.sh close <имя> [--root <каталог>] [--results <каталог>]
#
# <модель> — haiku, sonnet, opus или fable. Fable (claude-fable-5-1)
# запускается только по этому слову: навык /duty:dispatch передаёт его,
# когда пользователь согласился.
#
# Символ workspace — случайный эмодзи из набора symbols.tasks конфигурации
# (вне репозитория — symbols.outside пользовательского слоя), не занятый
# открытыми workspace (подписи задач и токены symbol); он хранится в служебном
# блоке brief.md и сообщается herdr токеном symbol (workspace report-metadata
# --source duty). Пустой набор — символ выдаёт herdr дочернему workspace
# по [ui.workspace_symbols] каталога родителя (токены в workspace get); herdr его
# не дал — запасной scripts/symbols.txt, он токеном не шлётся.
# Цвет заголовка назначает herdr по ui.workspace_symbols.color_rule (токен color
# в workspace get); herdr цвета не дал — плагин считает цвет сам по тому же
# правилу (scripts/rule-color.py) и шлёт токеном color.
# symbols.main — постоянный символ
# главной сессии, задачам не выдаётся; по нему главная сессия и пользователь узнают задачу
# в подписях herdr. Метка workspace и вкладки —
# «<символ> <имя> · <модель>»; имя агента herdr и сессии Claude — <имя>.
# Агент получает окружение DUTY_ROLE=worker, DUTY_TASK, DUTY_NAME,
# DUTY_SYMBOL, DUTY_MODEL и DUTY_PROJECT (корень проекта, если он есть). list и close находят workspace по id из
# служебного блока brief.md и метке «<имя>», «<имя> · …» или «<символ> <имя> · …»,
# а агента — по имени, при его отсутствии по панели из строки «- панель
# агента:» того же блока. Значения служебного блока берутся из последнего
# блока: --resume дописывает новый.
#
# Ворота постановки: записка (--brief или собранная из --goal по шаблону
# reference/brief-template.md) проверяется до создания каталога задачи и до
# обращений к herdr (scripts/rules-lint.py brief): обязательны разделы «Цель»,
# «Критерий готовности» (с командой в обратных кавычках), «Условие остановки»,
# «Приоритеты», «Ссылки» с подразделом «Прочитанное при постановке» и строкой «Справка».
# Нарушение — отказ с кодом 1 и именами разделов. --goal требует --done,
# --stop, --priorities, --read и --recall; без
# --brief, --goal и --resume запуск отклоняется. --resume записку не проверяет.
#
# --resume — продолжение задачи в прежнем каталоге: brief.md, rules.md
# и state.md остаются, прежний workspace задачи без живого агента
# закрывается, к brief.md дописывается новый служебный блок,
# исполнитель продолжает с шага из state.md (agents/worker.md, п. 1).
#
# Запуск — шаги и критерий готовности каждого (проверяется командой):
#   1. каталог задачи <results>/<имя>/ с brief.md, rules.md и runs/ — файлы
#      непустые;
#   2. workspace с меткой «<имя> · <модель>» (слово из аргумента), DUTY_ROLE=worker, DUTY_TASK=<каталог> —
#      дочерний workspace главной сессии (--parent $HERDR_WORKSPACE_ID, один
#      уровень), если задан HERDR_WORKSPACE_ID и DUTY_ROLE не worker; флаг
#      пробуется всегда, отказ herdr (флаг неизвестен, вложенность, родитель
#      исчез) — повтор без него; каждый пропуск родителя — с предупреждением
#      и причиной —
#      есть в herdr workspace list;
#   3. служебный блок в brief.md — строка с id workspace есть в файле;
#   4. claude --agent duty:worker в корневой панели, приглашение прочитать
#      brief.md — позиционным аргументом команды (herdr agent prompt на
#      только что запущенном агенте теряет Enter длинного текста) —
#      herdr agent get показывает agent=claude. Срок start — 30 с: при
#      приглашении аргументом агент занят сразу, и herdr может не счесть его
#      готовым (timeout) при исправной работе. Тогда экран читается через
#      herdr pane read; приглашение в строке ввода или в транскрипте — шаг
#      выполнен с предупреждением, имя агента закрепляется herdr agent rename
#      (если agent_status панели уже working или blocked, агент запущен и
#      работает: экран не читается, приглашение мог вытеснить его первый
#      вызов инструмента; статус проверяется раньше экрана)
#      (после timeout за именем агента нет); пока запуск не завершён, rename
#      отвечает agent_launch_pending — это не отказ: herdr применяет имя
#      из agent start сам, сценарий предупреждает и идёт дальше; другая
#      ошибка rename — отказ; приглашения нет — отказ. Перед start сценарий
#      ждёт приглашения оболочки панели (herdr pane wait-output, в фоне
#      во время записи служебного блока; срок вышел — предупреждение).
#      Если после timeout на экране есть текст команды (--agent duty:worker),
#      а приглашения нет, команда застряла в строке ввода оболочки с ble.sh
#      (Enter в режиме vi не принимает неполный буфер): строка очищается
#      (esc g g d G i), команда набирается заново и принимается ctrl+j;
#      ход рассчитан на bash с ble.sh в режиме vi (ctrl+c там переводит в
#      normal-режим и строку не очищает); в оболочке с другой раскладкой
#      клавиши попадут в строку как текст, и шаг завершится прежним отказом
#      с экраном,
#      затем ждётся рамка интерфейса Claude Code (wait-output, 30 с);
#   5. агент (по панели: имени у него может ещё не быть) перешёл в working
#      или blocked (herdr agent wait, 10 с); если
#      состояния нет, решает экран: приглашение в транскрипте (выше двух
#      последних линий «─») — шаг выполнен, статус может быть и idle
#      (короткий первый ход, состояние herdr запаздывает); приглашение
#      в строке ввода (между этими линиями) — Enter досылается один раз
#      и проверка повторяется; приглашения на экране нет — отказ.
#      После шага имя проверяется herdr agent get: нет его — rename повторяется
#      один раз, при agent_launch_pending снова — предупреждение «имя
#      применится после запуска» без отказа; list и close работают и без имени.
# При отказе шага сценарий останавливается с кодом 1 и печатает, что уже
# создано: workspace не закрывается, чтобы причину было видно в нём.
#
# list — открытые workspace задач (служебный блок brief.md в каталоге
# результатов и живой workspace с тем же id и меткой): символ, состояние
# агента, строка «Шаг:» из state.md, просроченные ожидания раздела «Ожидания»
# («⚠ просрочено +N мин», state-waits.py) и наличие report.md; по нему
# /duty:cleanup предлагает закрытие.
#
# stalled — задачи, которые можно продолжить: state.md есть, шаг в нём
# не завершён (dutylib.py task-finished), живого агента нет (задачи
# нет в list или агент вышел), и нет более нового каталога, имя которого
# начинается с имени задачи (продолжение под новым именем).
#
# close — закрытие workspace задачи извне: агент в idle или done либо уже
# вышел, report.md есть; отметки реестра держателя <имя> снимаются
# принудительно с предупреждением, сигнал task-done-<имя> даётся, если
# исполнитель его не дал (signals.py --keep; после передачи — заголовок
# «Передача» любого уровня в brief.md — не даётся), вкладки кроме корневой
# и сам workspace закрываются; готово, когда workspace нет в herdr workspace list.

set -euo pipefail
# Значения ключей подставляются в шаблон как есть: «&» в них не метасимвол.
shopt -u patsub_replacement 2> /dev/null || true

die() { printf 'dispatch.sh: %s\n' "$*" >&2; exit 1; }
# Запасной набор символов: по одному эмодзи в строке (одна кодовая точка,
# один глиф); действует, когда конфигурация набора не задаёт.
# Имя задачи начинается с a-z, поэтому первый токен метки, не содержащий a-z,
# — всегда символ: эмодзи, а у задач прежних версий буква Ⓐ…Ⓩ.
symbols_fallback=$(dirname "${BASH_SOURCE[0]}")/symbols.txt
# Метка workspace без символа в начале: «🦊 имя · модель» → «имя · модель».
JQ_BARE='def bare: sub("^[^a-z ]+ "; "");'

warn() { printf 'dispatch.sh: предупреждение: %s\n' "$*" >&2; }

# Каталог пользовательского слоя: каталог файла DUTY_CONFIG_USER, иначе ~/.config/duty.
if [ -n "${DUTY_CONFIG_USER:-}" ]; then
    user_config_dir=$(dirname "$DUTY_CONFIG_USER")
else
    user_config_dir=$HOME/.config/duty
fi

plugin_root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
busy="$plugin_root/scripts/busy.py"
signals="$plugin_root/scripts/signals.py"
state_waits="$plugin_root/scripts/state-waits.py"
config_py="$plugin_root/scripts/duty_config.py"
dutylib="$plugin_root/scripts/dutylib.py"

# duty_config.py с корнем проекта, если он найден.
cfg() {
    local cmd=$1
    shift
    if [ -n "$root" ]; then
        python3 -I "$config_py" "$cmd" --root "$root" "$@"
    else
        python3 -I "$config_py" "$cmd" "$@"
    fi
}

# Корень проекта и каталог результатов — из конфигурации (reference/config.md).
# Корня может не быть (вне git): тогда задачи работают из текущего каталога.
resolve_paths() {
    local rc=0
    if [ -z "$root" ]; then
        root=$(python3 -I "$config_py" root 2> /dev/null) || rc=$?
        [ "$rc" != 2 ] || die "конфигурация не разбирается: python3 -I $config_py check"
        [ "$rc" = 0 ] || root=''
    fi
    if [ -n "$root" ]; then
        [ -d "$root" ] || die "каталога проекта $root нет"
        root=$(realpath "$root")
    fi
    if [ -z "$results" ]; then
        results=$(cfg get paths.results) || die 'каталог результатов не получен: duty_config.py get paths.results'
    fi
    # Каталога результатов может ещё не быть (общий режим): первая задача его создаёт.
    mkdir -p "$results" || die "каталог результатов $results не создан"
    results=$(realpath "$results")
}

# Набор символов задач в массив symbol_set: symbols.tasks конфигурации
# (DUTY_SYMBOLS_FILE модуль уже учёл); вне репозитория — symbols.outside
# пользовательского слоя; пустой набор — запасной symbols.txt. symbols_own=1 —
# набор задан конфигурацией (свой), 0 — набор запасной: символ тогда берётся
# у herdr (herdr_tokens), запасной набор нужен лишь когда herdr токена не дал.
load_symbols() {
    local key=symbols.tasks json
    [ -n "$root" ] || key=symbols.outside
    json=$(cfg get "$key" --default '[]') || die "набор символов $key не получен"
    symbol_set=()
    symbols_own=0
    local sym
    while IFS= read -r sym; do
        [ -z "$sym" ] || symbol_set+=("$sym")
    done < <(jq -r '.[]?' <<< "$json")
    [ ${#symbol_set[@]} -eq 0 ] || symbols_own=1
    if [ ${#symbol_set[@]} -eq 0 ] && [ -r "$symbols_fallback" ]; then
        while IFS= read -r sym; do
            [ -z "$sym" ] || symbol_set+=("$sym")
        done < "$symbols_fallback"
    fi
}

# Токены symbol и color на workspace одним вызовом report-metadata (источник
# duty): herdr.fg = "$color" и символ из токена понимает только herdr, у которого
# report-metadata знает --token (форк duty-*); токен report-metadata любого
# источника главнее токенов, которые herdr выдаёт сам по [ui.workspace_symbols].
# Аргументы — workspace, символ, цвет; пустое значение не шлётся (цвет от herdr и символ herdr —
# токен оставлен herdr). Поддержка определяется по ответу сервера, а не по версии
# или справке: токены шлются всегда. Код 2 (команда или флаг неизвестны) — пропуск
# с пометкой; иной код — ошибка отправки, в предупреждение идёт текст ошибки.
# Запуск от этого не прерывается.
report_tokens() {
    local ws=$1 symbol=$2 color=$3 out rc=0 msg what
    local -a tokens=()
    [ -z "$symbol" ] || tokens+=(--token "symbol=$symbol")
    [ -z "$color" ] || tokens+=(--token "color=$color")
    [ ${#tokens[@]} -gt 0 ] || return 0
    what="${symbol:+символ $symbol}${symbol:+${color:+, }}${color:+цвет $color}"
    out=$(herdr workspace report-metadata "$ws" --source duty "${tokens[@]}" 2>&1) || rc=$?
    case $rc in
        0) return 0 ;;
        2) warn "токены workspace $ws пропущены: report-metadata не поддерживает --token в этом herdr (команда или флаг неизвестны)" ;;
        *)
            msg=$(grep -m1 '^{' <<< "$out" | jq -r '.error.message // empty' 2> /dev/null) || msg=
            [ -n "$msg" ] || msg=$(head -n 1 <<< "$out")
            warn "$what у workspace $ws не выставлены: $msg"
            ;;
    esac
    return 0
}

# Токены symbol и color, которые herdr выдал workspace по [ui.workspace_symbols]
# каталога родителя: две строки — символ и цвет — из workspace get; нет токенов
# (старый herdr, нет записи каталога) или ответ не разобран — пустые строки.
herdr_tokens() {
    local out
    out=$(herdr workspace get "$1" 2> /dev/null | grep -m1 '^{') || out=
    jq -r '(.result.workspace.tokens // {}) | (.symbol // ""), (.color // "")' <<< "$out" 2> /dev/null || printf '\n\n'
}

# Срок ожидания приглашения оболочки панели, мс. Замер 2026-11-05 на этой
# станции, интерактивный bash в псевдотерминале (.bashrc с ble.sh, mise
# activate, tv init): printf 'exit\n' | script -q -c 'bash -li' /dev/null —
# 568 / 544 / 548 мс; наибольшее 568 мс, с двойным запасом 1136 мс, округление
# вверх до секунды — 2 с. Истечение срока не отказ: шаг 4 идёт дальше,
# а застрявшую в строке ввода команду подхватывает запасной ход.
shell_ready_timeout_ms=2000

require_tools() {
    [ "${HERDR_ENV:-}" = 1 ] || die 'сессия не в herdr (HERDR_ENV не 1)'
    local tool
    for tool in "$@"; do
        command -v "$tool" > /dev/null || die "нет $tool"
    done
}

# Id workspace из служебного блока brief.md задачи.
task_workspace() {
    # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
    sed -n 's/^- workspace: `\([^`]*\)`.*/\1/p' "$1/brief.md" 2> /dev/null | tail -1
}

# Символ workspace из служебного блока brief.md задачи.
task_symbol() {
    # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
    sed -n 's/^- символ workspace: `\([^`]*\)`.*/\1/p' "$1/brief.md" 2> /dev/null | tail -1
}

# Цвет workspace из служебного блока brief.md задачи.
task_color() {
    # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
    sed -n 's/^- цвет workspace: `\([^`]*\)`.*/\1/p' "$1/brief.md" 2> /dev/null | tail -1
}

# Строка «Шаг:» из state.md задачи, до 100 знаков; нет — «—».
task_step() {
    local step
    step=$(sed -n 's/^\(\*\*\)\{0,1\}Шаг\(\*\*\)\{0,1\}: *//p' "$1/state.md" 2> /dev/null | head -1)
    [ -n "$step" ] || { printf '—'; return; }
    [ ${#step} -le 100 ] || step="${step:0:99}…"
    printf '%s' "${step//|//}"
}

# Просроченные ожидания из раздела «Ожидания» state.md задачи (state-waits.py);
# нет — «—».
task_overdue() {
    local out
    out=$(python3 -I "$state_waits" "$1/state.md" 2> /dev/null) || out=
    [ -n "$out" ] || { printf '—'; return; }
    printf '%s' "${out//|//}"
}

# Есть ли у задачи открытый workspace с её меткой: 0 — есть.
workspace_open() {
    local workspaces=$1 ws=$2 name=$3
    jq -e --arg w "$ws" --arg n "$name" "$JQ_BARE"' .result.workspaces[] | select(.workspace_id == $w and ((.label | bare) == $n or (.label | bare | startswith($n + " · "))))' <<< "$workspaces" > /dev/null
}

# Каталоги задач с открытым workspace, по строке.
open_tasks() {
    local workspaces=$1 dir ws
    for dir in "$results"/*/; do
        dir=${dir%/}
        [ -f "$dir/brief.md" ] || continue
        ws=$(task_workspace "$dir")
        [ -n "$ws" ] || continue
        workspace_open "$workspaces" "$ws" "$(basename "$dir")" || continue
        printf '%s\n' "$dir"
    done
}

# Значения токена (symbol или color) всех открытых workspace из workspace list,
# через пробел с пробелом в конце; токены herdr и duty занимают символы и цвета
# наравне с подписями задач, чтобы наборы herdr и плагина не пересекались.
workspace_tokens() {
    jq -r --arg k "$2" '[.result.workspaces[]? | .tokens[$k]? // empty | select(type == "string" and . != "")] | map(. + " ") | join("")' <<< "$1" 2> /dev/null || true
}

# Случайный эмодзи из набора symbol_set, которого нет у открытых задач (символы
# прежних версий — буквы Ⓐ…Ⓩ — в набор не входят и просто занимают место
# в used). Код 1 — набор исчерпан или пуст.
free_symbol() {
    local workspaces=$1 used=' ' dir sym
    local -a free=()
    load_symbols
    [ ${#symbol_set[@]} -gt 0 ] || return 1
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        used+="$(task_symbol "$dir") "
    done < <(open_tasks "$workspaces")
    used+="$(workspace_tokens "$workspaces" symbol)"
    for sym in "${symbol_set[@]}"; do
        [[ $used == *" $sym "* ]] || free+=("$sym")
    done
    [ ${#free[@]} -gt 0 ] || return 1
    printf '%s' "${free[RANDOM % ${#free[@]}]}"
}

# Цвет задачи по правилу цвета herdr (запасной путь, когда herdr цвета не выдал):
# занятые оттенки — открытые задачи и токены color открытых workspace.
# Аргумент — workspace list; результат — #rrggbb.
free_color() {
    local workspaces=$1 used='' dir
    local -a taken=()
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        taken+=("$(task_color "$dir")")
    done < <(open_tasks "$workspaces")
    read -r -a used <<< "$(workspace_tokens "$workspaces" color)"
    taken+=("${used[@]}")
    python3 -I "$(dirname "${BASH_SOURCE[0]}")/rule-color.py" "${taken[@]}"
}

# Где на экране Claude Code текст приглашения: input — в строке ввода (между
# двумя последними линиями «─»), sent — выше верхней из них, в транскрипте,
# none — на экране нет. Экран — первый аргумент, «-» или его отсутствие —
# stdin; второй аргумент — искомый текст.
prompt_place() {
    local screen=${1:--} text=$2
    [ "$screen" != - ] || screen=$(cat)
    awk -v text="$text" '/─/ { a = b; b = NR } { line[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) if (index(line[i], text)) {
                if (a > 0 && i < a) found_sent = 1
                else if (a > 0 && i > a && i < b) found_input = 1
            }
            print found_input ? "input" : found_sent ? "sent" : "none"
        }' <<< "$screen"
}

agent_status() {
    herdr agent get "$1" 2> /dev/null | jq -r '.result.agent.agent_status // empty' || true
}

# Панель агента из служебного блока brief.md задачи.
task_pane() {
    # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
    sed -n 's/^- панель агента: `\([^`]*\)`.*/\1/p' "$1/brief.md" 2> /dev/null | tail -1
}

# Цель для herdr agent get/wait: имя агента, а если за ним агента нет —
# панель из brief.md (имя применяется после завершения запуска).
agent_target() {
    local name=$1 dir=$2
    if herdr agent get "$name" > /dev/null 2>&1; then
        printf '%s' "$name"
    else
        task_pane "$dir"
    fi
}

# Закрепляет имя за агентом в панели. Код 0 — закреплено; 3 — запуск ещё
# не завершён (agent_launch_pending, имя применит herdr сам); иное — ошибка,
# её текст в $rename_out.
try_rename() {
    local pane=$1 name=$2
    rename_out=$(herdr agent rename "$pane" "$name" 2>&1) && return 0
    case $rename_out in
        *agent_launch_pending*) return 3 ;;
        *) return 1 ;;
    esac
}

cmd_list() {
    local workspaces dir name ws status report sym
    workspaces=$(herdr workspace list)
    printf '| символ | задача | workspace | агент | шаг (state.md) | ожидания | report.md | каталог |\n|---|---|---|---|---|---|---|---|\n'
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        name=$(basename "$dir")
        ws=$(task_workspace "$dir")
        sym=$(task_symbol "$dir")
        status=$(agent_status "$(agent_target "$name" "$dir")")
        report='нет'
        [ -s "$dir/report.md" ] && report='есть'
        # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
        printf '| %s | %s | `%s` | %s | %s | %s | %s | `%s` |\n' "${sym:-—}" "$name" "$ws" "${status:-вышел}" \
            "$(task_step "$dir")" "$(task_overdue "$dir")" "$report" "$dir"
    done < <(open_tasks "$workspaces")
}

# Задачи без живого агента с незавершённым state.md (см. шапку, stalled).
cmd_stalled() {
    local workspaces live=' ' dir name status step other newer
    workspaces=$(herdr workspace list)
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        name=$(basename "$dir")
        status=$(agent_status "$(agent_target "$name" "$dir")")
        [ -z "$status" ] || live+="$name "
    done < <(open_tasks "$workspaces")
    printf '| задача | шаг (state.md) | каталог |\n|---|---|---|\n'
    for dir in "$results"/*/; do
        dir=${dir%/}
        [ -f "$dir/brief.md" ] && [ -f "$dir/state.md" ] || continue
        name=$(basename "$dir")
        [[ $live != *" $name "* ]] || continue
        step=$(task_step "$dir")
        python3 -I "$dutylib" task-finished "$dir/state.md" && continue
        # Продолжение под новым именем (<имя>-2, <имя>2, <имя>-r2) делает
        # прежний каталог прошлым; иные имена с тем же началом (<имя>-ui)
        # — отдельные задачи.
        newer=''
        for other in "$results/$name"?*/; do
            [[ $(basename "$other") =~ ^"$name"(-?r?)?[0-9]+$ ]] || continue
            [ -f "$other/brief.md" ] && { newer=1; break; }
        done
        [ -z "$newer" ] || continue
        # shellcheck disable=SC2016 # обратные кавычки — разметка Markdown, не подстановка
        printf '| %s | %s | `%s` |\n' "$name" "$step" "$dir"
    done
}

# Процессы ожидания задачи, пережившие закрытие workspace: signals.py wait
# с держателем <имя> (по реестру ожиданий) и wait-done.sh/inotifywait, чей
# каталог или аргументы лежат в каталоге задачи. Печатает PID через запятую.
find_orphans() {
    local name=$1 task=$2 proc pid comm cmdline cwd waits found=" "
    task=$(realpath -m "$task")
    waits=$(python3 -I "$signals" list --waits --json) || return 1
    for pid in $(jq -r --arg n "$name" '.waits[] | select(.holder == $n and .alive) | .pid' <<< "$waits"); do
        found+="$pid "
    done
    for proc in /proc/[0-9]*; do
        pid=${proc#/proc/}
        [ "$pid" != "$$" ] || continue
        comm=$(cat "/proc/$pid/comm" 2> /dev/null) || continue
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2> /dev/null) || continue
        # Только сами процессы ожидания: inotifywait или оболочка, чей
        # сценарий — wait-done.sh; редактор или grep с этим именем не в счёт.
        case $comm in
            inotifywait) ;;
            bash | sh) case $cmdline in
                    "bash "*wait-done.sh* | "/usr/bin/bash "*wait-done.sh* | "sh "*wait-done.sh*) ;;
                    *) continue ;;
                esac ;;
            *) continue ;;
        esac
        cwd=$(readlink "/proc/$pid/cwd" 2> /dev/null || true)
        if [[ $cmdline == *"$task/"* || $cmdline == *"$task " || $cwd == "$task" || $cwd == "$task/"* ]]; then
            found+="$pid "
        fi
    done
    # shellcheck disable=SC2086
    printf '%s\n' $found | sort -un | paste -sd, -
}

cmd_close() {
    local name=$1 orphans task ws status target agent_tab tabs tab marks resource signal_note
    task="$results/$name"
    [ -f "$task/brief.md" ] || die "задачи $name нет: $task/brief.md"
    ws=$(task_workspace "$task")
    [ -n "$ws" ] || die "в $task/brief.md нет служебного блока с workspace"
    workspace_open "$(herdr workspace list)" "$ws" "$name" \
        || die "workspace $ws с меткой $name не открыт"

    target=$(agent_target "$name" "$task")
    status=$(agent_status "$target")
    case ${status:-exited} in
        idle | done | exited) ;;
        *) die "агент $name в состоянии $status: задача не завершена" ;;
    esac
    [ -s "$task/report.md" ] || die "нет $task/report.md: задача не завершена"

    # busy.py лежит в том же каталоге scripts/, что и этот сценарий.
    marks=$(python3 -I "$busy" list --json) || die "busy.py list не выполнен"
    for resource in $(jq -r --arg n "$name" '.[] | select(.holder == $n) | .resource' <<< "$marks"); do
        warn "забытая отметка $resource держателя $name снимается"
        python3 -I "$busy" release "$resource" --holder "$name" > /dev/null \
            || die "отметка $resource не снята"
    done

    # Исполнитель даёт task-done-<имя> сам; здесь страховка на случай его сбоя.
    # После передачи (заголовок «Передача» любого уровня в brief.md) задача не завершена.
    if grep -Eq '^#+ +Передача' "$task/brief.md"; then
        signal_note='нет (передача)'
    elif python3 -I "$signals" signal "task-done-$name" "$task/report.md" --from dispatch.sh --keep > /dev/null; then
        signal_note="task-done-$name"
    else
        warn "сигнал task-done-$name не дан"
        signal_note='не дан (ошибка)'
    fi

    # Корневая вкладка — та, где агент; остальные создавал он сам.
    agent_tab=$(herdr agent get "$target" 2> /dev/null | jq -r '.result.agent.tab_id // empty' || true)
    tabs=$(herdr tab list --workspace "$ws" | jq -r '.result.tabs[].tab_id')
    [ -n "$agent_tab" ] || agent_tab=$(head -1 <<< "$tabs")
    for tab in $tabs; do
        [ "$tab" = "$agent_tab" ] && continue
        # Вкладка команды закрывается сама по done: между list и close она могла
        # исчезнуть, тогда ошибка закрытия не ошибка.
        if ! herdr tab close "$tab" > /dev/null; then
            herdr tab list --workspace "$ws" | jq -e --arg t "$tab" '.result.tabs[] | select(.tab_id == $t)' > /dev/null \
                && die "вкладка $tab не закрыта"
        fi
    done

    herdr workspace close "$ws" > /dev/null || die "workspace $ws не закрыт"
    ! herdr workspace list | jq -e --arg w "$ws" '.result.workspaces[] | select(.workspace_id == $w)' > /dev/null \
        || die "workspace $ws остался в herdr workspace list"
    printf 'closed=%s\nworkspace=%s\nreport=%s\nsignal=%s\n' "$name" "$ws" "$task/report.md" "$signal_note"

    # Сирот не убиваем: завершить их по известному PID должен исполнитель
    # или главная сессия.
    if ! orphans=$(find_orphans "$name" "$task"); then
        warn "перечень ожиданий задачи $name не получен"
        printf 'orphans=unknown\n'
    elif [ -n "$orphans" ]; then
        printf 'orphans=%s\n' "$orphans"
        warn "остались процессы ожидания задачи $name: завершить по PID (kill <pid>), не по имени"
    else
        printf 'orphans=none\n'
    fi
}

# Правила одного слоя (файл worker-rules.md); нет файла или он пуст — «—».
layer_rules() {
    if [ -s "$1" ]; then
        cat "$1"
    else
        printf '—'
    fi
}

# Записка для --goal: шаблон с подстановкой значений ключей.
render_brief() {
    local template
    template=$(< "$plugin_root/reference/brief-template.md")
    template=${template//'{{NAME}}'/"$name"}
    template=${template//'{{GOAL}}'/"$goal"}
    template=${template//'{{DONE}}'/"$done_cond"}
    template=${template//'{{STOP}}'/"$stop_cond"}
    template=${template//'{{PRIORITIES}}'/"$priorities_cond"}
    template=${template//'{{RECALL}}'/"$recall_cond"}
    printf '%s\n' "${template//'{{READ}}'/"$read_cond"}"
}

# Ворота постановки: до каталога задачи и до обращений к herdr.
gate_brief() {
    local out
    if [ -n "$resume" ]; then
        [ -z "$done_cond$stop_cond$priorities_cond$read_cond$recall_cond" ] || die '--done, --stop, --priorities, --read и --recall не нужны с --resume'
        return
    fi
    if [ -z "$brief_file" ] && [ -z "$goal" ]; then
        [ -z "$done_cond$stop_cond$priorities_cond$read_cond$recall_cond" ] || die '--done, --stop, --priorities, --read и --recall задаются вместе с --goal'
        die 'нужна записка: --brief <файл>, либо --goal с --done, --stop, --priorities, --read и --recall (reference/brief-template.md), либо --resume'
    fi
    if [ -n "$brief_file" ]; then
        [ -z "$done_cond$stop_cond$priorities_cond$read_cond$recall_cond" ] || die '--done, --stop, --priorities, --read и --recall задаются вместе с --goal'
        out=$(python3 -I "$plugin_root/scripts/rules-lint.py" brief "$brief_file" ${root:+--root "$root"}) \
            || die "записка $brief_file не проходит ворота (reference/brief-template.md):"$'\n'"$out"
        return
    fi
    [ -n "$done_cond" ] || die '--goal требует --done <критерий готовности с командой в обратных кавычках>'
    [ -n "$stop_cond" ] || die '--goal требует --stop <условие остановки>'
    [ -n "$priorities_cond" ] || die "--goal требует --priorities <порядок предпочтений на развилках | 'развилок нет — причина'>"
    [ -n "$read_cond" ] || die '--goal требует --read <прочитанное при постановке>'
    [ -n "$recall_cond" ] || die "--goal требует --recall <путь итога /duty:recall не старше суток | 'не нужна — причина'>"
    out=$(render_brief | python3 -I "$plugin_root/scripts/rules-lint.py" brief - ${root:+--root "$root"}) \
        || die "записка из --goal не проходит ворота (reference/brief-template.md):"$'\n'"$out"
}

# Схема конфигурации проекта: находки или ошибка TOML — отказ до каталога задачи.
check_config() {
    local out rc=0
    out=$(cfg check 2>&1) || rc=$?
    [ "$rc" = 0 ] || die "конфигурация проекта не проходит проверку (reference/config.md):"$'\n'"$out"
}

# Создание workspace задачи: аргументы функции (--parent <id>) — перед общими;
# печатает ответ herdr (stdout и stderr вместе), код возврата — herdr.
create_workspace() {
    herdr workspace create "$@" --cwd "${root:-$PWD}" --label "$label" \
        --env DUTY_ROLE=worker --env "DUTY_TASK=$task" --env "DUTY_NAME=$name" \
        ${symbol:+--env "DUTY_SYMBOL=$symbol"} --env "DUTY_MODEL=$model_arg" \
        ${root:+--env "DUTY_PROJECT=$root"} --no-focus 2>&1
}

cmd_start() {
    local name=$1 model_arg=$2 model task workspaces agents passed template rules
    local rc rename_out created created_json ws pane started_at_ms status prompt screen tab symbol label
    local -a claude_args

    # Имя служит и меткой workspace, и именем агента herdr, и именем сессии
    # Claude; правило имён агентов herdr — самое строгое из трёх.
    [[ $name =~ ^[a-z][a-z0-9_-]{0,31}$ ]] \
        || die "имя $name: [a-z][a-z0-9_-]{0,31}, как у агентов herdr"

    case $model_arg in
        haiku | sonnet | opus) model=$model_arg ;;
        fable) model=claude-fable-5-1 ;;
        *) die "модель $model_arg: haiku, sonnet, opus или fable" ;;
    esac

    [ -z "$brief_file" ] || [ -z "$goal" ] || die '--brief и --goal вместе не задаются'
    [ -z "$resume" ] || [ -z "$brief_file$goal$rules_file" ] || die '--resume продолжает прежние brief.md и rules.md: без --brief, --goal и --rules'
    [ -z "$brief_file" ] || [ -s "$brief_file" ] || die "записки $brief_file нет или она пуста"
    gate_brief
    check_config
    [ -z "$rules_file" ] || [ -s "$rules_file" ] || die "файла правил $rules_file нет или он пуст"

    task=$results/$name
    if [ -n "$resume" ]; then
        [ -s "$task/brief.md" ] && [ -s "$task/state.md" ] || die "--resume: в $task нет brief.md или state.md"
    else
        [ ! -e "$task" ] || die "каталог задачи $task уже есть: другое имя или продолжение --resume"
    fi

    workspaces=$(herdr workspace list)
    stale=$(jq -r --arg n "$name" "$JQ_BARE"' .result.workspaces[] | select((.label | bare) == $n or (.label | bare | startswith($n + " · "))) | .workspace_id' <<< "$workspaces")
    if [ -n "$stale" ]; then
        # Продолжение допускает прежний workspace задачи, в котором агент
        # вышел: его закрывают, иначе подсказка хука о продолжении
        # не сработала бы. Workspace с живым агентом не трогается.
        [ -n "$resume" ] || die "workspace с меткой $name уже есть"
        [ -z "$(agent_status "$(agent_target "$name" "$task")")" ] \
            || die "в workspace задачи $name агент жив: продолжение не нужно"
        for ws in $stale; do
            herdr workspace close "$ws" > /dev/null || die "прежний workspace $ws не закрыт"
        done
        workspaces=$(herdr workspace list)
    fi
    # Свой набор символов (конфигурация проекта или symbols.outside) выбирает
    # символ сам и сообщает herdr токеном; без него символ выдаёт herdr при
    # создании дочернего workspace, запасной набор — когда herdr его не дал.
    # Флаг symbols_own ставит load_symbols: в подоболочке $(…) его не видно,
    # поэтому загрузка — здесь.
    load_symbols
    symbol='' color='' fallback_symbol='' send_symbol=''
    if [ "$symbols_own" = 1 ]; then
        symbol=$(free_symbol "$workspaces") || die "нет свободного символа: все символы набора заняты открытыми задачами или набор пуст"
        send_symbol=$symbol
    else
        fallback_symbol=$(free_symbol "$workspaces") || fallback_symbol=''
    fi
    label="${symbol:+$symbol }$name · $model_arg"
    agents=$(herdr agent list)
    jq -e --arg n "$name" '[.result.agents[] | select(.name == $n)] | length == 0' <<< "$agents" > /dev/null \
        || die "агент herdr с именем $name уже есть"

    # Имя главной сессии для SendMessage: имя её агента herdr совпадает
    # с именем сессии, когда она запущена claude --name; иначе — заголовок
    # терминала, который Claude Code выставляет по имени сессии.
    if [ -z "$main" ] && [ -n "${HERDR_PANE_ID:-}" ]; then
        main=$(jq -r --arg p "$HERDR_PANE_ID" \
            '.result.agents[] | select(.pane_id == $p) | (.name // .terminal_title_stripped // empty)' <<< "$agents")
    fi
    [ -n "$main" ] || main='не определено — спросить пользователя'

    # Сигнал завершения прежней задачи с тем же именем не должен считаться
    # завершением новой.
    python3 -I "$signals" clear "task-done-$name" > /dev/null \
        || warn "сигнал task-done-$name не снят"

    # Шаг 1. Каталог задачи; при --resume файлы остаются прежними.
    mkdir -p "$task/runs"
    if [ -n "$resume" ]; then
        :
    elif [ -n "$brief_file" ]; then
        cp "$brief_file" "$task/brief.md"
    else
        render_brief > "$task/brief.md"
    fi
    if [ -z "$resume" ]; then
        passed='—'
        [ -z "$rules_file" ] || passed=$(< "$rules_file")
        rules=$(< "$plugin_root/reference/worker-rules.md")
        rules=${rules//'{{NAME}}'/$name}
        rules=${rules//'{{USER_RULES}}'/$(layer_rules "$user_config_dir/worker-rules.md")}
        if [ -n "$root" ]; then
            rules=${rules//'{{PROJECT_RULES}}'/$(layer_rules "$root/.duty/worker-rules.md")}
        else
            rules=${rules//'{{PROJECT_RULES}}'/—}
        fi
        printf '%s\n' "${rules//'{{RULES}}'/$passed}" > "$task/rules.md"
    fi
    [ -s "$task/brief.md" ] && [ -s "$task/rules.md" ] && [ -d "$task/runs" ] \
        || die "шаг 1: файлы задачи в $task не созданы"

    # Шаг 2. Workspace. Дочерний workspace главной сессии (один уровень):
    # --parent пробуется всегда, когда задан HERDR_WORKSPACE_ID и роль не worker;
    # отказ herdr (флаг неизвестен, вложенность, родитель исчез) — повтор
    # без флага с предупреждением; справка herdr не читается.
    created="" create_rc=0 parent_skip=""
    if [ -z "${HERDR_WORKSPACE_ID:-}" ]; then
        parent_skip="HERDR_WORKSPACE_ID не задан"
    elif [ "${DUTY_ROLE:-}" = worker ]; then
        parent_skip="запуск из workspace задачи (DUTY_ROLE=worker), вложенность один уровень"
    else
        created=$(create_workspace --parent "$HERDR_WORKSPACE_ID") || create_rc=$?
        if [ "$create_rc" -eq 0 ]; then
            :
        else
            code=$(grep -m1 '^{' <<< "$created" | jq -r '.error.code // empty' 2> /dev/null || true)
            if [ "$create_rc" -eq 2 ] && [[ "$created" == *"unexpected argument '--parent'"* ]] \
                || [ "$code" = workspace_parent_nested ] || [ "$code" = workspace_not_found ]; then
                parent_skip="herdr отклонил --parent $HERDR_WORKSPACE_ID (${code:-${created%%$'\n'*}}), создан без родителя"
            else
                die "шаг 2: herdr workspace create: $created"
            fi
        fi
    fi
    if [ -n "$parent_skip" ]; then
        warn "workspace без родителя: $parent_skip"
        create_rc=0
        created=$(create_workspace) || die "шаг 2: herdr workspace create: $created"
    fi
    # Ответ create приходит вместе со stderr: JSON — первая строка, начинающаяся с «{».
    created_json=$(grep -m1 '^{' <<< "$created" || true)
    ws=$(jq -er '.result.workspace.workspace_id' <<< "$created_json") || die "шаг 2: herdr workspace create: $created"
    pane=$(jq -er '.result.root_pane.pane_id' <<< "$created_json") || die "шаг 2: нет корневой панели: $created"
    herdr workspace list | jq -e --arg w "$ws" '.result.workspaces[] | select(.workspace_id == $w)' > /dev/null \
        || die "шаг 2: workspace $ws не виден в herdr workspace list"
    # Символ herdr — только при пустом своём наборе и только если herdr его выдал;
    # цвет — всегда от herdr, а без него по правилу, токеном.
    herdr_symbol='' herdr_color='' send_color=''
    { IFS= read -r herdr_symbol || true; IFS= read -r herdr_color || true; } < <(herdr_tokens "$ws")
    if [ "$symbols_own" != 1 ]; then
        symbol=${herdr_symbol:-$fallback_symbol}
        [ -n "$symbol" ] || { herdr workspace close "$ws" > /dev/null 2>&1 || true; die 'нет свободного символа: herdr токена не дал, запасной набор исчерпан'; }
        label="$symbol $name · $model_arg"
        herdr workspace rename "$ws" "$label" > /dev/null || warn "workspace $ws не переименован в $label"
    fi
    color=$herdr_color
    if [ -z "$color" ]; then
        color=$(free_color "$workspaces") || color=''
        if [ -n "$color" ]; then
            send_color=$color
        else
            warn 'цвет workspace не посчитан (rule-color.py): задача без цвета'
        fi
    fi
    report_tokens "$ws" "$send_symbol" "$send_color"

    # Ожидание приглашения оболочки идёт в фоне, пока пишется служебный блок:
    # команда, отправленная до конца .bashrc (mise activate, ble.sh), застревает
    # в строке ввода. Ожидание — событие внутри herdr (wait-output), не опрос;
    # образец: строка, кончающаяся знаком приглашения, или режим vi ble.sh.
    herdr pane wait-output "$pane" --source visible --timeout "$shell_ready_timeout_ms" \
        --regex '(?m)([$%#❯>] *$|-- INSERT --)' > /dev/null 2>&1 &
    shell_wait_pid=$!

    # Шаг 3. Служебный блок: агент читает его вместе с запиской.
    started_at_ms=$(date +%s%3N)
    project_line='вне репозитория'
    [ -z "$root" ] || project_line="\`$root\`"
    config_line='общий режим'
    [ -z "$root" ] || [ "$(cfg json | jq -r '._mode')" != project ] || config_line="\`$root/.duty/duty.toml\`"
    color_line=
    [ -z "$color" ] || color_line="- цвет workspace: \`$color\`"
    cat >> "$task/brief.md" << BLOCK

## Служебное (заполняет dispatch.sh)

- workspace: \`$ws\`, метка \`$label\`
- символ workspace: \`$symbol\`
${color_line:+$color_line
}- проект: $project_line
- конфигурация: $config_line
- панель агента: \`$pane\`
- агент herdr и сессия Claude: \`$name\`, модель \`$model\`
- главная сессия для SendMessage: \`$main\`
- каталог плагина: \`$plugin_root\`
- каталог задачи: \`$task\`
- держатель реестра занятости: \`$name\` (\`DUTY_NAME\`)
- запущено: \`started_at_ms=$started_at_ms\`
BLOCK
    grep -q "workspace: \`$ws\`" "$task/brief.md" || die "шаг 3: служебный блок не записан в $task/brief.md"

    if ! wait "$shell_wait_pid"; then
        warn "приглашение оболочки в $pane не дождались за $shell_ready_timeout_ms мс (или wait-output недоступен), агент запускается как есть"
    fi

    # Шаг 4. Агент. --plugin-dir нужен, когда плагин проверяется из другой
    # worktree, чем та, что подключена в корне. Первое приглашение идёт
    # позиционным аргументом: herdr agent prompt на только что запущенном
    # агенте набирает длинный текст в строку ввода, но Enter теряется.
    prompt="Прочитай $task/brief.md и выполняй."
    [ -z "$resume" ] || prompt="Прочитай $task/brief.md и $task/state.md и продолжай с записанного шага."
    claude_args=(--agent duty:worker --model "$model" --name "$name")
    [ -z "$plugin_dir" ] || claude_args+=(--plugin-dir "$(realpath "$plugin_dir")")
    # herdr считает агента готовым у свободной строки ввода, а при приглашении
    # аргументом агент занят с первой секунды: start может истечь по сроку
    # при исправной работе. Поэтому срок короткий, а при timeout решает экран.
    if ! start_out=$(herdr agent start "$name" --kind claude --pane "$pane" --timeout 30000 -- "${claude_args[@]}" "$prompt" 2>&1); then
        case $start_out in
            *'"timeout"'* | *agent_not_ready*) ;;
            *) die "шаг 4: агент не запущен: $start_out; workspace $ws оставлен, экран: herdr pane read $pane --source recent-unwrapped" ;;
        esac
        # Сначала статус панели: агент, уже начавший работу (working, blocked),
        # мог вытеснить текст приглашения с экрана первым вызовом инструмента
        # (машина под нагрузкой), поэтому экран при таком статусе не решает.
        status=$(agent_status "$pane")
        screen=""
        place=none
        case $status in
            working | blocked) ;;
            *)
                screen=$(herdr pane read "$pane" --source recent-unwrapped 2>&1 || true)
                place=$(prompt_place "$screen" brief.md)
                ;;
        esac
        if [ "$place" = none ] && [ "$status" != working ] && [ "$status" != blocked ] \
            && [[ $screen == *'--agent duty:worker'* ]]; then
            # Команда застряла в строке ввода оболочки (ble.sh в режиме vi: Enter
            # не принимает неполный буфер). Esc g g d G очищает строку, i возвращает
            # в insert, ctrl+j (accept-line) принимает буфер.
            warn "команда агента застряла в строке ввода оболочки $pane, набирается заново (esc g g d G i, ctrl+j)"
            printf -v shell_cmd '%q ' claude "${claude_args[@]}" "$prompt"
            if ! herdr pane send-keys "$pane" esc g g d G i > /dev/null \
                || ! herdr pane send-text "$pane" "${shell_cmd% }" > /dev/null \
                || ! herdr pane send-keys "$pane" ctrl+j > /dev/null; then
                die "шаг 4: запасной ход не отправил команду в $pane; workspace $ws оставлен"
            fi
            # Текст brief.md уже есть в набранной команде, поэтому ждётся не он, а
            # рамка интерфейса Claude Code (линия «─»): после неё prompt_place
            # отличает приглашение в строке ввода или транскрипте от строки оболочки.
            herdr pane wait-output "$pane" --source recent-unwrapped --regex '(?m)^─{3,}' --timeout 30000 > /dev/null 2>&1 || true
            screen=$(herdr pane read "$pane" --source recent-unwrapped 2>&1 || true)
            place=$(prompt_place "$screen" brief.md)
        fi
        case $status in
            working | blocked) warn "herdr не подтвердил готовность агента (агент уже работает: $status)" ;;
            *)
                [ "$place" != none ] \
                    || die "шаг 4: агент не готов, приглашения на экране нет: $start_out; workspace $ws оставлен, экран: herdr pane read $pane --source recent-unwrapped"
                warn "herdr не подтвердил готовность агента (приглашение на экране: $place)"
                ;;
        esac
        # После timeout имя за агентом не закреплено (agent get по имени даёт
        # agent_not_found), а по панели агент виден: закрепляем имя сами, чтобы
        # wait, list и close находили его, как обычно.
        # Пока запуск не завершён, rename отвечает agent_launch_pending; имя
        # herdr применит сам, поэтому это не отказ.
        rc=0
        try_rename "$pane" "$name" || rc=$?
        case $rc in
            0) ;;
            3) warn "имя $name применится после завершения запуска (agent_launch_pending)" ;;
            *) die "шаг 4: имя $name не закреплено за агентом в $pane: $rename_out; workspace $ws оставлен" ;;
        esac
    fi
    herdr agent get "$pane" | jq -e '.result.agent.agent == "claude"' > /dev/null \
        || die "шаг 4: в $pane не виден claude"

    # Подпись вкладки и агента у панели — только для глаз: сбой не останавливает запуск.
    tab=$(herdr agent get "$pane" 2> /dev/null | jq -r '.result.agent.tab_id // empty' || true)
    [ -z "$tab" ] || herdr tab rename "$tab" "$label" > /dev/null || warn "вкладка $tab не переименована"
    herdr pane report-metadata "$pane" --source duty --display-agent "$model_arg" > /dev/null \
        || warn "подпись агента у панели $pane не выставлена"

    # Шаг 5. Ждём начала работы: сценарию нужно лишь убедиться, что агент её
    # начал. Состояние herdr после start запаздывает, поэтому при отказе wait
    # решает экран, а не состояние.
    if herdr agent wait "$pane" --until working --until blocked --timeout 10000 > /dev/null 2>&1; then
        status=$(agent_status "$pane")
    else
        screen=$(herdr agent read "$pane" --source recent-unwrapped 2>&1 || true)
        place=$(prompt_place "$screen" brief.md)
        if [ "$place" = input ]; then
            # Приглашение стоит в строке ввода без вывода после него — Enter потерян.
            warn 'приглашение осталось в строке ввода, Enter досылается'
            herdr pane send-keys "$pane" enter > /dev/null || die "шаг 5: Enter не отправлен в $pane"
            if ! herdr agent wait "$pane" --until working --until blocked --timeout 10000 > /dev/null 2>&1; then
                screen=$(herdr agent read "$pane" --source recent-unwrapped 2>&1 || true)
                place=$(prompt_place "$screen" brief.md)
                [ "$place" = sent ] \
                    || die "шаг 5: агент не начал работу после досылки Enter; экран: herdr agent read $pane --source recent-unwrapped"
            fi
        elif [ "$place" != sent ]; then
            die "шаг 5: приглашения нет на экране, агент не начал работу за 10 с; экран: herdr agent read $pane --source recent-unwrapped"
        fi
        # sent: приглашение ушло в транскрипт, агент его принял. Короткий первый
        # ход мог закончиться раньше, чем herdr показал working, поэтому idle
        # здесь — успех, а не отказ.
        status=$(agent_status "$pane")
    fi
    status=${status:-?}

    # Имя к этому времени обычно применено; если нет — один повтор rename,
    # а при agent_launch_pending снова это предупреждение, не отказ.
    if ! herdr agent get "$name" > /dev/null 2>&1; then
        rc=0
        try_rename "$pane" "$name" || rc=$?
        case $rc in
            0) ;;
            3) warn "имя применится после запуска; list/close находят агента по панели из brief.md" ;;
            *) warn "имя $name не закреплено: $rename_out; list/close находят агента по панели из brief.md" ;;
        esac
    fi

    printf 'workspace=%s\nsymbol=%s\npane=%s\nagent=%s\nmodel=%s\nstatus=%s\ntask=%s\nmain=%s\n' \
        "$ws" "$symbol" "$pane" "$name" "$model" "$status" "$task" "$main"
}

[ $# -ge 1 ] || die 'нужна команда: <имя> <модель>, list, stalled или close <имя>'
command=start
case $1 in
    list) command=list; shift ;;
    stalled) command=stalled; shift ;;
    symbol) command=symbol; shift ;;
    close) command=close; shift; [ $# -ge 1 ] || die 'close: нужно имя задачи'; positional=("$1"); shift ;;
    *) [ $# -ge 2 ] || die 'нужны <имя> и <модель>'; positional=("$1" "$2"); shift 2 ;;
esac

brief_file='' goal='' done_cond='' stop_cond='' priorities_cond='' read_cond='' recall_cond='' rules_file='' main='' root='' results='' plugin_dir='' resume=''
while [ $# -gt 0 ]; do
    case $1 in
        --brief) brief_file=${2:?}; shift 2 ;;
        --goal) goal=${2:?}; shift 2 ;;
        --done) done_cond=${2:?}; shift 2 ;;
        --stop) stop_cond=${2:?}; shift 2 ;;
        --priorities) priorities_cond=${2:?}; shift 2 ;;
        --read) read_cond=${2:?}; shift 2 ;;
        --recall) recall_cond=${2:?}; shift 2 ;;
        --rules) rules_file=${2:?}; shift 2 ;;
        --main) main=${2:?}; shift 2 ;;
        --root) root=${2:?}; shift 2 ;;
        --results) results=${2:?}; shift 2 ;;
        --plugin-dir) plugin_dir=${2:?}; shift 2 ;;
        --resume) resume=1; shift ;;
        *) die "неизвестный ключ $1" ;;
    esac
done

case $command in
    list) require_tools herdr jq; resolve_paths; cmd_list ;;
    stalled) require_tools herdr jq; resolve_paths; cmd_stalled ;;
    close) require_tools herdr jq python3; resolve_paths; cmd_close "${positional[0]}" ;;
    start) require_tools herdr jq claude python3; resolve_paths; cmd_start "${positional[@]}" ;;
    symbol) require_tools herdr jq; resolve_paths; sym=$(free_symbol "$(herdr workspace list)") || die 'нет свободного символа'; printf '%s\n' "$sym" ;;
esac
