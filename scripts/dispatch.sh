#!/usr/bin/env bash
# Вынос задачи в отдельный workspace herdr с агентом duty:worker.
#
# Использование:
#   dispatch.sh <имя> <модель> [--brief <файл> | --goal <текст>]
#               [--rules <файл>] [--main <имя сессии>] [--root <каталог>]
#               [--results <каталог>] [--plugin-dir <каталог>]
#   dispatch.sh list [--root <каталог>] [--results <каталог>]
#   dispatch.sh close <имя> [--root <каталог>] [--results <каталог>]
#
# <модель> — haiku, sonnet, opus или fable. Fable (claude-fable-5-1)
# запускается только по этому слову: навык /duty:dispatch передаёт его,
# когда пользователь согласился.
#
# Метка workspace и вкладки — «<имя> · <модель>»; имя агента herdr и сессии
# Claude — <имя>. list и close находят workspace по id из служебного блока
# brief.md и метке «<имя>» либо «<имя> · …».
#
# Запуск — шаги и критерий готовности каждого (проверяется командой):
#   1. каталог задачи <results>/<имя>/ с brief.md, rules.md и runs/ — файлы
#      непустые;
#   2. workspace с меткой «<имя> · <модель>» (слово из аргумента), DUTY_ROLE=worker, DUTY_TASK=<каталог> —
#      есть в herdr workspace list;
#   3. служебный блок в brief.md — строка с id workspace есть в файле;
#   4. claude --agent duty:worker в корневой панели, приглашение прочитать
#      brief.md — позиционным аргументом команды (herdr agent prompt на
#      только что запущенном агенте теряет Enter длинного текста) —
#      herdr agent get показывает agent=claude;
#   5. агент перешёл в working или blocked (herdr agent wait, 10 с); если
#      состояния нет, решает экран: приглашение в транскрипте (выше двух
#      последних линий «─») — шаг выполнен, статус может быть и idle
#      (короткий первый ход, состояние herdr запаздывает); приглашение
#      в строке ввода (между этими линиями) — Enter досылается один раз
#      и проверка повторяется; приглашения на экране нет — отказ.
# При отказе шага сценарий останавливается с кодом 1 и печатает, что уже
# создано: workspace не закрывается, чтобы причину было видно в нём.
#
# list — открытые workspace задач (служебный блок brief.md в каталоге
# результатов и живой workspace с тем же id и меткой): состояние агента
# и наличие report.md; по нему /duty:cleanup предлагает закрытие.
#
# close — закрытие workspace задачи извне: агент в idle или done либо уже
# вышел, report.md есть; отметки реестра держателя <имя> снимаются
# принудительно с предупреждением, вкладки кроме корневой и сам workspace
# закрываются; готово, когда workspace нет в herdr workspace list.

set -euo pipefail

die() { printf 'dispatch.sh: %s\n' "$*" >&2; exit 1; }
warn() { printf 'dispatch.sh: предупреждение: %s\n' "$*" >&2; }

plugin_root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
busy="$plugin_root/scripts/busy.py"

# Корень монорепозитория: из сабмодуля — его надпроект.
resolve_paths() {
    if [ -z "$root" ]; then
        root=${CLAUDE_PROJECT_DIR:-}
        [ -n "$root" ] || root=$(git rev-parse --show-superproject-working-tree 2> /dev/null || true)
        [ -n "$root" ] || root=$(git rev-parse --show-toplevel 2> /dev/null) || die 'корень монорепозитория не найден, задайте --root'
    fi
    root=$(realpath "$root")
    [ -d "$root/.git" ] || [ -f "$root/.git" ] || die "$root — не корень репозитория"
    [ -n "$results" ] || results="$root/../ocp-dev-stand/results"
    [ -d "$results" ] || die "каталога результатов $results нет, задайте --results"
    results=$(realpath "$results")
}

require_tools() {
    [ "${HERDR_ENV:-}" = 1 ] || die 'сессия не в herdr (HERDR_ENV не 1)'
    local tool
    for tool in "$@"; do
        command -v "$tool" > /dev/null || die "нет $tool"
    done
}

