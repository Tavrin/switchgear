from __future__ import annotations

import json
import os
import re

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
        # The real ELF, not the ~/.local/bin/claude symlink.
        "path": "/home/user/.local/share/claude/versions/2.1.234",
        "launcher": "/home/user/.local/bin/claude",
        "version": "2.1.234",
    },
    "codex": {
        # The vendored static musl binary, NOT the `codex` on PATH -- that is a
        # `#!/usr/bin/env node` npm shim which dies inside the sandbox.
        "path": ("/home/user/.nvm/versions/node/v22.22.0/lib/node_modules/@openai/codex/"
                 "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"),
        "version": "0.147.0",
    },
    "grok": {
        # Grok SELF-UPDATES and repoints its launcher (measured: 1.0.4 -> 1.0.5
        # mid-session). Both are listed and either matches, so an updated binary
        # is still RECOGNISED as the provider rather than falling through to the
        # committed-mock branch.
        "path": "/home/user/.grok/downloads/grok-linux-x86_64",
        "launcher": "/home/user/.grok/bin/grok",
        "version": "1.0.4",
    },
}

# Versions an operator has VERIFIED on this machine, beyond the built-in
# defaults above.
#
# Codex, Claude Code and Grok all self-update, often weekly. A pin that refuses
# every new build and can only be changed by editing source is a pin someone
# eventually turns off -- so the record of "what has been checked" lives in an
# operator-owned file, and `ai-opencode providers verify` writes it after
# actually running the checks. The built-in versions stay as the last values
# verified in-repo; the file only ever ADDS.
VERIFIED_FILE = os.environ.get("AI_OPS_VERIFIED_FILE") or os.path.expanduser(
    "~/.config/ai-ops/verified-providers.json"
)


# Kept as module constants: several modules and the config probe reference the
# OpenCode pin directly.
PINNED_OPENCODE = PINNED_PROVIDERS["opencode"]["version"]
PINNED_BINARY = PINNED_PROVIDERS["opencode"]["path"]


def version_token(text: str | None) -> str | None:
    """The dotted version out of a CLI's `--version` line.

    Compare TOKENS, not whole lines. Measured: Grok prints
    `grok 1.0.5 (5115b46bc9) [stable]` on the host and
    `grok 1.0.5 (5115b46bc9)` inside the sandbox -- the channel suffix is
    dropped. Whole-line matching therefore fails for a build that was verified
    minutes earlier, which reads as a broken pin rather than a formatting
    difference.
    """
    if not text:
        return None
    match = re.search(r"\d+\.\d+(?:\.\d+)*", text)
    return match.group(0) if match else None


def load_verified() -> dict[str, list[str]]:
    """provider -> versions an operator verified locally. Absent file is fine."""
    try:
        with open(VERIFIED_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    if isinstance(data, dict):
        for name, versions in data.items():
            if isinstance(versions, list):
                out[name] = [v for v in versions if isinstance(v, str)]
    return out


def record_verified(provider: str, version: str) -> None:
    version = version_token(version) or version
    data = load_verified()
    versions = data.setdefault(provider, [])
    if version not in versions:
        versions.append(version)
    os.makedirs(os.path.dirname(VERIFIED_FILE), exist_ok=True)
    tmp = VERIFIED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, VERIFIED_FILE)


def accepted_versions(provider: str) -> list[str]:
    """Every version accepted for this provider: the built-in pin plus anything
    an operator has verified locally."""
    built_in = version_token((PINNED_PROVIDERS.get(provider) or {}).get("version"))
    out = [built_in] if built_in else []
    out += [v for v in load_verified().get(provider, []) if v not in out]
    return out


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
