#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname -- "$0")/../.." && pwd)
AGENT_OPS_ROOT=$ROOT
# shellcheck source=../helpers/assert.sh
source "$ROOT/tests/helpers/assert.sh"
export TEST_STATE
TEST_STATE=$(mktemp -d)
trap 'rm -rf "$TEST_STATE"' EXIT
export AI_OPS_STATE=$TEST_STATE
PATH="$ROOT/tests/helpers:$ROOT/bin:$PATH"

write_profile "$TEST_STATE/profile.json"

# missing git
set +e
err=$(ai-opencode --profile "$TEST_STATE/profile.json" scout "$TEST_STATE" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "not a git" "missing git"
pass "missing git refused"

git init -q -b main "$TEST_STATE/repo"
git -C "$TEST_STATE/repo" config user.email t@t
git -C "$TEST_STATE/repo" config user.name t
printf 'a\n' > "$TEST_STATE/repo/f"
git -C "$TEST_STATE/repo" add f
git -C "$TEST_STATE/repo" commit -q -m i

# timeout 0
set +e
err=$(AI_OPENCODE_TIMEOUT=0 ai-opencode --profile "$TEST_STATE/profile.json" scout "$TEST_STATE/repo" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "not a disable" "timeout 0"
pass "timeout 0 refused"

# foreign config dir
set +e
err=$(OPENCODE_CONFIG_DIR=/tmp/evil ai-opencode --profile "$TEST_STATE/profile.json" scout "$TEST_STATE/repo" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "OPENCODE_CONFIG_DIR" "config dir"
pass "foreign OPENCODE_CONFIG_DIR refused"

# state inside target
set +e
err=$(AI_OPS_STATE="$TEST_STATE/repo/state" ai-opencode --profile "$TEST_STATE/profile.json" scout "$TEST_STATE/repo" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "inside" "state inside target"
pass "state-inside-target refused"

# state symlink into target
ln -s "$TEST_STATE/repo" "$TEST_STATE/state-link"
set +e
err=$(AI_OPS_STATE="$TEST_STATE/state-link" ai-opencode --profile "$TEST_STATE/profile.json" scout "$TEST_STATE/repo" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
pass "state symlink into target refused"

# write kill switch on example profile
set +e
err=$(ai-opencode --profile "$ROOT/project-profiles/example.json" write "$TEST_STATE/repo" implement --envelope "$ROOT/tests/schema/fixtures/envelope-object-commands.json" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "write is disabled" "kill switch"
pass "write kill switch"

# containment required + missing backend
write_profile "$TEST_STATE/cprofile.json" \
  'profile["containment"]={"mode":"bwrap","required":True}' \
  'profile["write_enabled"]=True'
set +e
err=$(AI_OPS_BWRAP=/no/such/bwrap AI_OPS_WRITE=1 ai-opencode --profile "$TEST_STATE/cprofile.json" scout "$TEST_STATE/repo" "x" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
assert_contains "$err" "no silent fallback" "containment refuse"
pass "required containment missing backend refuses"
