"""Проверки хука forbid: `python3 -I scripts/tests/test_forbid.py` или через pytest."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "forbid.py"
SPEC = importlib.util.spec_from_file_location("forbid", HOOK)
assert SPEC and SPEC.loader
forbid = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forbid)

SECRET_ENV = "~/.config/example/vm-user.env"

BASE_TOML = """schema = 1

[git]
protected = ["dev", "stage", "master"]

[secrets]
dirs = ["~/.config/example"]
"""


def make_root(toml: str) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Временный корень проекта с `.duty/duty.toml`."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".duty").mkdir()
    (root / ".duty" / "duty.toml").write_text(toml, encoding="utf-8")
    return tmp, root


def settings_for(toml: str | None) -> "forbid.Settings":
    """Settings из конфигурации временного корня; None — корня и файла нет."""
    tmp, root = make_root(toml if toml is not None else "schema = 1\n")
    saved = {k: os.environ.get(k) for k in ("DUTY_PROJECT", "DUTY_CONFIG_USER")}
    os.environ["DUTY_PROJECT"] = str(root)
    os.environ["DUTY_CONFIG_USER"] = str(root / "no-user.toml")
    try:
        return forbid.load_settings(str(root))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmp.cleanup()


CFG = settings_for(BASE_TOML)

# ключ запрета -> (отвергаемые команды, разрешённые команды)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "network": (
        [
            "nmcli connection up amn0",
            "sudo -n amn-switch",
            "awg show amn0",
            "awg show amn0 endpoints extra",
            "awg set amn0 peer x",
            "sudo -n amn-zones-update --force",
            "amn-zones-update",
            "sudo -A -p 'туннель' systemctl restart awg-quick@amn0",
            "systemctl stop awg-quick@amn0",
            "systemctl -n status restart awg-quick@amn0",
            "ip link set tun0 down",
            "echo ok && wg-quick up wg0",
            "resolvectl dns eth0 1.1.1.1",
            "resolvectl domain eth0 example.org",
            "resolvectl revert eth0",
            "resolvectl flush-caches",
            "resolvectl -i eth0 dns eth0 1.1.1.1",
            "resolvectl",
            "openvpn --config x.ovpn",
        ],
        [
            "systemctl --user status herdr-prestart",
            "sudo -n awg show amn0 endpoints",
            "awg show amn0 latest-handshakes",
            "sudo -n amn-zones-update --count",
            "systemctl status awg-quick@amn0",
            "systemctl --no-pager status awg-quick@amn0",
            "systemctl -n 50 status awg-quick@amn0",
            "systemctl -o cat --lines 5 status awg-quick@amn0",
            "resolvectl status",
            "resolvectl status eth0",
            "resolvectl statistics",
            "resolvectl query example.org",
            "resolvectl -i eth0 query example.org",
            "resolvectl --no-pager status",
            "ip addr show tun0",
            "ip link show",
            "git commit -m 'nmcli и awg в сообщении'",
            "echo awg",
            "command -v pkill",
            "command -v nmcli",
            "grep -rn amn-switch docs/",
        ],
    ),
    "kill": (
        [
            "pkill -f herdr",
            "killall python3",
            "kill -9 -1",
            "kill -KILL -1",
            "kill -9 $(pgrep -f foo)",
            "kill `pgrep foo`",
            "fuser -k 8080/tcp",
            "pgrep foo | xargs kill",
            "pgrep foo | xargs -r kill -9",
            "sudo -n pkill x",
        ],
        [
            "kill 12345",
            "kill -0 12345",
            "kill -TERM 4321",
            "kill $(cat run.pid)",
            "pgrep -f foo",
            "fuser 8080/tcp",
            "echo pkill",
            "git log --grep='kill -9 -1'",
            "command -v killall",
        ],
    ),
    "git-add": (
        [
            "git add -A",
            "git add --all",
            "git add -u",
            "git add --update",
            "git -C x add -Av",
        ],
        [
            "git add path/a path/b",
            "git add .",
            "git add -p file",
            "git commit -m 'git add -A'",
        ],
    ),
    "git-stash": (
        [
            "git stash",
            "git stash pop",
            "git stash -m wip",
            "git stash -- path",
            "git -C x stash save a",
            "git stash apply stash@{0}",
            "git stash drop",
            "git stash clear",
            "git stash branch b",
            "git stash store abc",
            "cd x && git stash push -m a",
        ],
        [
            "git status",
            "git log --oneline",
            "git commit -m 'git stash'",
            "git stash list",
            "git stash show -p stash@{0}",
            "git -C x stash list",
        ],
    ),
    "git-force": (
        [
            "git push --force origin b",
            "git push -f origin b",
            "git push origin b --force-with-lease",
            "git push origin +b",
            "git push origin +HEAD:b",
        ],
        [
            "git push origin my-branch",
            "git push origin HEAD",
            "git push -u origin forbid-hook",
            "git fetch -f origin",
        ],
    ),
    "git-protected": (
        [
            "git push origin stage",
            "git push origin HEAD:master",
            "git push origin x:refs/heads/dev",
            "git push origin :stage",
            "git push origin --delete dev",
        ],
        [
            "git push origin devel",
            "git push origin my-stage",
            "git push origin master-fix",
        ],
    ),
    "herdr": (
        [
            "herdr tab create --workspace w1",
            "herdr pane run p1 'make'",
            "herdr pane send p1 text",
            "cd x && herdr pane run p1 y",
        ],
        [
            "herdr tab close w1:t3",
            "herdr tab list",
            "herdr pane wait-output p1 --match 'tab create'",
            "herdr notification show x",
            "bash /plug/scripts/tab-run.sh x -- ssh host cmd",
            "bash /plug/scripts/tab-run.sh x --cwd d -- 'make test && echo done'",
        ],
    ),
    "secret-file": (
        [
            f"cat {SECRET_ENV}",
            "grep password ~/.config/example/x.env",
            "sed -n 1p $HOME/.config/example/jira-pat",
            'head -c 5 "$HOME/.config/example/jira-pat"',
            "base64 < ~/.config/example/x",
            f"cp {SECRET_ENV} /tmp/x",
            "echo $(cat ~/.config/example/x)",
            "cat ~/.config/example/x.env",
            "grep PASS ~/.config/example/x.env",
            "python3 -Ic \"print(open('/home/user/.config/example/x.env').read())\"",
            "sed -n p ~/.config/example/x.env",
            "sed -i 'w /dev/stdout' ~/.config/example/x.env",
            "sed -i 's/A/B/w /dev/stdout' ~/.config/example/x.env",
            "grep -e -c ~/.config/example/x.env",
            "grep -ec ~/.config/example/x.env",
            "grep -eq ~/.config/example/x.env",
            "sed -i 's/A/A/gw /dev/stdout' ~/.config/example/x.env",
            "sed -i '1w /dev/stdout' ~/.config/example/x.env",
            "sed -i 'W /dev/stdout' ~/.config/example/x.env",
            "sed -i 'w /proc/self/fd/1' ~/.config/example/x.env",
            "grep --exclude --count PASS ~/.config/example/x.env",
            "grep --include -q PASS ~/.config/example/x.env",
            "rg -g -c PASS ~/.config/example/x.env",
            "rg -t -l PASS ~/.config/example/x.env",
            "grep -ie -q ~/.config/example/x.env",
            "awk '{print}' ~/.config/example/x.env",
            "cut -d= -f2 ~/.config/example/x.env",
            "tr a-z A-Z < ~/.config/example/x.env",
            "od -c ~/.config/example/x.env",
            "tee /tmp/x < ~/.config/example/x.env",
            "python3 -c \"print(open('/home/user/.config/example/x.env').read())\"",
            "python3 -I - <<'EOF'\nimport sys\nsys.stdout.write(open('/home/user/.config/example/x.env').read())\nEOF",
        ],
        [
            "python3 -I scripts/read-env.py ~/.config/example/x.env",
            "python3 -I -c \"import os; open(os.path.expanduser('~/.config/example/x.env')).read()\"",
            "python3 -I -c 'import os; p = os.environ[\"F\"]; print(open(p).read()[:0])'",
            "python3 -I -c \"p='/home/user/.config/example/x.env'; open(p,'w').write('A=2')\"",
            "sed -i 's/^A=.*/A=1/' ~/.config/example/x.env",
            "sed -i.bak -e 's/A/B/' ~/.config/example/x.env",
            "grep -c PASS ~/.config/example/x.env",
            "grep -ql PASS ~/.config/example/x.env",
            "grep -e PASS -c ~/.config/example/x.env",
            "grep -rc PASS ~/.config/example/x.env",
            "grep --count PASS ~/.config/example/x.env",
            "grep -m1 -c PASS ~/.config/example/x.env",
            "rg -g '*.env' -c PASS ~/.config/example/x.env",
            "grep -A 2 -q PASS ~/.config/example/x.env",
            "grep -q PASS ~/.config/example/x.env && echo есть",
            "ls -l ~/.config/example/x.env",
            "cmp ~/.config/example/x.env ~/.config/example/y.env",
            "set -a; . ~/.config/example/x.env; set +a",
            "grep -rn password docs/",
            "ls ~/.config/example/",
            f". {SECRET_ENV}",
            f"source {SECRET_ENV}",
            f"set -a; . {SECRET_ENV}; set +a; echo ok",
            f"test -f {SECRET_ENV}",
            f"stat {SECRET_ENV}",
            f"chmod 600 {SECRET_ENV}",
            f"cp {SECRET_ENV} ~/.config/example/copy.env",
            "cat docs/README.md",
            "head -c 32 /dev/urandom | base64 > ~/.config/example/x.env",
            "cat > ~/.config/example/x.env <<EOF\nA=1\nEOF",
        ],
    ),
    "secret-env": (
        [
            "env",
            "printenv",
            "export -p",
            "declare -p",
            "set",
            "env | grep X",
            "printenv JIRA_PAT",
        ],
        [
            "env FOO=1 make",
            "env -u X make",
            "printenv HOME",
            "set -a",
            "set +a",
            "set -e",
            "export FOO=1",
        ],
    ),
    "secret-echo": (
        [
            "echo $VM_PASSWORD",
            'echo "token: ${JIRA_TOKEN}"',
            'printf "%s\\n" "$api_key"',
            "echo $GH_PAT",
            'print "$SECRET"',
            'echo "$KEY" | cat',
            "echo $KEYS_DIR",
            "echo $DB_PASS",
            "echo $GITHUB_TOKEN",
            "echo $AGE_PASSPHRASE",
            "echo $PGPASSWORD",
            "echo $SSHPASS",
            "echo $APITOKEN",
            "echo $dbPassword",
        ],
        [
            'printf \'%s\' "$VM_PASSWORD" | sudo -S -p "" true',
            "echo $PATH",
            "echo $HOME $VM_USER",
            "echo '$VM_PASSWORD'",
            "echo password",
            "printf '%s' \"$TOKEN\" > run/token.txt",
            "printf '%s' \"$TOKEN\" | tr -d '\\n' | curl -H @- x",
            "echo $TOKEN 2>err | wc -c",
            'echo "passed: $PASSED"',
            "echo $MONKEY",
            "echo $BYPASS_FLAG",
            "echo $TOKENIZER_MODE",
        ],
    ),
    "secret-literal": (
        [
            "curl -u admin:hunter2 https://x",
            "curl --user admin:hunter2 https://x",
            "curl -uadmin:hunter2 https://x",
            "sshpass -p hunter2 ssh host",
            "sshpass -phunter2 ssh host",
        ],
        [
            'curl -u "$LOGIN:$PASS" https://x',
            "curl -u admin https://x",
            "curl -s https://x",
            "sshpass -e ssh host",
            "sshpass -f pw.txt ssh host",
            "sshpass -e ssh -p 2222 host",
            "sshpass -e ssh -p2222 host",
            'sshpass -p "$PW" ssh host',
            'set -a; . ~/.config/example/vm-user.env; set +a; ssh "$VM_USER@$h"',
        ],
    ),
}

NEUTRAL = [
    "ls -la",
    "git add path/a path/b",
    "git commit -m 'git stash; pkill; env; cat ~/.config/example/x'",
    "cat <<'EOF'\ngit stash\npkill x\nEOF",
    "python3 -I -m py_compile hooks/forbid.py",
    "# git stash",
]


class ForbidTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp, root = make_root(BASE_TOML)
        self.addCleanup(tmp.cleanup)
        self.root_dir = str(root)

    def test_denied(self) -> None:
        for key, (denied, _) in CASES.items():
            self.assertGreaterEqual(len(denied), 2, key)
            for command in denied:
                with self.subTest(key=key, command=command):
                    found = forbid.check(command, CFG)
                    self.assertIsNotNone(found, command)
                    assert found is not None
                    self.assertTrue(found.startswith(key.split("-")[0]), (key, found))

    def test_exact_key(self) -> None:
        for key, (denied, _) in CASES.items():
            for command in denied:
                with self.subTest(key=key, command=command):
                    self.assertEqual(forbid.check(command, CFG), key)

    def test_allowed(self) -> None:
        for key, (_, allowed) in CASES.items():
            self.assertGreaterEqual(len(allowed), 2, key)
            for command in allowed:
                with self.subTest(key=key, command=command):
                    self.assertIsNone(forbid.check(command, CFG))

    def test_neutral(self) -> None:
        for command in NEUTRAL:
            with self.subTest(command=command):
                self.assertIsNone(forbid.check(command, CFG))

    def test_nested(self) -> None:
        for command in (
            "bash -c 'git stash'",
            'eval "pkill x"',
            "bash /p/scripts/tab-run.sh x -- 'git stash'",
            "bash /p/scripts/tab-run.sh x -- herdr pane run p y",
            "echo $(git stash)",
            "git \\\npush --force origin b",
            "git \\\nadd -A",
            "bash <<EOF\npkill x\nEOF",
            "bash <<< 'pkill x'",
            "bash -o pipefail -c 'pkill x'",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(forbid.check(command, CFG))

    def test_every_key_has_message(self) -> None:
        for key in CASES:
            self.assertIn(key, forbid.DENY)

    def run_hook(
        self, payload: object, role: str | None
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "DUTY_ROLE"}
        env["DUTY_PROJECT"] = self.root_dir
        env["DUTY_CONFIG_USER"] = str(Path(self.root_dir) / "no-user.toml")
        if role:
            env["DUTY_ROLE"] = role
        return subprocess.run(
            [sys.executable, "-I", str(HOOK)],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def bash(self, command: str) -> dict[str, object]:
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def test_hook_deny_without_echo(self) -> None:
        secret = "hunter2"
        done = self.run_hook(self.bash(f"sshpass -p {secret} ssh host"), "worker")
        self.assertEqual(done.returncode, 0)
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertNotIn(secret, done.stdout)
        self.assertIn("rules.md", out["permissionDecisionReason"])
        self.assertNotIn("монорепозитори", out["permissionDecisionReason"])

    def test_hook_protected_text_from_config(self) -> None:
        done = self.run_hook(self.bash("git push origin stage"), "worker")
        reason = json.loads(done.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn("dev, master, stage", reason)

    def test_hook_closes_on_broken_config(self) -> None:
        (Path(self.root_dir) / ".duty" / "local.toml").write_text(
            "[git\nprotected =", encoding="utf-8"
        )
        for command in ("git push origin stage", "cat ~/.config/example/jira-pat"):
            done = self.run_hook(self.bash(command), "worker")
            out = json.loads(done.stdout)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn(
                "конфигурация duty не читается", out["permissionDecisionReason"]
            )
            self.assertIn("local.toml", out["permissionDecisionReason"])

    def test_hook_wrong_type_keeps_defaults_and_reports_once(self) -> None:
        (Path(self.root_dir) / ".duty" / "duty.toml").write_text(
            'schema = 1\n[git]\nprotected = "stage"\n', encoding="utf-8"
        )
        done = self.run_hook(self.bash("git push origin main"), "worker")
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(done.stderr.count("git.protected"), 1)
        done = self.run_hook(self.bash("git push origin stage"), "worker")
        self.assertEqual(done.stdout, "")

    def test_hook_silent_outside_worker(self) -> None:
        for role in (None, "main"):
            done = self.run_hook(self.bash("git stash"), role)
            self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_hook_allows(self) -> None:
        done = self.run_hook(self.bash("git add a.py"), "worker")
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        done = self.run_hook({"tool_name": "Read", "tool_input": {}}, "worker")
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_hook_survives_garbage(self) -> None:
        for payload in (
            "not json",
            "[]",
            {"tool_name": "Bash", "tool_input": {"command": 5}},
        ):
            done = self.run_hook(payload, "worker")
            self.assertEqual((done.returncode, done.stdout), (0, ""))


class RmResultsTest(unittest.TestCase):
    """Ручной `rm -r` в paths.results при живом каталоге (live_reason) отвергается."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.results = base / "results"
        self.live = self.results / "live-task"
        self.idle = self.results / "idle-run"
        self.live.mkdir(parents=True)
        self.idle.mkdir()
        (self.live / "brief.md").write_text("# Задача\n", encoding="utf-8")
        (self.live / "state.md").write_text("Шаг: 2 — идёт\n", encoding="utf-8")
        self.outside = base / "elsewhere"
        self.outside.mkdir()
        self.cfg = forbid.Settings(results=str(self.results), cwd=str(base))

    def check(self, command: str) -> str | None:
        return forbid.check(command, self.cfg)

    def test_denied_while_live(self) -> None:
        for flags in ("-r", "-rf", "-fr", "-R", "-Rf", "-f -r", "--recursive", "-vrf"):
            with self.subTest(flags=flags):
                found = self.check(f"rm {flags} {self.live}")
                self.assertIsNotNone(found)
                assert found is not None
                self.assertTrue(found.startswith("rm-results|"), found)

    def test_denied_inside_live_task_and_by_relative_path(self) -> None:
        self.assertIsNotNone(self.check(f"rm -rf {self.live}/wt/x"))
        self.cfg.cwd = str(self.results)
        self.assertIsNotNone(self.check("rm -rf live-task"))
        self.assertIsNotNone(self.check("rm -rf live-*"))
        self.assertIsNotNone(self.check(f"sudo rm -rf {self.results}"))
        self.assertIsNotNone(self.check(f"bash -c 'rm -rf {self.live}'"))

    def test_cd_in_command_changes_base(self) -> None:
        base = self.results.parent
        self.assertIsNotNone(self.check("cd results && rm -rf live-task"))
        self.assertIsNotNone(self.check(f"pushd {self.results}; rm -rf live-task"))
        self.assertIsNotNone(
            self.check("cd results; cd live-task; rm -rf ../live-task")
        )
        self.assertIsNone(self.check("cd results && cd .. && rm -rf live-task"))
        self.assertIsNone(self.check(f"cd {self.outside} && rm -rf live-task"))
        self.assertIsNone(self.check("cd /tmp && rm -rf x"))
        self.assertIsNone(self.check("cd; rm -rf x"))
        self.assertIsNone(self.check(f"cd {base} && rm -rf elsewhere"))

    def test_cd_unknown_denies_relative_while_live(self) -> None:
        for command in (
            "cd $TARGET && rm -rf x",
            "cd $(pwd) && rm -rf x",
            "cd - && rm -rf x",
            "cd results; popd; rm -rf x",
            "pushd; rm -rf x",
        ):
            with self.subTest(command=command):
                found = self.check(command)
                assert found is not None
                self.assertTrue(found.startswith("rm-results|"), found)
        self.assertIsNone(self.check("cd $TARGET && rm -rf /tmp/duty-nonexistent-dir"))
        (self.live / "state.md").write_text("Шаг: завершено\n", encoding="utf-8")
        self.assertIsNone(self.check("cd $TARGET && rm -rf x"))

    def test_text_names_busy_and_cleanup(self) -> None:
        found = self.check(f"rm -rf {self.live}")
        assert found is not None
        text = forbid.deny_text(found, self.cfg)
        self.assertIn("live-task", text)
        self.assertIn("/duty:cleanup", text)

    def test_allowed_without_live(self) -> None:
        self.assertIsNone(self.check(f"rm -rf {self.idle}"))
        (self.live / "state.md").write_text("Шаг: завершено\n", encoding="utf-8")
        self.assertIsNone(self.check(f"rm -rf {self.live}"))

    def test_allowed_non_recursive_and_outside(self) -> None:
        self.assertIsNone(self.check(f"rm {self.live}/brief.md"))
        self.assertIsNone(self.check(f"rm -f {self.live}/state.md"))
        self.assertIsNone(self.check(f"rm -rf {self.outside}"))
        self.assertIsNone(self.check("rm -rf /tmp/duty-nonexistent-dir"))

    def test_off_without_results(self) -> None:
        self.cfg.results = ""
        self.assertIsNone(self.check(f"rm -rf {self.live}"))

    def test_find_delete_and_exec_rm_denied_while_live(self) -> None:
        for command in (
            f"find {self.live} -delete",
            f"find {self.live} -name '*.md' -delete",
            f"find {self.results} -mindepth 1 -delete",
            f"find {self.live} -type f -exec rm -f {{}} +",
            f"find {self.live} -type d -execdir rm -rf {{}} \\;",
            f"sudo find {self.live} -delete",
            f"bash -c 'find {self.live} -delete'",
        ):
            with self.subTest(command=command):
                found = self.check(command)
                assert found is not None
                self.assertTrue(found.startswith("rm-results|"), found)
        self.cfg.cwd = str(self.live)
        self.assertIsNotNone(self.check("find . -delete"))
        self.assertIsNotNone(self.check("find -delete"))

    def test_find_allowed_without_delete_or_outside_or_idle(self) -> None:
        self.assertIsNone(self.check(f"find {self.live} -name '*.md'"))
        self.assertIsNone(self.check(f"find {self.live} -exec cat {{}} +"))
        self.assertIsNone(self.check(f"find {self.outside} -delete"))
        self.assertIsNone(self.check(f"find {self.idle} -delete"))
        (self.live / "state.md").write_text("Шаг: завершено\n", encoding="utf-8")
        self.assertIsNone(self.check(f"find {self.live} -delete"))

    def test_xargs_rm_recursive_denied_while_live(self) -> None:
        for command in (
            f"find {self.idle} -name x | xargs rm -rf",
            f"ls {self.live} | xargs -I{{}} rm -r {{}}",
            "ls | xargs -0 -n 1 rm -rf --",
            "ls | xargs /bin/rm -R",
        ):
            with self.subTest(command=command):
                found = self.check(command)
                assert found is not None
                self.assertTrue(found.startswith("rm-results|"), found)

    def test_xargs_rm_allowed_when_not_recursive_or_idle(self) -> None:
        self.assertIsNone(self.check("ls | xargs rm -f"))
        self.assertIsNone(self.check("ls | xargs echo rm -rf"))
        (self.live / "state.md").write_text("Шаг: завершено\n", encoding="utf-8")
        self.assertIsNone(self.check("ls | xargs rm -rf"))

    def test_open_paths_only_for_targets_inside_results(self) -> None:
        lib = forbid._dutylib()
        calls: list[int] = []
        real = lib.open_paths

        def counting() -> object:
            calls.append(1)
            return real()

        lib.open_paths = counting
        self.addCleanup(setattr, lib, "open_paths", real)
        self.assertIsNone(self.check(f"rm -rf {self.outside}"))
        self.assertIsNone(self.check("rm -rf /tmp/duty-nonexistent-dir"))
        self.assertIsNone(self.check(f"find {self.outside} -delete"))
        self.assertEqual(calls, [])
        self.assertIsNotNone(self.check(f"rm -rf {self.live}"))
        self.assertEqual(calls, [1])


