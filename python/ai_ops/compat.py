from __future__ import annotations

import os

# Pinned provider binaries, per provider.
#
# This used to be a single OpenCode pin, which had a sharp consequence once a
# second provider existed: resolve_provider decided "is this the live provider?"
# by comparing against that one path, so ANY other real binary -- the Grok CLI,
# the Codex CLI -- classified as a committed mock. It was fail-closed in effect
# (a mock is never handed a credential, so nothing could leak), but the
# classification was wrong in the dangerous direction: a real agent binary would
# have run without the AI_OPS_ALLOW_LIVE_PROVIDER gate and without a broker.
#
# `path` is the interpreter-free executable to exec. For Codex that is
# deliberately NOT the `codex` on PATH: that is a `#!/usr/bin/env node` npm shim
# which dies inside the sandbox with "node: No such file or directory".
#
# `version` is the tested contract, asserted from output captured INSIDE bwrap.
# None means no version has been pinned for that provider yet, which is honest
# rather than asserting a string nobody measured.
PINNED_PROVIDERS: dict[str, dict[str, str | None]] = {
    "opencode": {
        "path": "/home/user/.opencode/bin/opencode",
        "version": "1.18.18",
    },
    "claude": {
        # The real ELF, not the ~/.local/bin/claude symlink -- pinned by content
        # location so a version bump repoints the symlink and is CAUGHT rather
        # than silently followed.
        "path": "/home/user/.local/share/claude/versions/2.1.234",
        "version": "2.1.234",
    },
    "codex": {
        # The vendored static musl binary, NOT the `codex` on PATH -- that is a
        # `#!/usr/bin/env node` npm shim which dies inside the sandbox with
        # "node: No such file or directory".
        "path": ("/home/user/.nvm/versions/node/v22.22.0/lib/node_modules/@openai/codex/"
                 "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"),
        "version": "0.147.0",
    },
    "grok": {
        # The launcher symlinks into ~/.grok/downloads and Grok SELF-UPDATES,
        # repointing it (measured: 1.0.4 -> 1.0.5 mid-session). Both are listed:
        # `path` is the tested file, `launcher` the stable entry point. Matching
        # either is what stops an updated binary from falling through to the
        # committed-mock branch -- it is recognised as Grok and then refused
        # loudly by the version assertion until someone tests the new build.
        "path": "/home/user/.grok/downloads/grok-linux-x86_64",
        "launcher": "/home/user/.grok/bin/grok",
        "version": "1.0.4",
    },
}

# Kept as module constants because the OpenCode path is referenced directly in
# several places and in the config probe.
PINNED_OPENCODE = PINNED_PROVIDERS["opencode"]["version"]
PINNED_BINARY = PINNED_PROVIDERS["opencode"]["path"]


def pinned_for_path(path: str) -> tuple[str, dict[str, str | None]] | None:
    """Which pinned provider, if any, this executable IS.

    Compared by realpath so a symlinked launcher resolves to the same identity as
    the file it points at -- both Grok and the installed agent-ops launcher are
    symlinks, and treating those as different binaries would silently downgrade a
    live provider to mock.
    """
    target = os.path.realpath(path)
    for name, rec in PINNED_PROVIDERS.items():
        for key in ("path", "launcher"):
            pinned = rec.get(key)
            if pinned and os.path.exists(pinned) and os.path.realpath(pinned) == target:
                return name, rec
    return None
