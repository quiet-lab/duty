"""Проверки хука forbid: `python3 -I scripts/tests/test_forbid.py` или через pytest."""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "forbid.py"
SPEC = importlib.util.spec_from_file_location("forbid", HOOK)
assert SPEC and SPEC.loader
forbid = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forbid)

AXATA = "~/.config/axata/mne-vm-user.env"

# ключ запрета -> (отвергаемые команды, разрешённые команды)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "network": (
        [
            "nmcli connection up amn0",
            "sudo -n amn-switch",
            "awg show amn0 endpoints",
            "sudo -A -p 'туннель' systemctl restart awg-quick@amn0",
            "ip link set tun0 down",
            "echo ok && wg-quick up wg0",
            "resolvectl dns eth0 1.1.1.1",
            "openvpn --config x.ovpn",
        ],
        [
            "systemctl --user status herdr-prestart",
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
        ["git stash", "git stash pop", "git stash list", "cd x && git stash push -m a"],
        ["git status", "git log --oneline", "git commit -m 'git stash'"],
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
            "git push origin dev2",
            "git push origin HEAD:master",
            "git push origin x:refs/heads/dev",
            "git push origin :dev2",
            "git push origin --delete dev",
        ],
        [
            "git push origin devel",
            "git push origin my-dev2",
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
            f"cat {AXATA}",
            "grep password ~/.config/axata/x.env",
            "sed -n 1p $HOME/.config/axata/jira-pat",
            'head -c 5 "$HOME/.config/axata/jira-pat"',
            "base64 < ~/.config/axata/x",
            f"cp {AXATA} /tmp/x",
            "echo $(cat ~/.config/axata/x)",
            "cat ~/.config/axata/x.env",
            "grep PASS ~/.config/axata/x.env",
            "sed -n p ~/.config/axata/x.env",
            "awk '{print}' ~/.config/axata/x.env",
            "cut -d= -f2 ~/.config/axata/x.env",
            "tr a-z A-Z < ~/.config/axata/x.env",
            "od -c ~/.config/axata/x.env",
            "tee /tmp/x < ~/.config/axata/x.env",
            "python3 -c \"print(open('/home/mne/.config/axata/x.env').read())\"",
            "python3 -I - <<'EOF'\nimport sys\nsys.stdout.write(open('/home/mne/.config/axata/x.env').read())\nEOF",
        ],
        [
            "python3 -I scripts/read-env.py ~/.config/axata/x.env",
            "python3 -I -c \"import os; open(os.path.expanduser('~/.config/axata/x.env')).read()\"",
            "python3 -I -c 'import os; p = os.environ[\"F\"]; print(open(p).read()[:0])'",
            "sed -i 's/^A=.*/A=1/' ~/.config/axata/x.env",
            "sed -i.bak -e 's/A/B/' ~/.config/axata/x.env",
            "grep -c PASS ~/.config/axata/x.env",
            "grep -ql PASS ~/.config/axata/x.env",
            "grep -q PASS ~/.config/axata/x.env && echo есть",
            "ls -l ~/.config/axata/x.env",
            "cmp ~/.config/axata/x.env ~/.config/axata/y.env",
            "set -a; . ~/.config/axata/x.env; set +a",
            "grep -rn password docs/",
            "ls ~/.config/axata/",
            f". {AXATA}",
            f"source {AXATA}",
            f"set -a; . {AXATA}; set +a; echo ok",
            f"test -f {AXATA}",
            f"stat {AXATA}",
            f"chmod 600 {AXATA}",
            f"cp {AXATA} ~/.config/axata/copy.env",
            "cat docs/README.md",
            "head -c 32 /dev/urandom | base64 > ~/.config/axata/x.env",
            "cat > ~/.config/axata/x.env <<EOF\nA=1\nEOF",
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
            "echo $MNE_VM_PASSWORD",
            'echo "token: ${JIRA_TOKEN}"',
            'printf "%s\\n" "$api_key"',
            "echo $GH_PAT",
            'print "$SECRET"',
            'echo "$KEY" | cat',
        ],
        [
            'printf \'%s\' "$MNE_VM_PASSWORD" | sudo -S -p "" true',
            "echo $PATH",
            "echo $HOME $MNE_VM_USER",
            "echo '$MNE_VM_PASSWORD'",
            "echo password",
            "printf '%s' \"$TOKEN\" > run/token.txt",
            "printf '%s' \"$TOKEN\" | tr -d '\\n' | curl -H @- x",
            "echo $TOKEN 2>err | wc -c",
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
            'set -a; . ~/.config/axata/mne-vm-user.env; set +a; ssh "$MNE_VM_USER@$h"',
        ],
    ),
}

NEUTRAL = [
    "ls -la",
    "git add path/a path/b",
    "git commit -m 'git stash; pkill; env; cat ~/.config/axata/x'",
    "cat <<'EOF'\ngit stash\npkill x\nEOF",
    "python3 -I -m py_compile hooks/forbid.py",
    "# git stash",
]


class ForbidTest(unittest.TestCase):
    def test_denied(self) -> None:
        for key, (denied, _) in CASES.items():
            self.assertGreaterEqual(len(denied), 2, key)
            for command in denied:
                with self.subTest(key=key, command=command):
                    found = forbid.check(command)
                    self.assertIsNotNone(found, command)
                    assert found is not None
                    self.assertTrue(found.startswith(key.split("-")[0]), (key, found))

    def test_exact_key(self) -> None:
        for key, (denied, _) in CASES.items():
            for command in denied:
                with self.subTest(key=key, command=command):
                    self.assertEqual(forbid.check(command), key)

    def test_allowed(self) -> None:
        for key, (_, allowed) in CASES.items():
            self.assertGreaterEqual(len(allowed), 2, key)
            for command in allowed:
                with self.subTest(key=key, command=command):
                    self.assertIsNone(forbid.check(command))

    def test_neutral(self) -> None:
        for command in NEUTRAL:
            with self.subTest(command=command):
                self.assertIsNone(forbid.check(command))

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
                self.assertIsNotNone(forbid.check(command))

    def test_every_key_has_message(self) -> None:
        for key in CASES:
            self.assertIn(key, forbid.DENY)

    def run_hook(
        self, payload: object, role: str | None
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "DUTY_ROLE"}
        if role:
            env["DUTY_ROLE"] = role
        return subprocess.run(
            [sys.executable, "-I", str(HOOK)],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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


if __name__ == "__main__":
    unittest.main()
