#!/usr/bin/env python3
"""Конфигурация проекта для плагина duty: слои, поиск корня, проверка схемы.

Слои (позднее перекрывает раннее): встроенные значения по умолчанию →
`~/.config/duty/config.toml` (путь перекрывает `DUTY_CONFIG_USER`) →
`<корень>/.duty/duty.toml` → `<корень>/.duty/local.toml`. Таблицы сливаются
по ключам, списки заменяются целиком. Переменные `DUTY_RESULTS_DIR`,
`DUTY_BUSY_DIR`, `DUTY_SIGNALS_DIR` перекрывают пути `[paths]`,
`DUTY_SYMBOLS_FILE` — набор `symbols.tasks` (символ на строку).

Корень проекта — первый найденный: `--root`, `DUTY_PROJECT`,
`CLAUDE_PROJECT_DIR`, подъём от текущего каталога (`--cwd`) до каталога
с `.duty/duty.toml` не выше надпроекта git (из linked worktree — главное
рабочее дерево, `main_tree`, если в нём есть `.duty/duty.toml`, иначе сам
worktree); без `.duty/` корень — надпроект или верх git (общий режим), без git
корня нет. Относительные пути конфигурации отсчитываются от базы: главное
дерево для linked worktree, иначе корень.

Команды: root, get, json, env, check, init. Коды выхода: 0 — успех,
1 — находки `check` или отказ `init`, 2 — ошибка TOML или неверное обращение,
3 — нет корня или ключа. Владелец схемы — `reference/config.md`.
Без сторонних зависимостей, Python 3.11 и новее, запуск `python3 -I`.
"""

import argparse
import copy
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_MISSING = 3
SCHEMA = 1
DEFAULT_USER_FILE = "~/.config/duty/config.toml"
DEFAULT_STATE = "~/.local/state/duty"
PATH_KEYS = ("results", "busy", "signals", "sandbox")
ENV_PATHS = {
    "results": "DUTY_RESULTS_DIR",
    "busy": "DUTY_BUSY_DIR",
    "signals": "DUTY_SIGNALS_DIR",
}
# Ключи, которые допустимы только в пользовательском слое.
USER_ONLY = {"paths.state", "symbols.outside"}

# Типы значений схемы: str, int, bool, list (список строк) и записи вида
# ("tables", {поле: тип}) — массив таблиц, у которого обязательны все поля.
STR, INT, BOOL, LIST = "str", "int", "bool", "list"
SCHEMA_KEYS: dict[str, dict[str, Any]] = {
    "project": {"name": STR},
    "paths": {
        "results": STR,
        "busy": STR,
        "signals": STR,
        "sandbox": STR,
        "state": STR,
    },
    "git": {
        "protected": LIST,
        "working": LIST,
        "keep_prefixes": LIST,
        "task_prefixes": LIST,
        "named_dev_branches": BOOL,
        "default_parent": STR,
        "parent_key": STR,
        "forbid_add_all": BOOL,
        "forbid_stash": BOOL,
    },
    "secrets": {"dirs": LIST},
    "forbid": {
        "network": BOOL,
        "herdr_direct": BOOL,
        "rules": ("tables", {"words": LIST, "reason": STR}),
    },
    "bitbucket": {"url": STR, "token_file": STR, "user": STR, "sets_repo": STR},
    "jenkins": {
        "env_file": STR,
        "cli_url": STR,
        "insecure_tls": BOOL,
        "fake_job": STR,
        "nodes": LIST,
        "feed_markers": LIST,
    },
    "jira": {"url": STR, "token_file": STR, "ticket": STR},
    "docs": {
        "rules": LIST,
        "rules_optional": LIST,
        "review": STR,
        "decisions": STR,
        "decision_insert": STR,
        "decision_index": STR,
        "prose": STR,
        "lint": STR,
        "sweep_area": LIST,
        "sweep_exclude": LIST,
        "openspec": LIST,
        "post_change": ("tables", {"paths": LIST, "note": STR}),
    },
    "busy": {"resources": ("tables", {"name": STR, "why": STR, "until": STR})},
    "cleanup": {"keep_trees": LIST, "skip_trees": LIST, "docker_keep_projects": LIST},
    "stats": {"test_repos": LIST, "rules_paths": LIST, "pipeline_paths": LIST},
    "symbols": {"main": STR, "tasks": LIST, "outside": LIST},
}

