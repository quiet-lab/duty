#!/usr/bin/env python3
"""Запреты агента задачи в виде хука PreToolUse для Bash.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Правила, которые раньше жили только текстом (`reference/worker-rules.md`,
`agents/worker.md`, правила проекта), здесь отвергаются механизмом.
Перечни (защищённые ветви, каталоги секретов, свои правила) берутся из
конфигурации проекта (`scripts/duty_config.py`, `.duty/duty.toml`); без неё
действуют встроенные значения по умолчанию. Команда разбирается по словам и конвейерам, а не по подстрокам:
запрещённое слово внутри строкового литерала (сообщение коммита, аргумент
`grep`) команду не отвергает. Подстановки `$(…)`, обратные кавычки,
`bash -c`, `eval` и команда после `--` у `tab-run.sh` проверяются рекурсивно.

Запреты (названия — в `DENY`):

- `network` — переключение туннеля и сети станции; чтение состояния туннеля
  из перечня без пароля `~/.claude/CLAUDE.md`, «Повышение привилегий»
  (`NETWORK_READS`), `resolvectl status|query|statistics`, разрешено;
- `kill` — завершение процессов по образцу;
- `git` — `git add -A/-u`, подкоманды записи `git stash`, принудительный push,
  push в защищённые ветви (`git.protected`);
- `herdr` — вкладки и ввод в панели в обход `scripts/tab-run.sh`;
- `secret` — вывод содержимого каталогов секретов (`secrets.dirs`), печать
  окружения и секретов, пароль литералом в `curl -u` и `sshpass -p`;
- `rm` — ручной `rm -r` (и `-rf`, `-fr`, `-R`, `--recursive`) в `paths.results`,
  пока `dutylib.live_reason` каталога задачи или прогона непуста (общая
  проверка с навыком `cleanup`); уборка — `/duty:cleanup`;
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
from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_DEPTH = 4

DENY = {
    "network": (
        "Запрещено переключать туннель и сеть станции (nmcli, awg, wg, openvpn, "
        "amn-*, resolvectl, ip link set up/down, systemctl awg-quick@…): "
        "правила проекта (.duty/worker-rules.md) и ~/.claude/CLAUDE.md; "
        "переключает сам пользователь. Состояние читай через ip addr show, "
        "awg show amn0 endpoints|latest-handshakes, amn-zones-update --count, "
        "systemctl status awg-quick@amn0."
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
        "Запрещено менять стек git stash (git stash без подкоманды, push, save, "
        "pop, apply, drop, clear, branch, store): стек общий для всех worktree "
        "(правила проекта (.duty/worker-rules.md) и ~/.claude/CLAUDE.md); "
        "чтение (list, show) разрешено. "
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
    "rm-results": (
        "Запрещён ручной rm -r в каталоге результатов (paths.results), пока "
        "там есть живое: {live}. Уборка — /duty:cleanup (перечень с размерами, "
        "затем apply): навык проверяет то же признаки живого и не трогает "
        "идущие задачи, прогоны и открытые файлы процессов."
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
        "Запрещено печатать значение переменной, в имени которой есть часть "
        "(через _) PASS, PASSWORD, TOKEN, SECRET, PAT, KEY и подобные "
        "(echo, printf, print): rules.md задач «пароли и токены "
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
# Чтение состояния туннеля — перечень команд без пароля из ~/.claude/CLAUDE.md,
# «Повышение привилегий» (записка Q22: ложный отказ дороже пропуска); остальные
# вызовы команд NETWORK_COMMANDS отвергаются. Сравнение — по аргументам целиком.
NETWORK_READS = {
    ("awg", ("show", "amn0", "endpoints")),
    ("awg", ("show", "amn0", "latest-handshakes")),
    ("amn-zones-update", ("--count",)),
}
# Чтение resolvectl: подкоманды без изменения настроек и ключи со значением,
# которые стоят до подкоманды (форма `--ключ=значение` — одно слово).
RESOLVECTL_READS = {"status", "query", "statistics"}
RESOLVECTL_VALUE_FLAGS = {
    "-i",
    "--interface",
    "-t",
    "--type",
    "-c",
    "--class",
    "-p",
    "--protocol",
    "-H",
    "--host",
}
# Подкоманды git stash, которые меняют общий стек; `git stash` без подкоманды
# и с ключом вместо неё (`-m`, `-p`, `--`) — тот же push.
STASH_WRITES = {"push", "save", "pop", "apply", "drop", "clear", "branch", "store"}
# Части имени переменной (через «_»), по которым она считается секретной.
SECRET_PARTS = {
    "PASS",
    "PASSWD",
    "PASSWORD",
    "PASSWORDS",
    "PASSPHRASE",
    "TOKEN",
    "TOKENS",
    "SECRET",
    "SECRETS",
    "KEY",
    "KEYS",
    "APIKEY",
    "PAT",
    "SSHPASS",
    "PGPASS",
}
# Окончания слитных имён (PGPASSWORD, APITOKEN, dbPassword): у длинных слов
# ложных совпадений почти нет; короткие PASS и KEY окончанием не берутся
# (BYPASS, MONKEY).
SECRET_SUFFIXES = ("PASSWORD", "PASSWD", "PASSPHRASE", "TOKEN", "SECRET")
# Ключи systemctl со значением отдельным словом: значение — не глагол.
SYSTEMCTL_VALUE_FLAGS = {
    "-n",
    "-o",
    "-H",
    "-M",
    "-p",
    "-P",
    "-t",
    "-s",
    "--lines",
    "--output",
    "--host",
    "--machine",
    "--property",
    "--type",
    "--state",
    "--signal",
    "--kill-whom",
    "--root",
}
KILL_COMMANDS = {"pkill", "killall"}
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
# Команды, читающие файл и печатающие его строки: печатающие ввод (кроме tee,
# который читает только ввод) и читающие только файлы.
READERS = (PIPE_PRINTERS - {"tee"}) | {
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "gawk",
    "mawk",
    "bat",
    "strings",
    "hexdump",
    "tac",
    "nl",
    "cut",
}
PRINTERS = {"echo", "printf", "print"}
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
    "busybox": set(),
    "toybox": set(),
}
# Ключи GNU xargs (getopt «+0a:E:e::i::I:l::L:n:prP:xs:d:tv»): с обязательным
# значением — отдельным словом или слитно; с необязательным — только слитно.
XARGS_SHORT_VALUE = set("aEILnPsd")
XARGS_SHORT_OPTIONAL = set("eil")
# Длинные ключи: 0 — без значения, 1 — значение обязательно, 2 — только через `=`.
XARGS_LONG = {
    "null": 0,
    "arg-file": 1,
    "delimiter": 1,
    "eof": 2,
    "replace": 2,
    "max-lines": 2,
    "max-args": 1,
    "max-procs": 1,
    "max-chars": 1,
    "process-slot-var": 1,
    "interactive": 0,
    "no-run-if-empty": 0,
    "open-tty": 0,
    "verbose": 0,
    "exit": 0,
    "show-limits": 0,
    "help": 0,
    "version": 0,
}
# Команды, которые удаляют или опустошают свои аргументы.
DELETERS = {"rm", "unlink", "rmdir", "shred", "truncate"}
# Оболочки, интерпретаторы и запускатели: что они выполнят над целями find и xargs,
# до запуска неизвестно, поэтому под `-exec` и xargs они считаются удалением
# с неизвестной целью (fail-closed). Исключения — `shell_reads_only` и
# `python_checks_only`: проверка синтаксиса, тело `-c` из команд чтения, модули проверок.
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
# Оболочки, тело `-c` которых разбирается: синтаксис POSIX и bash. У zsh
# (квалификаторы шаблонов, флаг `(e)`), ksh и прочих литерал тела исполняется
# иначе, чем его видит `parse`, — их тело не разбирается.
PARSED_SHELLS = {"sh", "bash", "dash", "ash"}
# Короткие ключи таких оболочек после `-` (`o` берёт имя следующим словом);
# `-n` — разбор без исполнения, только с `--` после ключей: строки стандартного
# ввода xargs встают сразу за ключами, и без `--` строка `+n` снимает `-n`;
# ключи с `+` (`+n`, `+o noexec`) — отказ.
SHELL_FLAGS = set("ceuxvno")
SHELL_LONG = {"--norc", "--noprofile", "--posix"}
# Тело `-c` под find и xargs пропускается, только если каждая его простая команда —
# из перечня команд чтения: без записи, исполнения чужого кода и вычисления имён
# (перечень разрешённого, а не запрещённого, — ревью RP-2445: запрещённое у bash
# не перечислить). `for`, `fi`, `done` — слова управления; у printf, test и `[`
# ключ `-v` вычисляет имя переменной и запрещён.
BODY_COMMANDS = {
    "echo",
    "printf",
    "test",
    "[",
    "true",
    "false",
    ":",
    "for",
    "fi",
    "done",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "egrep",
    "fgrep",
    "cut",
    "tr",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "stat",
    "ls",
    "du",
    "cmp",
    "diff",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "b2sum",
    "cksum",
    "nl",
    "tac",
    "rev",
}
# Арифметика, `[[ ]]` и любые `${…}` (косвенная ссылка `${!1}`, `${1@P}`, срез,
# индекс): данные в них исполняются как код.
BODY_DATA_EVAL_RE = re.compile(r"\(\(|\[\[|\$\[|\$\{")
# `python -m <модуль>` под find и xargs: модули проверок, которые читают файлы
# и не удаляют их; ключи записи (`BODY_PY_WRITES`) — отказ.
PYTHON_CHECK_MODULES = {"py_compile", "tabnanny", "pyflakes", "pycodestyle", "mypy"}
PYTHON_FLAGS = set("BEIOPSbqsu")
# Ключи модулей проверок, которые пишут или правят файлы.
PY_WRITE_FLAGS = re.compile(
    r"-o|--output|--add-noqa|--fix|--unsafe-fixes|--watch|-w$|--junit-xml"
    r"|--cache-dir|--install-types|--[a-z-]*-report|--write"
)
INTERPRETER_RE = re.compile(
    r"(pypy|perl|ruby|node|nodejs|php|lua|luajit|tclsh|wish|julia|Rscript|deno|bun"
    r"|pwsh|osascript|awk|gawk|mawk|nawk)[0-9.]*"
)
LAUNCHERS = {
    "npx",
    "bunx",
    "npm",
    "pnpm",
    "yarn",
    "uv",
    "uvx",
    "pipx",
    "poetry",
    "pdm",
    "hatch",
    "make",
    "just",
}


# Команды, меняющие окружение следующих команд строки (`env_touched`).
ENV_SETTERS = {"export", "declare", "typeset", "readonly", "set", "source", ".", "eval"}
# Совет в отказе на оболочку под find и xargs (ключ `rm-results|…|sh`).
SHELL_HINT = (
    " Проверка синтаксиса под xargs и find — с «--» после -n: xargs -n1 bash -n -- "
    "(без «--» строка ввода +n снимает -n); тело -c — только из команд чтения "
    "(BODY_COMMANDS)."
)

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
    results: str = ""  # paths.results; пусто — проверка rm -r выключена
    cwd: str | None = (
        ""  # None: каталог после `cd` вычислить нельзя; каталог вызова хука: от него считаются относительные пути rm
    )
    # окружение изменено раньше в той же строке (export, присваивание, source):
    # исключения для оболочки и python -m под find и xargs не действуют
    env_touched: bool = False
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
    results = get("paths.results", "")
    return Settings(
        results=str(results) if results else "",
        protected=set(get("git.protected", ["main", "master"])),
        forbid_add_all=bool(get("git.forbid_add_all", True)),
        forbid_stash=bool(get("git.forbid_stash", True)),
        network=bool(get("forbid.network", True)),
        herdr=bool(get("forbid.herdr_direct", True)),
        secret_dirs=dirs,
        secret_res=[p for d in dirs for p in secret_patterns(d)],
        rules=rules,
    )


class ConfigUnreadable(Exception):
    """Конфигурация не читается: хук закрывается, а не открывается."""


def load_settings(cwd: str | None = None) -> Settings:
    """Конфигурация загружается один раз на вызов хука.

    Ошибка TOML — ConfigUnreadable (вызов отвергается); прочий сбой загрузки —
    значения по умолчанию. Значения неверного типа заменены умолчаниями, их
    перечень печатается в stderr один раз.
    """
    try:
        path = PLUGIN / "scripts" / "duty_config.py"
        spec = importlib.util.spec_from_file_location("duty_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("duty_config", module)
        spec.loader.exec_module(module)
        cfg = module.load(root=None, cwd=cwd or os.getcwd())
        if cfg.findings:
            print(
                "forbid.py: значения конфигурации неверного типа заменены "
                "умолчаниями:\n" + "\n".join(cfg.findings),
                file=sys.stderr,
            )
        settings = settings_from_config(cfg)
        settings.cwd = cwd or os.getcwd()
        return settings
    except Exception as error:
        if type(error).__name__ == "ConfigError":
            raise ConfigUnreadable(str(error)) from error
        print(
            f"forbid.py: конфигурация не загружена, значения по умолчанию: {error!r}",
            file=sys.stderr,
        )
        return Settings()


def deny_text(key: str, cfg: Settings) -> str:
    """Текст отказа: подстановка актуальных перечней из конфигурации."""
    if key.startswith("rm-results|"):
        live = key.partition("|")[2]
        hint = SHELL_HINT if live.endswith("|sh") else ""
        live = live.removesuffix("|sh")
        return DENY["rm-results"].replace("{live}", live) + hint
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
    """Имя секретное, если часть через «_» — из SECRET_PARTS или кончается SECRET_SUFFIXES.

    Подстрока не годится: `PASSED`, `MONKEY`, `BYPASS_FLAG` давали ложный отказ.
    """
    return any(
        part in SECRET_PARTS or part.endswith(SECRET_SUFFIXES)
        for part in name.upper().split("_")
    )


def systemctl_status(args: list[str]) -> bool:
    """`systemctl [ключи] status …` — чтение состояния службы."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in SYSTEMCTL_VALUE_FLAGS else 1
    return i < len(args) and args[i] == "status"


