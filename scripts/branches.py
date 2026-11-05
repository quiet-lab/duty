"""Таблица ветвей в origin каждого репозитория с предложением по каждой.

Только чтение. Данные берутся из местных refs/remotes без fetch; git
не меняет ничего, к Bitbucket идут только запросы GET. В конце печатается
дата последнего fetch каждого репозитория: по ней видно, насколько свежи
сведения.

Использование: branches.py [all|<подстрока репозитория>] [--mine] [--no-pr]
[--root <корень проекта>]

Защищённые и рабочие ветви, префиксы задач, ключ родителя сабмодуля, шаблон
тикета и адрес Bitbucket берутся из `.duty/duty.toml` (`reference/config.md`:
`[git]`, `[jira]`, `[bitbucket]`). Без `[bitbucket]` скрипт работает как с --no-pr.

--mine   только ветви пользователя (git config user.name): последний коммит его
         или не менее половины собственных коммитов ветви;
--no-pr  не обращаться к Bitbucket (колонка PR пуста).
"""

import importlib.util
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Каталог скрипта убран из sys.path ключом -I, поэтому модули подключаются по пути.
_spec = importlib.util.spec_from_file_location(
    "dutylib", Path(__file__).with_name("dutylib.py")
)
dutylib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dutylib)
gates = dutylib.import_path("gates", Path(__file__).with_name("gates.py"))

# Заполняются из конфигурации в setup(): ветви, которых в таблице нет
# (git.protected и git.working), префиксы сохранённых копий и задач.
HIDDEN: set[str] = set()
KEEP_PREFIXES: tuple[str, ...] = ()
TASK_PREFIXES: tuple[str, ...] = ()
NAMED_DEV = False
MAX_PR_PAGES = 5


def setup(cfg) -> None:
    global HIDDEN, KEEP_PREFIXES, TASK_PREFIXES, NAMED_DEV
    HIDDEN = set(cfg.get("git.protected", [])) | set(cfg.get("git.working", []))
    KEEP_PREFIXES = tuple(cfg.get("git.keep_prefixes", []))
    TASK_PREFIXES = tuple(
        dict.fromkeys([*cfg.get("git.task_prefixes", []), *KEEP_PREFIXES])
    )
    NAMED_DEV = bool(cfg.get("git.named_dev_branches", False))


def named_dev_branch(name):
    # Именная ветвь разработчика: имя без префикса и без косой черты;
    # её собирает Jenkins, слитой она не считается. Без git.named_dev_branches
    # именных ветвей нет.
    return (
        NAMED_DEV
        and "/" not in name
        and not name.startswith(TASK_PREFIXES)
        and name not in HIDDEN
    )


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def git_ok(repo: Path, *args: str) -> bool:
    return (
        subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, check=False
        ).returncode
        == 0
    )


def repos(root: Path, cfg) -> list[tuple[str, Path, str]]:
    """Репозитории: имя, каталог, родитель (ключ git.parent_key, иначе branch, иначе git.default_parent)."""
    default = cfg.get("git.default_parent", "master")
    parent_key = cfg.get("git.parent_key", "parent")
    res = [(".", root, default)]
    out = git(root, "config", "-f", ".gitmodules", "--get-regexp", r"\.path$")
    for line in out.splitlines():
        key, path = line.split()
        name = key.removeprefix("submodule.").removesuffix(".path")
        parent = git(
            root, "config", "-f", ".gitmodules", f"submodule.{name}.{parent_key}"
        ) or git(root, "config", "-f", ".gitmodules", f"submodule.{name}.branch")
        res.append((path, root / path, parent or default))
    return res


def bb_slug(repo: Path) -> tuple[str, str] | None:
    m = re.search(
        r"/scm/([^/]+)/([^/]+?)(?:\.git)?$", git(repo, "remote", "get-url", "origin")
    )
    return (m.group(1).upper(), m.group(2)) if m else None


def prs_by_branch(key: str, slug: str) -> dict[str, dict]:
    """Последний pull request на ветвь: открытый важнее, иначе самый свежий."""
    best: dict[str, dict] = {}
    start = 0
    for _ in range(MAX_PR_PAGES):
        page = gates.bb(
            f"/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests?state=ALL&order=NEWEST&limit=100&start={start}"
        )
        for p in page["values"]:
            name = p["fromRef"]["displayId"]
            cur = best.get(name)
            if cur is None or (p["state"] == "OPEN" and cur["state"] != "OPEN"):
                best[name] = p
        if page.get("isLastPage", True):
            break
        start = page["nextPageStart"]
    return best


