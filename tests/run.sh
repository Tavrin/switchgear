#!/usr/bin/env bash
# Hermetic + config-probe suite. Never PATH-selects a provider.
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$ROOT/python"
unset AI_OPS_ALLOW_LIVE_PROVIDER || true
unset OPENCODE_PERMISSION || true

echo "== schema =="
/usr/bin/python3 "$ROOT/tests/schema/test_schemas.py"

echo "== noun gate =="
bash "$ROOT/tests/policy/test_no_project_nouns.sh"

echo "== adversarial (absolute mock) =="
/usr/bin/python3 "$ROOT/tests/test_adversarial.py"

echo "== adapters / normalized vocabulary (real captured stream) =="
/usr/bin/python3 "$ROOT/tests/test_adapters.py"

echo "== credential classes (api-key + oauth) =="
/usr/bin/python3 "$ROOT/tests/test_credentials.py"

echo "== quota / budget =="
/usr/bin/python3 "$ROOT/tests/test_quota.py"

echo "== live-smoke retry rule (hermetic) =="
bash "$ROOT/tests/test_live_retry.sh"

echo "== OpenCode config probe (no model) =="
/usr/bin/python3 "$ROOT/tests/test_config_probe.py"

echo
echo "ALL HERMETIC/ADVERSARIAL TESTS PASSED"
