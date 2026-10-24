#!/usr/bin/env bash
# Обёртка длинной команды для вкладки workspace herdr.
#
#   tabrun.sh <имя> -- <команда> [аргументы…]
#
# Команда идёт в текущем каталоге вкладки; её stdout и stderr — на экран
# вкладки и в runs/<имя>/log.txt каталога задачи ($DUTY_TASK, без него —
# текущий каталог). По окончании — runs/<имя>/done с кодом завершения
# (появляется переименованием, ждущий видит его целиком) и при HERDR_ENV=1
# уведомление herdr. Каталог runs/<имя>/ должен быть новым: прежняя отметка
# done иначе сразу отпустила бы ждущего waitdone.sh.
set -uo pipefail

if [[ $# -lt 3 || $2 != -- ]]; then
    echo "вызов: tabrun.sh <имя> -- <команда> [аргументы…]" >&2
    exit 2
fi
name=$1
shift 2
if [[ $name == */* || $name == .* ]]; then
    echo "имя прогона без / и без точки в начале: $name" >&2
    exit 2
fi
dir=${DUTY_TASK:-$PWD}/runs/$name
if [[ -e $dir && -n $(ls -A "$dir" 2>/dev/null) ]]; then
    echo "каталог $dir уже занят прежним прогоном: другое имя или убрать каталог" >&2
    exit 2
fi
mkdir -p "$dir"
{
    printf 'cwd: %s\n' "$PWD"
    printf 'cmd:'
    printf ' %q' "$@"
    printf '\nstarted_ms: %s\npid: %s\n' "$(date +%s%3N)" "$$"
} > "$dir/cmd.txt"

"$@" 2>&1 | tee "$dir/log.txt"
code=${PIPESTATUS[0]}

printf 'finished_ms: %s\ncode: %s\n' "$(date +%s%3N)" "$code" >> "$dir/cmd.txt"
printf '%s\n' "$code" > "$dir/.done.tmp"
mv -f "$dir/.done.tmp" "$dir/done"
if [[ ${HERDR_ENV:-} == 1 ]]; then
    sound=done
    [[ $code == 0 ]] || sound=request
    herdr notification show "Вкладка $name: код $code" --body "$dir" --sound "$sound" \
        >/dev/null 2>&1 || true
fi
echo "== $name: код $code, итог — $dir"
exit "$code"
