#!/usr/bin/env bash
# Символ workspace задачи: DUTY_SYMBOL, а когда символ выдал herdr и окружение
# его не получило, — строка «символ workspace» последнего служебного блока
# $DUTY_TASK/brief.md. Печатает символ или пустую строку.
set -euo pipefail

if [ -n "${DUTY_SYMBOL:-}" ]; then
    printf '%s\n' "$DUTY_SYMBOL"
    exit 0
fi
brief=${DUTY_TASK:+$DUTY_TASK/brief.md}
if [ -n "$brief" ] && [ -r "$brief" ]; then
    sed -n 's/^- символ workspace: `\(.*\)`$/\1/p' "$brief" | tail -n 1
else
    printf '\n'
fi
