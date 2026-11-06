"""Символы workspace: выбор эмодзи из конфигурации, разбор подписей, служебный блок, list и close на двойнике herdr."""

from __future__ import annotations

import colorsys
import json
import os
import re
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
        case " $* " in *" --parent "*)
            if [ -n "${FAKE_HERDR_NO_PARENT:-}" ]; then
                echo "error: unexpected argument '--parent' found" >&2
                exit 2
            fi
            if [ -n "${FAKE_HERDR_PARENT_NESTED:-}" ]; then
                echo '{"error":{"code":"workspace_parent_nested","message":"вложенность один уровень"}}'
                exit 1
            fi ;;
        esac
        while [ $# -gt 0 ]; do [ "$1" = --label ] && label=$2; shift; done
        id=w$((9 + $(jq '.result.workspaces | length' "$FAKE_DIR/ws.json")))
        jq --arg l "$label" --arg i "$id" '.result.workspaces += [{workspace_id: $i, label: $l}]' "$FAKE_DIR/ws.json" > "$FAKE_DIR/ws.tmp"
        mv "$FAKE_DIR/ws.tmp" "$FAKE_DIR/ws.json"
        [ -z "${FAKE_HERDR_CREATE_WARN:-}" ] || echo "warning: something" >&2
        echo '{"result":{"workspace":{"workspace_id":"'"$id"'"},"root_pane":{"pane_id":"p9"}}}' ;;
    "agent get")
        if [ "$3" = p9 ]; then
            echo '{"result":{"agent":{"agent":"claude","agent_status":"working","tab_id":"t1"}}}'
        else
            exit 1
        fi ;;
    "workspace report-metadata")
        if [ -n "${FAKE_HERDR_NO_COLOR:-}" ]; then
            echo "error: unexpected argument '--token' found" >&2
            exit 2
        fi
        if [ -n "${FAKE_HERDR_COLOR_ERROR:-}" ]; then
            echo '{"error":{"code":"workspace_not_found","message":"workspace w9 не найден"}}'
            exit 1
        fi
        exit 0 ;;
    "pane wait-output")
        [ -z "${FAKE_HERDR_WAIT_FAIL:-}" ] || exit 1
        exit 0 ;;
    "pane read")
        n=$(cat "$FAKE_DIR/reads" 2> /dev/null || echo 0)
        echo $((n + 1)) > "$FAKE_DIR/reads"
        if [ -n "${FAKE_HERDR_STUCK:-}" ]; then
            if grep -q 'send-keys.*ctrl+j' "$FAKE_DIR/herdr.log"; then
                printf 'x\n─\n$ claude --agent duty:worker\nПрочитай brief.md и выполняй.\n─\n> \n─\n'
            else
                printf '$ claude --agent duty:worker --model sonnet\n'
            fi
        fi ;;
    "agent start")
        if [ -n "${FAKE_HERDR_STUCK:-}" ]; then
            echo '{"error":{"code":"timeout"}}'
            exit 1
        fi ;;
    "agent read") exit 0 ;;
    "workspace list") cat "$FAKE_DIR/ws.json" ;;
    "workspace get") [ ! -r "$FAKE_DIR/get.json" ] || cat "$FAKE_DIR/get.json" ;;
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
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith("DUTY_") and k != "HERDR_WORKSPACE_ID"
            },
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
            "--recall",
            "не нужна — проба",
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

    @staticmethod
    def in_rule(color: str) -> bool:
        """Цвет в диапазонах правила: S 50–70 %, V 70–100 %, оттенок кратен 10°."""
        r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
        hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
        # запас на округление до 8 бит
        return (
            0.49 <= sat <= 0.71
            and 0.69 <= val <= 1.001
            and abs(hue * 36 - round(hue * 36)) < 0.15
        )

    @staticmethod
    def hue_slot(color: str) -> int:
        r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
        return round(colorsys.rgb_to_hsv(r, g, b)[0] * 36) % 36

    def task_color_line(self, name: str) -> str:
        block = (self.results / name / "brief.md").read_text(encoding="utf-8")
        lines = [x for x in block.splitlines() if x.startswith("- цвет workspace: ")]
        self.assertEqual(len(lines), 1, block)
        return lines[0].split("`")[1]

    def start_task(self, name: str = "tsk", **env: str) -> subprocess.CompletedProcess:
        return self.run_cmd(
            name,
            "sonnet",
            "--goal",
            "Сделать",
            "--done",
            "`true` зелёный",
            "--stop",
            "падение — отчёт",
            "--read",
            "`CLAUDE.md`",
            "--recall",
            "не нужна — проба",
            **env,
        )

    def herdr_log(self) -> str:
        return (self.dir / "herdr.log").read_text(encoding="utf-8")

    def color_tokens(self) -> list[str]:
        return re.findall(r"--token\tcolor=(#[0-9a-f]{6})\t", self.herdr_log())

    def test_start_without_herdr_color_sends_rule_color(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        color = self.task_color_line("tsk")
        self.assertTrue(self.in_rule(color), color)
        self.assertIn(
            f"workspace\treport-metadata\tw9\t--source\tduty\t--token\tsymbol=🦊\t--token\tcolor={color}\t",
            self.herdr_log(),
        )
        self.assertNotIn("report-metadata\t--help", self.herdr_log())

    def test_start_color_skipped_on_upstream_herdr(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(FAKE_HERDR_NO_COLOR="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("report-metadata не поддерживает --token", r.stderr)
        self.assertNotIn("report-metadata\t--help", self.herdr_log())

    def test_start_color_server_error_warns_with_message(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(FAKE_HERDR_COLOR_ERROR="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("не выставлены: workspace w9 не найден", r.stderr)
        self.assertNotIn("report-metadata\t--help", self.herdr_log())

    def herdr_get(self, symbol: str | None, color: str | None) -> None:
        """Ответ workspace get двойника: токены herdr (None — токена нет)."""
        tokens = {k: v for k, v in (("symbol", symbol), ("color", color)) if v}
        ws: dict = {"workspace_id": "w9"}
        if tokens:
            ws["tokens"] = tokens
        (self.dir / "get.json").write_text(
            json.dumps({"result": {"workspace": ws}}), encoding="utf-8"
        )

    def brief_symbol(self, name: str) -> str:
        block = (self.results / name / "brief.md").read_text(encoding="utf-8")
        lines = [x for x in block.splitlines() if x.startswith("- символ workspace: ")]
        self.assertEqual(len(lines), 1, block)
        return lines[0].split("`")[1]

    def test_own_set_sends_symbol_only_when_herdr_gave_color(self) -> None:
        self.project_config(["🦊"])
        self.herdr_get("🦄", "#f38ba8")  # символ herdr при своём наборе не берётся
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            "workspace\treport-metadata\tw9\t--source\tduty\t--token\tsymbol=🦊\t",
            self.herdr_log(),
        )
        self.assertNotIn("color=", self.herdr_log())
        self.assertEqual(self.task_color_line("tsk"), "#f38ba8")
        self.assertEqual(self.brief_symbol("tsk"), "🦊")
        self.assertIn("DUTY_SYMBOL=🦊", self.herdr_log())
        self.assertNotIn("workspace\trename\t", self.herdr_log())

    def test_own_set_without_herdr_color_sends_both_in_one_call(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        color = self.task_color_line("tsk")
        calls = [
            x
            for x in self.herdr_log().splitlines()
            if x.startswith("workspace\treport-metadata")
        ]
        self.assertEqual(len(calls), 1, calls)
        self.assertIn(f"--token\tsymbol=🦊\t--token\tcolor={color}\t", calls[0])

    def test_color_from_herdr_is_not_sent(self) -> None:
        self.project_config(["🦊"])
        self.herdr_get("🦄", "#f38ba8")
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task_color_line("tsk"), "#f38ba8")
        self.assertIn("--token\tsymbol=🦊\t", self.herdr_log())
        self.assertNotIn("color=", self.herdr_log())

    def test_no_set_takes_symbol_and_color_from_herdr(self) -> None:
        self.herdr_get("🦄", "#f38ba8")
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.brief_symbol("tsk"), "🦄")
        self.assertEqual(self.task_color_line("tsk"), "#f38ba8")
        log = self.herdr_log()
        self.assertIn("workspace\trename\tw9\t🦄 tsk · sonnet\t", log)
        self.assertNotIn("--token", log)  # цвет и символ от herdr, токены не шлются
        self.assertNotIn("DUTY_SYMBOL=", log)

    def test_no_set_no_tokens_uses_fallback(self) -> None:
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.brief_symbol("tsk"), self.symbols())
        self.assertNotIn("symbol=", self.herdr_log())
        color = self.task_color_line("tsk")
        self.assertTrue(self.in_rule(color), color)
        self.assertEqual(self.color_tokens(), [color])

    def test_no_set_herdr_without_symbol_uses_fallback_symbol_and_herdr_color(
        self,
    ) -> None:
        self.herdr_get(None, "#f38ba8")
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.brief_symbol("tsk"), self.symbols())
        self.assertEqual(self.task_color_line("tsk"), "#f38ba8")
        self.assertEqual(self.color_tokens(), [])

    def test_tokens_of_open_workspaces_are_occupied(self) -> None:
        self.project_config(["🦊", "🦄"])
        self.workspaces.append(
            {
                "workspace_id": "w2",
                "label": "x",
                "tokens": {"symbol": "🦊", "color": "#a6e3a1"},
            }
        )
        self.write_ws()
        for _ in range(10):
            r = self.run_cmd("symbol")
            self.assertEqual(r.stdout.strip(), "🦄", r.stdout + r.stderr)

    def test_rule_color_avoids_hues_of_open_workspaces(self) -> None:
        self.project_config(["🦊"])
        # заняты все оттенки, кроме 120° (слот 12): белый корень оттенка не занимает
        busy = []
        for slot in range(36):
            if slot == 12:
                continue
            r, g, b = colorsys.hsv_to_rgb(slot / 36, 0.6, 0.85)
            busy.append(
                f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
            )
        self.workspaces.append(
            {"workspace_id": "w1", "label": "root", "tokens": {"color": "#ffffff"}}
        )
        for i, c in enumerate(busy):
            self.workspaces.append(
                {"workspace_id": f"x{i}", "label": "x", "tokens": {"color": c}}
            )
        self.write_ws()
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        color = self.task_color_line("tsk")
        self.assertTrue(self.in_rule(color), color)
        self.assertEqual(self.hue_slot(color), 12, color)

    def create_calls(self) -> list[str]:
        return [
            x
            for x in self.herdr_log().splitlines()
            if x.startswith("workspace\tcreate\t")
        ]

    def test_start_passes_parent(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(HERDR_WORKSPACE_ID="w1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.create_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--parent\tw1\t", calls[0])
        self.assertNotIn("workspace без родителя", r.stderr)

    def test_start_parent_retry_on_old_herdr(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(HERDR_WORKSPACE_ID="w1", FAKE_HERDR_NO_PARENT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.create_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("--parent\tw1\t", calls[0])
        self.assertNotIn("--parent", calls[1])
        self.assertIn("workspace без родителя", r.stderr)
        self.assertIn("unexpected argument", r.stderr)

    def test_start_parent_retry_on_nested(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(HERDR_WORKSPACE_ID="w1", FAKE_HERDR_PARENT_NESTED="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.create_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertNotIn("--parent", calls[1])
        self.assertIn("workspace_parent_nested", r.stderr)

    def test_start_create_warning_before_json(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(FAKE_HERDR_CREATE_WARN="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = (self.dir / "herdr.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"\bw\d+\t")

    def test_start_worker_role_has_no_parent(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(HERDR_WORKSPACE_ID="w1", DUTY_ROLE="worker")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.create_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertNotIn("--parent", calls[0])
        self.assertIn("вложенность один уровень", r.stderr)

    def test_start_without_workspace_id_warns(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.create_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertNotIn("--parent", calls[0])
        self.assertIn("HERDR_WORKSPACE_ID не задан", r.stderr)

    def test_start_waits_for_shell_prompt_before_agent_start(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.herdr_log()
        self.assertIn("pane\twait-output\tp9\t", log)
        self.assertIn("--regex\t", log)
        self.assertLess(log.index("pane\twait-output"), log.index("agent\tstart"))

    def test_start_continues_when_wait_output_fails(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(FAKE_HERDR_WAIT_FAIL="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("приглашение оболочки", r.stderr)

    def test_start_retypes_command_stuck_in_shell(self) -> None:
        self.project_config(["🦊"])
        r = self.start_task(FAKE_HERDR_STUCK="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.herdr_log()
        self.assertIn("застряла", r.stderr)
        keys = "pane\tsend-keys\tp9\tesc\tg\tg\td\tG\ti\t"
        text = [x for x in log.splitlines() if x.startswith("pane\tsend-text\tp9\t")]
        self.assertEqual(len(text), 1, log)
        self.assertIn("--agent duty:worker", text[0])
        self.assertIn("pane\tsend-keys\tp9\tctrl+j\t", log)
        self.assertLess(log.index(keys), log.index("send-text"))
        self.assertLess(log.index("send-text"), log.index("ctrl+j"))

    def test_two_tasks_get_different_hues(self) -> None:
        self.project_config(["🦊", "🐺"])
        for name in ("one", "two"):
            r = self.start_task(name)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        one, two = self.task_color_line("one"), self.task_color_line("two")
        self.assertTrue(self.in_rule(one) and self.in_rule(two), (one, two))
        self.assertNotEqual(self.hue_slot(one), self.hue_slot(two))

    def test_removed_color_keys_are_ignored(self) -> None:
        # старая палитра проекта больше не читается: цвет считается по правилу
        self.project_config(["🦊"])
        with (self.root / ".duty" / "duty.toml").open("a", encoding="utf-8") as f:
            f.write('colors = ["#a6e3a1"]\n')
        r = self.start_task()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        color = self.task_color_line("tsk")
        self.assertTrue(self.in_rule(color), color)

    def test_color_line_does_not_break_list_and_close(self) -> None:
        # Блок со строкой цвета, как его пишет cmd_start; агент завершён, отчёт есть.
        self.task("tsk", "🦊", "🦊 tsk · sonnet", "w1", report=True)
        brief_path = self.results / "tsk" / "brief.md"
        with brief_path.open("a", encoding="utf-8") as f:
            f.write("- цвет workspace: `#a6e3a1`\n- проект: вне репозитория\n")
        r = self.run_cmd("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tsk", r.stdout)
        self.assertIn("🦊", r.stdout)
        r = self.run_cmd("close", "tsk")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("workspace=w1", r.stdout)

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
            "--recall",
            "не нужна — проба",
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
            "--recall",
            "не нужна — проба",
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


class TaskSymbolTest(unittest.TestCase):
    """scripts/task-symbol.sh: DUTY_SYMBOL, иначе символ из последнего служебного блока brief.md."""

    SCRIPT = PLUGIN / "scripts" / "task-symbol.sh"

    def run_script(self, brief: str | None, **env: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            e = {k: v for k, v in os.environ.items() if not k.startswith("DUTY_")}
            if brief is not None:
                (Path(d) / "brief.md").write_text(brief, encoding="utf-8")
                e["DUTY_TASK"] = d
            e.update(env)
            r = subprocess.run(
                ["bash", str(self.SCRIPT)],
                env=e,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_env_symbol_wins(self) -> None:
        self.assertEqual(
            self.run_script("- символ workspace: `🦄`\n", DUTY_SYMBOL="🦊"), "🦊"
        )

    def test_reads_symbol_given_by_herdr_from_brief(self) -> None:
        self.assertEqual(
            self.run_script("# t\n\n- символ workspace: `🦄`\n- цвет: `#fff`\n"), "🦄"
        )

    def test_last_service_block_wins(self) -> None:
        brief = "- символ workspace: `🦄`\n\n- символ workspace: `🐟`\n"
        self.assertEqual(self.run_script(brief), "🐟")

    def test_no_brief_prints_empty(self) -> None:
        self.assertEqual(self.run_script(None), "")
