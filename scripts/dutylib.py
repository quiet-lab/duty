"""Общие помощники сценариев плагина duty и расширений .duty/*.d/.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Сценарии плагина запускаются с `python3 -I`, поэтому модуль импортируется
по пути, как `gates` в `branches.py`:

    _spec = importlib.util.spec_from_file_location("dutylib", Path(__file__).with_name("dutylib.py"))
    dutylib = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(dutylib)

или помощником `import_path` из уже загруженного модуля. Без сторонних
зависимостей.
"""

import importlib.util
import json
import os
import pwd
import re
import signal
import ssl
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = Path(__file__).resolve().parents[1]
EXTENSION_KINDS = ("facts", "cleanup")


def import_path(name: str, path: Path) -> ModuleType:
    """Импорт модуля из файла: под `python3 -I` каталог сценария не в sys.path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не загружается {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


duty_config = import_path("duty_config", PLUGIN_DIR / "scripts" / "duty_config.py")


class NotConfigured(Exception):
    """Нужный файл доступа не задан или отсутствует: сервис не проверяется."""


_CONFIGS: dict[tuple, object] = {}
_DEFAULT_ROOT: list[str | None] = [None]


def _config_key(root: str | None) -> tuple:
    env = tuple(
        sorted(
            (k, v)
            for k, v in os.environ.items()
            if k.startswith("DUTY_") or k == "CLAUDE_PROJECT_DIR"
        )
    )
    return (root, os.getcwd(), env)


def config(root: str | None = None):
    """Конфигурация проекта: на процесс один разбор на (корень, каталог,
    переменные `DUTY_*`, `CLAUDE_PROJECT_DIR`); смена любой из них даёт
    новую загрузку. Без аргумента берётся корень последнего явного вызова. Ошибка TOML — `duty_config.ConfigError`, в кеш не идёт."""
    # Корень, названный явно (--root), действует для последующих config().
    if root is not None:
        _DEFAULT_ROOT[0] = root
    else:
        root = _DEFAULT_ROOT[0]
    key = _config_key(root)
    cfg = _CONFIGS.get(key)
    if cfg is None:
        cfg = duty_config.load(root=root)
        _CONFIGS[key] = cfg
    return cfg


def config_cache_clear() -> None:
    _CONFIGS.clear()
    _DEFAULT_ROOT[0] = None


config.cache_clear = config_cache_clear  # type: ignore[attr-defined]


def config_path(key: str, root: str | None = None):
    """Путь из конфигурации (`[paths]` с перекрытием переменными окружения);
    ошибка конфигурации — SystemExit."""
    try:
        cfg = config(root)
    except duty_config.ConfigError as error:
        raise SystemExit(f"ошибка конфигурации: {error}")
    value = cfg.get(key)
    assert isinstance(value, str) and value
    return cfg.path_of(value)


def access_file(section: str, key: str, what: str) -> Path:
    """Файл доступа из конфигурации; не задан или нет файла — NotConfigured."""
    cfg = config()
    raw = cfg.get(f"{section}.{key}", "")
    if not isinstance(raw, str) or not raw.strip():
        raise NotConfigured(f"[{section}]: {key} не задан — {what} не проверено")
    path = cfg.path_of(raw)
    if not path.is_file():
        raise NotConfigured(
            f"[{section}]: {key}: нет файла {raw} — {what} не проверено"
        )
    return path


def ssl_context(cfg=None) -> ssl.SSLContext:
    """Проверка сертификата выключается только ключом `jenkins.insecure_tls`."""
    if (cfg or config()).get("jenkins.insecure_tls", False):
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def sh(*args: str, cwd: Path | None = None) -> str:
    """Вывод команды без завершающих пробелов; при сбое — пустая строка."""
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def git(*args: str, cwd: Path | None = None) -> str:
    return sh("git", *args, cwd=cwd)


def size(path: Path) -> str:
    """Размер на диске по `du -sh`; «?», если du не ответил."""
    return sh("du", "-sh", "--", str(path)).split("\t")[0] or "?"


def subuid_range() -> range:
    """Подчинённый диапазон uid пользователя из /etc/subuid (по имени)."""
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        for line in Path("/etc/subuid").read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            if name == user:
                start, _, count = rest.partition(":")
                return range(int(start), int(start) + int(count))
    except (OSError, ValueError):
        pass
    return range(0)


def owner_kind(path: Path, subuids: range | None = None) -> str:
    """Владелец файла: own — пользователь; subuid — uid из подчинённого
    диапазона (контейнеры rootless Docker от не-root uid), удаляется через
    rootlesskit без root; other — настоящий root или иной uid, нужен sudo."""
    uid = path.stat().st_uid
    if uid == os.getuid():
        return "own"
    return (
        "subuid" if uid in (subuid_range() if subuids is None else subuids) else "other"
    )


def _with_ancestors(paths: Iterable[Path]) -> dict[Path, Path]:
    """Индекс «каталог → один из путей внутри него»: сам путь и все его предки."""
    index: dict[Path, Path] = {}
    for path in paths:
        index.setdefault(path, path)
        for parent in path.parents:
            if parent in index:
                break
            index[parent] = path
    return index


def _proc_paths(with_fds: bool) -> tuple[list[Path], list[Path]]:
    """Текущие каталоги и открытые файлы процессов за один обход `/proc`; чужие процессы пропускаются."""
    cwds: list[Path] = []
    files: list[Path] = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cwds.append(Path(os.readlink(p / "cwd")))
            fds = list((p / "fd").iterdir()) if with_fds else []
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/"):
                files.append(Path(target.removesuffix(" (deleted)")))
    return cwds, files


# Общий предел двух вызовов docker (`ps` и `inspect`): зависший демон
# не должен вешать хук и сценарии; второй вызов получает остаток срока.
DOCKER_TIMEOUT_S = 10
_clock = time.monotonic


class DockerUnknown(RuntimeError):
    """Docker не ответил в срок: источники монтирования контейнеров неизвестны."""


class ProcIndex(dict[Path, Path]):
    """Индекс «каталог → путь внутри него»; `docker_unknown` — монтирования контейнеров не получены."""

    docker_unknown = False


def container_mounts() -> list[Path]:
    """Источники монтирования работающих контейнеров (`docker ps` и `docker inspect`).

    Пусто — docker нет, демон не отвечает ошибкой или контейнеров нет.
    Не уложились в общий срок `DOCKER_TIMEOUT_S` оба вызова вместе —
    `DockerUnknown`: «неизвестно» не равно «пусто», вызывающий отказывает
    (`proc_indexes` помечает индекс, `live_reason` по метке возвращает причину).

    Процессы контейнера rootless Docker принадлежат uid из подчинённого
    диапазона: `/proc/<pid>/{cwd,fd}` их не отдаёт, и серия стенда,
    пишущая в смонтированный каталог результатов, в обходе `/proc` не видна.
    """
    deadline = _clock() + DOCKER_TIMEOUT_S

    def docker(*args: str) -> subprocess.CompletedProcess[str] | None:
        left = deadline - _clock()
        if left <= 0:
            raise DockerUnknown("срок вызовов docker исчерпан")
        try:
            return subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=left,
            )
        except subprocess.TimeoutExpired:
            raise DockerUnknown("docker не ответил в срок") from None
        except OSError:
            return None  # docker не установлен или не запускается

    ids = docker("ps", "-q")
    if ids is None or ids.returncode != 0 or not ids.stdout.split():
        return []
    info = docker(
        "inspect",
        "--format",
        "{{range .Mounts}}{{println .Source}}{{end}}",
        *ids.stdout.split(),
    )
    if info is None or info.returncode != 0:
        return []
    return [Path(row) for row in info.stdout.splitlines() if row.startswith("/")]


def cwd_index() -> dict[Path, Path]:
    """Индекс текущих каталогов процессов: один обход `/proc` на запуск."""
    return _with_ancestors(_proc_paths(False)[0])


def cwd_users(path: Path, index: dict[Path, Path] | None = None) -> bool:
    """Есть ли процесс с текущим каталогом внутри path (index — результат `cwd_index`)."""
    return path in (cwd_index() if index is None else index)


def open_paths() -> dict[Path, Path]:
    """Индекс текущих каталогов и открытых файлов процессов (`/proc/<pid>/{cwd,fd}`).

    Собирается один раз на перечень: обход `/proc` на каждый путь дорог,
    поиск каталога — по индексу (`_with_ancestors`).
    Процессы других пользователей пропускаются (нет прав на чтение);
    их место для контейнеров rootless Docker занимают источники
    монтирования работающих контейнеров (`container_mounts`).
    """
    return proc_indexes()[1]


def proc_indexes() -> tuple[dict[Path, Path], dict[Path, Path]]:
    """Индексы `cwd_index` и `open_paths` за один обход `/proc`."""
    cwds, files = _proc_paths(True)
    try:
        mounts = container_mounts()
        unknown = False
    except DockerUnknown:
        mounts, unknown = [], True
    opened = ProcIndex(_with_ancestors([*cwds, *files, *mounts]))
    opened.docker_unknown = unknown
    return _with_ancestors(cwds), opened


# Строка шага state.md: «Шаг: …» или «**Шаг:** …» (как `task_step` в dispatch.sh).
_STEP_LINE = re.compile(r"^(?:\*\*)?Шаг(?:\*\*)?:(?:\*\*)?(.*)$", re.MULTILINE)
# Значение шага завершённой задачи: необязательный номер с тире, затем слово
# целиком, без учёта регистра, с допустимой точкой в конце.
_STEP_FINISHED = re.compile(
    r"(?:\d+\s*[—–-]\s*)?(?:завершено|остановлена)\.?", re.IGNORECASE
)


def task_finished(state: str) -> bool:
    """Завершена ли задача по тексту её state.md — единственный признак плагина.

    Берётся первая строка «Шаг:»; её значение без пробелов по краям, без
    префикса «<номер> —», равно «завершено» или «остановлена». «передача» —
    не завершение: такую задачу продолжают. Зовут `live_reason` и
    `dispatch.sh stalled` (подкоманда `task-finished`).
    """
    match = _STEP_LINE.search(state)
    return bool(match and _STEP_FINISHED.fullmatch(match.group(1).strip()))


def parse_meta(text: str) -> dict[str, str]:
    """Строки `ключ: значение` из meta.txt прогона tab-run.sh; прочие строки пропускаются."""
    meta: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if sep and key:
            meta[key] = value.strip()
    return meta


def read_meta(run_dir: Path) -> dict[str, str]:
    """meta.txt прогона tab-run.sh; испорченные байты заменяются; пустой словарь — файла нет или он не читается."""
    try:
        text = (run_dir / "meta.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_meta(text)


def run_started_ms(run_dir: Path, meta: dict[str, str] | None = None) -> int:
    """Начало прогона tab-run.sh: строка `started_ms` в meta.txt; 0 — не прочитать.

    meta — уже прочитанный `read_meta(run_dir)`, чтобы не читать файл повторно.
    """
    value = (read_meta(run_dir) if meta is None else meta).get("started_ms", "")
    return int(value) if value.isascii() and value.isdigit() else 0


def tab_run_alive(run_dir: Path, meta: dict[str, str] | None = None) -> bool:
    """Прогон tab-run.sh без `done`, чей cmd.sh ещё работает (PID из meta.txt сверяется с командной строкой).

    meta — уже прочитанный `read_meta(run_dir)`, чтобы не читать файл повторно.
    """
    if (run_dir / "done").exists():
        return False
    pid = (read_meta(run_dir) if meta is None else meta).get("pid", "")
    if not (pid.isascii() and pid.isdigit()):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return str(run_dir / "cmd.sh").encode() in cmdline


def live_reason(top: Path, opened: dict[Path, Path] | None = None) -> str:
    """Почему каталог результатов (задачи или прогона) нельзя убирать; пустая строка — признаков нет.

    Признаки: процесс с текущим каталогом или открытым файлом внутри
    (серия стенда пишет журнал, итог `summary.json` появляется только
    в конце); прогон tab-run.sh в `runs/*` без `done` с живым PID;
    задача (`brief.md`) со `state.md`, где шаг не завершён (`task_finished`).
    Индекс с `docker_unknown` (docker не ответил) — признак для любого каталога:
    монтирования контейнеров неизвестны, убирать нельзя.
    """
    index = open_paths() if opened is None else opened
    if getattr(index, "docker_unknown", False):
        return "docker не ответил в срок: монтирования контейнеров неизвестны"
    if top in index:
        return f"открыт файл процесса ({index[top]})"
    for meta in sorted(top.glob("runs/*/meta.txt")):
        if tab_run_alive(meta.parent):
            return f"прогон {meta.parent.name} без done с живым pid"
    if (top / "brief.md").is_file():
        try:
            state = (top / "state.md").read_text(encoding="utf-8")
        except OSError:
            return ""  # без state.md задача не идёт — как у dispatch.sh stalled
        if not task_finished(state):
            return "задача не завершена (state.md без «завершено» или «остановлена» в шаге)"
    return ""


def active_sessions(home: Path | None = None) -> set[str]:
    """Сеансы с живым процессом Claude Code (`~/.claude/sessions/<pid>.json`)."""
    ids = set()
    for f in ((home or Path.home()) / ".claude/sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            os.kill(int(d["pid"]), 0)
            ids.add(d["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return ids


def _tail(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def run_extensions(
    root: Path, kind: str, timeout_s: float
) -> list[tuple[str, str, str | None]]:
    """Запуск расширений `<корень>/.duty/<kind>.d/*` по порядку имён.

    Каждый файл запускается из корня проекта с окружением `DUTY_PROJECT`,
    `DUTY_CONFIG` (путь `.duty/duty.toml`) и `DUTY_PLUGIN` (каталог плагина);
    срок одного запуска — timeout_s секунд, по истечении группа процессов
    расширения получает SIGKILL. Скрытые файлы (с точки) и каталоги
    пропускаются. Возвращает (имя, stdout, ошибка или None); сбой, срок
    и неисполняемый файл — ошибка строкой, исключений нет.
    Контракты вывода: facts — разделы `## …`, cleanup — JSON по строке
    (reference/config.md).
    """
    if kind not in EXTENSION_KINDS:
        raise ValueError(f"неизвестный вид расширений: {kind}")
    root = Path(root).resolve()
    folder = root / ".duty" / f"{kind}.d"
    if not folder.is_dir():
        return []
    env = dict(os.environ)
    env.update(
        DUTY_PROJECT=str(root),
        DUTY_CONFIG=str(root / ".duty" / "duty.toml"),
        DUTY_PLUGIN=str(PLUGIN_DIR),
    )
    results: list[tuple[str, str, str | None]] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.is_dir():
            continue
        if not os.access(entry, os.X_OK):
            results.append((entry.name, "", "не исполняемый файл (нужен chmod +x)"))
            continue
        try:
            proc = subprocess.Popen(
                [str(entry)],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            results.append((entry.name, "", f"не запускается: {error}"))
            continue
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Расширение могло породить потомков, держащих каналы вывода:
            # снимается вся группа, иначе communicate ждал бы их.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = proc.communicate()
            results.append((entry.name, out, f"срок {timeout_s:g} с истёк"))
            continue
        if proc.returncode != 0:
            detail = _tail(err)
            message = f"код выхода {proc.returncode}" + (
                f": {detail}" if detail else ""
            )
            results.append((entry.name, out, message))
        else:
            results.append((entry.name, out, None))
    return results


def _main(argv: list[str]) -> int:
    """`dutylib.py task-finished <state.md>`: код 0 — задача завершена, 1 — нет или файла нет."""
    if len(argv) != 2 or argv[0] != "task-finished":
        print("использование: dutylib.py task-finished <state.md>", file=sys.stderr)
        return 2
    try:
        state = Path(argv[1]).read_text(encoding="utf-8")
    except OSError:
        return 1
    return 0 if task_finished(state) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
