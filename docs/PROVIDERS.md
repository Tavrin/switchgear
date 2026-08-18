# Provider adapters: measured ground truth

What is actually true of each candidate provider on this machine, established by
running the binaries rather than by reading their documentation. Two claims in
`HANDOFF.md` turned out to be wrong; both are corrected below.

The adapter seam is `python/ai_ops/adapters.py`: `argv()`, `version_argv()`,
`agent_name()`, `session_id()`, `normalize()`. `get_adapter()` keys off the
profile's `provider` field — the **binary**, not the model pool. `opencode-go`
and `openrouter` are both reached through one `opencode` binary, so selecting an
adapter by pool would already be wrong today.

**Rule for adding one: no adapter ships on documentation.** Capture a real event
stream and commit it as a fixture, and settle the isolation question with a
no-model probe inside the real sandbox. The worst defect in this project's
history was a mock that invented a vocabulary the provider never emitted.

---

## OpenCode 1.18.x — shipped

Vocabulary captured live and committed as
`tests/fixtures/opencode-real-scout.jsonl`. Isolation settled by finding F04
(four review rounds). See `adapters.py` for the measured event shapes.

---

## Codex 0.147.0 — isolation PROVEN, credentials BLOCKED

### Probe method

`codex doctor` (its own config/auth/runtime diagnosis) run on the host, then run
again under `sandbox.build_bwrap_argv` with a synthetic HOME, and the two
resolutions diffed. This is the technique that settled F04.

### Result: the synthetic HOME isolates Codex completely

| Resolved | Host | Inside the sandbox |
|---|---|---|
| auth | configured, `~/.codex/auth.json`, mode `chatgpt` | **none found** |
| MCP servers | **3** (stdio) | **0** |
| rollouts / history | 1,173 files, 3.08 GB | 0 files, 0 B |
| state DB | present, integrity ok | missing |
| `config.toml` | the real one | absent (inside the synthetic HOME) |

`CODEX_HOME` defaults to `$HOME/.codex`, so the synthetic HOME alone is
sufficient — no override is required, though an adapter should still set it
explicitly rather than depend on that default holding.

Host MCP servers and plugins do **not** leak in. That is the F04 question, and
for Codex the answer is clean.

### Two findings that would have cost real time

**The `codex` on PATH is an npm shim, not the binary.** It is a `#!/usr/bin/env
node` script; running it inside the sandbox fails with
`/usr/bin/env: 'node': No such file or directory`. An adapter must pin the
vendored static binary
(`.../@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex`,
static-pie, no interpreter), which is also what the never-PATH-lookup rule
demands. Pinning the shim would additionally require putting node inside the
boundary.

**The blocker: Codex auth is ChatGPT OAuth, not an API key.** `stored auth mode:
chatgpt`, `stored API key: false`, and the probe's own reachability check reports
`reachability mode: ChatGPT auth`. The credential broker is built for an
OpenAI-compatible HTTP surface: it injects `Authorization: Bearer <key>`, pins
the request model, and allowlists `/chat/completions` and `/messages`. A ChatGPT
OAuth session does not map onto that.

So a Codex adapter needs a decision before it can be written, not during:

1. **API-key path** — give Codex a real API key via its supported env var and
   route it through the existing broker. Cleanest fit for the current design;
   costs a separately-billed key.
2. **Broker a different upstream shape** — teach the broker Codex's backend.
   Wider blast radius: the allowlist and model pin are load-bearing security
   properties, not conveniences.
3. **Let the credential into the sandbox** — reopens the exfiltration residual
   the broker was built to close. Not recommended.

Until that is settled, Codex fails closed: no credential in a synthetic HOME
means no work, which the probe confirms (`auth: none found`).

Note the probe ran **without** a broker and therefore without `--unshare-net`,
which is why it could reach the network at all and returned a 401 handshake. With
a broker the network namespace applies and that call would not leave the sandbox.

---

## Grok — isolation proven, vocabulary captured, adapter shipped

`HANDOFF.md` states Grok has "**no** agent/permission model at all, so it would
rely purely on the OS boundary." That is not what the binary reports.
`~/.grok/bin/grok --help` shows:

- `--agent <NAME>` — agent name or definition file path
- `--agents <JSON>` — inline subagent definitions
- `--allow <RULE>` / `--deny <RULE>` — permission rules
- `--always-approve` — auto-approve all tool execution (the thing to never set)
- `-p` headless single-turn, with `--output-format`:
  - `json`
  - `streaming-json` — NDJSON of native ACP session updates
  - `streaming-messages-json` — NDJSON in the **Anthropic Messages wire format**

