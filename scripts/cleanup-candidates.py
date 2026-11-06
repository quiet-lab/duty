"""Перечень кандидатов на уборку для /duty:cleanup.
" 2>/dev/null; exec python3 -I "$0" "$@" # файл, переданный оболочке (bash файл.py), запускается python3

Сам ничего не удаляет: печатает таблицу «вид | что | размер | решение | команда».
Строки с решением «удалить» исполняет навык по одной; остальные остаются
пользователю. Меняет только ссылки origin/* (git fetch --prune), чтобы
видеть удалённые в origin ветви.

Аргументы: корень проекта (необязательно, по умолчанию корень из
конфигурации `.duty/duty.toml`, иначе текущий каталог) и повторяемый --exclude <шаблон> — glob по имени ветви; такие ветви
и worktree, выписанные на них, остаются в перечне со строкой «оставлено».

Признаки ветвей, каталог результатов, основные деревья и проекты docker,
которые нельзя трогать, берутся из конфигурации проекта (reference/config.md:
`git.*`, `paths.results`, `cleanup.*`). Строки, зависящие от устройства
проекта, добавляют расширения `.duty/cleanup.d/*`: каждая строка их вывода —
объект JSON с ключами kind, sub, parent, what, size, verdict, cmd.
"""

import fnmatch
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent


