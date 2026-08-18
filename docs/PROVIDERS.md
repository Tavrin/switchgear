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

## Grok — the handoff was wrong about this one, not yet probed

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

**Not yet established** (do not write the adapter until it is):

- config discovery — does `~/.grok/config.toml` resolution follow `$HOME`? Run
  the equivalent no-model probe.
- whether host agents, plugins and `~/.grok/bundled` leak into a synthetic HOME.
- the real event vocabulary — capture a stream and commit it as a fixture.
- whether its credential can go through the broker, i.e. whether it is an API key
  against an OpenAI- or Anthropic-shaped endpoint.

---

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
