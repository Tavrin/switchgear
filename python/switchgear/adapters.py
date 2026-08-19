"""Compatibility shim. The adapters live in `switchgear.harnesses` now.

Kept so that `from .adapters import get_adapter` -- in this package, in the
tests, and in anything outside the repo that reached for it -- keeps working
across the split. Everything re-exported here is the same object the package
defines; there is no second implementation to drift.

New code should import from `switchgear.harnesses` directly.
"""

from __future__ import annotations

from .harnesses import (  # noqa: F401  (re-exported for backwards compatibility)
    EFFORT_SUPPORTED,
    EFFORT_UNMEASURED,
    EFFORT_UNSUPPORTED,
    REQUIRED_METHODS,
    TERMINAL_COMPLETED,
    TERMINAL_EMPTY,
    TERMINAL_FAILED,
    TERMINAL_NEEDS_INPUT,
    TEXT_LIMIT,
    ClaudeCodeAdapter,
    CodexAdapter,
    GrokAdapter,
    OpenCodeAdapter,
    HarnessAdapter,
    ProviderAdapter,
    _ADAPTERS,
    get_adapter,
    parse_lenient,
    validate_adapter,
)

__all__ = [
    "EFFORT_SUPPORTED",
    "EFFORT_UNMEASURED",
    "EFFORT_UNSUPPORTED",
    "REQUIRED_METHODS",
    "TERMINAL_COMPLETED",
    "TERMINAL_EMPTY",
    "TERMINAL_FAILED",
    "TERMINAL_NEEDS_INPUT",
    "TEXT_LIMIT",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "GrokAdapter",
    "OpenCodeAdapter",
    "HarnessAdapter",
    "ProviderAdapter",
    "_ADAPTERS",
    "get_adapter",
    "parse_lenient",
    "validate_adapter",
]
