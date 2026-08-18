#!/usr/bin/env bash
# Hermetic guard on tests/live.sh's live_job() retry rule.
#
# live.sh itself costs money and needs a network, so it cannot run in CI -- but
# its retry rule is the part most likely to rot into "retry until green", which
# would hide exactly the class of defect live.sh exists to catch. The rule:
#
#   retry ONLY when forwarded == 0  (never reached a model -> infrastructure)
#   never  when forwarded >  0      (a model answered and our handling broke)
#
# This extracts live_job() verbatim from live.sh and drives it with a stub CLI,
# so the rule is tested rather than merely read.
set -uo pipefail

LIVE="$(cd "$(dirname -- "$0")" && pwd)/live.sh"
D=$(mktemp -d -t aiops-retry-XXXXXX); trap 'rm -rf "$D"' EXIT

jq_() { python3 -c "import json,sys; d=json.load(open('$1')); print(d$2)" 2>/dev/null; }

sed -n '/^live_job() {/,/^}/p' "$LIVE" > "$D/fn.sh"
[ -s "$D/fn.sh" ] || { echo "FAIL: could not extract live_job() from $LIVE"; exit 1; }
# shellcheck disable=SC1090
. "$D/fn.sh"

CLI="$D/stub"; BASE=()
mkstub() {
  cat > "$CLI" <<STUB
#!/usr/bin/env bash
n=\$(cat "$D/count" 2>/dev/null || echo 0); n=\$((n+1)); echo \$n > "$D/count"
$1
STUB
  chmod +x "$CLI"; rm -f "$D/count"
}

FAILED=0
t() { # t <name> <expected_rc> <expected_call_count>
  local name=$1 erc=$2 ecalls=$3 rc calls
  live_job "$D/out.json"; rc=$?
  calls=$(cat "$D/count" 2>/dev/null || echo 0)
  if [ "$rc" = "$erc" ] && [ "$calls" = "$ecalls" ]; then
    printf '  PASS %s\n' "$name"
  else
    printf '  FAIL %s: rc=%s (want %s) calls=%s (want %s)\n' "$name" "$rc" "$erc" "$calls" "$ecalls"
    FAILED=1
  fi
}

mkstub 'echo "{\"status\":\"ok\",\"provider_calls\":{\"forwarded\":2}}"'
t "success returns 0 without retrying" 0 1

mkstub 'echo "{\"status\":\"awaiting_review\",\"provider_calls\":{\"forwarded\":3}}"'
t "awaiting_review counts as success" 0 1

mkstub 'echo "{\"status\":\"error\",\"provider_calls\":{\"forwarded\":1}}"'
t "a failure that reached a model is NOT retried" 1 1

mkstub 'echo "{\"status\":\"error\",\"provider_calls\":{\"forwarded\":0}}"'
t "a failure that reached no model retries, capped at 3" 1 3

mkstub 'echo "not json"; exit 1'
t "unparseable output is treated as transport, capped at 3" 1 3

mkstub 'if [ "$n" -lt 2 ]; then echo "{\"status\":\"error\",\"provider_calls\":{\"forwarded\":0}}"; else echo "{\"status\":\"ok\",\"provider_calls\":{\"forwarded\":1}}"; fi'
t "a transient transport failure recovers on retry" 0 2

exit $FAILED