# Ключи, обязательные при наличии таблицы: без них сервис не работает.
REQUIRED_KEYS = {
    "bitbucket": ("url", "token_file"),
    "jenkins": ("env_file",),
}

# Значения по умолчанию, не зависящие от корня. Таблиц bitbucket, jenkins
# и jira здесь нет: их отсутствие означает «сервис не подключён»;
# значения их необязательных ключей — в OPTIONAL_DEFAULTS.
DEFAULTS: dict[str, Any] = {
    "git": {
        "working": [],
        "keep_prefixes": ["backup/", "saved/"],
        "task_prefixes": [],
        "named_dev_branches": False,
        "parent_key": "parent",
        "forbid_add_all": True,
        "forbid_stash": True,
    },
    "secrets": {"dirs": []},
    "forbid": {"network": True, "herdr_direct": True, "rules": []},
    "docs": {
        "rules": [],
        "rules_optional": [],
        "review": "",
        "decisions": "",
        "decision_insert": "",
        "decision_index": "",
        "prose": "",
        "lint": "",
        "sweep_area": ["docs/", "*.md"],
        "sweep_exclude": [],
        "openspec": [],
        "post_change": [],
    },
    "busy": {"resources": []},
    "cleanup": {"keep_trees": [], "skip_trees": [], "docker_keep_projects": []},
    "stats": {
        "test_repos": [],
        "rules_paths": [".claude/"],
        "pipeline_paths": ["scripts/", ".github/"],
    },
    "symbols": {"main": "", "tasks": [], "outside": []},
}
OPTIONAL_DEFAULTS: dict[str, dict[str, Any]] = {
    "bitbucket": {"sets_repo": ""},
    "jenkins": {
        "cli_url": "",
        "insecure_tls": False,
        "fake_job": "",
        "nodes": [],
        "feed_markers": [],
    },
    "jira": {},
}


class ConfigError(Exception):
    """Файл слоя не читается или не разбирается как TOML."""


def _git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def git_bound(cwd: Path) -> Path | None:
    """Верхняя граница поиска: надпроект git, иначе верх рабочего дерева."""
    top = _git(cwd, "rev-parse", "--show-superproject-working-tree") or _git(
        cwd, "rev-parse", "--show-toplevel"
    )
    return Path(top).resolve() if top else None


def main_tree(path: Path) -> Path | None:
    """Главное рабочее дерево для linked worktree `path`; None — не worktree.

    Worktree проекта: общий каталог git `<дерево>/.git` — родитель и есть
    главное дерево. Worktree сабмодуля: общий каталог
    `<проект>/.git/modules/<путь>` — подъём до каталога с `.duty/duty.toml`,
    не выше `$HOME`.
    """
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    top = _git(path, "rev-parse", "--show-toplevel")
    if not common or not top:
        return None
    cpath = Path(common).resolve()
    if cpath.name == ".git":
        main = cpath.parent
        return main if main != Path(top).resolve() else None
    if ".git" in cpath.parts and "modules" in cpath.parts:
        home = Path.home().resolve()
        for d in cpath.parents:
            if d == home or d == d.parent:
                break
            if (d / ".duty" / "duty.toml").is_file():
                return d
    return None


def _has_duty(d: Path) -> bool:
    return (d / ".duty" / "duty.toml").is_file()


def _worktree_root(path: Path) -> Path:
    """Откуда читать конфигурацию для каталога `path`.

    Linked worktree: главное дерево, если в нём есть `.duty/duty.toml`,
    иначе верх самого worktree; не worktree — `path`.
    """
    main = main_tree(path)
    if main is None:
        return path
    if _has_duty(main):
        return main
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top and _has_duty(Path(top).resolve()) else main