def resolvectl_read(args: list[str]) -> bool:
    """`resolvectl [ключи] status|query|statistics …` — чтение состояния DNS.

    Решение пользователя (RP-2233, комментарий 16832): `resolvectl` запускается
    напрямую без sudo, чтение разрешено; `dns`, `domain`, `revert`, `flush-caches`
    и прочие подкоманды остаются запрещёнными.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in RESOLVECTL_VALUE_FLAGS else 1
    return i < len(args) and args[i] in RESOLVECTL_READS


def check_network(name: str, args: list[str]) -> str | None:
    if (name, tuple(args)) in NETWORK_READS:
        return None
    if name == "resolvectl" and resolvectl_read(args):
        return None
    if name in NETWORK_COMMANDS:
        return "network"
    if (
        name == "systemctl"
        and any("awg-quick@" in a for a in args)
        and not systemctl_status(args)
    ):
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
    if (
        name == "xargs"
        and (tail := xargs_command(args)[0])
        and resolve(tail)[0] in KILL_COMMANDS | {"kill"}
    ):
        return "kill"
    return None


def _dutylib() -> object:
    """Модуль `scripts/dutylib.py` (под `python3 -I` каталог сценария не в sys.path)."""
    module = sys.modules.get("dutylib")
    if module is None:
        path = PLUGIN / "scripts" / "dutylib.py"
        spec = importlib.util.spec_from_file_location("dutylib", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules["dutylib"] = module
        spec.loader.exec_module(module)
    return module


def rm_recursive(texts: list[str]) -> bool:
    """В аргументах rm есть рекурсивный ключ: -r, -R, --recursive, в любой связке коротких."""
    for t in texts:
        if t == "--":
            break
        if t == "--recursive" or re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", t):
            return True
    return False


def rm_targets(args: list[Word], cwd: str | None) -> list[Path]:
    """Пути-цели rm без ключей: `~` и `$HOME` раскрыты, шаблон усечён до каталога перед ним.

    Слово с прочей подстановкой пропускается: его значение до запуска неизвестно.
    Относительная цель при неизвестном `cwd` (None) даёт корень `/`: он предок любого
    каталога результатов, то есть проверяются все живые каталоги.
    """
    out: list[Path] = []
    options = True
    for word in args:
        text = word.text
        if options and text == "--":
            options = False
        elif options and text.startswith("-") and len(text) > 1:
            continue
        elif not word.subs and set(word.vars) <= {"HOME"}:
            text = os.path.expanduser(text)
            text = text.replace("${HOME}", str(Path.home())).replace(
                "$HOME", str(Path.home())
            )
            parts = []
            for part in text.split("/"):
                if re.search(r"[*?\[]", part):
                    break
                parts.append(part)
            rel = "/".join(parts) or "."
            if cwd is None and not rel.startswith("/"):
                out.append(Path("/"))
                continue
            joined = os.path.join(cwd or "/", rel)
            out.append(Path(os.path.normpath(joined)))
    return out


FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def xargs_command(args: list[Word]) -> tuple[list[Word] | None, set[str]]:
    """Команда xargs с её аргументами после ключей (None — команды нет, `echo`)
    и строки замены (`{}` и заданные `-I`, `-i`, `--replace`).

    Ключи разбираются как getopt GNU xargs: связки коротких (`-0n 1`), слитные
    значения (`-n1`, `--max-args=1`), длинные ключи и их однозначные сокращения.
    """
    holders = {"{}"}
    i = 0
    while i < len(args):
        text = args[i].text
        if text == "--":
            i += 1
            break
        if text.startswith("--"):
            name, eq, value = text[2:].partition("=")
            found = [k for k in XARGS_LONG if k == name] or [
                k for k in XARGS_LONG if k.startswith(name)
            ]
            kind = XARGS_LONG[found[0]] if len(found) == 1 else 0
            if found == ["replace"] and value:
                holders.add(value)
            i += 2 if kind == 1 and not eq else 1
        elif text.startswith("-") and len(text) > 1:
            i += 1
            for j, ch in enumerate(text[1:], start=2):
                if ch in XARGS_SHORT_VALUE:
                    if j == len(text):  # значение — следующее слово
                        if ch == "I" and i < len(args):
                            holders.add(args[i].text)
                        i += 1
                    elif ch == "I":
                        holders.add(text[j:])
                    break
                if ch in XARGS_SHORT_OPTIONAL:
                    if ch == "i" and text[j:]:
                        holders.add(text[j:])
                    break
        else:
            break
    holders.discard("")
    return (args[i:] if i < len(args) else None), holders


ANYWHERE = Path("/")  # цель неизвестна: проверяются все живые каталоги результатов


def word_paths(words: list[Word], holders: set[str], cwd: str | None) -> list[Path]:
    """Явные цели из аргументов: без слов со строкой замены (их цели — сами пути find
    или стандартный ввод xargs)."""
    return rm_targets([w for w in words if not any(h in w.text for h in holders)], cwd)


def deletion_targets(
    words: list[Word],
    cwd: str | None,
    holders: set[str],
    from_stdin: bool,
    depth: int = 0,
    clean: bool = True,
) -> list[Path] | None:
    """Цели, которые удалит команда под `find -exec` или xargs; None — не удаляет.

    `clean` — перед find или xargs нет присваиваний и `env` (`env_taints`): иначе
    исключения для оболочки и python -m не действуют.

    Обёртки (`env`, `sudo`, `nice`, `timeout`, `command`, `busybox` …) снимаются.
    Удаление — команды `DELETERS`, find с удалением, xargs с удаляющей командой,
    `git rm`/`git clean`, rsync с удалением источника или приёмника; их явные
    цели возвращаются, вызывающий добавляет свои (пути find, корень для xargs).
    Оболочка, интерпретатор и запускатель считаются удалением целей вызывающего
    (fail-closed), кроме `shell_reads_only` (`-n`, тело `-c` из команд чтения) и
    `python_checks_only` (модуль проверки); `env -S` даёт корень `/`.
    Имя команды, неизвестное до запуска (подстановка, шаблон, строка замены,
    имя со стандартного ввода xargs после обёртки без команды), даёт корень `/`.
    """
    if depth > MAX_DEPTH:
        return [ANYWHERE]
    name, args, empty_env = resolve(words)
    if not name or empty_env:
        # обёртка без команды: под xargs имя придёт со стандартного ввода
        wrapped = bool(words) and base(words[0].text) in WRAPPERS
        return [ANYWHERE] if from_stdin and wrapped else None
    command = words[len(words) - len(args) - 1]
    if (
        not command.literal
        or command.text != "["
        and re.search(r"[$*?\[{]", command.text)  # `$1`, шаблон, `{rm,-rf,x}`
        or any(h in command.text for h in holders)
    ):
        return [ANYWHERE]
    texts = [a.text for a in args]
    prefix = words[: len(words) - len(args) - 1]
    if env_split(prefix):  # `env -S 'строка'`: настоящая команда — в строке
        return [ANYWHERE]
    clean = clean and not env_taints(prefix)
    if name in SHELLS:  # исключения — только без обёрток и присваиваний впереди
        return (
            None
            if clean and not prefix and shell_reads_only(name, args, holders, depth)
            else []
        )
    if PYTHON_RE.fullmatch(name):
        safe = clean and not prefix and python_checks_only(args, from_stdin, holders)
        return None if safe else []
    if name in LAUNCHERS or INTERPRETER_RE.fullmatch(name):
        return []
    if (
        name in DELETERS
        or name == "git"
        and git_subcommand(texts)[0] in {"rm", "clean"}
        or name == "rsync"
        and any(t.startswith(("--remove-source-files", "--delete")) for t in texts)
    ):
        return word_paths(args, holders, cwd)
    if name == "find":
        return find_targets(args, cwd, depth + 1, clean)
    if name == "xargs":
        tail, inner = xargs_command(args)
        if (
            tail is None
            or deletion_targets(tail, cwd, inner, True, depth + 1, clean) is None
        ):
            return None
        return [ANYWHERE]
    return None


def env_taints(prefix: list[Word]) -> bool:
    """Перед командой — присваивание или `env`: окружение (BASH_ENV, PYTHONPATH …)
    задаёт строка, и оболочка или python исполнит по нему чужой файл."""
    return any(ASSIGN_RE.match(w.text) or base(w.text) == "env" for w in prefix)


def env_split(prefix: list[Word]) -> bool:
    """Среди обёрток перед командой — `env -S`/`--split-string` (строка — команда)."""
    after_env = False
    for word in prefix:
        if base(word.text) == "env":
            after_env = True
        elif after_env and re.match(r"-[A-Za-z]*S|--sp", word.text):
            return True
    return False


def shell_reads_only(
    name: str, args: list[Word], holders: set[str], depth: int
) -> bool:
    """Оболочка под `find -exec` и xargs ничего не удаляет: `-n` (разбор без
    исполнения) или тело `-c` из команд чтения (`body_reads_only`).

    Только `PARSED_SHELLS` и ключи `SHELL_FLAGS`; `-n` — только с `--` после
    ключей. Файл сценария, стандартный ввод, тело из подстановки, неизвестный
    ключ — False (удаление целей вызывающего).
    """
    if name not in PARSED_SHELLS:
        return False
    flags, i, ended, options = shell_options(args)
    for word in options:
        text = word.text
        if not word.literal or text.startswith("+"):
            return False
        if text.startswith("--"):
            if text not in SHELL_LONG:
                return False
        elif not set(text[1:]) <= SHELL_FLAGS:
            return False
    if "n" in flags:
        return ended
    if "c" not in flags or i >= len(args) or not args[i].literal:
        return False
    return body_reads_only(args[i].text, holders, depth + 1)


def body_reads_only(body: str, holders: set[str], depth: int) -> bool:
    """Тело `sh -c` под `find -exec` и xargs состоит из команд чтения.

    Каждая простая команда — из `BODY_COMMANDS` литералом в начале (без обёрток
    и присваиваний), перенаправления — только ввод и `/dev/null`, без heredoc;
    подстановки проверяются так же. Строка замены в теле — код из данных;
    `${…}`, арифметика и `[[ ]]` — отказ.
    """
    if depth > MAX_DEPTH or any(h in body for h in holders):
        return False
    if BODY_DATA_EVAL_RE.search(body):
        return False
    for cmd in parse(body):
        if cmd.stdin:
            return False
        for op, target in cmd.redirs:
            if op not in {"<", "<<<"} and target.text != "/dev/null":
                return False
        for word in cmd.words + [w for _, w in cmd.redirs]:
            if not all(body_reads_only(inner, set(), depth + 1) for inner in word.subs):
                return False
        words = [w for w in cmd.words if w.text not in RESERVED]
        if not words:
            continue
        head = words[0]
        if not head.literal or "$" in head.text or head.text not in BODY_COMMANDS:
            return False
        if head.text == "printf" and any(w.text.startswith("-v") for w in words[1:]):
            return False  # и слитный -vИМЯ
        if head.text in {"test", "["} and any(w.text == "-v" for w in words[1:]):
            return False
    return True


def python_checks_only(args: list[Word], from_stdin: bool, holders: set[str]) -> bool:
    """`python -m <модуль проверки>` (`PYTHON_CHECK_MODULES`, `ruff check`,
    `ruff format --check|--diff`) без ключей записи `PY_WRITE_FLAGS`.

    Под find (`-execdir` запускает в каталоге найденного) нужен `-I` или `-P`:
    без них модуль ищется и в текущем каталоге. Под xargs нужен `--` после ключей
    модуля, и строка замены — только после него: строки ввода иначе встают
    ключами модуля (`--config-file=…`, `--python-executable=…`).
    """
    texts = [a.text for a in args]
    flags = ""
    i = 0
    while i < len(texts) and texts[i] != "-m":
        text = texts[i]
        if text in {"-W", "-X"}:
            i += 2
        elif re.fullmatch(r"-[WX].+", text):
            i += 1
        elif re.fullmatch(r"-[A-Za-z]+", text) and set(text[1:]) <= PYTHON_FLAGS:
            flags += text[1:]
            i += 1
        else:
            return False
    if i + 1 >= len(texts) or not all(a.literal for a in args):
        return False
    if not from_stdin and not set(flags) & {"I", "P"}:
        return False
    module, rest = texts[i + 1], texts[i + 2 :]
    if from_stdin:
        if "--" not in rest:
            return False
        rest = rest[: rest.index("--")]
        if any(h in t for t in rest for h in holders):
            return False
    if any(PY_WRITE_FLAGS.match(t) for t in rest):
        return False
    if module in PYTHON_CHECK_MODULES:
        return True
    if module != "ruff" or not rest:
        return False
    return (
        rest[0] == "check"
        or rest[0] == "format"
        and ("--check" in rest or "--diff" in rest)
    )


def find_exec_bodies(args: list[Word]) -> list[list[Word]]:
    """Команды `-exec`, `-execdir`, `-ok`, `-okdir` find; конец — `;` или `+` после `{}`."""
    texts = [a.text for a in args]
    bodies = []
    for i, t in enumerate(texts):
        if t in FIND_EXEC:
            end = i + 1
            while end < len(texts) and not (
                texts[end] == ";" or texts[end] == "+" and texts[end - 1] == "{}"
            ):
                end += 1
            bodies.append(args[i + 1 : end])
    return bodies


def find_targets(
    args: list[Word], cwd: str | None, depth: int = 0, clean: bool = True
) -> list[Path] | None:
    """Цели удаления `find`: начальные пути и явные цели тела `-exec`; None — не удаляет.

    Удаление — `-delete` и `-exec`/`-execdir`/`-ok`/`-okdir` с командой, для
    которой `deletion_targets` не None. Ключи `-H`, `-L`, `-P`, `-D…`, `-O…` перед
    путями пропускаются; без путей начальный каталог — текущий.
    """
    texts = [a.text for a in args]
    deletes = "-delete" in texts
    extra: list[Path] = []
    for body in find_exec_bodies(args):
        found = deletion_targets(body, cwd, {"{}"}, False, depth, clean)
        if found is not None:
            deletes = True
            extra += found
    if not deletes:
        return None
    i = 0
    while i < len(texts) and re.fullmatch(r"-([HLP]|[DO]\w*)", texts[i]):
        i += 1
    starts: list[Word] = []
    while i < len(texts) and not (
        texts[i].startswith(("-", "(", "\\(")) or texts[i] in {"!", "\\!"}
    ):
        starts.append(args[i])
        i += 1
    return (rm_targets(starts, cwd) or rm_targets([Word(text=".")], cwd)) + extra


def check_rm(
    name: str, args: list[Word], cfg: Settings, prefix: list[Word] | None = None
) -> str | None:
    """Удаление внутри `paths.results` при живом каталоге: ключ `rm-results|<каталог>: <причина>`.

    Удаляют `rm -r`, `find … -delete`, `find … -exec <удаление>` и
    `xargs <удаление>` (`deletion_targets`). `find` обходит дерево, а цели `xargs`
    приходят со стандартного ввода, поэтому под ними удалением считается любой
    `rm`, не только рекурсивный. Цели `xargs` неизвестны и считаются корнем `/`
    (проверяются все живые каталоги результатов). `prefix` — слова перед
    командой: присваивания и `env` там (или раньше в строке, `cfg.env_touched`)
    снимают исключения для оболочки и python -m. Отказ на оболочку под find
    и xargs несёт совет `SHELL_HINT` (суффикс ключа `|sh`).
    """
    if not cfg.results:
        return None
    clean = not cfg.env_touched and not env_taints(prefix or [])
    bodies: list[list[Word]] = []
    if name == "rm" and rm_recursive([a.text for a in args]):
        targets = rm_targets(args, cfg.cwd)
    elif (
        name == "find" and (found := find_targets(args, cfg.cwd, 0, clean)) is not None
    ):
        targets = found
        bodies = find_exec_bodies(args)
    elif name == "xargs" and (tail_holders := xargs_command(args))[0] is not None:
        tail, holders = tail_holders
        if deletion_targets(tail, cfg.cwd, holders, True, 0, clean) is None:
            return None
        targets = [ANYWHERE]
        bodies = [tail]
    else:
        return None
    hint = "|sh" if any(resolve(b)[0] in SHELLS for b in bodies) else ""
    results = Path(os.path.realpath(cfg.results))
    lib = _dutylib()
    opened = None
    for target in targets:
        target = Path(os.path.realpath(target))
        if target.is_relative_to(results) and target != results:
            tops = [results / target.relative_to(results).parts[0]]
        elif results.is_relative_to(target):  # сам каталог результатов или его предок
            tops = (
                sorted(p for p in results.iterdir() if p.is_dir())
                if results.is_dir()
                else []
            )
        else:
            continue
        if opened is None:  # обход /proc — только для цели внутри результатов
            opened = lib.open_paths()  # type: ignore[attr-defined]
        for top in tops:
            why = lib.live_reason(top.resolve(), opened)  # type: ignore[attr-defined]
            if why:
                return f"rm-results|{top}: {why}{hint}"
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
        if not cfg.forbid_stash:
            return None
        if not rest or rest[0].startswith("-") or rest[0] in STASH_WRITES:
            return "git-stash"
        return None
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


# Длинные ключи grep и rg без значения; прочие длинные ключи без «=» могут
# брать значение из следующего слова (в том числе сокращённые), и оно не ключ.
GREP_PLAIN_LONG = GREP_QUIET_LONG | {
    "--recursive",
    "--dereference-recursive",
    "--line-number",
    "--ignore-case",
    "--no-ignore-case",
    "--no-messages",
    "--extended-regexp",
    "--fixed-strings",
    "--perl-regexp",
    "--invert-match",
    "--word-regexp",
    "--line-regexp",
    "--with-filename",
    "--no-filename",
    "--only-matching",
    "--null",
    "--text",
    "--byte-offset",
    "--initial-tab",
    "--null-data",
    "--unbuffered",
    "--no-heading",
    "--hidden",
}
# Короткие ключи, берущие значение (остаток связки или следующее слово).
GREP_VALUE_SHORT = "efmABCdD"
RG_VALUE_SHORT = "efmABCdDgtTrMjE"
# Запись sed в стандартный вывод: команда или флаг `w`/`W` в любой позиции.
SED_WRITE_STDOUT = re.compile(
    r"[wW][ \t]*(?:/dev/(?:stdout|stderr|tty|fd/[12])|/proc/[^/\s]+/fd/[12])"
)


def grep_flags(texts: list[str], value_short: str = GREP_VALUE_SHORT) -> list[str]:
    """Ключи grep без значений ключей и без операндов после `--`; результат — `-x` по одному."""
    flags: list[str] = []
    skip = False
    for t in texts:
        if skip:
            skip = False
        elif t == "--":
            break
        elif t.startswith("--"):
            if "=" in t:
                continue
            if t in GREP_PLAIN_LONG:
                flags.append(t)
            else:
                skip = True
        elif re.fullmatch(r"-[A-Za-z0-9]+", t):
            for i, c in enumerate(t[1:], 1):
                if c in value_short:
                    skip = i == len(t) - 1
                    break
                flags.append("-" + c)
    return flags


def prints_nothing(name: str, texts: list[str]) -> bool:
    """Команда по ключам не выводит строки файла: счёт, имена, правка на месте."""
    short = [t for t in texts if re.fullmatch(r"-[A-Za-z]+.*", t)]
    if name in {"grep", "egrep", "fgrep", "rg"}:
        flags = grep_flags(texts, RG_VALUE_SHORT if name == "rg" else GREP_VALUE_SHORT)
        return any(t in GREP_QUIET_LONG or t in {"-c", "-l", "-L", "-q"} for t in flags)
    if name == "sed":
        if any(SED_WRITE_STDOUT.search(t) for t in texts):
            return False
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


SHELL_VALUE_LONG = {"--rcfile", "--init-file"}


def shell_options(args: list[Word]) -> tuple[set[str], int, bool, list[Word]]:
    """Ключи оболочки по правилам bash: буквы ключей с `-`, индекс первого
    операнда (тело при `-c`), признак конца ключей `--`/`-` и слова ключей
    без их значений.

    `-c` может стоять среди ключей: тело — первый операнд после всех ключей;
    каждая буква `o`/`O` берёт имя следующим словом, `--rcfile`/`--init-file` — значение.
    """
    flags: set[str] = set()
    words: list[Word] = []
    i = 0
    while i < len(args):
        text = args[i].text
        if text in {"--", "-"}:
            return flags, i + 1, True, words
        if text.startswith("--"):
            words.append(args[i])
            i += 2 if text in SHELL_VALUE_LONG else 1
        elif re.fullmatch(r"[-+][A-Za-z]+", text):
            words.append(args[i])
            if text[0] == "-":
                flags |= set(text[1:])
            i += 1 + sum(ch in "oO" for ch in text[1:])
        else:
            break
    return flags, i, False, words


def inner_commands(name: str, args: list[Word]) -> list[str]:
    """Строки, которые команда сама выполнит: `bash -c`, `eval`, `tab-run.sh`."""
    texts = [a.text for a in args]
    if name in {"bash", "sh", "zsh", "dash"}:
        flags, i, _, _ = shell_options(args)
        if i < len(texts):
            if "c" in flags:
                return [texts[i]]
            if base(texts[i]) == "tab-run.sh":
                return inner_commands("tab-run.sh", args[i + 1 :])
    if name == "eval":
        return [" ".join(texts)]
    if name == "tab-run.sh" and "--" in texts:
        tail = texts[texts.index("--") + 1 :]
        if len(tail) == 1:
            return tail
        return [" ".join(shell_quote(t) for t in tail)] if tail else []
    return []


def next_cwd(name: str, args: list[Word], cwd: str | None) -> str | None:
    """Каталог после `cd`/`pushd`/`popd`; None, если его вычислить нельзя."""
    if name == "popd":
        return None
    words = [a for a in args if not (a.text.startswith("-") and len(a.text) > 1)]
    if not words:
        return None if name == "pushd" else str(Path.home())
    word = words[0]
    if word.subs or not set(word.vars) <= {"HOME"} or word.text == "-":
        return None
    text = os.path.expanduser(word.text)
    text = text.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    if text.startswith("/"):
        return os.path.normpath(text)
    if cwd is None:
        return None
    return os.path.normpath(os.path.join(cwd or "/", text))


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def check(src: str, cfg: Settings | None = None, depth: int = 0) -> str | None:
    """Ключ из DENY (или `rule-N`), если команда запрещена, иначе None."""
    cfg = cfg or Settings()
    if depth > MAX_DEPTH:
        return None
    commands = parse(src)
    cwd = cfg.cwd
    env_touched = cfg.env_touched
    for index, cmd in enumerate(commands):
        cfg = replace(cfg, cwd=cwd, env_touched=env_touched)
        for word in cmd.words + [w for _, w in cmd.redirs]:
            for inner in word.subs:
                found = check(inner, cfg, depth + 1)
                if found:
                    return found
        name, args, empty_env = resolve(cmd.words)
        prefix = cmd.words[: len(cmd.words) - len(args) - 1] if name else cmd.words
        if (
            name in ENV_SETTERS
            or not name
            and any(ASSIGN_RE.match(w.text) for w in prefix)
        ):
            env_touched = True  # действует на следующие команды строки
        if not name:
            continue
        texts = [a.text for a in args]
        if name in {"cd", "pushd", "popd"} and not cmd.pipe_out:
            cwd = next_cwd(name, args, cwd)
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
            or check_rm(name, args, cfg, prefix)
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
        if name == "find":  # тело -exec — команда со своими запретами
            inner_texts += [
                " ".join(shell_quote(w.text) for w in body)
                for body in find_exec_bodies(args)
                if body
            ]
        if name == "xargs" and (tail := xargs_command(args)[0]):
            # команда xargs — со своими запретами, как тело -exec find
            inner_texts.append(" ".join(shell_quote(w.text) for w in tail))
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
        try:
            cfg = load_settings(
                data.get("cwd") if isinstance(data.get("cwd"), str) else None
            )
        except ConfigUnreadable as error:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"конфигурация duty не читается: {error}; "
                                "исправьте и повторите"
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
            return 0
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
