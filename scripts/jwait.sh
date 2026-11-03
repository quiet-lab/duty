#!/usr/bin/env bash
# Дождаться конца сборки Jenkins и записать итог в каталог.
#
#   jwait.sh <задание> <номер|last> [--out <каталог>]
#
# Задание — полное имя (`dev2/qms/PR-2580`) или адрес задания. Итог:
# <каталог>/console.log, <каталог>/report.md и последним <каталог>/done
# с кодом (0 — SUCCESS, 1 — другой итог сборки, 2 — ожидание не удалось).
# Каталог по умолчанию — $DUTY_TASK/runs/jenkins-<задание>-<номер>, без
# DUTY_TASK — ocp-dev-stand/results/wait/<задание>-<номер>. Ожидание —
# по событию Jenkins CLI `console -f` по WebSocket, без опроса (jwait.py,
# запись 0049 монорепозитория). Сборки не запускает.
set -euo pipefail
exec python3 -I "$(dirname "$(readlink -f "$0")")/jwait.py" "$@"
