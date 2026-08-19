"""Harness adapters: the seam where provider-specific shapes stop.

Everything above this package -- the digest, `logs`, `status`, and anything a
caller consumes -- works on a NORMALIZED vocabulary. Everything below it is one
agent CLI's private format. That is the structural advantage over a wrapper that
passes a raw format through and would need the digest written once per provider.

The normalized vocabulary, from docs/OBSERVABILITY.md:

    {"event": "status",   "sessionId": str}
    {"event": "tool",     "name": str, "target": str}
    {"event": "text",     "content": str}          # truncated at write time
    {"event": "finished", "status": str, "turns": int,
                          "costUSD": float, "tokens": int, "exitSummary": str}

`status` is one of completed / completed_empty / needs_input. `needs_input` is
load-bearing for a caller: it parks the work back to the operator rather than
recording a failure.

`sessionId` is the single most load-bearing field: without a durable session
identifier there is no resume, and a reply to a finished job cannot exist.

`changed_files` is deliberately NOT part of this vocabulary. A caller derives the
result manifest from git itself and validates the landed tree at merge; it never
trusts an agent-reported file list. The rail keeps its own freeze delta for its
own gates -- that is a different thing, computed by the controller from the
worktree, not reported by the worker.

## Layout

One module per harness, because a Claude protocol change should produce a
Claude-shaped diff. The four share almost no byte-identical behaviour, so this is
about review locality, not about factoring out commonality that is not there.

    base.py           the contract, the vocabulary constants, validate_adapter
    normalization.py  helpers shared by more than one harness
    {opencode,grok,claude,codex}.py

Imports run one way: base <- normalization <- harnesses <- here.

Adding one is a static edit here plus a captured real fixture -- deliberately not
a plugin loader. A dynamically loaded third-party adapter would undercut exactly
what this package is relied on for: supply-chain trust, version pinning, adapter
completeness and credential posture.
"""

from __future__ import annotations

from .base import (  # noqa: F401  (re-exported: this is the package's surface)
    EFFORT_SUPPORTED,
    EFFORT_UNMEASURED,
    EFFORT_UNSUPPORTED,
    REQUIRED_METHODS,
    TERMINAL_COMPLETED,
    TERMINAL_EMPTY,
    TERMINAL_FAILED,
    TERMINAL_NEEDS_INPUT,
    TEXT_LIMIT,
    ProviderAdapter,
    validate_adapter,
)
from .claude import ClaudeCodeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter
from .normalization import parse_lenient  # noqa: F401  (re-exported)
from .opencode import OpenCodeAdapter

_ADAPTERS = {
    "opencode": OpenCodeAdapter(),
    "grok": GrokAdapter(),
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}


for _name, _adapter in _ADAPTERS.items():
    # At import, so an incomplete adapter cannot reach a job.
    validate_adapter(_name, _adapter)


def get_adapter(provider: str | None):
    """Resolve the CLI adapter for a profile's `provider` field.

    Note this is the PROVIDER BINARY (opencode), not the model pool
    (opencode-go, openrouter) that registry.provider_record resolves. Several
    pools are reached through one binary, so conflating them would pick the
    wrong adapter the moment a second pool appears.
    """
    from ..errors import Refuse

    adapter = _ADAPTERS.get((provider or "").strip().lower())
    if adapter is None:
        raise Refuse(f"no adapter for provider {provider!r} (known: {sorted(_ADAPTERS)})")
    return adapter
