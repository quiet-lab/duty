"""Перечень кандидатов на уборку для /duty:cleanup.

Сам ничего не удаляет: печатает таблицу «вид | что | размер | решение | команда».
Строки с решением «удалить» исполняет навык по одной; остальные остаются
пользователю. Меняет только ссылки origin/* (git fetch --prune), чтобы
видеть удалённые в origin ветви.

Аргумент — корень монорепозитория.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).resolve()
HOME = Path.home()
STAND = HOME / ".local/share/ocp-dev-stand"
RESULTS = STAND / "results"
SCRATCH = Path(f"/tmp/claude-{os.getuid()}")
PROTECTED = {"dev", "dev2", "master", "new_ocp", "dev-local"}
# Основные деревья стенда переводит на новую ревизию сам стенд; удаляются
# только вместе со стендом (docs/dev-stand.md, «Остановка и очистка»).
STAND_MAIN = {STAND / "src" / n for n in ("qms", "ui", "tests", "jssip-phone")}
rows: list[tuple[str, str, str, str, str]] = []


def sh(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def size(path: Path) -> str:
    return sh("du", "-sh", "--", str(path)).split("\t")[0] or "?"


def add(kind: str, what: str, sz: str, verdict: str, cmd: str = "") -> None:
    rows.append((kind, what, sz, verdict, cmd))


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
    return items[1:]  # первый — основное дерево сабмодуля


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
            f"git -C {shlex.quote(str(repo))} worktree prune")
    checked_out = set()
    for wt in worktrees(repo):
        path = Path(wt["worktree"])
        branch = wt.get("branch", "").removeprefix("refs/heads/")
        checked_out.add(branch)
        if path in STAND_MAIN or not path.exists():
            continue
        if not branch:
            if path.parent.name.endswith("-wt"):
                continue  # деревья ревизий стенда убирает instances-prune
            add("worktree", str(path), size(path), "оставить: без ветви, чьё — неясно")
            continue
        if sh("git", "status", "--porcelain", cwd=path):
            add("worktree", f"{path} [{branch}]", size(path), "оставить: грязное дерево")
            continue
        if in_live_session(path) or cwd_users(path):
            add("worktree", f"{path} [{branch}]", size(path), "оставить: живой сеанс или процесс")
            continue
        gone = not sh("git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=repo)
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, f"origin/{parent}"], cwd=repo
        ).returncode == 0
        if gone or merged:
            why = "ветвь удалена в origin" if gone else f"слита в {parent}"
            add("worktree", f"{path} [{branch}]", size(path), f"удалить: {why}",
                f"git -C {shlex.quote(str(repo))} worktree remove {shlex.quote(str(path))}")
    merged_branches = [
        b
        for b in sh("git", "branch", "--format=%(refname:short)", "--merged", f"origin/{parent}", cwd=repo).splitlines()
        # backup/* и saved/* — сохранённые копии, их удаляет только пользователь.
        if b not in PROTECTED and b not in checked_out and not b.startswith(("backup/", "saved/"))
    ]
    if merged_branches:
        add("ветви", f"{rel}: {len(merged_branches)} местных, слитых в origin/{parent}", "—", "удалить",
            f"git -C {shlex.quote(str(repo))} branch -d " + " ".join(shlex.quote(b) for b in merged_branches))

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
        add("scratchpad", str(d), size(d), "оставить: журнал сеанса свежее часа")
        continue
    wts = [ln.split()[0] for ln in sh("bash", "-c", f"find {shlex.quote(str(d))} -maxdepth 3 -name .git -type f").splitlines()]
    if wts:
        add("scratchpad", str(d), size(d), f"оставить: внутри worktree ({len(wts)}), убрать их строками выше")
        continue
    add("scratchpad", str(d), size(d), "удалить: сеанс завершён", f"rm -rf -- {shlex.quote(str(d))}")

if RESULTS.is_dir():
    for f in sorted(RESULTS.rglob("*")):
        if f.name not in ("video.webm", "trace.zip") or not f.is_file():
            continue
        failed = (f.parent / "error-context.md").exists() or any(f.parent.glob("test-failed-*.png"))
        if not failed:
            rm = f"rm -f -- {shlex.quote(str(f))}"
            if f.owner() == "root":
                rm = "sudo -A -p 'Удалить видео прошедшего теста, созданное root в контейнере' " + rm
            add("results", str(f.relative_to(STAND)), size(f), "удалить: тест прошёл", rm)
    # Пустые каталоги root остаются от контейнеров прогонов и сами не исчезают.
    for f in RESULTS.rglob("*"):
        try:
            if f.is_dir() and f.owner() == "root" and not any(f.iterdir()):
                add("results", str(f.relative_to(STAND)), "0", "удалить через sudo: пустой каталог root",
                    "sudo -A -p 'Удалить пустой каталог результатов, созданный root в контейнере' "
                    f"rmdir -- {shlex.quote(str(f))}")
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
cache = [ln for ln in sh("docker", "system", "df").splitlines() if ln.startswith("Build Cache")]
if cache:
    total, reclaim = cache[0].split()[4:6]
    add("docker", f"кэш сборки {total}", reclaim,
        "решение пользователя: следующая сборка пойдёт дольше", "docker builder prune -f")

print("| № | Вид | Что | Размер | Решение | Команда |")
print("|---|---|---|---|---|---|")
for i, (kind, what, sz, verdict, cmd) in enumerate(rows, 1):
    # Длинный префикс scratchpad сокращается только в колонке «Что»; команды остаются точными.
    what = what.replace(str(project), "<scratch>").replace(str(HOME), "~")
    print(f"| {i} | {kind} | {what} | {sz} | {verdict} | {('`' + cmd + '`') if cmd else ''} |")
