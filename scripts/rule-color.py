#!/usr/bin/env python3
"""Цвет задачи по правилу herdr ui.workspace_symbols.color_rule.

Запасной путь dispatch.sh, когда herdr цвета не выдал: оттенок из 36
с шагом 10°, насыщенность 50–70 %, яркость 70–100 % (HSV), случайные.
Аргументы — занятые цвета #rrggbb (открытые задачи и токены color открытых
workspace); оттенки занятых цветов не выбираются, пока есть свободные; все
заняты — любой из 36. Серые и белые цвета (корни) оттенка не имеют и ничего
не занимают. Печатает #rrggbb без перевода строки.
"""

from __future__ import annotations

import colorsys
import random
import sys

STEP = 10
SATURATION = (50, 70)
VALUE = (70, 100)
GRAY = 0.02


def hue_slot(color: str) -> int | None:
    """Номер оттенка (0–35) цвета #rrggbb; None — цвет серый или не разобран."""
    digits = color.lstrip("#")
    if len(digits) != 6:
        return None
    try:
        r, g, b = (int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
    if sat <= GRAY:
        return None
    return round(hue * 360 / STEP) % (360 // STEP)


def rule_color(used: list[str], rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    busy = {s for s in map(hue_slot, used) if s is not None}
    slots = list(range(360 // STEP))
    slot = rng.choice([s for s in slots if s not in busy] or slots)
    sat = rng.randint(*SATURATION) / 100
    val = rng.randint(*VALUE) / 100
    r, g, b = colorsys.hsv_to_rgb(slot * STEP / 360, sat, val)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


if __name__ == "__main__":
    print(rule_color(sys.argv[1:]), end="")
