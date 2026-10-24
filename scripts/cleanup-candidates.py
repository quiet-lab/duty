"""Перечень кандидатов на уборку для /duty:cleanup.

Сам ничего не удаляет: печатает таблицу «вид | что | размер | решение | команда».
Строки с решением «удалить» исполняет навык по одной; остальные остаются
пользователю. Меняет только ссылки origin/* (git fetch --prune), чтобы
видеть удалённые в origin ветви.

Аргументы: корень монорепозитория (необязательно, по умолчанию текущий
каталог) и повторяемый --exclude <шаблон> — glob по имени ветви; такие ветви
остаются в перечне со строкой «оставлено».
"""

import fnmatch
import json
import os
import pwd
import shlex
import subprocess
import sys
import time
from pathlib import Path

_args = sys.argv[1:]
EXCLUDES: list[str] = []
_positional: list[str] = []
while _args:
    _a = _args.pop(0)
    if _a == "--exclude" and _args:
        EXCLUDES.append(_args.pop(0))
    elif _a.startswith("--exclude="):
        EXCLUDES.append(_a.removeprefix("--exclude="))
    else:
        _positional.append(_a)
def sh(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


ROOT = Path(_positional[0] if _positional else os.getcwd()).resolve()
HOME = Path.home()
# Данные стенда лежат в каталоге ocp-dev-stand рядом с корнем монорепозитория.
_project = os.environ.get("CLAUDE_PROJECT_DIR") or sh("git", "rev-parse", "--show-toplevel", cwd=Path.cwd()) or str(ROOT)
STAND = Path(_project).resolve().parent / "ocp-dev-stand"
RESULTS = STAND / "results"
SCRATCH = Path(f"/tmp/claude-{os.getuid()}")
PROTECTED = {"dev", "dev2", "master", "new_ocp", "dev-local"}
# Основные деревья стенда переводит на новую ревизию сам стенд; удаляются
# только вместе со стендом (docs/dev-stand.md, «Остановка и очистка»).
OUR_PREFIXES = ("dev2-RP-", "bugfix/RP-", "chore/", "feature/")
ME = subprocess.run(["git", "config", "user.name"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
STAND_MAIN = {STAND / "src" / n for n in ("qms", "ui", "tests", "jssip-phone")}
# Поля строки: вид, сабмодуль, родитель, что, размер, решение, команда.
rows: list[tuple[str, str, str, str, str, str, str]] = []


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


SUBUIDS = subuid_range()


def owner_kind(path: Path) -> str:
    """Владелец файла: own — пользователь; subuid — uid из подчинённого
    диапазона (контейнеры rootless Docker от не-root uid), удаляется через
    rootlesskit без root; other — настоящий root или иной uid, нужен sudo."""
    uid = path.stat().st_uid
    if uid == os.getuid():
        return "own"
    return "subuid" if uid in SUBUIDS else "other"


def removal(path: Path, args: str, why: str) -> str:
    """Команда удаления с учётом владельца; args — флаги и путь для rm/rmdir."""
    kind = owner_kind(path)
    if kind == "subuid":
        return f"rootlesskit rm -rf -- {shlex.quote(str(path))}"
    prefix = f"sudo -A -p '{why}' " if kind == "other" else ""
    return f"{prefix}{args}"


def size(path: Path) -> str:
    return sh("du", "-sh", "--", str(path)).split("\t")[0] or "?"


def add(kind: str, what: str, sz: str, verdict: str, cmd: str = "", sub: str = "", parent: str = "") -> None:
    rows.append((kind, sub, parent, what, sz, verdict, cmd))


def active_sessions() -> set[str]:
    """Сеансы с живым процессом Claude Code (~/.claude/sessions/<pid>.json)."""
    ids = set()
    for f in (HOME / ".claude/sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            os.kill(int(d["pid"]), 0)
            ids.add(d["sessionId"])
        except (OSError, ValueError, KeyError):
            continue
    return ids


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


def submodules() -> list[tuple[Path, str]]:
    out = sh("git", "config", "-f", ".gitmodules", "--get-regexp", r"\.path$", cwd=ROOT)
    res = []
    for line in out.splitlines():
        key, path = line.split()
        name = key.removeprefix("submodule.").removesuffix(".path")
        parent = sh("git", "config", "-f", ".gitmodules", f"submodule.{name}.parent", cwd=ROOT) or sh(
            "git", "config", "-f", ".gitmodules", f"submodule.{name}.branch", cwd=ROOT
        )
        res.append((ROOT / path, parent))
    return res


def worktrees(repo: Path) -> list[dict[str, str]]:
    items, cur = [], {}
    for line in sh("git", "worktree", "list", "--porcelain", cwd=repo).splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v or "1"
    return items


live = active_sessions()
in_live_session = lambda p: any(s in str(p) for s in live)  # noqa: E731

for repo, parent in submodules():
    if not (repo / ".git").exists():
        continue
    rel = repo.relative_to(ROOT)
    sh("git", "fetch", "--quiet", "--prune", "--no-tags", "origin", cwd=repo)
    stale = sh("git", "worktree", "prune", "--dry-run", "-v", cwd=repo)
    if stale:
        add("worktree", f"{rel}: {stale.splitlines()[0][:80]}…", "—", "удалить",
            f"git -C {shlex.quote(str(repo))} worktree prune", str(rel), parent)
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
        if path in STAND_MAIN or not path.exists():
            continue
        if not branch:
            if path.parent.name.endswith("-wt"):
                continue  # деревья ревизий стенда убирает instances-prune
            add("worktree", str(path), size(path), "оставлено: без ветви, чьё — неясно", "", str(rel), parent)
            continue
        if sh("git", "status", "--porcelain", cwd=path):
            add("worktree", f"{path} [{branch}]", size(path), "оставлено: грязное дерево", "", str(rel), parent)
            continue
        if in_live_session(path) or cwd_users(path):
            add("worktree", f"{path} [{branch}]", size(path), "оставлено: живой сеанс или процесс", "", str(rel), parent)
            continue
        gone = not sh("git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=repo)
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, f"origin/{parent}"], cwd=repo
        ).returncode == 0
        # Ветвь без origin/<ветвь> и без origin-ветви, содержащей её вершину,
        # не удалена в origin, а никогда не отправлялась: дерево такой ветви
        # решает пользователь, хотя сама ветвь при удалении дерева сохраняется.
        if gone and not merged and not sh("git", "branch", "-r", "--contains", branch, cwd=repo):
            ahead = sh("git", "rev-list", "--count", f"origin/{parent}..{branch}", cwd=repo)
            add("worktree", f"{path} [{branch}]", size(path),
                f"решение пользователя: ветвь не отправлена в origin, {ahead} коммитов вне origin/{parent}",
                "", str(rel), parent)
            continue
        if gone or merged:
            why = "ветвь удалена в origin" if gone else f"слита в {parent}"
            add("worktree", f"{path} [{branch}]", size(path), f"удалить: {why}",
                f"git -C {shlex.quote(str(repo))} worktree remove {shlex.quote(str(path))}", str(rel), parent)
    for b in sh("git", "branch", "--format=%(refname:short)", cwd=repo).splitlines():
        # backup/* и saved/* — сохранённые копии, их удаляет только пользователь.
        if b in PROTECTED or b.startswith(("backup/", "saved/")):
            continue
        # Слитость — это «origin/<родитель> содержит вершину ветви», а не
        # слитость в HEAD сабмодуля, которую проверяет `git branch -d`.
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", b, f"origin/{parent}"], cwd=repo, capture_output=True
        ).returncode == 0
        if not merged:
            continue
        cmd = f"git -C {shlex.quote(str(repo))} branch -D {shlex.quote(b)}"
        if not b.startswith(OUR_PREFIXES) and sh("git", "log", "-1", "--format=%an", b, cwd=repo) != ME:
            add("ветвь", b, "—", "оставлено: чужая ветвь (автор не пользователь)", "", str(rel), parent)
            continue
        if any(fnmatch.fnmatchcase(b, pat) for pat in EXCLUDES):
            add("ветвь", b, "—", "оставлено: исключена параметром --exclude", "", str(rel), parent)
        elif b in occupied:
            dirty = " (есть незакоммиченные изменения)" if sh("git", "status", "--porcelain", cwd=occupied[b]) else ""
            add("ветвь", b, "—", f"оставлено: занята worktree {occupied[b]}{dirty}", "", str(rel), parent)
        else:
            add("ветвь", b, "—", f"удалить: origin/{parent} содержит вершину", cmd, str(rel), parent)

if any((STAND / "src").glob("*-wt/*")):
    add("стенд", "деревья ревизий src/*-wt/<коммит>", "—", "удалить: уборка стенда",
        f"cd {shlex.quote(str(ROOT))} && just dev-stand::instances-prune")

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
    for g in sh("find", str(d), "-maxdepth", "6", "-name", ".git", "-not", "-path", "*/node_modules/*").splitlines():
        repo_dir = Path(g).parent
        if sh("git", "status", "--porcelain", cwd=repo_dir):
            problems.append(f"{repo_dir.name}: грязное дерево")
        if sh("git", "log", "--branches", "--not", "--remotes", "--oneline", "-n", "1", cwd=repo_dir):
            problems.append(f"{repo_dir.name}: невыгруженные коммиты")
    if problems:
        add("scratchpad", str(d), size(d), "оставлено: " + "; ".join(problems))
        continue
    add("scratchpad", str(d), size(d), "удалить: сеанс завершён", f"rm -rf -- {shlex.quote(str(d))}")

if RESULTS.is_dir():
    for f in sorted(RESULTS.rglob("*")):
        if f.name not in ("video.webm", "trace.zip") or not f.is_file():
            continue
        failed = (f.parent / "error-context.md").exists() or any(f.parent.glob("test-failed-*.png"))
        if not failed:
            rm = removal(f, f"rm -f -- {shlex.quote(str(f))}", "Удалить видео прошедшего теста, созданное в контейнере")
            add("results", str(f.relative_to(STAND)), size(f), "удалить: тест прошёл", rm)
    # Пустые каталоги чужих владельцев остаются от контейнеров прогонов
    # и сами не исчезают.
    for f in RESULTS.rglob("*"):
        try:
            if f.is_dir() and owner_kind(f) != "own" and not any(f.iterdir()):
                kind = owner_kind(f)
                verdict = ("удалить через rootlesskit: пустой каталог контейнера" if kind == "subuid"
                           else "решение пользователя: нужен sudo, пустой каталог чужого владельца")
                add("results", str(f.relative_to(STAND)), "0", verdict,
                    removal(f, f"rmdir -- {shlex.quote(str(f))}",
                            "Удалить пустой каталог результатов, созданный в контейнере"))
        except OSError:
            continue

usage = sh("df", "-h", "--output=used,size,pcent", "/tmp").splitlines()[-1].split()
add("/tmp", f"занято {usage[0]} из {usage[1]} ({usage[2]})", usage[0], "сведения")
for line in sh("bash", "-c", "du -xsh /tmp/* 2>/dev/null | sort -rh | head -5").splitlines():
    sz, path = line.split("\t", 1)
    if not path.startswith(str(SCRATCH)):
        add("/tmp", path, sz, "решение пользователя")

for line in sh("docker", "ps", "-a", "--filter", "status=exited", "--format",
               '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Status}}').splitlines():
    name, proj, status = line.split("|")
    if proj == "ocp-stand":
        continue  # остановленные одноразовые службы стенда; снимает just dev-stand::down
    add("docker", f"контейнер {name} ({proj or 'без проекта'}, {status})", "—", "решение пользователя",
        f"docker rm {shlex.quote(name)}")
dangling = sh("docker", "images", "-f", "dangling=true", "-q").split()
if dangling:
    add("docker", f"висячие образы: {len(dangling)}", "—", "удалить", "docker image prune -f")
# Размер и «Что» — освобождаемый объём (RECLAIMABLE), а не весь кэш.
for line in sh("docker", "system", "df", "--format", "{{.Type}}|{{.Reclaimable}}").splitlines():
    typ, _, reclaim = line.partition("|")
    reclaim = reclaim.split(" (")[0]
    if typ == "Build Cache" and reclaim and not reclaim.startswith("0B"):
        add("docker", f"кэш сборки, освобождаемо {reclaim}", reclaim,
            "решение пользователя: следующая сборка пойдёт дольше", "docker builder prune -f")

# Строки опознаются по полям (сабмодуль, ветвь, путь, имя), не по номеру.
print("| Вид | Сабмодуль | Родитель | Что | Размер | Решение | Команда |")
print("|---|---|---|---|---|---|---|")
for kind, sub, parent, what, sz, verdict, cmd in rows:
    # Длинный префикс scratchpad сокращается только в колонке «Что»; команды остаются точными.
    what = what.replace(str(project), "<scratch>").replace(str(HOME), "~")
    print(f"| {kind} | {sub} | {parent} | {what} | {sz} | {verdict} | {('`' + cmd + '`') if cmd else ''} |")
