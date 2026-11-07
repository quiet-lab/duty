#!/usr/bin/env bash
# Запуск пробы flow плагина duty: подстановка плейсхолдеров, dispatch.sh,
# команды проверки для главной сессии.
#
# Использование:
#   run-probe.sh <номер> [--sandbox <каталог>] [--main <имя сессии>]
#                [--root <каталог>] [--dry]
#
# Номера: 2 rules-midway, 3 tab-running, 4 opus-subagents,
# 5 jwait-probe, 6 overflow-handoff, 7 neighbours (только команды),
# 6b — приглашение для продолжения пробы 6 (только текст),
# 9 ask-workspace, 9b ask-coordinator.
# Порядок запуска: 3, 2, 6, 7, 5, 4, 9, 9b.
#
# Записка и правила рендерятся в <песочница>/.render/: {{SANDBOX}} —
# каталог песочницы, {{PLUGIN}} — каталог плагина. С --dry сценарий только
# рендерит и печатает команду dispatch.sh, не запуская её.
# Песочница по умолчанию — paths.sandbox, каталог результатов — paths.results
# из конфигурации проекта; корень проекта — CLAUDE_PROJECT_DIR, иначе надпроект
# сабмодуля (reference/config.md).

set -euo pipefail

die() { printf 'run-probe.sh: %s\n' "$*" >&2; exit 1; }

here=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
plugin=$(realpath "$here/../..")

num= sandbox= main= root= dry=0
while [ $# -gt 0 ]; do
    case $1 in
        --sandbox) sandbox=${2:?'--sandbox требует каталог'}; shift ;;
        --main) main=${2:?'--main требует имя'}; shift ;;
        --root) root=${2:?'--root требует каталог'}; shift ;;
        --dry) dry=1 ;;
        -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) die "неизвестный ключ $1" ;;
        *) [ -z "$num" ] || die 'номер задан дважды'; num=$1 ;;
    esac
    shift
done
[ -n "$num" ] || die 'использование: run-probe.sh <номер> [--sandbox <каталог>] [--main <имя>] [--dry]'

if [ -z "$sandbox" ] || [ -z "$root" ]; then
    if [ -z "$root" ]; then
        root=${CLAUDE_PROJECT_DIR:-}
        [ -n "$root" ] || root=$(git -C "$plugin" rev-parse --show-superproject-working-tree 2> /dev/null || true)
        [ -n "$root" ] || root=$(git -C "$plugin" rev-parse --show-toplevel)
    fi
fi
cfg() { python3 -I "$plugin/scripts/duty_config.py" --root "$root" get "$1"; }
[ -n "$sandbox" ] || sandbox=$(cfg paths.sandbox)
[ -d "$sandbox" ] || die "песочницы $sandbox нет: make-sandbox.sh $sandbox"
sandbox=$(realpath "$sandbox")
root=$(realpath "$root")
res=$(realpath -m "$(cfg paths.results)")

# Номер -> имя задачи и модель.
case $num in
    2) name=rules-midway model=haiku ;;
    3) name=tab-running model=haiku ;;
    4) name=opus-subagents model=opus ;;
    5) name=jwait-probe model=haiku ;;
    6) name=overflow-handoff model=haiku ;;
    6b | 6б) name=overflow-handoff model=haiku ;;
    7) name=neighbours model= ;;
    9) name=ask-workspace model=sonnet ;;
    9b | 9б) name=ask-coordinator model=opus ;;
    *) die "нет пробы $num (есть 2-7, 6b, 9 и 9b; проба 8 — push-wake/run.sh)" ;;
esac

render() { # источник приёмник
    sed -e "s#{{SANDBOX}}#$sandbox#g" -e "s#{{PLUGIN}}#$plugin#g" "$1" > "$2"
}

dispatch=$plugin/scripts/dispatch.sh
task=$res/$name

# Команды проверки главной сессии: $SB — песочница, $TASK — каталог задачи.
checks() {
    printf '\nПроверка главной сессией (SB=%s, TASK=%s):\n' "$sandbox" "$task"
    case $num in
        3) cat <<CMD
touch $sandbox/marker/go
cat $task/runs/waiter/done
bash $dispatch list
CMD
            printf 'Ожидается: done rc=0, вкладка и workspace закрыты.\n' ;;
        2) cat <<CMD