So Grok has both an agent model and a permission model, and a streaming NDJSON
output an adapter can normalize. On surface alone it is a *better* adapter target
than Codex: headless mode, structured streaming, allow/deny rules, and
`~/.grok/auth.json` is a file-based credential store rather than an OAuth
session.

### Isolation: PROVEN, and it was the most exposed of the three

`grok inspect` ("show the configuration Grok discovers for this directory") is
the config dump. Host vs the same binary under `build_bwrap_argv` with a
synthetic HOME:

| Resolved | Host | Inside the sandbox |
|---|---|---|
| Project instructions | **`~/.claude/CLAUDE.md`** (~2448 tokens) | 0 — none |
| Permissions | **375 rules** from `~/.claude/settings.local.json` | 0 loaded |
| Skills | **46** (user + bundled, several tagged `[claude]`) | 0 |
| Plugins / MCP / LSP / Hooks | present | 0 / 0 / 0 / 0 |
| Config sources | user + project | none |
| Agents | host set | 3 builtin only |

Note what the host row says: Grok reads **Claude Code's own configuration** — its
CLAUDE.md as agent instructions and its `settings.local.json` as permission
rules. Its "harness compatibility" layer deliberately ingests other harnesses'
config. That is a far larger leak surface than OpenCode's, and it is exactly the
F04 class of problem: a delegated agent inheriting the operator's own harness
instructions and permission grants. The synthetic HOME closes all of it.

One fail-open default worth recording: `Project trusted: yes` in **both** runs.
On the host that comes from `trusted_folders.toml`; inside, with no config at
all, it still defaults to trusted. Grok's own trust gate is therefore not a
control we can lean on — the OS boundary is, which is the standing posture
anyway.

### Vocabulary: CAPTURED, adapter shipped

`tests/fixtures/grok-real-scout.jsonl` — 77 events from a live
`-p --output-format streaming-json` run (grok 1.0.4, grok-4.6-build, $0.0067).
Three ways the real dialect differs from anything a doc would have predicted:

- **`text` and `thought` arrive as deltas**, often one token per event. The
  adapter coalesces; a per-event normalizer would emit hundreds of fragments.
- **`sessionId` exists only in the terminal `end` event.** Mid-run the stream
  carries no session identifier at all, so resume-after-crash has nothing to key
  on until the run closes. Reported honestly as `None` until then.
- Cost and turns arrive **pre-totalled** on `end` (`total_cost_usd`,
  `num_turns`, `usage.total_tokens`), unlike OpenCode's per-step summation.

`thought` content never reaches the digest — reasoning belongs to the full
stream for a human, not to a projection that lands in a parent agent's context.
Only `end_turn` was observed as a normal close; any other `stopReason` is
reported as `failed` with the provider's own word for it, because guessing which
unobserved reasons are benign would be the invented-vocabulary mistake again.

Its credential is an **OAuth/OIDC session**, not an API key — see the next
section, which is what stands between the shipped adapter and a live lane.

---

## The OAuth problem, and the plan of record (2026-08-18)

The stated target is one rail invoking **OpenCode, Grok, Codex (ChatGPT
account), and Claude Code (Claude account)**. Only the first uses an API key.
The other three are OAuth-session CLIs — and measured on this machine, their
credentials are **structurally identical** (key names inspected, values never
read):

| Provider | File | Contents |
|---|---|---|
| Grok | `~/.grok/auth.json` | access token (`key`), `refresh_token`, `expires_at`, **`oidc_issuer` + `oidc_client_id`** |
| Codex | `~/.codex/auth.json` | `tokens.access_token`, `tokens.refresh_token`, `account_id`; `OPENAI_API_KEY: null` |
| Claude Code | `~/.claude/.credentials.json` | `claudeAiOauth.accessToken`, `.refreshToken`, `.expiresAt` |

Every one is a short-lived access token plus a long-lived refresh token in a
file the controller can read. That uniformity is the answer to "isn't there a
good solution": **the broker generalizes from "holds an API key" to "holds a
credential of some class"**, and OAuth is just the second class.

### The design: credential classes in the broker

- **class `api-key`** (opencode-go, openrouter): today's path, unchanged.
- **class `oauth`**: the controller reads the session file, performs the token
  refresh **controller-side** (Grok's file literally carries its OIDC issuer and
  client id — refresh is a plain POST; the other two have known refresh flows),
  holds the access token in memory, and the broker injects
  `Authorization: Bearer <access-token>` upstream. The sandbox keeps exactly
  what it has today: `--unshare-net`, a unix socket, a placeholder credential.

The invariant that must survive, stated once: **the refresh token never crosses
any boundary** — not into the sandbox, not into a child env, not into a log. It
is the whole subscription; an access token expires in about an hour, a refresh
token does not.

