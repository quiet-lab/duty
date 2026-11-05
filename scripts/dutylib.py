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
import signal
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
