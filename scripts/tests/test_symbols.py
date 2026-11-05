"""Символы workspace: выбор эмодзи из конфигурации, разбор подписей, служебный блок, list и close на двойнике herdr."""

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
# create отвечает workspace w9 и панелью p9, агент панели p9 — claude; аргументы
# каждого вызова пишутся строкой в herdr.log.
FAKE_HERDR = r"""#!/usr/bin/env bash
printf '%s\t' "$@" >> "$FAKE_DIR/herdr.log"
printf '\n' >> "$FAKE_DIR/herdr.log"
case "$1 $2" in
    "--version "*) echo "herdr 0.9.5 ${FAKE_HERDR_BUILD-(duty-v0.9.5.1 4a69c66)}" ;;
    "workspace create")
        while [ $# -gt 0 ]; do [ "$1" = --label ] && label=$2; shift; done
        jq --arg l "$label" '.result.workspaces += [{workspace_id: "w9", label: $l}]' "$FAKE_DIR/ws.json" > "$FAKE_DIR/ws.tmp"
        mv "$FAKE_DIR/ws.tmp" "$FAKE_DIR/ws.json"
        echo '{"result":{"workspace":{"workspace_id":"w9"},"root_pane":{"pane_id":"p9"}}}' ;;
    "agent get")
        if [ "$3" = p9 ]; then
            echo '{"result":{"agent":{"agent":"claude","agent_status":"working","tab_id":"t1"}}}'
        else
            exit 1
        fi ;;
    "workspace list") cat "$FAKE_DIR/ws.json" ;;
    "workspace close")
        jq --arg w "$3" '.result.workspaces |= map(select(.workspace_id != $w))' "$FAKE_DIR/ws.json" > "$FAKE_DIR/ws.tmp"
        mv "$FAKE_DIR/ws.tmp" "$FAKE_DIR/ws.json" ;;
    "tab list") echo '{"result":{"tabs":[{"tab_id":"t1"}]}}' ;;
    "agent list") echo '{"result":{"agents":[]}}' ;;
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
        claude = bin_dir / "claude"
        claude.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        claude.chmod(0o755)
        # Пользовательский слой изолирован: файла нет, если тест его не создаёт.
        self.user_cfg = self.dir / "user" / "config.toml"
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("DUTY_")},
            "DUTY_CONFIG_USER": str(self.user_cfg),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERDR_ENV": "1",
            "FAKE_DIR": str(self.dir),
            "DUTY_BUSY_DIR": str(self.dir / "busy"),
            "DUTY_SIGNALS_DIR": str(self.dir / "signals"),
        }
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        self.workspaces: list[dict] = []
        self.write_ws()

    def project_config(self, symbols: list[str]) -> None:
        """Конфигурация проекта: набор symbols.tasks и каталог результатов."""
        duty = self.root / ".duty"
        duty.mkdir(exist_ok=True)
        tasks = ", ".join(f'"{s}"' for s in symbols)
        (duty / "duty.toml").write_text(
            f'schema = 1\n\n[paths]\nresults = "{self.results}"\n\n'
            f"[symbols]\ntasks = [{tasks}]\n",
            encoding="utf-8",
        )

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
        syms = self.symbols()[:6]
        self.project_config(syms)
        # Заняты четыре из шести; ещё одна задача — с буквой прежних версий.
        keep = syms[:2]
        for i, s in enumerate(syms[2:]):
            self.task(f"t{i}", s, f"{s} t{i} · sonnet", f"w{i}")
        self.task("old", "Ⓐ", "Ⓐ old · opus", "wold")
        seen = set()
        for _ in range(6):
            r = self.run_cmd("symbol")
            self.assertEqual(r.returncode, 0, r.stderr)
            seen.add(r.stdout.strip())
        self.assertTrue(seen)
        self.assertLessEqual(seen, set(keep))

    def test_pick_is_random(self) -> None:
        syms = self.symbols()[:6]
        self.project_config(syms)
        seen = {self.run_cmd("symbol").stdout.strip() for _ in range(14)}
        self.assertGreater(len(seen), 2)
        self.assertLessEqual(seen, set(syms))

    def test_fallback_set_without_config(self) -> None:
        r = self.run_cmd("symbol")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(r.stdout.strip(), self.symbols())

    def test_outside_repo_uses_user_outside_set(self) -> None:
        outside = self.dir / "plain"
        outside.mkdir()
        self.user_cfg.parent.mkdir()
        self.user_cfg.write_text('[symbols]\noutside = ["🦊"]\n', encoding="utf-8")
        env = {**self.env, "DUTY_RESULTS_DIR": str(self.results)}
        for expected, setup in (("🦊", True), (None, False)):
            if not setup:
                self.user_cfg.unlink()
            r = subprocess.run(
                ["bash", str(DISPATCH), "symbol"],
                check=False,
                env=env,
                cwd=outside,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            if expected:
                self.assertEqual(r.stdout.strip(), expected)
            else:
                self.assertIn(r.stdout.strip(), self.symbols())

    def test_start_writes_project_and_config_lines(self) -> None:
        self.project_config(["🦊"])
        duty = self.root / ".duty"
        (duty / "worker-rules.md").write_text("правило проекта\n", encoding="utf-8")
        self.user_cfg.parent.mkdir()
        (self.user_cfg.parent / "worker-rules.md").write_text(
            "правило пользователя\n", encoding="utf-8"
        )
        r = self.run_cmd(
            "tsk",
            "sonnet",
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "падение — отчёт",
            "--read",
            "`CLAUDE.md`",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        task = self.results / "tsk"
        block = (task / "brief.md").read_text(encoding="utf-8")
        self.assertIn(f"- проект: `{self.root.resolve()}`", block)
        self.assertIn(f"- конфигурация: `{self.root.resolve()}/.duty/duty.toml`", block)
        self.assertIn("- символ workspace: `🦊`", block)
        rules = (task / "rules.md").read_text(encoding="utf-8")
        self.assertIn("## Правила пользователя\n\nправило пользователя", rules)
        self.assertIn("## Правила проекта\n\nправило проекта", rules)
        log = (self.dir / "herdr.log").read_text(encoding="utf-8")
        self.assertIn(f"DUTY_PROJECT={self.root.resolve()}", log)

    def start_task(self, **env: str) -> subprocess.CompletedProcess:
        return self.run_cmd(
            "tsk",
            "sonnet",
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "падение — отчёт",
            "--read",
            "`CLAUDE.md`",
            **env,
        )

    def herdr_log(self) -> str:
        return (self.dir / "herdr.log").read_text(encoding="utf-8")

    def test_start_reports_color_token(self) -> None:
        self.project_config(["🦊"])
        with (self.root / ".duty" / "duty.toml").open("a", encoding="utf-8") as f:
            f.write('color = "#a6e3a1"\n')
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            "workspace\treport-metadata\tw9\t--source\tduty\t--token\tcolor=#a6e3a1\t",
            self.herdr_log(),
        )

    def test_start_without_color_sends_no_token(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("workspace\treport-metadata", self.herdr_log())
        self.assertNotIn("color=", self.herdr_log())

    def test_start_color_skipped_on_upstream_herdr(self) -> None:
        self.project_config(["🦊"])
        with (self.root / ".duty" / "duty.toml").open("a", encoding="utf-8") as f:
            f.write('color = "#a6e3a1"\n')
        r = self.start_task(FAKE_HERDR_BUILD="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("не форк duty-v*", r.stderr)
        self.assertNotIn("workspace\treport-metadata", self.herdr_log())

    def test_outside_repo_color_from_user_layer(self) -> None:
        outside = self.dir / "plain"
        outside.mkdir()
        self.user_cfg.parent.mkdir()
        self.user_cfg.write_text(
            '[symbols]\noutside = ["🦊"]\noutside_color = "#f5c2e7"\n', encoding="utf-8"
        )
        r = subprocess.run(
            [
                "bash",
                str(DISPATCH),
                "tsk",
                "sonnet",
                "--goal",
                "Сделать",
                "--done",
                "`true` зелёный",
                "--stop",
                "падение — отчёт",
                "--read",
                "`CLAUDE.md`",
            ],
            check=False,
            env={**self.env, "DUTY_RESULTS_DIR": str(self.results)},
            cwd=outside,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--token\tcolor=#f5c2e7", self.herdr_log())

    def test_start_general_mode_line(self) -> None:
        r = self.run_cmd(
            "tsk",
            "sonnet",
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "падение — отчёт",
            "--read",
            "`CLAUDE.md`",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        block = (self.results / "tsk" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("- конфигурация: общий режим", block)
        rules = (self.results / "tsk" / "rules.md").read_text(encoding="utf-8")
        self.assertIn("## Правила проекта\n\n—", rules)

    def test_start_refused_on_bad_config(self) -> None:
        self.project_config(["🦊"])
        with (self.root / ".duty" / "duty.toml").open("a", encoding="utf-8") as f:
            f.write("\n[nonsense]\nx = 1\n")
        r = self.run_cmd(
            "tsk",
            "sonnet",
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "падение — отчёт",
            "--read",
            "`CLAUDE.md`",
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("не проходит проверку", r.stderr)
        self.assertFalse((self.results / "tsk").exists())

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
