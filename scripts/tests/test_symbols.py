"""Символы workspace: выбор эмодзи, разбор подписей, list и close на двойнике herdr."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
DISPATCH = PLUGIN / "scripts" / "dispatch.sh"
SYMBOLS_FILE = PLUGIN / "scripts" / "symbols.txt"

# Двойник herdr: состояние workspace — файл ws.json; close убирает workspace из файла.
FAKE_HERDR = r"""#!/usr/bin/env bash
case "$1 $2" in
    "workspace list") cat "$FAKE_DIR/ws.json" ;;
    "workspace close")
        jq --arg w "$3" '.result.workspaces |= map(select(.workspace_id != $w))' "$FAKE_DIR/ws.json" > "$FAKE_DIR/ws.tmp"
        mv "$FAKE_DIR/ws.tmp" "$FAKE_DIR/ws.json" ;;
    "tab list") echo '{"result":{"tabs":[{"tab_id":"t1"}]}}' ;;
    "agent get") exit 1 ;;
    *) exit 0 ;;
esac
"""


def brief(ws: str, sym: str) -> str:
    return f"## Служебное\n\n- workspace: `{ws}`, метка\n- символ workspace: `{sym}`\n"


class SymbolsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / ".git").mkdir(parents=True)
        self.results = self.dir / "results"
        self.results.mkdir()
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        herdr = bin_dir / "herdr"
        herdr.write_text(FAKE_HERDR, encoding="utf-8")
        herdr.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERDR_ENV": "1",
            "FAKE_DIR": str(self.dir),
            "DUTY_BUSY_DIR": str(self.dir / "busy"),
            "DUTY_SIGNALS_DIR": str(self.dir / "signals"),
        }
        self.workspaces: list[dict] = []
        self.write_ws()

    def write_ws(self) -> None:
        (self.dir / "ws.json").write_text(
            json.dumps({"result": {"workspaces": self.workspaces}}), encoding="utf-8"
        )

    def task(
        self, name: str, sym: str, label: str, ws: str, report: bool = False
    ) -> None:
        d = self.results / name
        d.mkdir()
        (d / "brief.md").write_text(brief(ws, sym), encoding="utf-8")
        if report:
            (d / "report.md").write_text("итог\n", encoding="utf-8")
        self.workspaces.append({"workspace_id": ws, "label": label})
        self.write_ws()

    def run_cmd(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "bash",
                str(DISPATCH),
                *args,
                "--root",
                str(self.root),
                "--results",
                str(self.results),
            ],
            check=False,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def symbols(self) -> list[str]:
        return [s for s in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if s]

    def test_set_is_single_codepoints(self) -> None:
        syms = self.symbols()
        self.assertGreaterEqual(len(syms), 80)
        self.assertEqual(len(syms), len(set(syms)))
        for s in syms:
            self.assertEqual(len(s), 1, s)
            self.assertGreater(ord(s), 0x2000, s)

    def test_pick_avoids_occupied(self) -> None:
        syms = self.symbols()
        # Заняты все, кроме двух; один из занятых — буква прежних версий.
        keep = syms[:2]
        for i, s in enumerate(syms[2:]):
            self.task(f"t{i}", s, f"{s} t{i} · sonnet", f"w{i}")
        self.task("old", "Ⓐ", "Ⓐ old · opus", "wold")
        seen = set()
        for _ in range(30):
            r = self.run_cmd("symbol")
            self.assertEqual(r.returncode, 0, r.stderr)
            seen.add(r.stdout.strip())
        self.assertEqual(seen, set(keep))

    def test_pick_is_random(self) -> None:
        seen = {self.run_cmd("symbol").stdout.strip() for _ in range(25)}
        self.assertGreater(len(seen), 3)
        self.assertLessEqual(seen, set(self.symbols()))

    def test_exhausted(self) -> None:
        small = self.dir / "small.txt"
        small.write_text("🦊\n🐻\n", encoding="utf-8")
        self.task("a", "🦊", "🦊 a · sonnet", "w1")
        self.task("b", "🐻", "🐻 b · sonnet", "w2")
        r = self.run_cmd("symbol", DUTY_SYMBOLS_FILE=str(small))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("нет свободного символа", r.stderr)

    def test_closed_task_frees_symbol(self) -> None:
        small = self.dir / "small.txt"
        small.write_text("🦊\n", encoding="utf-8")
        d = self.results / "gone"
        d.mkdir()
        (d / "brief.md").write_text(
            brief("w9", "🦊"), encoding="utf-8"
        )  # workspace закрыт
        r = self.run_cmd("symbol", DUTY_SYMBOLS_FILE=str(small))
        self.assertEqual(r.stdout.strip(), "🦊")

    def test_list_parses_emoji_and_letter_labels(self) -> None:
        self.task("alpha", "🦊", "🦊 alpha · sonnet", "w1")
        self.task("beta", "Ⓑ", "Ⓑ beta · opus", "w2")
        self.task("gamma", "🧀", "🧀 gamma · haiku · run1", "w3")
        self.task("plain", "—", "plain · sonnet", "w4")
        r = self.run_cmd("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {
            ln.split("|")[2].strip(): ln.split("|")[1].strip()
            for ln in r.stdout.splitlines()[2:]
        }
        self.assertEqual(
            rows, {"alpha": "🦊", "beta": "Ⓑ", "gamma": "🧀", "plain": "—"}
        )

    def test_list_ignores_foreign_label(self) -> None:
        self.task("alpha", "🦊", "🐻 other · sonnet", "w1")
        r = self.run_cmd("list")
        self.assertEqual(len(r.stdout.splitlines()), 2)

    def test_close_finds_emoji_and_letter(self) -> None:
        self.task("alpha", "🦊", "🦊 alpha · sonnet", "w1", report=True)
        self.task("beta", "Ⓑ", "Ⓑ beta · opus", "w2", report=True)
        for name, ws in (("alpha", "w1"), ("beta", "w2")):
            r = self.run_cmd("close", name)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"workspace={ws}", r.stdout)
        self.assertEqual(self.run_cmd("list").stdout.count("\n"), 2)


if __name__ == "__main__":
    unittest.main()