### Measured per-CLI hooks — and a correction to what I claimed

I originally read env-var NAMES out of the binaries and concluded that both the
redirect and the token injection were available, so the full containment tier
would extend to all three OAuth CLIs. **Half of that was wrong, and running it
is what showed the difference.** Recording it here because inferring behaviour
from strings in a binary is the same mistake as inferring an event vocabulary
from documentation.

| CLI | Backend redirect | Placeholder credential accepted? |
|---|---|---|
| Grok | `GROK_CLI_BASE_URL` / `GROK_MODELS_BASE_URL` — **works, measured** | **NO — measured** |
| Codex | `chatgpt_base_url` (config) | unprobed |
| Claude Code | `ANTHROPIC_BASE_URL` | unprobed |

The redirect half is proven for Grok: with `GROK_CLI_BASE_URL` pointed at a
recording server, every request went to loopback — `GET /models`,
`POST /chat/completions`, `POST /responses`.

The token half is refuted for Grok. Four separate approaches were probed against
a signed-out HOME, and every one produced `Error: Not signed in`:

1. `GROK_AUTH_PROVIDER_ACCESS_TOKEN` + `_EXPIRES_AT` — **zero** requests made.
2. `XAI_API_KEY=<placeholder>` — it does send `Authorization: Bearer <placeholder>`
   to `GET /models`, but still refuses even when that call is answered `200`.
3. A synthetic `~/.grok/auth.json` carrying a placeholder — zero requests.
4. The same with a structurally valid fake JWT (`at+jwt`/ES256 header, all twelve
   real claim names, far-future `exp`) — zero requests.

The CLI validates its session LOCALLY, before any network call, in a way a
placeholder cannot satisfy — almost certainly a signature check against the
issuer's key. So for Grok the full tier is not available: it cannot be handed a
placeholder while the broker holds the real value.

### What this means per provider

- **API-key providers** (opencode-go, openrouter) keep the full tier: credential
  never inside, `--unshare-net`, placeholder in the sandbox.
- **Grok** can only reach the **fallback tier**: its real ACCESS token inside the
  sandbox (refresh token stripped, so the subscription itself stays behind),
  `--unshare-net` retained, and egress still limited to the one brokered upstream
  by `GROK_CLI_BASE_URL`. Weaker than the full tier — a hostile provider could
  read an access token good for about an hour — and much stronger than running it
  on the host. **Not implemented: it is a deliberate posture change and needs an
  explicit decision, not a default.**
- **Codex and Claude Code** are unprobed on this axis. Do not assume they behave
  like either OpenCode or Grok; probe each the same way, for free, before
  designing around them.

Current state: a live Grok job runs the sandbox, resolves the OAuth session
controller-side, and the provider then refuses with `Not signed in` —
fail-closed, and exactly what the measurements predict.

### What changes vs an API key, honestly

- An OAuth access token is **account-scoped**, not a separately-budgeted key.
  While brokered, the path allowlist and model pin still bound what it is spent
  on; but revocation means the account session, not one key. The per-provider
  `allowed_paths` and the model-pin extractor therefore move into the provider
  record — chatgpt.com, api.x.ai and api.anthropic.com do not share
  `/chat/completions`.
- Invoking Claude Code from agent-ops spends the same subscription that runs
  the operator's own interactive session. Not a security issue — a quota-
  contention one; the measured-spend ledger applies unchanged.

### Remaining engineering

Done (2026-08-18): credential classes with per-provider `allowed_paths` and an
opened-per-provider GET surface; per-provider binary pinning and version
assertion; `isolation_env` behind the adapter seam; the free redirect probe.

Open:

1. **The Grok tier decision** above — access token inside the sandbox, or leave
   Grok fail-closed until xAI offers a delegated-auth mode.
2. **Probe Codex and Claude Code** for placeholder acceptance, the same way and
   for free, before assuming either tier applies to them.
3. **Controller-side refresh**, once a token endpoint is measured per provider
   rather than written from documentation. Until then an expired session refuses
   with an instruction to re-login, and the refresh token stays unread.

## What every new adapter must do

1. Pin an absolute, interpreter-free binary. Never a PATH lookup, never a shim.
2. Run the provider's own config dump under `build_bwrap_argv` and diff host vs
   isolated. Host config, credentials, MCP servers, plugins and history must all
   be absent, not merely denied.
3. Capture a real event stream, commit it as a fixture, and normalize from the
   fixture — never from documentation and never from the mock.
4. Route the credential through the broker, or state why it cannot and fail
   closed until that is resolved.
5. Add a tripwire test asserting the fixture still carries the provider's real
   vocabulary, so a later "fix" cannot quietly substitute an invented one.
