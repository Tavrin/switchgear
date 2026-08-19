#!/usr/bin/env bash
# The package must not contain absolute paths under anyone's home directory.
#
# It did: six of them in compat.py, which meant switchgear only worked on the
# machine it was written on. That is fatal for distribution and for CI, and it
# is invisible while you only ever run it in one place.
#
# Fixtures are exempt: they are captured provider output and the paths inside
# them are evidence of what a real run looked like, not code that runs.
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

hits=$(grep -rn -E '(^|[^A-Za-z0-9_])/(home|Users)/[a-z]' \
        --include='*.py' --include='*.sh' --include='*.json' --include='*.toml' \
        python bin tests project-profiles models commands schemas 2>/dev/null \
      | grep -v '^tests/fixtures/' \
      | grep -v 'test_no_machine_paths.sh' || true)

if [ -n "$hits" ]; then
  echo "FAIL - machine-specific paths found:" >&2
  echo "$hits" >&2
  echo >&2
  echo "Provider installs are DISCOVERED (compat.DISCOVERY) or named in an" >&2
  echo "operator file (SWITCHGEAR_PROVIDERS_FILE). Tests must read the discovered" >&2
  echo "path and skip when nothing is installed." >&2
  exit 1
fi
echo "ok - no machine-specific paths (scanned: python bin tests project-profiles models commands schemas)"