class ConfigTest(unittest.TestCase):
    def test_defaults_without_config(self) -> None:
        cfg = settings_for(None)
        self.assertEqual(forbid.check("git push origin main", cfg), "git-protected")
        self.assertIsNone(forbid.check("git push origin stage", cfg))
        self.assertIsNone(forbid.check("cat ~/.config/example/x.env", cfg))
        # остальные secret-* действуют без каталогов
        self.assertEqual(forbid.check("printenv", cfg), "secret-env")

    def test_add_all_and_stash_switches(self) -> None:
        cfg = settings_for(
            "schema = 1\n[git]\nforbid_add_all = false\nforbid_stash = false\n"
        )
        self.assertIsNone(forbid.check("git add -A", cfg))
        self.assertIsNone(forbid.check("git stash", cfg))
        self.assertEqual(forbid.check("git add -A", CFG), "git-add")

    def test_network_and_herdr_switches(self) -> None:
        cfg = settings_for(
            "schema = 1\n[forbid]\nnetwork = false\nherdr_direct = false\n"
        )
        self.assertIsNone(forbid.check("nmcli connection up x", cfg))
        self.assertIsNone(forbid.check("herdr pane run p x", cfg))
        self.assertEqual(forbid.check("nmcli connection up x", CFG), "network")

    def test_project_rule(self) -> None:
        cfg = settings_for(
            'schema = 1\n[[forbid.rules]]\nwords = ["instances-down", "all"]\n'
            'reason = "снимать только своими номерами"\n'
        )
        found = forbid.check("just dev-stand::instances-down all", cfg)
        self.assertEqual(found, "rule-0")
        assert found
        text = forbid.deny_text(found, cfg)
        self.assertIn("снимать только своими номерами", text)
        self.assertIn("[[forbid.rules]]", text)
        self.assertIsNone(forbid.check("just dev-stand::instances-down 3", cfg))
        self.assertEqual(forbid.check("bash -c 'instances-down all'", cfg), "rule-0")

    def test_second_secret_dir(self) -> None:
        cfg = settings_for(
            'schema = 1\n[secrets]\ndirs = ["~/.config/example", "/etc/secrets"]\n'
        )
        self.assertEqual(forbid.check("cat /etc/secrets/db.env", cfg), "secret-file")
        self.assertEqual(forbid.check("cat ~/.config/example/x", cfg), "secret-file")
        self.assertIsNone(forbid.check("cat /var/etc/secretsx/db.env", cfg))
        self.assertIsNone(forbid.check("grep -c x /etc/secrets/db.env", cfg))

    def test_home_dir_forms(self) -> None:
        cfg = settings_for('schema = 1\n[secrets]\ndirs = ["~/vault"]\n')
        for path in (
            "~/vault/a",
            "$HOME/vault/a",
            "${HOME}/vault/a",
            os.path.expanduser("~/vault/a"),
        ):
            with self.subTest(path=path):
                self.assertEqual(forbid.check(f"cat {path}", cfg), "secret-file")

    def test_broken_config_is_unreadable(self) -> None:
        tmp, root = make_root("schema = [broken")
        self.addCleanup(tmp.cleanup)
        saved = os.environ.get("DUTY_PROJECT")
        os.environ["DUTY_PROJECT"] = str(root)
        try:
            with self.assertRaises(forbid.ConfigUnreadable):
                forbid.load_settings(str(root))
        finally:
            if saved is None:
                os.environ.pop("DUTY_PROJECT", None)
            else:
                os.environ["DUTY_PROJECT"] = saved


if __name__ == "__main__":
    unittest.main()