def green(sha: str) -> str:
    """Итог сборок коммита: зелёные, красные, идут или нет сборок."""
    latest: dict[str, dict] = {}
    for s in gates.bb(f"/rest/build-status/1.0/commits/{sha}")["values"]:
        if gates.is_feed(s):
            continue
        if s["key"] not in latest or s.get("dateAdded", 0) > latest[s["key"]].get(
            "dateAdded", 0
        ):
            latest[s["key"]] = s
    states = {s["state"] for s in latest.values()}
    if not states:
        return "нет сборок"
    if "FAILED" in states:
        return "красные"
    if "INPROGRESS" in states:
        return "идут"
    return "зелёные" if states == {"SUCCESSFUL"} else "неясно"


def own_share(repo: Path, base: str, name: str, me: str, me_mail: str) -> float:
    """Доля коммитов пользователя среди собственных (без слияний) коммитов ветви.

    Ветвь, в которую вошли чужие коммиты при слиянии родителя, не должна
    считаться ветвью пользователя из-за одного его коммита среди десятков.
    """
    log = git(
        repo, "log", "--no-merges", "--format=%an\t%ae", f"{base}..origin/{name}"
    ).splitlines()
    if not log:
        return 0.0
    own = sum(
        1
        for line in log
        if line.split("\t")[0] == me or line.split("\t")[1].split("@")[0] == me_mail
    )
    return own / len(log)


def worktree_branches(repo: Path) -> dict[str, str]:
    res, path = {}, ""
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line.startswith("branch "):
            res[line.removeprefix("branch refs/heads/")] = path
    return res


def cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip() or "—"


