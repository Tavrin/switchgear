# Adding a harness (provider)

The rail is built so a new agent CLI is **three edits and a captured fixture**,
and so that a half-finished one fails closed with a message naming the missing
piece rather than doing something silently wrong. This file is the procedure and
the measured status of every candidate.

## The fleet, measured 2026-08-18

The one question that decides how a CLI integrates: **when redirected at a
loopback endpoint, does it send a PLACEHOLDER credential, or validate its session
locally first?** If it sends the placeholder, the broker swaps in the real value
and the credential never enters the sandbox — the full tier. If it validates
locally, the real (access) token must be inside the sandbox — the fallback tier.

| CLI | Auth | Tier | readonly | bounded-write |
|---|---|---|---|---|
| **OpenCode** | API key | full | **live** | **live** |
| **Claude Code** | Claude OAuth | full | **live** | **live** |
| **Codex** | ChatGPT OAuth | full | **live** | **live** |
| **Grok** | xAI OIDC | fallback | **live** | wired, **unproven** (session expired mid-test; needs `grok login`) |

Tier = whether the credential enters the sandbox. Full: never (placeholder in,
real token swapped by the broker). Fallback: the access token is inside, refresh
token stripped, egress still broker-locked.

Facts behind the table, all measured against recording servers with no spend:

- **Three of four accept a placeholder** and send it to the redirected endpoint,
  so the broker swaps in the real credential and it never enters the sandbox.
  Only Grok validates its session locally (four fakes tried, three made zero
  network calls).
- **The auth header differs.** Claude Code's OAuth path is
  `Authorization: Bearer` via `ANTHROPIC_AUTH_TOKEN`; `ANTHROPIC_API_KEY` would
  select the BYOK `x-api-key` path instead. Hence `auth_header`/`auth_scheme` per
  provider.
- **Codex is websocket-first.** By default it reaches inference over
  `wss://api.openai.com/v1/responses`, which ignores the base-url redirect and
  which an HTTP broker cannot proxy. `supports_websockets = false` in its config
  forces the HTTP transport; without it the provider is simply not brokerable.
- **Some headers are derived from the credential.** Codex sends
  `ChatGPT-Account-ID` computed from its own token's claims — a placeholder token
  yields the wrong account, so the broker drops the incoming header and injects
  one derived from the real token (`Credential.extra_headers`).
- **Query strings matter.** Claude posts to `/v1/messages?beta=true`; an
  allowlist matched with `endswith` denies it. The matcher compares the path
  component only.
- **A provider may be more than one file.** Codex needs its sibling helper
  binaries (`codex-code-mode-host`, bundled `rg`); binding only the executable
  produced a job that answered "the workspace execution tool is unavailable".
  Adapters declare `extra_binds()`.

## The three edits

### 1. `models/registry.json` — the data

A provider record and at least one model. This is controller-owned: a profile
may name these ids, never define them.

```jsonc
"providers": {
  "<name>": {
    "upstream": "https://api.example.com/v1",
    "credential_class": "oauth",          // or "api-key"
    "auth_file": "~/.example/auth.json",  // oauth: the CLI's own session file
    "auth_format": "<extractor key>",     // oauth: see credentials.py
    "auth_header": "x-api-key",            // omit for authorization: Bearer
    "auth_scheme": "",                     // omit for "Bearer"
    "allowed_paths": ["/messages"],        // POST inference surfaces
    "allowed_get_paths": ["/models"]       // GET surfaces the CLI needs first
  }
}
"models": {
  "<name>/<wire-model-id>": {             // the id the CLI puts on the wire,
    "provider": "<name>",                 // NOT the one it prints in output
    "model_family": "...", "vendor_family": "..."
  }
}
```

The wire id matters: Grok's CLI reports `grok-4.6-build` but sends `grok-4.6`.
Pinning the reported name would make the broker's model-pin deny every request.
Get it from the free redirect probe (below), not from the CLI's own output.

### 2. `python/ai_ops/adapters.py` — the behaviour

A class with `name`, `argv`, `version_argv`, `agent_name`, `session_id`,
`normalize`, `isolation_env`, `broker_runtime`; registered in `_ADAPTERS`.
`normalize` is the only substantial one, and the standing rule makes even it
mechanical: **write it from a committed real capture, never from documentation
and never from the mock.** The terminal-honesty policy is shared
(`_finish_events`), so a new adapter cannot relax "truncated is never
completed".

### 3. `python/ai_ops/compat.py` — the pin

A `PINNED_PROVIDERS[name] = {"path": <abs interpreter-free binary>, "version":
<tested string>}`. Never the PATH entry if it is a shim: the `codex` on PATH is
`#!/usr/bin/env node` and dies in the sandbox; pin the vendored static binary.
An absent pin refuses to run live — "no pin" never means "no constraint".

## Before writing any of it: two free probes

1. **Isolation probe** — run the CLI's own config dump under
   `sandbox.build_bwrap_argv` with a synthetic HOME and diff against the host.
   Host config, credentials, MCP servers, plugins and history must all be
   absent, not merely denied. (Grok read Claude Code's CLAUDE.md and 375
   permission rules on the host; all gone inside.)
2. **Redirect probe** — point the CLI's base-URL knob at a recording HTTP server
   and run one headless prompt. It costs nothing (the server 401s) and tells you:
   the real wire paths, the real wire model id, whether a placeholder credential
   is sent or the session is validated locally, and which auth header carries it.
   Every correction above came from this probe; skipping it is how the
   invented-vocabulary class of bug gets in.

## Then: capture, and a tripwire

Capture one real stream (`-p`/`exec`/headless + the CLI's streaming-json mode),
commit it as `tests/fixtures/<name>-real-*.jsonl`, write `normalize` from it, and
add a tripwire test asserting the fixture still carries the real event types AND
the absence of any invented ones. That is what keeps a later "tidy-up" from
quietly substituting a fiction.

## Fail-closed guarantees (why half-adding is safe)

Verified: with a provider named but each piece missing in turn —

- no adapter → `no adapter for provider 'x' (known: [...])`
- no pin → `no pinned version for provider 'x'; refusing to run it live`
- no registry record → `provider 'x' is not in the controller registry`
- no credential → refuses rather than running unbrokered on the host network
- expired OAuth session → refuses with "re-login", refresh token never read

None of these is a silent downgrade. You cannot get a live run out of a
partially-added provider.
