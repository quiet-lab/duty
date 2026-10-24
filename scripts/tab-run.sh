#!/usr/bin/env bash
# Длинная команда во вкладке своего workspace herdr с итогом в файлы.
#
#   tab-run.sh <имя> [--cwd <каталог>] -- <команда…>
#
# Вкладка создаётся в workspace вызывающего ($HERDR_WORKSPACE_ID) в каталоге
# --cwd (по умолчанию текущий), команда идёт в текущем каталоге вкладки.
# Один аргумент после -- — строка для bash (конвейеры, перенаправления),
# несколько — команда с аргументами, каждый экранируется. Команда пишется
# файлом runs/<имя>/cmd.sh, в терминал уходит одна короткая строка без
# кавычек пользователя. Каталог итогов — runs/<имя>/ в $DUTY_TASK (без
# него — в текущем каталоге), он должен быть новым: прежняя отметка done
# сразу отпустила бы ожидающего wait-done.sh.
#
# Итог: out.log (stdout и stderr вместе, они же на экране вкладки),
# meta.txt (каталог, started_ms, finished_ms, код) и последним done с кодом
# завершения — под временным именем с переименованием, поэтому ожидающий
# видит его целиком. При HERDR_ENV=1 вкладка показывает уведомление herdr
# (звук done, при ненулевом коде request) и печатает «DUTY-DONE <имя>
# rc=<код>» для herdr pane wait-output. В ответ — JSON с tab_id, pane_id
# и run_dir.

set -euo pipefail

die() { printf 'tab-run.sh: %s\n' "$*" >&2; exit 2; }
usage='вызов: tab-run.sh <имя> [--cwd <каталог>] -- <команда…>'

[ $# -ge 3 ] || die "$usage"
name=$1
shift
cwd=$PWD
while [ $# -gt 0 ] && [ "$1" != -- ]; do
    case $1 in
        --cwd) cwd=${2:?--cwd без значения}; shift 2 ;;
        *) die "неизвестный ключ $1; $usage" ;;
    esac
done
[ "${1:-}" = -- ] && [ $# -ge 2 ] || die "$usage"
shift

[[ $name =~ ^[a-z0-9][a-z0-9_.-]{0,63}$ ]] || die "имя $name: только строчные латинские буквы, цифры, _ . -"
[ "${HERDR_ENV:-}" = 1 ] || die 'сессия не в herdr (HERDR_ENV не 1)'
[ -n "${HERDR_WORKSPACE_ID:-}" ] || die 'HERDR_WORKSPACE_ID не задан'
[ -d "$cwd" ] || die "каталога $cwd нет"
command -v jq > /dev/null || die 'нет jq'

if [ $# -eq 1 ]; then
    command=$1
else
    command=$(printf '%q ' "$@")
fi

base=${DUTY_TASK:-$PWD}
[ -d "$base" ] || die "каталога задачи $base нет"
mkdir -p "$base/runs"
run_dir=$(realpath "$base/runs")/$name
# mkdir без -p отказывает, если каталог уже есть: это и есть проверка
# новизны, без гонки между проверкой и созданием.
mkdir "$run_dir" 2> /dev/null || die "каталог $run_dir уже есть: другое имя или убрать каталог"
cwd=$(realpath "$cwd")

{
    printf '#!/usr/bin/env bash\n'
    printf '# Прогон %s от tab-run.sh; идёт в текущем каталоге вкладки (%s).\n' "$name" "$cwd"
    printf 'run_dir=%q\n' "$run_dir"
    printf 'printf "cwd: %%s\\nstarted_ms: %%s\\n" "$PWD" "$(date +%%s%%3N)" > "$run_dir/meta.txt"\n'
    printf '{\n%s\n} 2>&1 | tee "$run_dir/out.log"\n' "$command"
    printf 'rc=${PIPESTATUS[0]}\n'
    printf 'printf "finished_ms: %%s\\ncode: %%s\\n" "$(date +%%s%%3N)" "$rc" >> "$run_dir/meta.txt"\n'
    printf 'printf "%%s\\n" "$rc" > "$run_dir/.done.tmp" && mv -f "$run_dir/.done.tmp" "$run_dir/done"\n'
    printf 'if [ "${HERDR_ENV:-}" = 1 ]; then\n'
    printf '    sound=done; [ "$rc" = 0 ] || sound=request\n'
    printf '    herdr notification show %q --body "$run_dir" --sound "$sound" > /dev/null 2>&1 || true\n' "Вкладка $name завершена"
    printf 'fi\n'
    printf 'printf "DUTY-DONE %%s rc=%%s\\n" %q "$rc"\n' "$name"
} > "$run_dir/cmd.sh"
bash -n "$run_dir/cmd.sh" || die "команда не разбирается bash: $run_dir/cmd.sh"

tab=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$cwd" --label "$name" --no-focus)
tab_id=$(jq -er '.result.tab.tab_id' <<< "$tab") || die "herdr tab create: $tab"
pane_id=$(jq -er '.result.root_pane.pane_id' <<< "$tab") || die "herdr tab create: $tab"

herdr pane run "$pane_id" "bash $(printf '%q' "$run_dir/cmd.sh")" > /dev/null \
    || die "herdr pane run $pane_id не принял команду; вкладка $tab_id остаётся"

jq -n --arg tab "$tab_id" --arg pane "$pane_id" --arg dir "$run_dir" \
    '{tab_id: $tab, pane_id: $pane, run_dir: $dir}'
