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
    "grok": {
        # The launcher symlinks into ~/.grok/downloads; the real file is pinned
        # so an update that repoints the symlink is caught rather than followed.
        "path": "/home/user/.grok/downloads/grok-linux-x86_64",
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
        pinned = rec.get("path")
        if pinned and os.path.realpath(pinned) == target:
            return name, rec
    return None