def main() -> int:
    argv = sys.argv[1:]
    mine_only = "--mine" in argv
    no_pr = "--no-pr" in argv
    explicit = argv[argv.index("--root") + 1] if "--root" in argv else None
    skip = {"--mine", "--no-pr", "--root", explicit}
    words = [a for a in argv if a not in skip]
    want = words[0] if words and words[0] != "all" else None
    try:
        cfg = gates.config(explicit)
    except gates.duty_config.ConfigError as e:
        print(f"ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    root = cfg.root
    if root is None:
        print(
            "корень проекта не найден: запустите из репозитория git или задайте --root <корень>",
            file=sys.stderr,
        )
        return 2
    setup(cfg)
    ticket_re = cfg.get("jira.ticket", "") if cfg.has("jira") else ""
    if not cfg.has("bitbucket"):
        no_pr = True
        print(gates.missing_section("bitbucket", "pull request'ы") + ".")
    me = git(root, "config", "user.name")
    me_mail = git(root, "config", "user.email").split("@")[0]
    is_me = lambda name, mail: (
        name == me or (me_mail and mail.split("@")[0].strip("<>") == me_mail)
    )

    print(
        "| Репозиторий | Ветвь | Последний коммит | Впереди/позади | PR | Тикет | Worktree | Предложение |"
    )
    print("|---|---|---|---|---|---|---|---|")
    fetches: list[tuple[float, str]] = []
    counts: dict[str, int] = {}
    denied = ""
    notes: list[str] = []
    for rel, repo, parent in repos(root, cfg):
        if (
            want
            and want not in rel
            and want not in (str(bb_slug(repo)) if bb_slug(repo) else "")
        ):
            continue
        if not (repo / ".git").exists():
            notes.append(f"{rel}: каталог не развёрнут")
            continue
        base = f"origin/{parent}"
        if not git_ok(repo, "rev-parse", "--verify", "--quiet", base):
            notes.append(f"{rel}: нет {base}")
            continue
        fh = Path(
            git(repo, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD")
        )
        if fh.exists():
            fetches.append((fh.stat().st_mtime, rel))
        fmt = (
            "%(refname:short)\t%(objectname)\t%(committerdate:unix)\t%(authorname)\t%(authoremail)\t%(ahead-behind:"
            + base
            + ")\t%(subject)"
        )
        branches = []
        for line in git(
            repo, "for-each-ref", f"--format={fmt}", "refs/remotes/origin"
        ).splitlines():
            ref, sha, ts, an, ae, ab, subj = line.split("\t", 6)
            name = ref.removeprefix("origin/")
            if ref == "origin" or name == "HEAD" or name in HIDDEN:
                continue
            ahead, behind = (int(x) for x in ab.split())
            branches.append((name, sha, int(ts), an, ae, ahead, behind, subj))
        if not branches:
            continue
        prs: dict[str, dict] = {}
        slug = bb_slug(repo)
        if not no_pr and not denied and slug:
            try:
                prs = prs_by_branch(*slug)
            except gates.NotConfigured as e:
                no_pr = True
                print(f"{e}.")
            except gates.BitbucketDenied as e:
                denied = str(e)
        wts = worktree_branches(repo)
        for name, sha, ts, an, ae, ahead, behind, subj in sorted(
            branches, key=lambda b: -b[2]
        ):
            mine = is_me(an, ae) or (
                ahead > 0 and own_share(repo, base, name, me, me_mail) >= 0.5
            )
            if mine_only and not mine:
                continue
            merged = ahead == 0
            if not merged and ahead <= 300:
                cherry = git(repo, "cherry", base, f"origin/{name}")
                merged = bool(cherry) and not any(
                    c.startswith("+") for c in cherry.splitlines()
                )
            pr = prs.get(name)
            pr_cell, ci = "—", ""
            if pr:
                link = pr["links"]["self"][0]["href"]
                pr_cell = f"[#{pr['id']}]({link}) {pr['state'].lower()}"
                if pr["state"] == "OPEN":
                    try:
                        ci = green(pr["fromRef"]["latestCommit"])
                    except gates.NotConfigured:
                        pass
                    except gates.BitbucketDenied as e:
                        denied = str(e)
                    pr_cell += f", сборки: {ci or 'не проверены'}"
                    if pr["fromRef"]["latestCommit"] != sha:
                        pr_cell += " (местная ссылка отстаёт)"
            elif no_pr or denied:
                pr_cell = "не проверен"
            # Предложение. Чужие ветви не трогаются, даже слитые.
            if named_dev_branch(name) and not mine:
                prop = f"оставить: именная, владелец {an}"
            elif not mine:
                prop = f"решить: чужая ветвь ({an})" + (
                    "; слита в родителя" if merged else ""
                )
            elif name.startswith(KEEP_PREFIXES):
                prop = "оставить: сохранённая копия"
            elif pr and pr["state"] == "OPEN":
                if ahead == 0:
                    prop = "решить: PR открыт, коммитов впереди нет"
                elif ci == "зелёные":
                    prop = "слить: PR открыт, сборки зелёные"
                else:
                    prop = f"решить: PR открыт, сборки: {ci or 'не проверены'}"
            elif merged:
                prop = f"удалить: слита в {parent}" + (
                    " (содержимое вошло иначе, чем коммитами)" if ahead else ""
                )
            elif pr and pr["state"] == "MERGED":
                same = pr["fromRef"]["latestCommit"] == sha
                prop = (
                    "удалить: PR слит"
                    if same
                    else "решить: после слияния PR добавлены коммиты"
                )
            elif pr and pr["state"] == "DECLINED":
                prop = f"решить: PR отклонён, коммитов впереди: {ahead}"
            elif no_pr or denied:
                prop = f"решить: PR не проверен, коммитов впереди: {ahead}"
            else:
                prop = f"оставить: коммитов впереди: {ahead}, PR нет"
            counts[prop.split(":")[0]] = counts.get(prop.split(":")[0], 0) + 1
            ticket = (
                ", ".join(dict.fromkeys(re.findall(ticket_re, name, re.IGNORECASE)))
                if ticket_re
                else ""
            )
            ticket = ticket or "—"
            date = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
            wt = wts.get(name)
            wt_cell = f"да: {'/'.join(Path(wt).parts[-2:])}" if wt else "нет"
            print(
                f"| {rel} | {cell(name)} | {date}, {cell(an)}: {cell(subj[:60])} | +{ahead}/−{behind} | "
                f"{cell(pr_cell)} | {ticket.upper()} | {wt_cell} | {cell(prop)} |"
            )
    print()
    print(
        "Итого: "
        + (", ".join(f"{k} — {v}" for k, v in sorted(counts.items())) or "ветвей нет")
    )
    if fetches:
        fetches.sort()
        fmt_t = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")
        age_h = (time.time() - fetches[0][0]) / 3600
        print(
            f"Последний fetch: самый старый — {fetches[0][1]} {fmt_t(fetches[0][0])} ({age_h:.0f} ч назад), "
            f"самый свежий — {fetches[-1][1]} {fmt_t(fetches[-1][0])}. Данные из местных refs/remotes."
        )
    for n in notes:
        print(f"Не проверено: {n}.")
    if denied:
        print(
            f"ОСТАНОВКА: {denied}. Повторять нельзя: четыре неверных входа блокируют учётную запись."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