def _load(name: str) -> ModuleType:
    """Модули плагина импортируются по пути: под `python3 -I` каталог сценария не в sys.path."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dutylib = _load("dutylib")
duty_config = _load("duty_config")
sh, size, owner_kind, cwd_users, active_sessions = (
    dutylib.sh,
    dutylib.size,
    dutylib.owner_kind,
    dutylib.cwd_users,
    dutylib.active_sessions,
)

_args = sys.argv[1:]
EXCLUDES: list[str] = []
_positional: list[str] = []
while _args:
    _a = _args.pop(0)
    if _a in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    elif _a == "--exclude":
        if not _args:
            sys.exit("--exclude требует шаблон ветви")
        EXCLUDES.append(_args.pop(0))
    elif _a.startswith("--exclude="):
        EXCLUDES.append(_a.removeprefix("--exclude="))
    else:
        _positional.append(_a)

try:
    CFG = duty_config.load(root=_positional[0] if _positional else None)
except duty_config.ConfigError as error:
    sys.exit(f"cleanup-candidates.py: {error}")
ROOT = Path(_positional[0] if _positional else (CFG.root or os.getcwd())).resolve()
HOME = Path.home()
RESULTS = Path(CFG.get("paths.results"))
SCRATCH = Path(f"/tmp/claude-{os.getuid()}")
# Защищённые и рабочие ветви, сохранённые копии, префиксы своих ветвей.
PROTECTED = set(CFG.get("git.protected", [])) | set(CFG.get("git.working", []))
KEEP_PREFIXES = tuple(CFG.get("git.keep_prefixes", []))
OUR_PREFIXES = tuple(CFG.get("git.task_prefixes", []))
NAMED_DEV = bool(CFG.get("git.named_dev_branches", False))
PARENT_KEY = CFG.get("git.parent_key", "parent")
DEFAULT_PARENT = CFG.get("git.default_parent", "master")
DOCKER_KEEP = set(CFG.get("cleanup.docker_keep_projects", []))
ME = subprocess.run(
    ["git", "config", "user.name"],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()
# Маски каталогов, чьи worktree не перечисляются и не измеряются (cleanup.skip_trees).
SKIP_MASKS = [str(CFG.path_of(t)) for t in CFG.get("cleanup.skip_trees", [])]


def skipped(path: Path) -> bool:
    return any(
        fnmatch.fnmatch(str(p), m) for m in SKIP_MASKS for p in (path, *path.parents)
    )


# Основные деревья стенда переводит на новую ревизию сам стенд; удаляются
# только вместе со стендом.
STAND_MAIN = {CFG.path_of(t).resolve() for t in CFG.get("cleanup.keep_trees", [])}
# Поля строки: вид, сабмодуль, родитель, что, размер, решение, команда.
rows: list[tuple[str, str, str, str, str, str, str]] = []


def removal(path: Path, args: str, why: str) -> str:
    """Команда удаления с учётом владельца; args — флаги и путь для rm/rmdir."""
    kind = owner_kind(path)
    if kind == "subuid":
        return f"rootlesskit rm -rf -- {shlex.quote(str(path))}"
    prefix = f"sudo -A -p '{why}' " if kind == "other" else ""
    return f"{prefix}{args}"


def add(
    kind: str,
    what: str,
    sz: str,
    verdict: str,
    cmd: str = "",
    sub: str = "",
    parent: str = "",
) -> None:
    rows.append((kind, sub, parent, what, sz, verdict, cmd))


def is_excluded(branch: str) -> bool:
    return any(fnmatch.fnmatchcase(branch, pat) for pat in EXCLUDES)


def submodules() -> list[tuple[Path, str]]:
    out = sh("git", "config", "-f", ".gitmodules", "--get-regexp", r"\.path$", cwd=ROOT)
    res = []
    for line in out.splitlines():
        key, path = line.split()
        name = key.removeprefix("submodule.").removesuffix(".path")
        parent = (
            sh(
                "git",
                "config",
                "-f",
                ".gitmodules",
                f"submodule.{name}.{PARENT_KEY}",
                cwd=ROOT,
            )
            or sh(
                "git",
                "config",
                "-f",
                ".gitmodules",
                f"submodule.{name}.branch",
                cwd=ROOT,
            )
            or DEFAULT_PARENT
        )
        res.append((ROOT / path, parent))
    return res


def worktrees(repo: Path) -> list[dict[str, str]]:
    items, cur = [], {}
    for line in sh("git", "worktree", "list", "--porcelain", cwd=repo).splitlines() + [
        ""
    ]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v or "1"
    return items


live = active_sessions()
in_live_session = lambda p: any(s in str(p) for s in live)

for repo, parent in submodules():
    if not (repo / ".git").exists():
        continue
    rel = repo.relative_to(ROOT)
    sh("git", "fetch", "--quiet", "--prune", "--no-tags", "origin", cwd=repo)
    stale = sh("git", "worktree", "prune", "--dry-run", "-v", cwd=repo)
    if stale:
        add(
            "worktree",
            f"{rel}: {stale.splitlines()[0][:80]}…",
            "—",
            "удалить",
            f"git -C {shlex.quote(str(repo))} worktree prune",
            str(rel),
            parent,
        )
    # Ветвь -> рабочий каталог, где она выписана; из `git worktree list`
    # самого сабмодуля, первое дерево — основное.
    all_wts = worktrees(repo)
    occupied = {
        w["branch"].removeprefix("refs/heads/"): Path(w["worktree"])
        for w in all_wts
        if w.get("branch") and Path(w["worktree"]).exists()
    }
    for wt in all_wts[1:]:
        path = Path(wt["worktree"])
        branch = wt.get("branch", "").removeprefix("refs/heads/")
        if path.resolve() in STAND_MAIN or skipped(path) or not path.exists():
            continue
        if not branch:
            add(
                "worktree",
                str(path),
                size(path),
                "оставлено: без ветви, чьё — неясно",
                "",
                str(rel),
                parent,
            )
            continue
        # Исключение действует на worktree ветви независимо от того, есть ли
        # ветвь в origin: после её удаления там worktree иначе попал бы в кандидаты.
        if is_excluded(branch):
            add(
                "worktree",
                f"{path} [{branch}]",
                size(path),
                "оставлено: ветвь исключена параметром --exclude",
                "",
                str(rel),
                parent,
            )
            continue
        if sh("git", "status", "--porcelain", cwd=path):
            add(
                "worktree",
                f"{path} [{branch}]",
                size(path),
                "оставлено: грязное дерево",
                "",
                str(rel),
                parent,
            )
            continue
        if in_live_session(path) or cwd_users(path):
            add(
                "worktree",
                f"{path} [{branch}]",
                size(path),
                "оставлено: живой сеанс или процесс",
                "",
                str(rel),
                parent,
            )
            continue
        # «Был upstream и исчез» ([gone]) — ветвь удалена в origin. Ветвь без
        # upstream и без origin/<ветвь> никогда не отправлялась: это рабочая
        # ветвь с идущей задачей, не кандидат, даже если вершина уже в родителе.
        track = sh(
            "git",
            "for-each-ref",
            "--format=%(upstream:track)|%(upstream:short)",
            f"refs/heads/{branch}",
            cwd=repo,
        )
        track, _, upstream = track.partition("|")
        has_origin_ref = bool(
            sh(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{branch}",
                cwd=repo,
            )
        )
        gone = "gone" in track
        merged = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch, f"origin/{parent}"],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        )
        if not gone and not upstream and not has_origin_ref:
            ahead = sh(
                "git", "rev-list", "--count", f"origin/{parent}..{branch}", cwd=repo
            )
            add(
                "worktree",
                f"{path} [{branch}]",
                size(path),
                f"решение пользователя: ветвь не отправлялась в origin, {ahead} коммитов вне origin/{parent}",
                "",
                str(rel),
                parent,
            )
            continue
        if gone or merged:
            why = "ветвь удалена в origin" if gone else f"слита в {parent}"
            add(
                "worktree",
                f"{path} [{branch}]",
                size(path),
                f"удалить: {why}",
                f"git -C {shlex.quote(str(repo))} worktree remove {shlex.quote(str(path))}",
                str(rel),
                parent,
            )
    for b in sh("git", "branch", "--format=%(refname:short)", cwd=repo).splitlines():
        # Ветви из git.keep_prefixes — сохранённые копии, их удаляет только пользователь.
        if b in PROTECTED or b.startswith(KEEP_PREFIXES):
            continue
        # Слитость — это «origin/<родитель> содержит вершину ветви», а не
        # слитость в HEAD сабмодуля, которую проверяет `git branch -d`.
        merged = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", b, f"origin/{parent}"],
                cwd=repo,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not merged:
            continue
        cmd = f"git -C {shlex.quote(str(repo))} branch -D {shlex.quote(b)}"
        # Ветвь с последним коммитом не от пользователя git не удаляется никогда.
        # git.named_dev_branches лишь уточняет причину: именная ветвь разработчика.
        if (
            not b.startswith(OUR_PREFIXES)
            and sh("git", "log", "-1", "--format=%an", b, cwd=repo) != ME
        ):
            named = NAMED_DEV and "/" not in b
            why = (
                "именная ветвь разработчика (автор не пользователь)"
                if named
                else "чужая ветвь (автор не пользователь)"
            )
            add("ветвь", b, "—", f"оставлено: {why}", "", str(rel), parent)
            continue
        if is_excluded(b):
            add(
                "ветвь",
                b,
                "—",
                "оставлено: исключена параметром --exclude",
                "",
                str(rel),
                parent,
            )
        elif b in occupied:
            dirty = (
                " (есть незакоммиченные изменения)"
                if sh("git", "status", "--porcelain", cwd=occupied[b])
                else ""
            )
            add(
                "ветвь",
                b,
                "—",
                f"оставлено: занята worktree {occupied[b]}{dirty}",
                "",
                str(rel),
                parent,
            )
        else:
            add(
                "ветвь",
                b,
                "—",
                f"удалить: origin/{parent} содержит вершину",
                cmd,
                str(rel),
                parent,
            )

project = SCRATCH / ("-" + str(ROOT).strip("/").replace("/", "-"))
transcripts = HOME / ".claude/projects" / project.name
for d in sorted(project.glob("*")) if project.is_dir() else []:
    if d.name in live or cwd_users(d):
        continue
    t = transcripts / f"{d.name}.jsonl"
    # Сеанс без процесса, но с журналом свежее часа, мог продолжиться через
    # --resume другим процессом: такой каталог решает пользователь.
    if t.exists() and time.time() - t.stat().st_mtime < 3600:
        add("scratchpad", str(d), size(d), "оставлено: журнал сеанса свежее часа")
        continue
    # Внутри каталога бывают клоны и worktree (.git — файл или каталог): пока в них
    # есть незакоммиченное или не выгруженное в origin, каталог не удаляется.
    problems = []
    for g in sh(
        "find",
        str(d),
        "-maxdepth",
        "6",
        "-name",
        ".git",
        "-not",
        "-path",
        "*/node_modules/*",
    ).splitlines():
        repo_dir = Path(g).parent
        if sh("git", "status", "--porcelain", cwd=repo_dir):
            problems.append(f"{repo_dir.name}: грязное дерево")
        if sh(
            "git",
            "log",
            "--branches",
            "--not",
            "--remotes",
            "--oneline",
            "-n",
            "1",
            cwd=repo_dir,
        ):
            problems.append(f"{repo_dir.name}: невыгруженные коммиты")
    if problems:
        add("scratchpad", str(d), size(d), "оставлено: " + "; ".join(problems))
        continue
    add(
        "scratchpad",
        str(d),
        size(d),
        "удалить: сеанс завершён",
        f"rm -rf -- {shlex.quote(str(d))}",
    )

STAND = RESULTS.parent
if RESULTS.is_dir():
    for f in sorted(RESULTS.rglob("*")):
        if f.name not in ("video.webm", "trace.zip") or not f.is_file():
            continue
        failed = (f.parent / "error-context.md").exists() or any(
            f.parent.glob("test-failed-*.png")
        )
        if not failed:
            rm = removal(
                f,
                f"rm -f -- {shlex.quote(str(f))}",
                "Удалить видео прошедшего теста, созданное в контейнере",
            )
            add(
                "results",
                str(f.relative_to(STAND)),
                size(f),
                "удалить: тест прошёл",
                rm,
            )
    # Пустые каталоги чужих владельцев остаются от контейнеров прогонов
    # и сами не исчезают.
    for f in RESULTS.rglob("*"):
        try:
            if f.is_dir() and owner_kind(f) != "own" and not any(f.iterdir()):
                kind = owner_kind(f)
                verdict = (
                    "удалить через rootlesskit: пустой каталог контейнера"
                    if kind == "subuid"
                    else "решение пользователя: нужен sudo, пустой каталог чужого владельца"
                )
                add(
                    "results",
                    str(f.relative_to(STAND)),
                    "0",
                    verdict,
                    removal(
                        f,
                        f"rmdir -- {shlex.quote(str(f))}",
                        "Удалить пустой каталог результатов, созданный в контейнере",
                    ),
                )
        except OSError:
            continue

usage = sh("df", "-h", "--output=used,size,pcent", "/tmp").splitlines()[-1].split()
add("/tmp", f"занято {usage[0]} из {usage[1]} ({usage[2]})", usage[0], "сведения")
for line in sh(
    "bash", "-c", "du -xsh /tmp/* 2>/dev/null | sort -rh | head -5"
).splitlines():
    sz, path = line.split("\t", 1)
    if not path.startswith(str(SCRATCH)):
        add("/tmp", path, sz, "решение пользователя")

for line in sh(
    "docker",
    "ps",
    "-a",
    "--filter",
    "status=exited",
    "--format",
    '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Status}}',
).splitlines():
    name, proj, status = line.split("|")
    if proj in DOCKER_KEEP:
        continue  # проекты из cleanup.docker_keep_projects снимает их владелец
    add(
        "docker",
        f"контейнер {name} ({proj or 'без проекта'}, {status})",
        "—",
        "решение пользователя",
        f"docker rm {shlex.quote(name)}",
    )
dangling = sh("docker", "images", "-f", "dangling=true", "-q").split()
if dangling:
    add(
        "docker",
        f"висячие образы: {len(dangling)}",
        "—",
        "удалить",
        "docker image prune -f",
    )
# Кэш сборки старше двух суток удаляется по решению пользователя, свежий
# остаётся: он ускоряет текущие сборки. Объём по возрасту Docker не
# сообщает (docker buildx du с --filter итога не печатает), поэтому
# «Размер» — весь освобождаемый объём кэша по docker system df, верхняя
# граница того, что освободит команда.
for line in sh(
    "docker", "system", "df", "--format", "{{.Type}}|{{.Reclaimable}}"
).splitlines():
    typ, _, reclaim = line.partition("|")
    reclaim = reclaim.split(" (")[0]
    if typ == "Build Cache" and reclaim and not reclaim.startswith("0B"):
        add(
            "docker",
            f"кэш сборки старше 48 ч (весь освобождаемый кэш — {reclaim}, свежий остаётся)",
            f"до {reclaim}",
            "удалить",
            "docker builder prune -f --filter until=48h",
        )

# Расширения проекта (.duty/cleanup.d): по строке JSON на строку таблицы.
FIELDS = ("kind", "sub", "parent", "what", "size", "verdict", "cmd")
for ext_name, out, err in dutylib.run_extensions(ROOT, "cleanup", 120):
    if err:
        rows.append(("расширение", "", "", ext_name, "—", f"не получено: {err}", ""))
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("не объект JSON")  # noqa: TRY004 — ошибка структуры ловится ниже вместе с ошибкой разбора JSON
            rows.append(tuple(str(obj.get(k, "")) for k in FIELDS))  # type: ignore[arg-type]
        except ValueError as error:
            rows.append(
                (
                    "расширение",
                    "",
                    "",
                    ext_name,
                    "—",
                    f"не получено: строка не JSON ({error})",
                    "",
                )
            )

# Строки опознаются по полям (сабмодуль, ветвь, путь, имя), не по номеру.
print("| Вид | Сабмодуль | Родитель | Что | Размер | Решение | Команда |")
print("|---|---|---|---|---|---|---|")
for kind, sub, parent, what, sz, verdict, cmd in rows:
    # Длинный префикс scratchpad сокращается только в колонке «Что»; команды остаются точными.
    what = what.replace(str(project), "<scratch>").replace(str(HOME), "~")
    print(
        f"| {kind} | {sub} | {parent} | {what} | {sz} | {verdict} | {('`' + cmd + '`') if cmd else ''} |"
    )
