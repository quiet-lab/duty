"""Общие помощники сценариев плагина duty и расширений `.duty/*.d/`.

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


def cwd_users(path: Path) -> bool:
    """Есть ли процесс с текущим каталогом внутри path."""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if Path(os.readlink(p / "cwd")).is_relative_to(path):
                return True
        except OSError:
            continue
    return False


def open_paths() -> list[Path]:
    """Текущие каталоги и открытые файлы всех доступных процессов (`/proc/<pid>/{cwd,fd}`).

    Собирается один раз на перечень: обход `/proc` на каждый путь дорог.
    Процессы других пользователей пропускаются (нет прав на чтение).
    """
    found: list[Path] = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            found.append(Path(os.readlink(p / "cwd")))
            fds = list((p / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/"):
                found.append(Path(target.removesuffix(" (deleted)")))
    return found


_PID_LINE = re.compile(r"^pid: (\d+)$", re.MULTILINE)


def tab_run_alive(run_dir: Path) -> bool:
    """Прогон tab-run.sh без `done`, чей cmd.sh ещё работает (PID из meta.txt сверяется с командной строкой)."""
    if (run_dir / "done").exists():
        return False
    try:
        match = _PID_LINE.search((run_dir / "meta.txt").read_text(encoding="utf-8"))
        if not match:
            return False
        cmdline = Path(f"/proc/{match.group(1)}/cmdline").read_bytes()
    except OSError:
        return False
    return str(run_dir / "cmd.sh").encode() in cmdline


def live_reason(top: Path, opened: list[Path] | None = None) -> str:
    """Почему каталог результатов (задачи или прогона) нельзя убирать; пустая строка — признаков нет.

    Признаки: процесс с текущим каталогом или открытым файлом внутри
    (серия стенда пишет журнал, итог `summary.json` появляется только
    в конце); прогон tab-run.sh в `runs/*` без `done` с живым PID;
    задача (`brief.md`) без строки «Шаг: заверш…» в `state.md`.
    """
    for path in open_paths() if opened is None else opened:
        if path.is_relative_to(top):
            return f"открыт файл процесса ({path})"
    for meta in sorted(top.glob("runs/*/meta.txt")):
        if tab_run_alive(meta.parent):
            return f"прогон {meta.parent.name} без done с живым pid"
    if (top / "brief.md").is_file():
        try:
            state = (top / "state.md").read_text(encoding="utf-8")
        except OSError:
            state = ""
        if not re.search(r"^Шаг: заверш", state, re.MULTILINE):
            return "задача не завершена (state.md без «Шаг: заверш…»)"
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
