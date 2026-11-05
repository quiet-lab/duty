#!/usr/bin/env python3
"""Запреты агента задачи в виде хука PreToolUse для `Bash`.

Правила, которые раньше жили только текстом (`reference/worker-rules.md`,
`agents/worker.md`, правила проекта), здесь отвергаются механизмом.
Перечни (защищённые ветви, каталоги секретов, свои правила) берутся из
конфигурации проекта (`scripts/duty_config.py`, `.duty/duty.toml`); без неё
действуют встроенные значения по умолчанию. Команда разбирается по словам и конвейерам, а не по подстрокам:
запрещённое слово внутри строкового литерала (сообщение коммита, аргумент
`grep`) команду не отвергает. Подстановки `$(…)`, обратные кавычки,
`bash -c`, `eval` и команда после `--` у `tab-run.sh` проверяются рекурсивно.

Запреты (названия — в `DENY`):

- `network` — переключение туннеля и сети станции;
- `kill` — завершение процессов по образцу;
- `git` — `git add -A/-u`, `git stash`, принудительный push, push в защищённые
  ветви (`git.protected`);
- `herdr` — вкладки и ввод в панели в обход `scripts/tab-run.sh`;
- `secret` — вывод содержимого каталогов секретов (`secrets.dirs`), печать
  окружения и секретов, пароль литералом в `curl -u` и `sshpass -p`;
- `rule` — свои правила проекта (`[[forbid.rules]]`).

Причина отказа называет запрет и правило, но не повторяет команду: в ней
может быть секрет. Вне `DUTY_ROLE=worker` хук молчит; сбой разбора пропускает
команду (код 0, запись в stderr), чтобы не ломать сессию.
"""

import importlib.util
import itertools
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_DEPTH = 4

DENY = {
    "network": (
        "Запрещено переключать туннель и сеть станции (nmcli, awg, wg, openvpn, "
        "amn-*, resolvectl, ip link set up/down, systemctl awg-quick@…): "
        "правила проекта (.duty/worker-rules.md) и ~/.claude/CLAUDE.md; "
        "переключает сам пользователь. Состояние сети читай через ip addr show."
    ),
    "kill": (
        "Запрещено завершать процессы по образцу (pkill, killall, fuser -k, "
        "kill -9 -1, kill $(pgrep …), xargs kill): ~/.claude/CLAUDE.md, раздел "
        "«Субагенты» — только свои процессы по известным PID. "
        "Вместо этого: kill <PID>."
    ),
    "git-add": (
        "Запрещено git add -A/--all/-u/--update: правила проекта "
        "(.duty/worker-rules.md) и ~/.claude/CLAUDE.md — в индекс только свои "
        "файлы по путям. "
        "Вместо этого: git add <путь> …"
    ),
    "git-stash": (
        "Запрещено git stash: стек общий для всех worktree "
        "(правила проекта (.duty/worker-rules.md) и ~/.claude/CLAUDE.md). "
        "Вместо этого: коммит WIP в своей ветви или копия в каталоге задачи."
    ),
    "git-force": (
        "Запрещён принудительный push (--force, -f, --force-with-lease, "
        "refspec с «+»): история origin не переписывается "
        "(правила проекта (.duty/worker-rules.md) и ~/.claude/CLAUDE.md). "
        "Отправленная ветвь "
        "правится новыми коммитами."
    ),
    "git-protected": (
        "Запрещён push в защищённые ветви {protected}: правила проекта "
        "(.duty/worker-rules.md) и ~/.claude/CLAUDE.md — слияние и перенос "
        "только по решению пользователя. "
        "Вместо этого: git push origin <своя ветвь>."
    ),
    "herdr": (
        "Запрещено создавать вкладки и писать в панели herdr напрямую "
        "(tab create, pane run, pane send): agents/worker.md, п. 4 — "
        "вкладка команды только через scripts/tab-run.sh. "
        "Вместо этого: bash <плагин>/scripts/tab-run.sh <имя> -- <команда>."
    ),
    "secret-file": (
        "Запрещён вывод содержимого файлов каталогов секретов (secrets.dirs: "
        "{secrets}) (cat, less, "
        "head, tail, grep без -c/-l/-q, sed без -i, awk, cut, tr, od, base64, "
        "cp и tee вне этих каталогов, python3 с print или sys.stdout): "
        "rules.md задач «пароли и токены в вывод не попадают». Разрешено "
        "читать в переменные и править на месте без вывода значений: "
        "set -a; . <каталог секретов>/<файл>; set +a, python3 -I без печати, "
        "sed -i, grep -c/-l/-q, test -f, ls -l, stat, cmp."
    ),
    "secret-env": (
        "Запрещено печатать окружение целиком или секретную переменную "
        "(env, printenv, export -p, declare -p, set без аргументов): "
        "rules.md задач «пароли и токены в вывод не попадают». "
        'Проверка наличия — test -n "$ИМЯ", без вывода значения.'
    ),
    "secret-echo": (
        "Запрещено печатать значение переменной с PASS, TOKEN, SECRET, PAT, "
        "KEY в имени (echo, printf, print): rules.md задач «пароли и токены "
        "в вывод не попадают». Вместо этого: передача в stdin "
        "через printf '%s' \"$ИМЯ\" | команда."
    ),
    "secret-literal": (
        "Запрещён пароль литералом в команде (curl -u логин:пароль, "
        "sshpass -p пароль): rules.md задач «пароли и токены только из "
        "файлов через set -a и stdin». Вместо этого: set -a; . "
        '<каталог секретов>/<файл>; set +a и -u "$ЛОГИН:$ПАРОЛЬ", sshpass -e или -f.'
    ),
}