# Id workspace из служебного блока brief.md задачи.
task_workspace() {
    sed -n 's/^- workspace: `\([^`]*\)`.*/\1/p' "$1/brief.md" 2> /dev/null | tail -1
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

cmd_list() {
    local workspaces dir name ws status report
    workspaces=$(herdr workspace list)
    printf '| задача | workspace | агент | report.md | каталог |\n|---|---|---|---|---|\n'
    for dir in "$results"/*/; do
        dir=${dir%/}
        [ -f "$dir/brief.md" ] || continue
        name=$(basename "$dir")
        ws=$(task_workspace "$dir")
        [ -n "$ws" ] || continue
        jq -e --arg w "$ws" --arg n "$name" \
            '.result.workspaces[] | select(.workspace_id == $w and (.label == $n or (.label | startswith($n + " · "))))' <<< "$workspaces" > /dev/null \
            || continue
        status=$(agent_status "$name")
        report='нет'
        [ -s "$dir/report.md" ] && report='есть'
        printf '| %s | `%s` | %s | %s | `%s` |\n' "$name" "$ws" "${status:-вышел}" "$report" "$dir"
    done
}

cmd_close() {
    local name=$1 task ws status agent_tab tabs tab marks resource
    task="$results/$name"
    [ -f "$task/brief.md" ] || die "задачи $name нет: $task/brief.md"
    ws=$(task_workspace "$task")
    [ -n "$ws" ] || die "в $task/brief.md нет служебного блока с workspace"
    herdr workspace list | jq -e --arg w "$ws" --arg n "$name" \
        '.result.workspaces[] | select(.workspace_id == $w and (.label == $n or (.label | startswith($n + " · "))))' > /dev/null \
        || die "workspace $ws с меткой $name не открыт"

    status=$(agent_status "$name")
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

    # Корневая вкладка — та, где агент; остальные создавал он сам.
    agent_tab=$(herdr agent get "$name" 2> /dev/null | jq -r '.result.agent.tab_id // empty' || true)
    tabs=$(herdr tab list --workspace "$ws" | jq -r '.result.tabs[].tab_id')
    [ -n "$agent_tab" ] || agent_tab=$(head -1 <<< "$tabs")
    for tab in $tabs; do
        [ "$tab" = "$agent_tab" ] && continue
        herdr tab close "$tab" > /dev/null || die "вкладка $tab не закрыта"
    done

    herdr workspace close "$ws" > /dev/null || die "workspace $ws не закрыт"
    ! herdr workspace list | jq -e --arg w "$ws" '.result.workspaces[] | select(.workspace_id == $w)' > /dev/null \
        || die "workspace $ws остался в herdr workspace list"
    printf 'closed=%s\nworkspace=%s\nreport=%s\n' "$name" "$ws" "$task/report.md"
}

cmd_start() {
    local name=$1 model_arg=$2 model task workspaces agents passed template rules
    local created ws pane started_at_ms state status prompt screen input tab
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
    [ -z "$brief_file" ] || [ -s "$brief_file" ] || die "записки $brief_file нет или она пуста"
    [ -z "$rules_file" ] || [ -s "$rules_file" ] || die "файла правил $rules_file нет или он пуст"

    task=$results/$name
    [ ! -e "$task" ] || die "каталог задачи $task уже есть: другое имя или продолжение в старом workspace"

    workspaces=$(herdr workspace list)
    jq -e --arg n "$name" '[.result.workspaces[] | select(.label == $n or (.label | startswith($n + " · ")))] | length == 0' <<< "$workspaces" > /dev/null \
        || die "workspace с меткой $name уже есть"
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

    # Шаг 1. Каталог задачи.
    mkdir -p "$task/runs"
    if [ -n "$brief_file" ]; then
        cp "$brief_file" "$task/brief.md"
    else
        template=$(< "$plugin_root/reference/brief-template.md")
        template=${template//'{{NAME}}'/$name}
        printf '%s\n' "${template//'{{GOAL}}'/${goal:-—}}" > "$task/brief.md"
    fi
    passed='—'
    [ -z "$rules_file" ] || passed=$(< "$rules_file")
    rules=$(< "$plugin_root/reference/worker-rules.md")
    rules=${rules//'{{NAME}}'/$name}
    printf '%s\n' "${rules//'{{RULES}}'/$passed}" > "$task/rules.md"
    [ -s "$task/brief.md" ] && [ -s "$task/rules.md" ] && [ -d "$task/runs" ] \
        || die "шаг 1: файлы задачи в $task не созданы"

    # Шаг 2. Workspace.
    created=$(herdr workspace create --cwd "$root" --label "$name · $model_arg" \
        --env DUTY_ROLE=worker --env "DUTY_TASK=$task" --env "DUTY_NAME=$name" --no-focus)
    ws=$(jq -er '.result.workspace.workspace_id' <<< "$created") || die "шаг 2: herdr workspace create: $created"
    pane=$(jq -er '.result.root_pane.pane_id' <<< "$created") || die "шаг 2: нет корневой панели: $created"
    herdr workspace list | jq -e --arg w "$ws" '.result.workspaces[] | select(.workspace_id == $w)' > /dev/null \
        || die "шаг 2: workspace $ws не виден в herdr workspace list"

    # Шаг 3. Служебный блок: агент читает его вместе с запиской.
    started_at_ms=$(date +%s%3N)
    cat >> "$task/brief.md" << BLOCK

## Служебное (заполняет dispatch.sh)

- workspace: \`$ws\`, метка \`$name · $model_arg\`
- панель агента: \`$pane\`
- агент herdr и сессия Claude: \`$name\`, модель \`$model\`
- главная сессия для SendMessage: \`$main\`
- каталог плагина: \`$plugin_root\`
- каталог задачи: \`$task\`
- держатель реестра занятости: \`$name\` (\`DUTY_NAME\`)
- запущено: \`started_at_ms=$started_at_ms\`
BLOCK
    grep -q "workspace: \`$ws\`" "$task/brief.md" || die "шаг 3: служебный блок не записан в $task/brief.md"

    # Шаг 4. Агент. --plugin-dir нужен, когда плагин проверяется из другой
    # worktree, чем та, что подключена в корне. Первое приглашение идёт
    # позиционным аргументом: herdr agent prompt на только что запущенном
    # агенте набирает длинный текст в строку ввода, но Enter теряется.
    prompt="Прочитай $task/brief.md и выполняй."
    claude_args=(--agent duty:worker --model "$model" --name "$name")
    [ -z "$plugin_dir" ] || claude_args+=(--plugin-dir "$(realpath "$plugin_dir")")
    herdr agent start "$name" --kind claude --pane "$pane" --timeout 120000 -- "${claude_args[@]}" "$prompt" > /dev/null \
        || die "шаг 4: агент не готов; workspace $ws оставлен, экран: herdr agent read $pane --source recent-unwrapped"
    herdr agent get "$name" | jq -e '.result.agent.agent == "claude"' > /dev/null \
        || die "шаг 4: в $pane не виден claude"

    # Подпись вкладки и агента у панели — только для глаз: сбой не останавливает запуск.
    tab=$(herdr agent get "$name" 2> /dev/null | jq -r '.result.agent.tab_id // empty' || true)
    [ -z "$tab" ] || herdr tab rename "$tab" "$name · $model_arg" > /dev/null || warn "вкладка $tab не переименована"
    herdr pane report-metadata "$pane" --source duty --display-agent "$model_arg" > /dev/null \
        || warn "подпись агента у панели $pane не выставлена"

    # Шаг 5. Ждём начала работы: сценарию нужно лишь убедиться, что агент её
    # начал. Состояние herdr после start запаздывает, поэтому при отказе wait
    # решает экран, а не состояние.
    if herdr agent wait "$name" --until working --until blocked --timeout 10000 > /dev/null 2>&1; then
        status=$(agent_status "$name")
    else
        screen=$(herdr agent read "$pane" --source recent-unwrapped 2>&1 || true)
        place=$(prompt_place "$screen" brief.md)
        if [ "$place" = input ]; then
            # Приглашение стоит в строке ввода без вывода после него — Enter потерян.
            warn 'приглашение осталось в строке ввода, Enter досылается'
            herdr pane send-keys "$pane" enter > /dev/null || die "шаг 5: Enter не отправлен в $pane"
            if ! herdr agent wait "$name" --until working --until blocked --timeout 10000 > /dev/null 2>&1; then
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
        status=$(agent_status "$name")
    fi
    status=${status:-?}

    printf 'workspace=%s\npane=%s\nagent=%s\nmodel=%s\nstatus=%s\ntask=%s\nmain=%s\n' \
        "$ws" "$pane" "$name" "$model" "$status" "$task" "$main"
}

[ $# -ge 1 ] || die 'нужна команда: <имя> <модель>, list или close <имя>'
command=start
case $1 in
    list) command=list; shift ;;
    close) command=close; shift; [ $# -ge 1 ] || die 'close: нужно имя задачи'; positional=("$1"); shift ;;
    *) [ $# -ge 2 ] || die 'нужны <имя> и <модель>'; positional=("$1" "$2"); shift 2 ;;
esac

brief_file='' goal='' rules_file='' main='' root='' results='' plugin_dir=''
while [ $# -gt 0 ]; do
    case $1 in
        --brief) brief_file=${2:?}; shift 2 ;;
        --goal) goal=${2:?}; shift 2 ;;
        --rules) rules_file=${2:?}; shift 2 ;;
        --main) main=${2:?}; shift 2 ;;
        --root) root=${2:?}; shift 2 ;;
        --results) results=${2:?}; shift 2 ;;
        --plugin-dir) plugin_dir=${2:?}; shift 2 ;;
        *) die "неизвестный ключ $1" ;;
    esac
done

case $command in
    list) require_tools herdr jq; resolve_paths; cmd_list ;;
    close) require_tools herdr jq python3; resolve_paths; cmd_close "${positional[0]}" ;;
    start) require_tools herdr jq claude; resolve_paths; cmd_start "${positional[@]}" ;;
esac
