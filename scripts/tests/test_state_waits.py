"""Проверки state-waits.py: запуск `python3 -I scripts/tests/test_state_waits.py`."""

import calendar
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "state_waits", Path(__file__).resolve().parents[1] / "state-waits.py"
)
assert _spec and _spec.loader
state_waits = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state_waits)

NOW = calendar.timegm((2026, 11, 4, 12, 0, 0))

STATE = """# state
Шаг: 3 — ждём

## Ожидания

- сборка Jenkins: pid 100 wait-done.sh, с 10:00, срок 60 мин
- сигнал x: pid 101 python3, с 11:30, срок 60 мин
- старая строка: pid 102 inotifywait

## Следующий шаг
- не ожидание: pid 1 x, с 01:00, срок 1 мин
"""


class StateWaitsTest(unittest.TestCase):
    def test_marks_overdue_and_missing_ttl(self) -> None:
        marks = state_waits.marks(STATE, NOW)
        self.assertEqual(
            marks,
            ["⚠ просрочено +60 мин: сборка Jenkins", "⚠ без срока: старая строка"],
        )

    def test_midnight_wraparound(self) -> None:
        text = "## Ожидания\n- ждём: pid 5 x, с 23:30, срок 30 мин\n"
        now = calendar.timegm((2026, 11, 4, 0, 40, 0))
        self.assertEqual(state_waits.marks(text, now), ["⚠ просрочено +40 мин: ждём"])

    def test_timeout_record_from_signals(self) -> None:
        text = "## Ожидания\n\nПросрочено: ожидание сигнала x (w), срок 5 мин истёк в 11:00 UTC; сигнала нет\n"
        self.assertEqual(len(state_waits.marks(text, NOW)), 1)

    def test_no_section(self) -> None:
        self.assertEqual(state_waits.marks("# state\nШаг: 1\n", NOW), [])


if __name__ == "__main__":
    unittest.main()
