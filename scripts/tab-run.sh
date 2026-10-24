#!/usr/bin/env bash
# Длинная команда во вкладке своего workspace herdr с итогом в файл.
#
# Использование: tab-run.sh <имя> <каталог> <команда>
#
# Вкладка создаётся в workspace вызывающего ($HERDR_WORKSPACE_ID), команда
# пишется в $DUTY_TASK/runs/<имя>/cmd.sh и запускается в корневой панели
# вкладки. Вывод — в out.log, код завершения — в done: файл пишется под
# временным именем и переименовывается, поэтому ожидающий видит его целиком
# (wait-done.sh). Последняя строка в панели — «DUTY-DONE <имя> rc=<код>»
# для herdr pane wait-output. В ответ — JSON с tab_id, pane_id и каталогом.

set -euo pipefail

die() { printf 'tab-run.sh: %s\n' "$*" >&2; exit 1; }

[ $# -eq 3 ] || die 'нужно три аргумента: <имя> <каталог> <команда>'
name=$1 cwd=$2 command=$3

[[ $name =~ ^[a-z0-9][a-z0-9_.-]{0,63}$ ]] || die "имя $name: только строчные латинские буквы, цифры, _ . -"
[ "${HERDR_ENV:-}" = 1 ] || die 'сессия не в herdr (HERDR_ENV не 1)'
[ -n "${HERDR_WORKSPACE_ID:-}" ] || die 'HERDR_WORKSPACE_ID не задан'
[ -n "${DUTY_TASK:-}" ] && [ -d "$DUTY_TASK" ] || die 'DUTY_TASK не задан или каталога нет'
[ -d "$cwd" ] || die "каталога $cwd нет"
command -v jq > /dev/null || die 'нет jq'

run_dir="$DUTY_TASK/runs/$name"
[ ! -e "$run_dir/done" ] || die "в $run_dir уже есть done: выберите другое имя"
mkdir -p "$run_dir"
cwd=$(realpath "$cwd")

# Команда кладётся файлом, а не строкой в pane run: так в терминал уходит
# одна короткая строка без кавычек пользователя.
{
    printf '#!/usr/bin/env bash\n'
    printf 'cd %q || exit 1\n' "$cwd"
    printf '{\n%s\n} > %q 2>&1\n' "$command" "$run_dir/out.log"
    printf 'rc=$?\n'
    printf 'printf "%%s\\n" "$rc" > %q && mv %q %q\n' \
        "$run_dir/done.tmp" "$run_dir/done.tmp" "$run_dir/done"
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