def find_root(
    root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Корень проекта по порядку из докстринга модуля; None — корня нет."""
    if root:
        given = Path(root).expanduser().resolve()
        if given.is_dir():
            return _worktree_root(given)
        return given
    for env in ("DUTY_PROJECT", "CLAUDE_PROJECT_DIR"):
        if os.environ.get(env):
            return Path(os.environ[env]).expanduser().resolve()
    start = Path(cwd or os.getcwd()).resolve()
    if main_tree(start) is not None:
        return _worktree_root(start)
    bound = git_bound(start)
    if bound is None:
        return None
    for d in (start, *start.parents):
        if (d / ".duty" / "duty.toml").is_file():
            return d
        if d == bound:
            break
    return bound


def user_file() -> Path:
    return Path(os.environ.get("DUTY_CONFIG_USER") or DEFAULT_USER_FILE).expanduser()


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error


def merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Слияние слоёв: таблицы — по ключам, остальное (и списки) — замена."""
    out = copy.deepcopy(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def lookup(data: dict[str, Any], key: str) -> tuple[bool, Any]:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


GIT_HEAD_KEYS = ("git.protected", "git.default_parent")


def origin_head(root: Path | None) -> str:
    """Имя ветви origin/HEAD без префикса; пусто, если её нет."""
    if root is None:
        return ""
    ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return ref.removeprefix("origin/")


def _abs(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    return Path(os.path.normpath(p if p.is_absolute() else base / p))


def read_symbols(path: Path) -> list[str]:
    return [
        s
        for s in (
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        )
        if s
    ]


class Config:
    """Слитая конфигурация проекта.

    Атрибуты: root — каталог, откуда читаются файлы слоёв (None вне git),
    base — база относительных путей (главное дерево для linked worktree,
    иначе root), mode — "project" при
    `.duty/duty.toml`, иначе "general", name — имя проекта, files — прочитанные
    файлы слоёв по порядку, layers — пары (файл, данные) для проверки схемы,
    findings — значения неверного типа, замененные значениями по умолчанию.
    """

    def __init__(
        self,
        root: Path | None,
        base: Path | None,
        mode: str,
        name: str,
        data: dict[str, Any],
        files: list[Path],
        layers: list[tuple[Path, dict[str, Any]]],
        findings: list[str] | None = None,
    ) -> None:
        self.findings = findings or []
        self.root = root
        self.base = base
        self.mode = mode
        self.name = name
        self.data = data
        self.files = files
        self.layers = layers

    def get(self, key: str, default: Any = None) -> Any:
        found, value = lookup(self.data, key)
        return value if found else default

    def has(self, key: str) -> bool:
        return lookup(self.data, key)[0]

    def path_of(self, value: str, base: Path | None = None) -> Path:
        """Путь из конфигурации: `~` раскрывается, относительный — от base (корня)."""
        return _abs(value, base or self.root or Path.cwd())

    def path(self, key: str) -> Path | None:
        value = self.get(key)
        return Path(value) if isinstance(value, str) and value else None

    def as_json(self) -> dict[str, Any]:
        out = copy.deepcopy(self.data)
        out["_root"] = str(self.root) if self.root else None
        out["_base"] = str(self.base) if self.base else None
        out["_files"] = [str(f) for f in self.files]
        out["_mode"] = self.mode
        return out


def sanitize_layer(
    path: Path, layer: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Слой без значений, тип которых не совпал со схемой, и находки о них.

    Отброшенное значение заменяется значением по умолчанию при слиянии.
    Неизвестные таблицы и ключи остаются: о них сообщает `check`.
    """
    clean: dict[str, Any] = {}
    found: list[str] = []
    for section, body in layer.items():
        if section == "schema":
            continue
        spec = SCHEMA_KEYS.get(section)
        if spec is None:
            clean[section] = body
            continue
        if not isinstance(body, dict):
            found.append(
                f"{path}: {section}: ожидается таблица, получено {_type_name(body)}"
            )
            continue
        table: dict[str, Any] = {}
        for key, value in body.items():
            kind = spec.get(key)
            dotted = f"{section}.{key}"
            if kind is None:
                table[key] = value
            elif isinstance(kind, tuple):
                bad = _check_tables(path, dotted, kind[1], value)
                if bad:
                    found += bad
                else:
                    table[key] = value
            elif _type_ok(kind, value):
                table[key] = value
            else:
                found.append(
                    f"{path}: {dotted}: ожидается {_EXPECTED[kind]}, получено {_type_name(value)}"
                )
        clean[section] = table
    return clean, found


def load(
    root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Config:
    """Читает слои и собирает конфигурацию; ошибка TOML — ConfigError."""
    found = find_root(root, cwd)
    here = Path(cwd or os.getcwd()).resolve()
    layers: list[tuple[Path, dict[str, Any]]] = []
    candidates = [user_file()]
    if found is not None:
        candidates += [found / ".duty" / "duty.toml", found / ".duty" / "local.toml"]
    for f in candidates:
        if f.is_file():
            layers.append((f, read_toml(f)))
    mode = (
        "project"
        if found is not None and (found / ".duty" / "duty.toml").is_file()
        else "general"
    )

    findings: list[str] = []
    cleaned: list[dict[str, Any]] = []
    for path, layer in layers:
        # Значение неверного типа в слияние не идёт: действует умолчание,
        # находка остаётся в Config.findings и в выводе check.
        clean, bad = sanitize_layer(path, layer)
        findings += bad
        cleaned.append(clean)
    # Ветвь origin/HEAD нужна только как умолчание: git не вызывается,
    # если слои сами задают git.protected и git.default_parent.
    if all(any(lookup(c, key)[0] for c in cleaned) for key in GIT_HEAD_KEYS):
        head = ""
    else:
        head = origin_head(found)
    protected = ["main", "master"] + (
        [head] if head and head not in ("main", "master") else []
    )
    data = merge(
        DEFAULTS, {"git": {"protected": protected, "default_parent": head or "master"}}
    )
    for clean in cleaned:
        data = merge(data, clean)
    for section, extra in OPTIONAL_DEFAULTS.items():
        if isinstance(data.get(section), dict):
            data[section] = merge(extra, data[section])

    raw_name = data.get("project", {}).get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else (found or here).name
    data.setdefault("project", {})["name"] = name
    base = (main_tree(found) or found) if found else here
    paths = data.setdefault("paths", {})
    for key in [k for k, v in paths.items() if not isinstance(v, str)]:
        del paths[key]
    state = _abs(paths.get("state") or DEFAULT_STATE, base)
    paths["state"] = str(state)
    for key in ("results", "busy", "signals"):
        env = os.environ.get(ENV_PATHS[key])
        if env:
            paths[key] = str(Path(env).expanduser())
        elif paths.get(key):
            paths[key] = str(_abs(paths[key], base))
        else:
            paths[key] = str(state / name / key)
    sandbox = paths.get("sandbox")
    paths["sandbox"] = str(
        _abs(sandbox, base)
        if sandbox
        else Path(paths["results"]) / "duty-tests" / "sandbox"
    )

    symbols_file = os.environ.get("DUTY_SYMBOLS_FILE")
    if symbols_file:
        try:
            data["symbols"]["tasks"] = read_symbols(Path(symbols_file).expanduser())
        except OSError as error:
            raise ConfigError(f"{symbols_file}: {error}") from error
    return Config(
        found,
        base if found else None,
        mode,
        name,
        data,
        [f for f, _ in layers],
        layers,
        findings,
    )


# --- Проверка схемы ---------------------------------------------------------


def _type_name(value: Any) -> str:
    return {bool: "bool", int: "int", str: "str", list: "list", dict: "table"}.get(
        type(value), type(value).__name__
    )


def _type_ok(kind: str, value: Any) -> bool:
    if kind == STR:
        return isinstance(value, str)
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == BOOL:
        return isinstance(value, bool)
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


_EXPECTED = {STR: "строка", INT: "целое", BOOL: "true или false", LIST: "список строк"}


def check_layer(
    path: Path, layer: dict[str, Any], *, project_file: bool, user_layer: bool
) -> list[str]:
    """Находки одного файла слоя: версия, неизвестные ключи, типы, слой ключа."""
    out: list[str] = []
    schema = layer.get("schema")
    if schema is None:
        if project_file:
            out.append(f"{path}: нет ключа schema (ожидается schema = {SCHEMA})")
    elif schema != SCHEMA or isinstance(schema, bool):
        out.append(f"{path}: schema = {schema!r}, поддерживается {SCHEMA}")
    for section, body in layer.items():
        if section == "schema":
            continue
        spec = SCHEMA_KEYS.get(section)
        if spec is None:
            out.append(f"{path}: неизвестная таблица [{section}]")
            continue
        if not isinstance(body, dict):
            out.append(
                f"{path}: {section}: ожидается таблица, получено {_type_name(body)}"
            )
            continue
        for key, value in body.items():
            dotted = f"{section}.{key}"
            kind = spec.get(key)
            if kind is None:
                out.append(f"{path}: неизвестный ключ {dotted}")
                continue
            if dotted in USER_ONLY and not user_layer:
                out.append(
                    f"{path}: {dotted} допустим только в пользовательском слое ({DEFAULT_USER_FILE})"
                )
            if isinstance(kind, tuple):
                out += _check_tables(path, dotted, kind[1], value)
            elif not _type_ok(kind, value):
                out.append(
                    f"{path}: {dotted}: ожидается {_EXPECTED[kind]}, получено {_type_name(value)}"
                )
    return out


def _check_tables(
    path: Path, dotted: str, fields: dict[str, str], value: Any
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        return [
            f"{path}: {dotted}: ожидается массив таблиц [[{dotted}]], получено {_type_name(value)}"
        ]
    out: list[str] = []
    for i, item in enumerate(value, 1):
        where = f"[[{dotted}]] №{i}"
        for key in item:
            if key not in fields:
                out.append(f"{path}: {where}: неизвестный ключ {key}")
        for key, kind in fields.items():
            if key not in item:
                out.append(f"{path}: {where}: нет ключа {key}")
            elif not _type_ok(kind, item[key]):
                out.append(
                    f"{path}: {where}: {key}: ожидается {_EXPECTED[kind]}, получено {_type_name(item[key])}"
                )
    return out


def _origin(layers: list[tuple[Path, dict[str, Any]]], key: str) -> Path | None:
    """Последний слой, где задан ключ: ему и приписывается находка."""
    for path, layer in reversed(layers):
        if lookup(layer, key)[0]:
            return path
    return None


def check_config(cfg: Config) -> list[str]:
    """Все находки `check`: по файлам слоёв и по слитой конфигурации."""
    user = user_file()
    out: list[str] = []
    for path, layer in cfg.layers:
        out += check_layer(
            path,
            layer,
            project_file=path.name == "duty.toml" and path.parent.name == ".duty",
            user_layer=path == user,
        )
    for section, keys in REQUIRED_KEYS.items():
        src = _origin(cfg.layers, section)
        if src is None or not isinstance(cfg.get(section), dict):
            continue
        for key in keys:
            value = cfg.get(f"{section}.{key}")
            if not isinstance(value, str) or not value.strip():
                out.append(
                    f"{src}: {section}.{key}: обязателен при наличии [{section}]"
                )
    out += _check_symbols(cfg)
    return out


def check_notes(cfg: Config) -> list[str]:
    """Примечания `check`: не находки, код выхода не меняют (нет файла доступа)."""
    out: list[str] = []
    base = cfg.base or cfg.root or Path.cwd()
    for section, spec in SCHEMA_KEYS.items():
        for key in spec:
            if not key.endswith("_file"):
                continue
            dotted = f"{section}.{key}"
            src = _origin(cfg.layers, dotted)
            value = cfg.get(dotted)
            if (
                src is not None
                and isinstance(value, str)
                and value
                and not _abs(value, base).is_file()
            ):
                out.append(f"примечание: {src}: {dotted}: нет файла {value}")
    return out


def _check_symbols(cfg: Config) -> list[str]:
    out: list[str] = []
    seen: dict[str, str] = {}
    for set_name in ("main", "tasks", "outside"):
        dotted = f"symbols.{set_name}"
        src = _origin(cfg.layers, dotted)
        if src is None:
            continue
        value = cfg.get(dotted)
        items = (
            [value]
            if isinstance(value, str)
            else value
            if isinstance(value, list)
            else []
        )
        for sym in items:
            if not isinstance(sym, str) or sym == "":
                continue
            if len(sym) != 1:
                points = " ".join(f"U+{ord(c):04X}" for c in sym)
                out.append(
                    f"{src}: {dotted}: символ «{sym}» из {len(sym)} кодовых точек ({points}), нужна одна"
                )
                continue
            if sym in seen:
                where = (
                    "в том же наборе" if seen[sym] == dotted else f"уже в {seen[sym]}"
                )
                out.append(f"{src}: {dotted}: повтор символа «{sym}» ({where})")
                continue
            seen[sym] = dotted
    return out


# --- Шаблон init ------------------------------------------------------------

INIT_TEMPLATE = """\
# Конфигурация плагина duty для проекта; схема — reference/config.md плагина.
# Все ключи, кроме schema, необязательны; в комментарии — значение по умолчанию.
# Местные перекрытия машины — .duty/local.toml (в .gitignore).
schema = 1

[project]
name = {name}  # имя для подписей, статистики и каталога состояния

# [paths]  # относительные пути — от корня проекта, ~ раскрывается
# results = "../stand/results"  # каталоги задач (~/.local/state/duty/<имя>/results)
# busy = "../stand/busy"        # реестр занятости (~/.local/state/duty/<имя>/busy)
# signals = "../stand/signals"  # сигналы между задачами (~/.local/state/duty/<имя>/signals)
# sandbox = "../stand/results/duty-tests/sandbox"  # песочница проб (<results>/duty-tests/sandbox)

# [git]
# protected = ["main"]          # push запрещён, ветви не удаляются (main, master и origin/HEAD)
# working = []                  # рабочие ветви: не показываются в таблице ветвей, не удаляются
# keep_prefixes = ["backup/", "saved/"]  # сохранённые копии, уборка их не трогает
# task_prefixes = []            # префиксы своих ветвей задач
# named_dev_branches = false    # ветвь без префикса и «/» — именная ветвь разработчика
# default_parent = "main"       # родитель по умолчанию (origin/HEAD, иначе master)
# parent_key = "parent"         # ключ .gitmodules с родителем сабмодуля
# forbid_add_all = true         # хук отвергает git add -A и git add -u
# forbid_stash = true           # хук отвергает git stash

# [secrets]
# dirs = ["~/.config/project"]  # каталоги доступов: вывод их содержимого запрещён

# [forbid]
# network = true                # переключение туннеля и сети станции запрещено
# herdr_direct = true           # herdr tab create, pane run, pane send — только через tab-run.sh
# [[forbid.rules]]              # образец «слово за словом» среди аргументов команды
# words = ["down", "all"]
# reason = "почему команда запрещена и где правило"

# [bitbucket]                   # без таблицы навыки обходятся без Bitbucket
# url = "https://bitbucket.example"
# token_file = "~/.config/project/bitbucket-token"
# user = "login"
# sets_repo = ""                # репозиторий наборов ветвей

# [jenkins]                     # без таблицы навыки обходятся без Jenkins
# env_file = "~/.config/project/jenkins.env"  # JENKINS_URL, JENKINS_USER, JENKINS_TOKEN
# cli_url = ""                  # контроллер для CLI по WebSocket (пусто — JENKINS_URL)
# insecure_tls = false          # не проверять сертификат
# fake_job = ""                 # задания, исключаемые из статистики
# nodes = []                    # узлы для /duty:inspect
# feed_markers = []             # подстроки имён заданий-лент (ключ или имя статуса сборки)

# [jira]                        # без таблицы тикеты не ведутся
# url = "https://jira.example"
# token_file = "~/.config/project/jira-token"
# ticket = "PRJ-\\\\d+"           # образец ключа тикета в именах ветвей и заголовках

# [docs]
# rules = ["CLAUDE.md"]         # файлы rules-lint сверх файлов плагина и .duty/*.md
# rules_optional = []           # отсутствие файла — примечание, не находка
# review = ""                   # правило ревью
# decisions = ""                # каталог записей решений
# decision_insert = ""          # команда вставки записи
# decision_index = ""           # команда обновления указателя записей
# prose = ""                    # требования к тексту
# lint = ""                     # линтер документов
# sweep_area = ["docs/", "*.md"]  # область /duty:docs-sweep
# sweep_exclude = []            # исключения из области
# openspec = []                 # корни OpenSpec
# [[docs.post_change]]          # напоминание после правки путей
# paths = ["docs/"]
# note = "команда или действие"

# [[busy.resources]]            # ресурс реестра занятости
# name = "station"
# why = "почему ресурс не размножается"
# until = "условие снятия"

# [cleanup]
# keep_trees = []               # деревья, которые уборка не трогает
# skip_trees = []               # маски каталогов: worktree внутри не перечисляются
# docker_keep_projects = []     # проекты compose, чьи остановленные контейнеры не предлагаются

# [stats]                       # классы путей для «Неделя в цифрах»
# test_repos = []
# rules_paths = [".claude/"]
# pipeline_paths = ["scripts/", ".github/"]

# [symbols]                     # по одной кодовой точке, без повторов между наборами
# main = ""                     # постоянный символ главной сессии репозитория
# tasks = []                    # символы workspace задач (пусто — scripts/symbols.txt плагина)
"""


def init_text(name: str) -> str:
    return INIT_TEMPLATE.format(name=json.dumps(name, ensure_ascii=False))


# --- Командная строка -------------------------------------------------------


def scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def env_lines(cfg: Config, prefix: str) -> list[str]:
    lines = [
        f"export {prefix}ROOT={shlex.quote(str(cfg.root) if cfg.root else '')}",
        f"export {prefix}MODE={shlex.quote(cfg.mode)}",
    ]
    for section, body in cfg.data.items():
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            if isinstance(value, dict) or isinstance(
                SCHEMA_KEYS.get(section, {}).get(key), tuple
            ):
                continue
            if isinstance(value, list):
                if not all(isinstance(v, str) for v in value):
                    continue
                text = ",".join(value)
            else:
                text = scalar_text(value)
            lines.append(
                f"export {prefix}{section.upper()}_{key.upper()}={shlex.quote(text)}"
            )
    return lines


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="корень проекта")
    common.add_argument(
        "--cwd", default=argparse.SUPPRESS, help="каталог, от которого ищется корень"
    )
    p = argparse.ArgumentParser(
        prog="duty_config.py", description=__doc__.splitlines()[0], parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("root", parents=[common], help="корень проекта")
    g = sub.add_parser("get", parents=[common], help="значение ключа")
    g.add_argument("key")
    g.add_argument("--default")
    sub.add_parser("json", parents=[common], help="вся конфигурация")
    e = sub.add_parser("env", parents=[common], help="строки export для bash")
    e.add_argument("--prefix", default="DUTY_CFG_")
    sub.add_parser("check", parents=[common], help="проверка схемы")
    i = sub.add_parser("init", parents=[common], help="создать .duty/duty.toml")
    i.add_argument("--name")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else 0
    root, cwd = getattr(args, "root", None), getattr(args, "cwd", None)
    if args.cmd == "init":
        found = find_root(root, cwd)
        if found is None:
            print(
                "duty_config.py: корень проекта не найден (нет git и --root)",
                file=sys.stderr,
            )
            return EXIT_MISSING
        target = found / ".duty" / "duty.toml"
        if target.exists():
            print(f"duty_config.py: {target} уже есть", file=sys.stderr)
            return EXIT_FINDINGS
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(init_text(args.name or found.name), encoding="utf-8")
        print(target)
        return 0
    try:
        cfg = load(root, cwd)
    except ConfigError as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_USAGE
    if args.cmd == "root":
        if cfg.root is None:
            return EXIT_MISSING
        print(cfg.root)
    elif args.cmd == "get":
        if not cfg.has(args.key):
            if args.default is None:
                return EXIT_MISSING
            print(args.default)
        else:
            print(scalar_text(cfg.get(args.key)))
    elif args.cmd == "json":
        print(json.dumps(cfg.as_json(), ensure_ascii=False, indent=2))
    elif args.cmd == "env":
        print("\n".join(env_lines(cfg, args.prefix)))
    elif args.cmd == "check":
        findings = check_config(cfg)
        for line in findings + check_notes(cfg):
            print(line)
        return EXIT_FINDINGS if findings else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
