#!/usr/bin/env bash
# Создание песочницы для проб плагина duty.
#
# Использование: make-sandbox.sh <каталог> [--reset]
#
# Состав каталога:
#   repo/        git-репозиторий, ветвь master, один коммит: пакет sandbox_pkg
#                из двух модулей и тесты; тест test_threshold_is_inclusive
#                падает намеренно (порог скидки `>` вместо `>=`);
#   origin.git/  голый репозиторий, `origin` для repo;
#   marker/      пустой каталог для файлов-отметок проб;
#   venv/        виртуальное окружение с pytest (в системном python3 его нет).
#
# Каталог уже есть и не пуст — отказ, если не задан --reset. С --reset
# сценарий возвращает существующую песочницу к исходному состоянию: удаляет
# ветви probe/* в origin.git и в repo, чистит marker/ и .render/,
# восстанавливает рабочее дерево repo до master.

set -euo pipefail

die() { printf 'make-sandbox.sh: %s\n' "$*" >&2; exit 1; }

dir= reset=0
while [ $# -gt 0 ]; do
    case $1 in
        --reset) reset=1 ;;
        -h | --help) sed -n '2,18p' "$0"; exit 0 ;;
        -*) die "неизвестный ключ $1" ;;
        *) [ -z "$dir" ] || die 'каталог задан дважды'; dir=$1 ;;
    esac
    shift
done
[ -n "$dir" ] || die 'использование: make-sandbox.sh <каталог> [--reset]'

git_id=(-c user.name='duty probes' -c user.email='duty-probes@localhost')

if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2> /dev/null)" ]; then
    [ "$reset" = 1 ] || die "$dir не пуст; для возврата к исходному состоянию — --reset"
    [ -d "$dir/repo/.git" ] && [ -d "$dir/origin.git" ] || die "$dir не похож на песочницу (нет repo или origin.git)"
    dir=$(realpath "$dir")
    # Ветви проб: в origin.git — по списку ссылок, в repo — местные и отслеживаемые.
    while IFS= read -r ref; do
        [ -n "$ref" ] && git -C "$dir/origin.git" update-ref -d "$ref"
    done < <(git -C "$dir/origin.git" for-each-ref --format='%(refname)' 'refs/heads/probe/')
    git -C "$dir/repo" checkout -q -f master
    git -C "$dir/repo" reset -q --hard master
    git -C "$dir/repo" clean -fdq
    while IFS= read -r br; do
        [ -n "$br" ] && git -C "$dir/repo" branch -q -D "$br"
    done < <(git -C "$dir/repo" for-each-ref --format='%(refname:short)' 'refs/heads/probe/')
    git -C "$dir/repo" remote prune origin > /dev/null 2>&1 || true
    mkdir -p "$dir/marker"
    find "$dir/marker" -mindepth 1 -delete
    rm -rf "$dir/.render"
    printf 'песочница %s возвращена к исходному состоянию\n' "$dir"
    exit 0
fi

mkdir -p "$dir"
dir=$(realpath "$dir")
mkdir -p "$dir/marker" "$dir/repo/sandbox_pkg" "$dir/repo/tests"

cd "$dir/repo"
git init -q -b master
: > sandbox_pkg/__init__.py
printf 'Песочница для проб плагина duty, вне проекта.\n' > README.md
printf '__pycache__/\n.pytest_cache/\n' > .gitignore

cat > pyproject.toml <<'PY'
[project]
name = "sandbox-pkg"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
test = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
PY

cat > sandbox_pkg/pricing.py <<'PY'
"""Расчёт скидки."""

DISCOUNT_THRESHOLD = 100.0
DISCOUNT_RATE = 0.1


def discount(amount: float) -> float:
    """Возвращает сумму скидки; скидка даётся от порога включительно."""
    if amount > DISCOUNT_THRESHOLD:
        return round(amount * DISCOUNT_RATE, 2)
    return 0.0


def total(amount: float) -> float:
    """Возвращает сумму к оплате после скидки."""
    return round(amount - discount(amount), 2)
PY

cat > sandbox_pkg/report.py <<'PY'
"""Сводка по заказам."""

from sandbox_pkg.pricing import discount, total


def summary(amounts: list[float]) -> dict[str, float]:
    """Возвращает сумму заказов, скидок и итог к оплате."""
    return {
        "gross": round(sum(amounts), 2),
        "discount": round(sum(discount(a) for a in amounts), 2),
        "net": round(sum(total(a) for a in amounts), 2),
    }
PY

cat > tests/test_pricing.py <<'PY'
from sandbox_pkg.pricing import discount, total


def test_below_threshold_has_no_discount():
    assert discount(50.0) == 0.0


def test_above_threshold_has_discount():
    assert discount(200.0) == 20.0


def test_threshold_is_inclusive():
    assert discount(100.0) == 10.0


def test_total_applies_discount():
    assert total(200.0) == 180.0
PY

cat > tests/test_report.py <<'PY'
from sandbox_pkg.report import summary


def test_summary_empty():
    assert summary([]) == {"gross": 0.0, "discount": 0.0, "net": 0.0}


def test_summary_mixed():
    assert summary([50.0, 200.0]) == {"gross": 250.0, "discount": 20.0, "net": 230.0}
PY

git add .gitignore README.md pyproject.toml sandbox_pkg tests
git "${git_id[@]}" commit -q -m 'Песочница для проб duty'

git init -q --bare -b master "$dir/origin.git"
git remote add origin "$dir/origin.git"
git push -q -u origin master
git remote set-head origin master

python3 -m venv "$dir/venv"
"$dir/venv/bin/pip" install -q pytest

printf 'песочница создана: %s\n' "$dir"