# запрет дописать до отметки; отметку создать после появления runs/tests и записи state.md
printf '%s\n' '- Push в origin запрещён (дописано главной сессией).' >> $task/rules.md
touch $sandbox/marker/rules-go
git -C $sandbox/origin.git branch
grep -n push $task/report.md
CMD
            printf 'Ожидается: в origin.git нет probe/rules-midway, в отчёте строка запрета.\n' ;;
        6) cat <<CMD
grep Передача $task/brief.md
grep -i передача $task/report.md
bash $dispatch list
ls $task/second-step.txt   # файла быть не должно
CMD
            printf 'Ожидается: раздел «Передача», state.md, report.md с «передача», workspace открыт.\nПродолжение: run-probe.sh 6b.\n' ;;
        6b | 6б) cat <<CMD
herdr agent prompt overflow-handoff "$(cat "$here/briefs/overflow-handoff.resume.txt")"
cat $task/second-step.txt
CMD
            printf 'Ожидается: second-step.txt со строкой «второй шаг выполнен», workspace закрыт.\n' ;;
        7) mkdir -p "$sandbox/.render"
            render "$here/briefs/neighbours.md" "$sandbox/.render/neighbours.md"
            cat "$sandbox/.render/neighbours.md"
            printf '\nКоманды: /duty:cleanup ; /duty:gates now ; bash %s list\n' "$dispatch" ;;
        5) cat <<CMD
cat $task/runs/jenkins/done
cat $task/runs/jenkins/meta.txt
head $task/report.md
CMD
            printf 'Ожидается: номер сборки, результат, длительность; либо «сборок в ходу нет».\n' ;;
        4) cat <<CMD
git -C $sandbox/origin.git branch
cat $task/runs/tests2/done
grep -i sonnet $task/report.md
CMD
            printf 'Ожидается: probe/fix-pricing в origin.git, tests2 с кодом 0, субагент sonnet в отчёте.\n' ;;
        9) cat <<CMD
# пока агент спрашивает (PANE — панель агента из вывода dispatch.sh):
herdr agent list                                   # статус панели пробы: blocked
herdr agent read \$PANE --source detection --format text   # «По документам:» и варианты
ls $sandbox/marker/                                # ask-step1, decide-me, без ask-step3
cat $task/runs/longrun/done                        # файла нет: прогон идёт
# ответ: выбрать вариант в диалоге панели (herdr pane send-keys \$PANE <клавиши>), затем
touch $sandbox/marker/ask-go
ls $sandbox/marker/
cat $task/runs/longrun/done
grep -A3 'Заданные вопросы' $task/report.md
bash $dispatch list
CMD
            printf 'Ожидается: до ответа blocked, ask-step3 нет, longrun идёт, от пробы в главной сессии сообщений нет; после — ask-step3, longrun-done, decide-me удалён или decide-kept, done=0, один вопрос с ответом, workspace закрыт.\n' ;;
        9b | 9б) cat <<CMD
# пока координатор спрашивает (PANE — панель координатора):
herdr agent list                                   # статус панели: blocked
herdr agent read \$PANE --source detection --format text   # «По документам:» и варианты
ls $sandbox/marker/                                # coord-b-step1, coord-decide, без coord-a-done
cat $task/runs/tests-a/done                         # файла нет: субагент A идёт
# ответ: выбрать вариант в диалоге панели (herdr pane send-keys \$PANE <клавиши>), затем
touch $sandbox/marker/coord-go
ls $sandbox/marker/
grep -A3 'Заданные вопросы' $task/report.md
grep -i 'Доля субагентов' $task/report.md
bash $dispatch list
CMD
            printf 'Ожидается: до ответа blocked, coord-a-done и done tests-a нет, сообщений в главной сессии нет; после — coord-a-done, coord-step3, решение по coord-decide, один вопрос от B в отчёте, таблица субагентов с моделями, workspace закрыт.\n' ;;
    esac
}

case $num in
    7 | 6b | 6б) checks; exit 0 ;;
esac

render_dir=$sandbox/.render
mkdir -p "$render_dir"
brief=$render_dir/$name.brief.md
rules=$render_dir/$name.rules.txt
render "$here/briefs/$name.brief.md" "$brief"
render "$here/briefs/$name.rules.txt" "$rules"

cmd=(bash "$dispatch" "$name" "$model" --brief "$brief" --rules "$rules" --root "$root")
[ -z "$main" ] || cmd+=(--main "$main")

if [ "$dry" = 1 ]; then
    printf 'Команда (не запущена):\n'
    printf '%q ' "${cmd[@]}"
    printf '\n'
else
    "${cmd[@]}"
fi
checks