NETWORK_COMMANDS = {
    "nmcli",
    "awg",
    "awg-quick",
    "amn-switch",
    "amn-country",
    "amn-apply-config",
    "amn-zones-update",
    "wg",
    "wg-quick",
    "openvpn",
    "resolvectl",
}
KILL_COMMANDS = {"pkill", "killall"}
READERS = {
    "cat",
    "less",
    "more",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "sed",
    "awk",
    "gawk",
    "mawk",
    "bat",
    "strings",
    "base64",
    "xxd",
    "od",
    "hexdump",
    "tac",
    "nl",
    "cut",
    "tr",
}
PRINTERS = {"echo", "printf", "print"}
# Команды после «|», которые печатают свой ввод на экран.
PIPE_PRINTERS = {
    "cat",
    "tee",
    "head",
    "tail",
    "less",
    "more",
    "xxd",
    "od",
    "base64",
    "sed",
    "awk",
    "tr",
}
PYTHON_RE = re.compile(r"python[0-9.]*")
PYTHON_PRINT_RE = re.compile(r"\bprint\b|sys\.std(out|err)")
GREP_QUIET_LONG = {
    "--count",
    "--files-with-matches",
    "--files-without-match",
    "--quiet",
    "--silent",
}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))")
RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "time"}
# Обёртки: ключи, которым нужно отдельное значение.
WRAPPERS = {
    "sudo": {"-p", "-u", "-g", "-h", "-C", "-r", "-t", "-T", "-D", "-R"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k"},
    "stdbuf": set(),
    "setsid": set(),
    "builtin": set(),
}
XARGS_VALUE_FLAGS = {"-n", "-P", "-I", "-d", "-L", "-s", "-E", "-a", "-l"}


PLUGIN = Path(__file__).resolve().parents[1]
RULE_NOTE = "правило проекта (.duty/duty.toml, [[forbid.rules]])"
HOME_FORMS = ("~", "$HOME", "${HOME}")


def secret_patterns(raw: str) -> list["re.Pattern[str]"]:
    """Образцы пути каталога секретов: `~/.config/<имя>` ловится как `.config/<имя>`
    и `XDG_CONFIG_HOME}/<имя>`, иной каталог — по полному пути и форме с `$HOME`."""
    text = raw.rstrip("/")
    if not text:
        return []
    m = re.fullmatch(r"~/\.config/([^/]+)", text)
    if m:
        name = re.escape(m.group(1))
        return [re.compile(rf"(\.config|XDG_CONFIG_HOME\}}?)/{name}(/|$)")]
    full = os.path.expanduser(text)
    home = os.path.expanduser("~")
    guard = r"(?<![A-Za-z0-9_.\-/])"
    out = [re.compile(guard + re.escape(full) + r"(/|$)")]
    if full == home or full.startswith(home + "/"):
        rel = re.escape(full[len(home) :])
        forms = "|".join(re.escape(f) for f in HOME_FORMS)
        out.append(re.compile(rf"(^|[^A-Za-z0-9_])({forms}){rel}(/|$)"))
    return out


@dataclass
class Settings:
    """Перечни запретов из конфигурации проекта (по умолчанию — встроенные)."""

    protected: set[str] = field(default_factory=lambda: {"main", "master"})
    forbid_add_all: bool = True
    forbid_stash: bool = True
    network: bool = True
    herdr: bool = True
    secret_dirs: list[str] = field(default_factory=list)
    secret_res: list["re.Pattern[str]"] = field(default_factory=list)
    rules: list[tuple[tuple[str, ...], str]] = field(default_factory=list)


def settings_from_config(cfg: object) -> Settings:
    """Settings из `duty_config.Config`; значения по умолчанию задаёт сам модуль."""
    get = cfg.get  # type: ignore[attr-defined]
    dirs = [d for d in get("secrets.dirs", []) if isinstance(d, str) and d]
    rules = []
    for rule in get("forbid.rules", []):
        words = tuple(w for w in rule.get("words", []) if isinstance(w, str))
        if words:
            rules.append((words, str(rule.get("reason", ""))))
    return Settings(
        protected=set(get("git.protected", ["main", "master"])),
        forbid_add_all=bool(get("git.forbid_add_all", True)),
        forbid_stash=bool(get("git.forbid_stash", True)),
        network=bool(get("forbid.network", True)),
        herdr=bool(get("forbid.herdr_direct", True)),
        secret_dirs=dirs,
        secret_res=[p for d in dirs for p in secret_patterns(d)],
        rules=rules,
    )


def load_settings(cwd: str | None = None) -> Settings:
    """Конфигурация загружается один раз на вызов хука; сбой — значения по умолчанию."""
    try:
        path = PLUGIN / "scripts" / "duty_config.py"
        spec = importlib.util.spec_from_file_location("duty_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("duty_config", module)
        spec.loader.exec_module(module)
        return settings_from_config(module.load(root=None, cwd=cwd or os.getcwd()))
    except Exception as error:  # noqa: BLE001 - без конфигурации хук не падает
        print(
            f"forbid.py: конфигурация не загружена, значения по умолчанию: {error!r}",
            file=sys.stderr,
        )
        return Settings()


def deny_text(key: str, cfg: Settings) -> str:
    """Текст отказа: подстановка актуальных перечней из конфигурации."""
    if key.startswith("rule-"):
        reason = cfg.rules[int(key[5:])][1]
        return f"Запрещено: {reason} — {RULE_NOTE}."
    secrets = ", ".join(cfg.secret_dirs) or "—"
    return (
        DENY[key]
        .replace("{protected}", ", ".join(sorted(cfg.protected)))
        .replace("{secrets}", secrets)
    )


@dataclass
class Word:
    text: str = ""
    vars: list[str] = field(default_factory=list)  # раскрываемые вне одинарных кавычек
    subs: list[str] = field(default_factory=list)  # тексты $(…) и `…`
    started: bool = False

    @property
    def literal(self) -> bool:
        return not self.vars and not self.subs


@dataclass
class Command:
    words: list[Word] = field(default_factory=list)
    redirs: list[tuple[str, Word]] = field(default_factory=list)
    pipe_out: bool = False
    stdin: list[str] = field(default_factory=list)  # тела heredoc


def _skip_subst(src: str, i: int) -> int:
    """Индекс после `)`, закрывающей `$(`, начатую перед позицией i."""
    depth = 1
    quote = ""
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\\":
            i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(src)


def _scan_inner(src: str, start: int, word: Word) -> int:
    """Разбирает `$…` и обратную кавычку с позиции start; возвращает позицию после."""
    ch = src[start]
    if ch == "`":
        end = start + 1
        while end < len(src) and src[end] != "`":
            end += 2 if src[end] == "\\" else 1
        word.subs.append(src[start + 1 : end])
        word.text += "`…`"
        return min(end + 1, len(src))
    if src.startswith("$(", start):
        end = _skip_subst(src, start + 2)
        word.subs.append(src[start + 2 : end - 1])
        word.text += "$(…)"
        return end
    match = VAR_RE.match(src, start)
    if match:
        word.vars.append(match.group(1) or match.group(2))
        word.text += match.group(0)
        if match.group(1) and src.startswith("}", match.end()):
            word.text += "}"
            return match.end() + 1
        return match.end()
    word.text += ch
    return start + 1


def parse(src: str) -> list[Command]:
    """Разбор команды на простые команды; вложенное — в `Word.subs`."""
    commands: list[Command] = [Command()]
    word = Word()
    redir_op: str | None = None
    heredocs: list[tuple[str, Command]] = []
    i = 0
    n = len(src)

    def new_command() -> None:
        flush()
        if commands[-1].words or commands[-1].redirs:
            commands.append(Command())

    def flush() -> None:
        nonlocal word, redir_op
        if not word.started:
            return
        if redir_op is not None:
            commands[-1].redirs.append((redir_op, word))
            redir_op = None
        else:
            commands[-1].words.append(word)
        word = Word()

    while i < n:
        ch = src[i]
        if ch in " \t\r":
            flush()
            i += 1
        elif ch == "\n":
            new_command()
            i += 1
            while heredocs:
                delimiter, owner = heredocs.pop(0)
                body: list[str] = []
                while i < n:
                    end = src.find("\n", i)
                    line = src[i : end if end != -1 else n]
                    i = end + 1 if end != -1 else n
                    if line.strip() == delimiter:
                        break
                    body.append(line)
                owner.stdin.append("\n".join(body))
        elif ch == ";":
            new_command()
            i += 1
        elif ch == "&":
            if src.startswith("&>", i):
                flush()
                redir_op = "&>"
                i += 2
                if src.startswith(">", i):
                    i += 1
            else:
                new_command()
                i += 2 if src.startswith("&&", i) else 1
        elif ch == "|":
            flush()
            commands[-1].pipe_out = not src.startswith("||", i)
            commands.append(Command())
            i += 2 if src[i + 1 : i + 2] in ("|", "&") else 1
        elif ch in "()" and not (ch == "(" and word.started):
            new_command()
            i += 1
        elif ch in "<>":
            if ch == "<" and src.startswith("<(", i) or src.startswith(">(", i):
                end = _skip_subst(src, i + 2)
                word.started = True
                word.subs.append(src[i + 2 : end - 1])
                word.text += "<(…)"
                i = end
                continue
            fd = ""
            if word.started and word.text.isdigit() and not word.subs:
                fd = word.text if word.text != "1" else ""
                word = Word()  # номер дескриптора: 2>&1
            else:
                flush()
            match = re.compile(r"<<<|<<-?|>>|>&|<&|>\||<|>").match(src, i)
            assert match is not None
            op = match.group(0)
            i = match.end()
            if op in (">&", "<&") and src[i : i + 1].isdigit():
                m = re.compile(r"\d+-?").match(src, i)
                assert m is not None
                i = m.end()
                continue
            if op.startswith("<<") and op != "<<<":
                m = re.compile(r"\s*(['\"]?)([^\s'\"]+)\1").match(src, i)
                if m:
                    heredocs.append((m.group(2), commands[-1]))
                    i = m.end()
                continue
            redir_op = fd + op
        elif ch == "#" and not word.started:
            end = src.find("\n", i)
            i = n if end == -1 else end
        elif ch == "\\" and src[i + 1 : i + 2] == "\n":
            i += 2  # продолжение строки
        else:
            word.started = True
            if ch == "'":
                end = src.find("'", i + 1)
                end = n if end == -1 else end
                word.text += src[i + 1 : end]
                i = end + 1
            elif ch == '"':
                i += 1
                while i < n and src[i] != '"':
                    if src[i] == "\\" and src[i + 1 : i + 2] == "\n":
                        i += 2
                    elif src[i] == "\\" and i + 1 < n:
                        word.text += src[i + 1]
                        i += 2
                    elif src[i] in "$`":
                        i = _scan_inner(src, i, word)
                    else:
                        word.text += src[i]
                        i += 1
                i += 1
            elif ch == "\\":
                word.text += src[i + 1 : i + 2]
                i += 2
            elif ch in "$`":
                i = _scan_inner(src, i, word)
            else:
                word.text += ch
                i += 1
    flush()
    return [c for c in commands if c.words or c.redirs]


def base(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def resolve(words: list[Word]) -> tuple[str, list[Word], bool]:
    """Имя команды, её аргументы и признак пустого `env` (печать окружения)."""
    i = 0
    while i < len(words):
        text = words[i].text
        name = base(text)
        if ASSIGN_RE.match(text) or text in RESERVED:
            i += 1
        elif name in {"command", "builtin"} and (
            i + 1 < len(words) and words[i + 1].text in {"-v", "-V"}
        ):
            return "", [], False  # проверка наличия, ничего не выполняет
        elif name in WRAPPERS:
            with_value = WRAPPERS[name]
            i += 1
            while i < len(words):
                arg = words[i].text
                if name == "env" and ASSIGN_RE.match(arg):
                    i += 1
                elif arg.startswith("-") and len(arg) > 1:
                    i += 2 if arg in with_value else 1
                elif name == "timeout" and re.fullmatch(r"[0-9.]+[smhd]?", arg):
                    i += 1
                    break
                else:
                    break
            if name == "env" and i >= len(words):
                return "env", [], True
        else:
            return name, words[i + 1 :], False
    return "", [], False


def secret_name(name: str) -> bool:
    up = name.upper()
    return bool(re.search(r"PASS|TOKEN|SECRET|KEY", up)) or "PAT" in up.split("_")


def check_network(name: str, args: list[str]) -> str | None:
    if name in NETWORK_COMMANDS:
        return "network"
    if name == "systemctl" and any("awg-quick@" in a for a in args):
        return "network"
    if (
        name == "ip"
        and "link" in args
        and "set" in args
        and ("up" in args or "down" in args)
    ):
        return "network"
    return None


def kill_substitution(args: list[Word]) -> bool:
    """Аргументы kill содержат подстановку с pgrep или pidof."""
    for word in args:
        for inner in word.subs:
            for cmd in parse(inner):
                n, _, _ = resolve(cmd.words)
                if n in {"pgrep", "pidof"}:
                    return True
    return False


def check_kill(name: str, args: list[Word]) -> str | None:
    texts = [a.text for a in args]
    if name in KILL_COMMANDS:
        return "kill"
    if name == "fuser" and any(re.fullmatch(r"-[A-Za-z]*k[A-Za-z]*", t) for t in texts):
        return "kill"
    if name == "kill":
        if (
            len(texts) >= 2
            and texts[-1] == "-1"
            and (texts[-2].startswith("-") or "--" in texts)
        ):
            return "kill"
        if kill_substitution(args):
            return "kill"
    if name == "xargs":
        i = 0
        while i < len(texts) and texts[i].startswith("-"):
            i += 2 if texts[i] in XARGS_VALUE_FLAGS else 1
        if i < len(texts) and base(texts[i]) in KILL_COMMANDS | {"kill"}:
            return "kill"
    return None


def git_subcommand(texts: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(texts) and texts[i].startswith("-"):
        i += 2 if texts[i] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
    return (texts[i], texts[i + 1 :]) if i < len(texts) else ("", [])


def push_destination(spec: str) -> str:
    dest = spec.split(":", 1)[1] if ":" in spec else spec
    dest = dest.lstrip("+")
    return dest.removeprefix("refs/heads/")


def check_git(args: list[Word], cfg: Settings) -> str | None:
    sub, rest = git_subcommand([a.text for a in args])
    if sub == "stash":
        return "git-stash" if cfg.forbid_stash else None
    if sub == "add":
        if not cfg.forbid_add_all:
            return None
        for a in rest:
            if a in {"--all", "--update"}:
                return "git-add"
            if re.fullmatch(r"-[A-Za-z]*[Au][A-Za-z]*", a):
                return "git-add"
        return None
    if sub != "push":
        return None
    positional: list[str] = []
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif a in {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}:
            skip = True
        elif a.startswith("--force") or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", a):
            return "git-force"
        elif not a.startswith("-"):
            positional.append(a)
    refspecs = positional[1:]
    if any(r.startswith("+") for r in refspecs):
        return "git-force"
    if any(push_destination(r) in cfg.protected for r in refspecs):
        return "git-protected"
    return None


def check_herdr(texts: list[str]) -> str | None:
    pairs = {("tab", "create"), ("pane", "run"), ("pane", "send")}
    if any(pair in pairs for pair in itertools.pairwise(texts)):
        return "herdr"
    return None


def has_secret_dir(words: list[Word], cfg: Settings) -> bool:
    return any(p.search(w.text) for w in words for p in cfg.secret_res)


def in_secret_dir(text: str, cfg: Settings) -> bool:
    return any(p.search(text) for p in cfg.secret_res)


def check_rules(name: str, texts: list[str], cfg: Settings) -> str | None:
    """Свои правила проекта: слова идут подряд среди имени команды и аргументов.

    Слово совпадает с аргументом целиком или с последним звеном `модуль::рецепт`
    (рецепт just в модуле: `dev-stand::instances-down`)."""
    line = [name] + [t.rsplit("::", 1)[-1] for t in texts]
    for index, (words, _) in enumerate(cfg.rules):
        size = len(words)
        for start in range(len(line) - size + 1):
            if tuple(line[start : start + size]) == words:
                return f"rule-{index}"
    return None


def prints_nothing(name: str, texts: list[str]) -> bool:
    """Команда по ключам не выводит строки файла: счёт, имена, правка на месте."""
    short = [t for t in texts if re.fullmatch(r"-[A-Za-z]+.*", t)]
    if name in {"grep", "egrep", "fgrep", "rg"}:
        if any(t in GREP_QUIET_LONG for t in texts):
            return True
        return any(re.fullmatch(r"-[A-Za-z]*[clLq][A-Za-z]*", t) for t in short)
    if name == "sed":
        return "--in-place" in texts or any(
            t.startswith("--in-place=") or re.fullmatch(r"-[A-Za-z]*i.*", t)
            for t in short
        )
    return False


def check_secrets(
    name: str,
    args: list[Word],
    redirs: list[tuple[str, Word]],
    empty_env: bool,
    next_name: str | None,
    redirect_to_file: bool,
    stdin: list[str] | None = None,
    cfg: Settings | None = None,
) -> str | None:
    cfg = cfg or Settings()
    texts = [a.text for a in args]
    operands = [t for t in texts if not t.startswith("-")]
    redir_words = [w for op, w in redirs if op in {"<", "<<<"}]
    if (
        name in READERS
        and has_secret_dir(args + redir_words, cfg)
        and not prints_nothing(name, texts)
    ):
        return "secret-file"
    if name == "tee" and has_secret_dir(redir_words, cfg):
        return "secret-file"
    if PYTHON_RE.fullmatch(name):  # путь в переменной не виден: не блокируем
        code = [t for t in texts if in_secret_dir(t, cfg)] + [
            s for s in (stdin or []) if in_secret_dir(s, cfg)
        ]
        if (any(re.fullmatch(r"-[A-Za-z]*c", t) for t in texts) or stdin) and any(
            PYTHON_PRINT_RE.search(c) for c in code
        ):
            return "secret-file"
    if (
        name == "cp"
        and has_secret_dir(args, cfg)
        and (not operands or not in_secret_dir(operands[-1], cfg))
    ):
        return "secret-file"
    if (
        empty_env
        or name == "printenv"
        and (not operands or any(secret_name(t) for t in operands))
    ):
        return "secret-env"
    if name == "export" and (not texts or texts == ["-p"]):
        return "secret-env"
    if name in {"declare", "typeset"} and "-p" in texts:
        names = [t for t in texts if not t.startswith("-")]
        if not names or any(secret_name(t) for t in names):
            return "secret-env"
    if name == "set" and not texts:
        return "secret-env"
    if name in PRINTERS:
        shown = any(secret_name(v) for a in args for v in a.vars)
        goes_away = next_name is not None and next_name not in PIPE_PRINTERS
        if shown and not goes_away and not redirect_to_file:
            return "secret-echo"
    if name == "curl":
        for i, t in enumerate(texts):
            cred: Word | None = None
            if t in {"-u", "--user"} and i + 1 < len(args):
                cred = args[i + 1]
            elif t.startswith("--user=") or (
                t.startswith("-u") and not t.startswith("--") and len(t) > 2
            ):
                cred = args[i]
            if cred and cred.literal and re.search(r":.", cred.text):
                return "secret-literal"
    if name == "sshpass":
        i = 0
        while i < len(texts) and texts[i].startswith("-"):  # ключи самого sshpass
            t = texts[i]
            if t == "-p" and i + 1 < len(args) and args[i + 1].literal:
                return "secret-literal"
            if t.startswith("-p") and len(t) > 2 and args[i].literal:
                return "secret-literal"
            i += 2 if t in {"-p", "-f", "-d"} else 1
    return None


def inner_commands(name: str, args: list[Word]) -> list[str]:
    """Строки, которые команда сама выполнит: `bash -c`, `eval`, `tab-run.sh`."""
    texts = [a.text for a in args]
    if name in {"bash", "sh", "zsh", "dash"}:
        skip = False
        for i, t in enumerate(texts):
            if skip:
                skip = False
            elif t in {"-o", "-O", "--rcfile", "--init-file"}:
                skip = True
            elif re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", t) and i + 1 < len(texts):
                return [texts[i + 1]]
            elif not t.startswith("-"):
                if base(t) == "tab-run.sh":
                    return inner_commands("tab-run.sh", args[i + 1 :])
                break
    if name == "eval":
        return [" ".join(texts)]
    if name == "tab-run.sh" and "--" in texts:
        tail = texts[texts.index("--") + 1 :]
        if len(tail) == 1:
            return tail
        return [" ".join(shell_quote(t) for t in tail)] if tail else []
    return []


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def check(src: str, cfg: Settings | None = None, depth: int = 0) -> str | None:
    """Ключ из DENY (или `rule-N`), если команда запрещена, иначе None."""
    cfg = cfg or Settings()
    if depth > MAX_DEPTH:
        return None
    commands = parse(src)
    for index, cmd in enumerate(commands):
        for word in cmd.words + [w for _, w in cmd.redirs]:
            for inner in word.subs:
                found = check(inner, cfg, depth + 1)
                if found:
                    return found
        name, args, empty_env = resolve(cmd.words)
        if not name:
            continue
        texts = [a.text for a in args]
        next_name = None
        if cmd.pipe_out and index + 1 < len(commands):
            last = index + 1  # конец конвейера: до экрана доходит его вывод
            while commands[last].pipe_out and last + 1 < len(commands):
                last += 1
            next_name = resolve(commands[last].words)[0] or ""
        redirect_to_file = any(
            op.startswith((">", "&>"))
            and not op.startswith(">&")
            and not w.text.startswith("/dev/")
            for op, w in cmd.redirs
        )
        found = (
            (check_network(name, texts) if cfg.network else None)
            or check_kill(name, args)
            or (check_git(args, cfg) if name == "git" else None)
            or (check_herdr(texts) if name == "herdr" and cfg.herdr else None)
            or check_rules(name, texts, cfg)
            or check_secrets(
                name,
                args,
                cmd.redirs,
                empty_env,
                next_name,
                redirect_to_file,
                cmd.stdin,
                cfg,
            )
        )
        if found:
            return found
        inner_texts = inner_commands(name, args)
        if name in {"bash", "sh", "zsh", "dash"}:  # heredoc и here-string — их ввод
            inner_texts += cmd.stdin + [w.text for op, w in cmd.redirs if op == "<<<"]
        for inner in inner_texts:
            found = check(inner, cfg, depth + 1)
            if found:
                return found
    return None


def main() -> int:
    if os.environ.get("DUTY_ROLE") != "worker":
        return 0
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return 0
        tool_input = data.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return 0
        cfg = load_settings(
            data.get("cwd") if isinstance(data.get("cwd"), str) else None
        )
        found = check(command, cfg)
        if found:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": deny_text(found, cfg),
                        }
                    },
                    ensure_ascii=False,
                )
            )
    except Exception as error:  # noqa: BLE001 - сбой хука не должен мешать сессии
        print(f"forbid.py: {error!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
