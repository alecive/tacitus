#!/usr/bin/env bash
#
# Cut a release tag: run the release tests on HEAD, then tag.
#
#   scripts/cut-tag.sh v1.2.0 [-m "one line"] [--force] [--skip-tests] [--push]
#
# Checks: on main, clean tree, the tag does not exist, CHANGELOG.md has a
# "## v1.2.0" heading (rename "## Unreleased" first). Then
# tests/release/run_release.py --tag v1.2.0 runs every lane against the
# models in effect (about an hour; the Claude runs use the shared quota).
# A green or amber verdict tags; red or harness stops, unless --force.
# The tag message carries the verdict line and the run id, so
# `git tag -n3` shows what each release was tested with.
#
# --skip-tests tags with no run (the message says so). --push pushes the
# tag to origin; nothing else is pushed.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TAG="${1:-}"
shift || true
MSG=""
FORCE=false
SKIP_TESTS=false
PUSH=false
while [[ $# -gt 0 ]]; do
    case $1 in
        -m) MSG="$2"; shift 2 ;;
        --force) FORCE=true; shift ;;
        --skip-tests) SKIP_TESTS=true; shift ;;
        --push) PUSH=true; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ ]]; then
    echo "usage: scripts/cut-tag.sh vX.Y.Z [-m \"one line\"] [--force] [--skip-tests] [--push]" >&2
    exit 2
fi

fail() { echo "cut-tag: $*" >&2; exit 1; }

BRANCH=$(git symbolic-ref --quiet --short HEAD || echo detached)
[[ "$BRANCH" == "main" ]] || $FORCE || fail "on $BRANCH, not main (--force to tag anyway)"
[[ -z "$(git status --porcelain)" ]] || $FORCE || fail "working tree is not clean"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    fail "tag $TAG already exists"
fi
if ! grep -qE "^## $TAG( |$)" CHANGELOG.md; then
    if grep -qE "^## Unreleased" CHANGELOG.md; then
        fail "CHANGELOG.md has no '## $TAG' heading; rename '## Unreleased' to '## $TAG ($(date +%F))' and commit"
    fi
    $FORCE || fail "CHANGELOG.md has no '## $TAG' heading (--force to tag without one)"
fi

HEAD_SHORT=$(git rev-parse --short HEAD)
VERDICT_LINE="Release tests: not run (--skip-tests)"
if ! $SKIP_TESTS; then
    if [[ -z "${EA_MODEL:-}" && -f "${EA_PRIVATE_DIR:-$HOME/.ea-private}/bot.env" ]]; then
        EA_MODEL=$(grep -E '^EA_MODEL=' "${EA_PRIVATE_DIR:-$HOME/.ea-private}/bot.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')
        export EA_MODEL
    fi
    echo "cut-tag: running the release tests on $HEAD_SHORT as $TAG (this takes about an hour)..."
    set +e
    OUT=$(python3 tests/release/run_release.py --tag "$TAG" 2> >(cat >&2))
    RC=$?
    set -e
    printf '%s\n' "$OUT"
    RUN_LINE=$(printf '%s\n' "$OUT" | grep -E "^Release tests " | head -1)
    VERDICT_LINE="${RUN_LINE:-Release tests: exited $RC with no report}"
    case $RC in
        0) ;;
        1) if printf '%s' "$RUN_LINE" | grep -q "RED"; then
               $FORCE || fail "a regression case failed; fix it or --force"
           else
               echo "cut-tag: amber (a lane's mean fell); tagging, the verdict is in the tag message"
           fi ;;
        2) $FORCE || fail "the suite could not run (harness); retry later or --force" ;;
        3) $FORCE || fail "no case ran" ;;
        *) $FORCE || fail "run_release.py exited $RC" ;;
    esac
fi

if [[ -z "$MSG" ]]; then
    MSG=$(awk -v tag="$TAG" '$0 ~ "^## " tag {getline; while ($0 ~ /^$/) getline; print; exit}' CHANGELOG.md)
    MSG="${TAG}: ${MSG:-release}"
fi
git tag -a "$TAG" -m "$MSG" -m "$VERDICT_LINE" -m "Commit $HEAD_SHORT; see CHANGELOG.md"
echo "cut-tag: tagged $TAG at $HEAD_SHORT"
echo "  $VERDICT_LINE"
if $PUSH; then
    git push origin "$TAG"
    echo "cut-tag: pushed $TAG to origin"
else
    echo "  push with: git push origin $TAG"
fi
