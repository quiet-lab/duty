"""Проверки конфигурации проекта и расширений: `python3 -I -m unittest discover -s scripts/tests -p 'test_duty_config.py'`."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import duty_config
import dutylib

SCRIPT = SCRIPTS / "duty_config.py"
# Переменные среды запуска, которые меняют поиск корня и пути: тесты их убирают.
CLEARED = (
    "DUTY_PROJECT",
    "CLAUDE_PROJECT_DIR",
    "DUTY_RESULTS_DIR",
    "DUTY_BUSY_DIR",
    "DUTY_SIGNALS_DIR",
    "DUTY_SYMBOLS_FILE",
    "DUTY_CONFIG_USER",
)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.user_file = self.home / "user.toml"
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update(GIT_ENV, HOME=str(self.home), DUTY_CONFIG_USER=str(self.user_file))
        self.env = env
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def repo(
        self,
        name: str = "proj",
        toml: str | None = 'schema = 1\n[project]\nname = "p"\n',
    ) -> Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        git(root, "init", "-q")
        if toml is not None:
            (root / ".duty").mkdir()
            (root / ".duty" / "duty.toml").write_text(toml, encoding="utf-8")
        return root

    def cli(
        self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            cwd=cwd or self.tmp,
            capture_output=True,
            text=True,
            check=False,
            env={**self.env, **(env or {})},
        )


class LayersTest(Base):
    def test_defaults_and_layer_order(self) -> None:
        root = self.repo(
            toml='schema = 1\n[git]\nprotected = ["dev"]\nworking = ["w"]\n'
            '[docs]\nreview = "r.md"\n'
        )
        self.user_file.write_text(
            '[git]\nworking = ["u1", "u2"]\ntask_prefixes = ["u/"]\n'
            '[docs]\nlint = "user-lint"\n',
            encoding="utf-8",
        )
        (root / ".duty" / "local.toml").write_text(
            '[docs]\nreview = "local.md"\n', encoding="utf-8"
        )
        cfg = duty_config.load(cwd=root)
        self.assertEqual(cfg.mode, "project")
        self.assertEqual(cfg.root, root)
        self.assertEqual(
            cfg.files,
            [self.user_file, root / ".duty/duty.toml", root / ".duty/local.toml"],
        )
        # Списки заменяются целиком, таблицы сливаются по ключам.
        self.assertEqual(cfg.get("git.working"), ["w"])
        self.assertEqual(cfg.get("git.protected"), ["dev"])
        self.assertEqual(cfg.get("git.task_prefixes"), ["u/"])
        self.assertEqual(cfg.get("docs.review"), "local.md")
        self.assertEqual(cfg.get("docs.lint"), "user-lint")
        self.assertEqual(cfg.get("git.keep_prefixes"), ["backup/", "saved/"])
        self.assertEqual(cfg.get("docs.sweep_area"), ["docs/", "*.md"])
        self.assertIs(cfg.get("git.forbid_stash"), True)
        self.assertEqual(cfg.get("nope.key", "d"), "d")
        self.assertEqual(cfg.name, "proj")

    def test_protected_default_includes_origin_head(self) -> None:
        root = self.repo(toml=None)
        git(root, "commit", "-q", "--allow-empty", "-m", "x")
        git(root, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git(
            root,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/trunk",
        )
        cfg = duty_config.load(cwd=root)
        self.assertEqual(cfg.get("git.protected"), ["main", "master", "trunk"])
        self.assertEqual(cfg.get("git.default_parent"), "trunk")

    def test_origin_head_not_queried_when_layers_set_git_keys(self) -> None:
        root = self.repo(
            toml='schema = 1\n[git]\nprotected = ["x"]\ndefault_parent = "x"\n'
        )
        real = duty_config._git
        calls: list[tuple[str, ...]] = []

        def spy(cwd: Path, *args: str) -> str:
            calls.append(args)
            return real(cwd, *args)

        with mock.patch.object(duty_config, "_git", spy):
            cfg = duty_config.load(root=root)
            self.assertEqual(cfg.get("git.protected"), ["x"])
            self.assertFalse([c for c in calls if "symbolic-ref" in c], calls)
            # Один из ключей не задан — origin/HEAD по-прежнему нужен.
            (root / ".duty" / "duty.toml").write_text(
                'schema = 1\n[git]\nprotected = ["x"]\n', encoding="utf-8"
            )
            duty_config.load(root=root)
            self.assertTrue([c for c in calls if "symbolic-ref" in c], calls)

    def test_services_absent_without_tables(self) -> None:
        root = self.repo(toml='schema = 1\n[jenkins]\nenv_file = "j.env"\n')
        cfg = duty_config.load(cwd=root)
        self.assertFalse(cfg.has("bitbucket"))
        self.assertFalse(cfg.has("jira"))
        self.assertTrue(cfg.has("jenkins"))
        self.assertEqual(cfg.get("jenkins.nodes"), [])
        self.assertEqual(cfg.get("jenkins.feed_markers"), [])
        self.assertIs(cfg.get("jenkins.insecure_tls"), False)

    def test_project_paths_relative_to_root_and_env_overrides(self) -> None:
        root = self.repo(
            toml='schema = 1\n[paths]\nresults = "../stand/results"\nbusy = "~/busy"\n'
        )
        cfg = duty_config.load(cwd=root)
        self.assertEqual(cfg.path("paths.results"), self.tmp / "stand/results")
        self.assertEqual(cfg.path("paths.busy"), self.home / "busy")
        self.assertEqual(
            cfg.path("paths.sandbox"), self.tmp / "stand/results/duty-tests/sandbox"
        )
        self.assertEqual(
            cfg.path("paths.signals"), self.home / ".local/state/duty/proj/signals"
        )
        sym = self.tmp / "sym.txt"
        sym.write_text("🐶\n\n🐱\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "DUTY_RESULTS_DIR": str(self.tmp / "r"),
                "DUTY_BUSY_DIR": str(self.tmp / "b"),
                "DUTY_SIGNALS_DIR": str(self.tmp / "s"),
                "DUTY_SYMBOLS_FILE": str(sym),
            },
        ):
            cfg = duty_config.load(cwd=root)
        self.assertEqual(cfg.path("paths.results"), self.tmp / "r")
        self.assertEqual(cfg.path("paths.busy"), self.tmp / "b")
        self.assertEqual(cfg.path("paths.signals"), self.tmp / "s")
        self.assertEqual(cfg.path("paths.sandbox"), self.tmp / "r/duty-tests/sandbox")
        self.assertEqual(cfg.get("symbols.tasks"), ["🐶", "🐱"])


class IsolationTest(Base):
    def test_symbols_file_does_not_touch_defaults(self) -> None:
        root = self.repo()
        listing = self.tmp / "symbols.txt"
        listing.write_text("🌱\n🌲\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DUTY_SYMBOLS_FILE": str(listing)}):
            self.assertEqual(duty_config.load(root).get("symbols.tasks"), ["🌱", "🌲"])
        self.assertEqual(duty_config.DEFAULTS["symbols"]["tasks"], [])
        self.assertEqual(duty_config.load(root).get("symbols.tasks"), [])

    def test_wrong_leaf_type_falls_back_to_default(self) -> None:
        root = self.repo(
            toml='schema = 1\n[git]\nprotected = "dev2"\nforbid_stash = 1\n'
            '[[busy.resources]]\nname = "s"\n'
        )
        cfg = duty_config.load(root)
        self.assertEqual(cfg.get("git.protected"), ["main", "master"])
        self.assertIs(cfg.get("git.forbid_stash"), True)
        self.assertEqual(cfg.get("busy.resources"), [])
        text = "\n".join(cfg.findings)
        self.assertIn("git.protected: ожидается список строк, получено str", text)
        self.assertIn("git.forbid_stash", text)
        self.assertIn("нет ключа why", text)
        env = self.cli("env", cwd=root).stdout
        self.assertNotIn("DUTY_CFG_GIT_PROTECTED=dev2", env)


class RootTest(Base):
    def test_explicit_root_wins(self) -> None:
        a, b, c = self.repo("a"), self.repo("b"), self.repo("c")
        os.environ["DUTY_PROJECT"] = str(b)
        os.environ["CLAUDE_PROJECT_DIR"] = str(c)
        self.assertEqual(duty_config.find_root(root=a, cwd=c), a)
        self.assertEqual(duty_config.find_root(cwd=a), b)
        del os.environ["DUTY_PROJECT"]
        self.assertEqual(duty_config.find_root(cwd=a), c)

    def test_climb_from_subdirectory(self) -> None:
        root = self.repo()
        deep = root / "x" / "y"
        deep.mkdir(parents=True)
        cfg = duty_config.load(cwd=deep)
        self.assertEqual((cfg.root, cfg.mode, cfg.name), (root, "project", "p"))

    def test_climb_stops_at_git_toplevel(self) -> None:
        # .duty/ выше верха git не находится: каталог над репозиторием — чужой.
        outer = self.tmp / "outer"
        (outer / ".duty").mkdir(parents=True)
        (outer / ".duty" / "duty.toml").write_text("schema = 1\n", encoding="utf-8")
        inner = outer / "inner"
        inner.mkdir()
        git(inner, "init", "-q")
        (inner / "sub").mkdir()
        cfg = duty_config.load(cwd=inner / "sub")
        self.assertEqual((cfg.root, cfg.mode), (inner, "general"))

    def test_submodule_climbs_to_superproject(self) -> None:
        lib = self.repo("lib", toml=None)
        git(lib, "commit", "-q", "--allow-empty", "-m", "x")
        sup = self.repo("sup")
        git(sup, "submodule", "add", "-q", str(lib), "mods/lib")
        cfg = duty_config.load(cwd=sup / "mods" / "lib")
        self.assertEqual((cfg.root, cfg.mode), (sup, "project"))
        (sup / ".duty" / "duty.toml").unlink()
        cfg = duty_config.load(cwd=sup / "mods" / "lib")
        self.assertEqual((cfg.root, cfg.mode), (sup, "general"))

    def test_project_worktree_root_is_main_tree(self) -> None:
        root = self.repo()
        git(root, "commit", "-q", "--allow-empty", "-m", "x")
        wt = self.tmp / "wt-proj"
        git(root, "worktree", "add", "-q", "-b", "t", str(wt))
        (wt / "sub").mkdir()
        self.assertEqual(duty_config.find_root(cwd=wt), root)
        self.assertEqual(duty_config.find_root(cwd=wt / "sub"), root)
        self.assertEqual(duty_config.find_root(root=wt), root)
        cfg = duty_config.load(cwd=wt)
        self.assertEqual((cfg.root, cfg.mode), (root, "project"))

    def test_submodule_worktree_root_is_superproject(self) -> None:
        lib = self.repo("lib", toml=None)
        git(lib, "commit", "-q", "--allow-empty", "-m", "x")
        sup = self.repo("sup")
        git(sup, "submodule", "add", "-q", str(lib), "mods/lib")
        wt = self.tmp / "elsewhere" / "lib-wt"
        git(sup / "mods" / "lib", "worktree", "add", "-q", "-b", "t", str(wt))
        self.assertEqual(duty_config.find_root(cwd=wt), sup)
        self.assertEqual(duty_config.find_root(root=wt), sup)
        cfg = duty_config.load(cwd=wt)
        self.assertEqual((cfg.root, cfg.mode), (sup, "project"))

    def test_env_beats_worktree_mapping(self) -> None:
        root = self.repo()
        git(root, "commit", "-q", "--allow-empty", "-m", "x")
        wt = self.tmp / "wt-env"
        git(root, "worktree", "add", "-q", "-b", "t2", str(wt))
        os.environ["DUTY_PROJECT"] = str(wt)
        self.assertEqual(duty_config.find_root(cwd=wt), wt)

    def _worktree_without_main_duty(self, name: str) -> tuple[Path, Path]:
        root = self.repo(name, toml=None)
        git(root, "commit", "-q", "--allow-empty", "-m", "x")
        wt = self.tmp / f"wt-{name}"
        git(root, "worktree", "add", "-q", "-b", f"t-{name}", str(wt))
        (wt / ".duty").mkdir()
        (wt / ".duty" / "duty.toml").write_text(
            'schema = 1\n[paths]\nbusy = "../ocp-dev-stand/busy"\n', encoding="utf-8"
        )
        return root, wt

    def _check_wt_only(self, cfg: "duty_config.Config", root: Path, wt: Path) -> None:
        self.assertEqual((cfg.root, cfg.base, cfg.mode), (wt, root, "project"))
        self.assertEqual(cfg.files[-1], wt / ".duty" / "duty.toml")
        self.assertEqual(
            cfg.get("paths.busy"), str(root.parent / "ocp-dev-stand" / "busy")
        )

    def test_worktree_without_main_duty_uses_worktree_files(self) -> None:
        root, wt = self._worktree_without_main_duty("wo")
        self.assertEqual(duty_config.find_root(root=wt), wt)
        self.assertEqual(duty_config.find_root(cwd=wt), wt)
        self._check_wt_only(duty_config.load(root=wt), root, wt)
        self._check_wt_only(duty_config.load(cwd=wt), root, wt)
        data = json.loads(self.cli("json", "--root", str(wt)).stdout)
        self.assertEqual((data["_root"], data["_base"]), (str(wt), str(root)))

    def test_env_project_on_worktree_paths_from_main_tree(self) -> None:
        root, wt = self._worktree_without_main_duty("we")
        os.environ["DUTY_PROJECT"] = str(wt)
        self._check_wt_only(duty_config.load(cwd=self.tmp), root, wt)

    def test_no_git_no_root(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        cfg = duty_config.load(cwd=plain)
        self.assertIsNone(cfg.root)
        self.assertEqual((cfg.mode, cfg.name), ("general", "plain"))
        r = self.cli("root", "--cwd", str(plain))
        self.assertEqual((r.returncode, r.stdout), (3, ""))


class GeneralModeTest(Base):
    def test_state_paths(self) -> None:
        root = self.repo("gen", toml=None)
        cfg = duty_config.load(cwd=root)
        state = self.home / ".local/state/duty/gen"
        self.assertEqual(cfg.mode, "general")
        self.assertEqual(cfg.path("paths.results"), state / "results")
        self.assertEqual(cfg.path("paths.busy"), state / "busy")
        self.assertEqual(cfg.path("paths.signals"), state / "signals")
        self.assertEqual(
            cfg.path("paths.sandbox"), state / "results/duty-tests/sandbox"
        )
        self.assertEqual(cfg.get("git.protected"), ["main", "master"])

    def test_state_from_user_layer(self) -> None:
        root = self.repo("gen", toml=None)
        self.user_file.write_text(
            '[paths]\nstate = "~/st"\n[symbols]\noutside = ["🍺"]\n', encoding="utf-8"
        )
        cfg = duty_config.load(cwd=root)
        self.assertEqual(cfg.path("paths.results"), self.home / "st/gen/results")
        self.assertEqual(cfg.get("symbols.outside"), ["🍺"])


class CliTest(Base):
    def test_root_get_json_env(self) -> None:
        root = self.repo(
            toml='schema = 1\n[project]\nname = "p"\n[git]\nprotected = ["dev", "master"]\n'
            'named_dev_branches = true\n[paths]\nresults = "res"\n'
        )
        sub = root / "deep"
        sub.mkdir()
        r = self.cli("root", cwd=sub)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, str(root)))
        self.assertEqual(self.cli("get", "project.name", cwd=sub).stdout, "p\n")
        self.assertEqual(
            self.cli("get", "git.named_dev_branches", cwd=sub).stdout, "true\n"
        )
        self.assertEqual(
            json.loads(self.cli("get", "git.protected", cwd=sub).stdout),
            ["dev", "master"],
        )
        self.assertEqual(
            self.cli("--root", str(root), "get", "paths.results").stdout.strip(),
            str(root / "res"),
        )
        missing = self.cli("get", "bitbucket.url", cwd=sub)
        self.assertEqual((missing.returncode, missing.stdout), (3, ""))
        self.assertEqual(
            self.cli("get", "bitbucket.url", "--default", "-", cwd=sub).stdout, "-\n"
        )
        data = json.loads(self.cli("json", "--cwd", str(sub)).stdout)
        self.assertEqual(data["_root"], str(root))
        self.assertEqual(data["_mode"], "project")
        self.assertEqual(data["_files"], [str(root / ".duty/duty.toml")])
        self.assertEqual(data["paths"]["results"], str(root / "res"))
        env = self.cli("env", "--prefix", "X_", cwd=root).stdout.splitlines()
        self.assertIn("export X_GIT_PROTECTED=dev,master", env)
        self.assertIn(f"export X_PATHS_RESULTS={root / 'res'}", env)
        self.assertIn("export X_MODE=project", env)
        self.assertFalse(any(line.startswith("export X_FORBID_RULES") for line in env))
        shown = subprocess.run(
            [
                "bash",
                "-c",
                'eval "$1"; printf %s "$X_PROJECT_NAME"',
                "-",
                "\n".join(env),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(shown.stdout, "p")

    def test_toml_error_is_2(self) -> None:
        root = self.repo(toml="schema = = 1\n")
        for cmd in (("check",), ("get", "project.name"), ("json",)):
            r = self.cli(*cmd, cwd=root)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("duty.toml", r.stderr)

    def test_init(self) -> None:
        root = self.repo(toml=None)
        r = self.cli("init", "--name", "имя", cwd=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        target = root / ".duty/duty.toml"
        text = target.read_text(encoding="utf-8")
        self.assertIn("schema = 1", text)
        self.assertIn('name = "имя"', text)
        self.assertIn("# [bitbucket]", text)
        self.assertEqual(self.cli("check", cwd=root).returncode, 0)
        self.assertEqual(self.cli("get", "project.name", cwd=root).stdout, "имя\n")
        # Раскомментированный шаблон — тоже верная схема.
        uncommented = "\n".join(
            line[2:]
            if line.startswith("# ") and "=" in line or line.startswith("# [")
            else line
            for line in text.splitlines()
        )
        target.write_text(uncommented, encoding="utf-8")
        found = self.cli("check", cwd=root).stdout.splitlines()
        self.assertTrue(all("нет файла" in line for line in found), found)
        again = self.cli("init", cwd=root)
        self.assertEqual(again.returncode, 1)


class CheckTest(Base):
    def findings(
        self, toml: str, user: str | None = None, name: str = "proj"
    ) -> tuple[int, list[str]]:
        root = self.repo(name, toml=toml)
        if user is not None:
            self.user_file.write_text(user, encoding="utf-8")
        r = self.cli("check", cwd=root)
        return r.returncode, r.stdout.splitlines()

    def test_clean(self) -> None:
        token = self.tmp / "token"
        token.write_text("x", encoding="utf-8")
        code, out = self.findings(
            f'schema = 1\n[bitbucket]\nurl = "u"\ntoken_file = "{token}"\n'
            '[[forbid.rules]]\nwords = ["a", "b"]\nreason = "r"\n'
            '[symbols]\nmain = "🍀"\ntasks = ["🌱", "🌲"]\ncolor = "#a6e3a1"\n',
            user='[symbols]\noutside = ["🍺", "☕"]\noutside_color = "#F5C2E7"\n',
        )
        self.assertEqual((code, out), (0, []))

    def test_color_format(self) -> None:
        code, out = self.findings(
            'schema = 1\n[symbols]\ncolor = "green"\n',
            user='[symbols]\noutside_color = "#12345"\n',
        )
        text = "\n".join(out)
        self.assertEqual(code, 1)
        self.assertIn("symbols.color: цвет «green» не в формате #rrggbb", text)
        self.assertIn("symbols.outside_color: цвет «#12345» не в формате #rrggbb", text)

    def test_outside_color_user_layer_only(self) -> None:
        code, out = self.findings('schema = 1\n[symbols]\noutside_color = "#a6e3a1"\n')
        self.assertEqual(code, 1)
        self.assertIn(
            "symbols.outside_color допустим только в пользовательском слое",
            "\n".join(out),
        )

    def test_schema_version(self) -> None:
        self.assertIn("поддерживается 1", " ".join(self.findings("schema = 2\n")[1]))
        self.assertIn(
            "нет ключа schema",
            " ".join(self.findings("[project]\nname = 'x'\n", name="p2")[1]),
        )

    def test_unknown_key_and_table(self) -> None:
        code, out = self.findings(
            'schema = 1\n[git]\nprotect = ["x"]\n[extra]\na = 1\n'
        )
        self.assertEqual(code, 1)
        text = "\n".join(out)
        self.assertIn("неизвестный ключ git.protect", text)
        self.assertIn("неизвестная таблица [extra]", text)
        self.assertTrue(
            all(
                line.startswith(str(self.tmp / "proj/.duty/duty.toml") + ": ")
                for line in out
            )
        )

    def test_types(self) -> None:
        code, out = self.findings(
            'schema = 1\n[git]\nprotected = "dev"\nforbid_stash = 1\n'
            '[paths]\nresults = 5\n[[busy.resources]]\nname = "s"\n'
            '[jenkins]\nfeed_markers = "feed-x/"\n'
        )
        text = "\n".join(out)
        self.assertEqual(code, 1)
        self.assertIn("git.protected: ожидается список строк, получено str", text)
        self.assertIn("git.forbid_stash: ожидается true или false, получено int", text)
        self.assertIn("paths.results: ожидается строка, получено int", text)
        self.assertIn(
            "jenkins.feed_markers: ожидается список строк, получено str", text
        )
        self.assertIn("нет ключа why", text)
        self.assertIn("нет ключа until", text)

    def test_missing_files(self) -> None:
        _, out = self.findings(
            'schema = 1\n[jenkins]\nenv_file = "~/none.env"\n[jira]\ntoken_file = "rel"\n'
        )
        text = "\n".join(out)
        self.assertIn("jenkins.env_file: нет файла ~/none.env", text)
        self.assertIn("jira.token_file: нет файла rel", text)

    def test_missing_access_file_is_note_with_code_0(self) -> None:
        root = self.repo(
            toml='schema = 1\n[jira]\nurl = "u"\ntoken_file = "~/none-token"\n'
        )
        r = self.cli("check", cwd=root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("примечание:", r.stdout)
        self.assertIn("jira.token_file: нет файла ~/none-token", r.stdout)

    def test_required_keys_of_services(self) -> None:
        code, out = self.findings(
            'schema = 1\n[bitbucket]\nuser = "u"\n[jenkins]\nenv_file = ""\n'
        )
        text = "\n".join(out)
        self.assertEqual(code, 1)
        self.assertIn("bitbucket.url: обязателен", text)
        self.assertIn("bitbucket.token_file: обязателен", text)
        self.assertIn("jenkins.env_file: обязателен", text)

    def test_user_only_keys(self) -> None:
        _, out = self.findings(
            'schema = 1\n[paths]\nstate = "s"\n[symbols]\noutside = ["🍺"]\n'
        )
        text = "\n".join(out)
        self.assertIn("paths.state допустим только в пользовательском слое", text)
        self.assertIn("symbols.outside допустим только в пользовательском слое", text)

    def test_symbols(self) -> None:
        code, out = self.findings(
            'schema = 1\n[symbols]\nmain = "🍀"\ntasks = ["🌱", "🌱", "🍀", "🏳️‍🌈"]\n',
            user='[symbols]\noutside = ["🌱", "🍺"]\n',
        )
        text = "\n".join(out)
        self.assertEqual(code, 1)
        self.assertIn("повтор символа «🌱» (в том же наборе)", text)
        self.assertIn("повтор символа «🍀» (уже в symbols.main)", text)
        self.assertIn("символ «🏳️‍🌈» из 4 кодовых точек", text)
        self.assertIn(
            f"{self.user_file}: symbols.outside: повтор символа «🌱» (уже в symbols.tasks)",
            text,
        )

    def test_selection_sets_are_valid(self) -> None:
        # Наборы пользователя (symbols-selection.txt) — по одной кодовой точке, без повторов.
        sets = {
            "main": "🍀",
            "tasks": [
                "🌱",
                "🌲",
                "🌳",
                "🌴",
                "🌵",
                "🌾",
                "🌿",
                "🍃",
                "🪴",
                "🍏",
                "🍐",
                "🥝",
                "🍈",
                "🫒",
                "🥦",
                "🥬",
                "🫑",
                "🫛",
                "\U0001fadd",
                "🥑",
                "🌽",
            ],
        }
        outside = [
            "🍺",
            "🍻",
            "🍷",
            "🍸",
            "🍹",
            "🍵",
            "☕",
            "🍾",
            "🥂",
            "🥃",
            "🥛",
            "🧉",
            "🧋",
        ]
        tasks = ", ".join(json.dumps(s, ensure_ascii=False) for s in sets["tasks"])
        code, out = self.findings(
            f'schema = 1\n[symbols]\nmain = "{sets["main"]}"\ntasks = [{tasks}]\n',
            user="[symbols]\noutside = ["
            + ", ".join(f'"{s}"' for s in outside)
            + "]\n",
        )
        self.assertEqual((code, out), (0, []))


class ExtensionsTest(Base):
    def ext(self, root: Path, name: str, body: str, mode: int = 0o755) -> None:
        folder = root / ".duty" / "facts.d"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / name
        f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        f.chmod(mode)

    def test_order_env_and_errors(self) -> None:
        root = self.repo()
        self.ext(
            root,
            "20-env",
            'printf "## env\\n%s|%s|%s|%s\\n" "$DUTY_PROJECT" "$DUTY_CONFIG" "$DUTY_PLUGIN" "$PWD"\n',
        )
        self.ext(root, "10-fail", "echo partial; echo boom >&2; exit 4\n")
        self.ext(root, "30-noexec", "echo never\n", mode=0o644)
        self.ext(root, ".hidden", "echo hidden\n")
        res = dutylib.run_extensions(root, "facts", 5)
        self.assertEqual([r[0] for r in res], ["10-fail", "20-env", "30-noexec"])
        self.assertEqual(res[0], ("10-fail", "partial\n", "код выхода 4: boom"))
        plugin = str(SCRIPTS.parent)
        self.assertEqual(
            res[1],
            (
                "20-env",
                f"## env\n{root}|{root}/.duty/duty.toml|{plugin}|{root}\n",
                None,
            ),
        )
        self.assertEqual(res[2][1], "")
        self.assertIn("не исполняемый", res[2][2])

    def test_timeout_kills_group(self) -> None:
        root = self.repo()
        fifo = self.tmp / "never-written"
        os.mkfifo(fifo)
        # Открытие канала без писателя блокирует навсегда — без sleep и таймеров.
        self.ext(root, "hang", f'echo start; cat "{fifo}" | cat\n')
        res = dutylib.run_extensions(root, "facts", 0.5)
        self.assertEqual(res, [("hang", "start\n", "срок 0.5 с истёк")])

    def test_missing_dir_and_bad_kind(self) -> None:
        root = self.repo()
        self.assertEqual(dutylib.run_extensions(root, "cleanup", 1), [])
        with self.assertRaises(ValueError):
            dutylib.run_extensions(root, "other", 1)


class HelpersTest(Base):
    def test_sh_git_size_owner(self) -> None:
        root = self.repo()
        self.assertEqual(
            dutylib.git("rev-parse", "--show-toplevel", cwd=root), str(root)
        )
        self.assertEqual(dutylib.sh("false"), "")
        self.assertEqual(dutylib.sh("no-such-command-duty"), "")
        self.assertNotEqual(dutylib.size(root), "?")
        self.assertEqual(dutylib.owner_kind(root), "own")
        self.assertFalse(dutylib.cwd_users(root))
        holder = subprocess.Popen(["cat"], cwd=root, stdin=subprocess.PIPE)
        try:
            self.assertTrue(dutylib.cwd_users(root))
        finally:
            holder.communicate()

    def test_active_sessions(self) -> None:
        sessions = self.home / ".claude/sessions"
        sessions.mkdir(parents=True)
        (sessions / "a.json").write_text(
            json.dumps({"pid": os.getpid(), "sessionId": "live"}), encoding="utf-8"
        )
        (sessions / "b.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(dutylib.active_sessions(self.home), {"live"})


if __name__ == "__main__":
    unittest.main()
