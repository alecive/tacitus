#!/usr/bin/env bash
# Regenerate deploy/docker/requirements.lock.
#
# Installs the top-level packages from deploy/docker/requirements.top in the
# pinned Python base image (the PYTHON_IMAGE default in the Dockerfile),
# then writes every resolved version to requirements.lock. Edit
# requirements.top to move a top-level version, then run this script and
# rebuild the image.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE="$(sed -n 's/^ARG PYTHON_IMAGE=//p' "$ROOT/Dockerfile" | head -1)"
[ -n "$IMAGE" ] || { echo "lock.sh: no PYTHON_IMAGE default in the Dockerfile" >&2; exit 1; }

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT
docker run --rm -v "$HERE:/w:ro" "$IMAGE" sh -euc '
    pip install -q --disable-pip-version-check -r /w/requirements.top >&2
    pip check >&2
    pip freeze --all --exclude pip --exclude setuptools --exclude wheel
' > "$OUT"

{
    sed -n '1,/^[^#]/{/^#/p}' "$HERE/requirements.lock"
    cat "$OUT"
} > "$HERE/requirements.lock.new"
mv "$HERE/requirements.lock.new" "$HERE/requirements.lock"
echo "Wrote $HERE/requirements.lock ($(grep -vc '^#' "$HERE/requirements.lock") packages)."
