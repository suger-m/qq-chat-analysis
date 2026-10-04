#!/usr/bin/env bash
# Relative output paths stay relative to the caller, including through symlinks.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="$HERE/qq.py"
[ -f "$ENTRY" ] || { echo 'Incomplete skill package: scripts/qq.py is missing.' >&2; exit 4; }
if [ "${1:-}" = '--python-path' ] && [ "$#" -eq 1 ]; then
  set -- --python-path
fi
export PYTHONDONTWRITEBYTECODE=1
usable() {
  "$1" -c 'import sys; assert sys.version_info >= (3,10); import blackboxprotobuf, cryptography' >/dev/null 2>&1
}
if [ -n "${QQ_PY:-}" ]; then
  PY="$(command -v -- "$QQ_PY" || true)"
  if [ -z "$PY" ] || ! usable "$PY"; then
    echo 'QQ_PY must select Python 3.10+ with the core dependencies. See references/install.md.' >&2
    exit 4
  fi
  exec "$PY" "$ENTRY" "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run --locked --script "$ENTRY" "$@"
fi
for name in python3 python; do
  PY="$(command -v "$name" || true)"
  if [ -n "$PY" ] && usable "$PY"; then
    exec "$PY" "$ENTRY" "$@"
  fi
done
echo 'Install uv, or select Python 3.10+ with QQ_PY and install scripts/runtime/requirements.txt. See references/install.md.' >&2
exit 4
