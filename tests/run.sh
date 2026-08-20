#!/usr/bin/env bash
# Hermetic + config-probe suite. Never PATH-selects a provider.
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$ROOT/python"
unset SWITCHGEAR_ALLOW_LIVE_PROVIDER || true
unset OPENCODE_PERMISSION || true

echo "== schema =="
/usr/bin/python3 "$ROOT/tests/schema/test_schemas.py"

echo "== machine-path gate =="
bash "$ROOT/tests/policy/test_no_machine_paths.sh"

echo "== noun gate =="
bash "$ROOT/tests/policy/test_no_project_nouns.sh"

echo "== doc link gate =="
bash "$ROOT/tests/policy/test_no_dangling_doc_links.sh"

echo "== provider discovery (no hardcoded installs) =="
/usr/bin/python3 "$ROOT/tests/test_discovery.py"

echo "== adversarial (absolute mock) =="
/usr/bin/python3 "$ROOT/tests/test_adversarial.py"

echo "== adapters / normalized vocabulary (real captured stream) =="
/usr/bin/python3 "$ROOT/tests/test_adapters.py"

echo "== credential classes (api-key + oauth) =="
/usr/bin/python3 "$ROOT/tests/test_credentials.py"

echo "== uid boundary (the worker is not you) =="
/usr/bin/python3 "$ROOT/tests/test_uid_boundary.py"

echo "== in-sandbox delegation (the client is the untrusted worker) =="
/usr/bin/python3 "$ROOT/tests/test_delegation.py"

echo "== guarded recursive deletes =="
/usr/bin/python3 "$ROOT/tests/test_safe_delete.py"

echo "== agent-directed content (injection at the review gate) =="
/usr/bin/python3 "$ROOT/tests/test_injection.py"

echo "== secret scanning of worker output =="
/usr/bin/python3 "$ROOT/tests/test_secrets.py"

echo "== doctor (broken installs, not just healthy ones) =="
/usr/bin/python3 "$ROOT/tests/test_doctor.py"

echo "== capabilities: derived facts, and declared ones labelled as such =="
/usr/bin/python3 "$ROOT/tests/test_capabilities.py"

echo "== contract fixture pack + published --json shape =="
/usr/bin/python3 "$ROOT/tests/test_contract_fixtures.py"

echo "== provider health + cost rollup =="
/usr/bin/python3 "$ROOT/tests/test_health.py"

echo "== quota / budget =="
/usr/bin/python3 "$ROOT/tests/test_quota.py"

echo "== live-smoke retry rule (hermetic) =="
bash "$ROOT/tests/test_live_retry.sh"

echo "== OpenCode config probe (no model) =="
/usr/bin/python3 "$ROOT/tests/test_config_probe.py"

echo
echo "ALL HERMETIC/ADVERSARIAL TESTS PASSED"
echo "(soak test is opt-in and takes minutes: bash tests/soak.sh)"
