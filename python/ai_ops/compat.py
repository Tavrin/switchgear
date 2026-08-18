from __future__ import annotations

import json
import os
import re

# Where each provider's binary is FOUND, expressed as patterns rather than as
# paths on one machine.
#
# This table used to hold absolute paths under a specific home directory, which
# meant the package only worked on the machine it was written on -- fatal for
# distribution and for CI, which must run somewhere else by definition.
#
# `path_globs` are shell globs, expanded against the running user's home. A glob
# rather than a fixed path because these CLIs version their install directories
# (`~/.local/share/claude/versions/<v>`) and vendor per-node-version
# (`~/.nvm/versions/node/<v>/...`); pinning one of those is pinning today.
#
# ALL matches are registered, not just one. `pinned_for_path` decides "is this
# executable a live provider", and a real provider binary it does not recognise
# is classified as a committed mock -- which is fail-closed for credentials but
# wrong in the dangerous direction, since it would run a real agent without the
# live gate. Two installed versions is the normal state of a self-updating CLI,
# so both must be recognised.
#
# `exec_path` is the interpreter-free executable to exec; `launcher` is what the
# user types. For Codex the two differ and it matters: the `codex` on PATH is a
# `#!/usr/bin/env node` shim that dies inside the sandbox with "node: No such
# file or directory".
#
# `version` is the tested contract, asserted from output captured INSIDE bwrap.
# It is not machine-specific, so it stays here; anything an operator verifies
# locally goes in the operator-owned file below.
DISCOVERY: dict[str, dict[str, object]] = {
    "opencode": {
        "path_globs": ["~/.opencode/bin/opencode"],
        "version": "1.18.18",
    },
    "claude": {
        # The real ELF, not the ~/.local/bin/claude symlink.
        "path_globs": ["~/.local/share/claude/versions/*"],
        "launcher_globs": ["~/.local/bin/claude"],
        "version": "2.1.234",
    },
    "codex": {
        "path_globs": [
            "~/.nvm/versions/node/*/lib/node_modules/@openai/codex/node_modules/"
            "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex",
            "/usr/lib/node_modules/@openai/codex/node_modules/"
            "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex",
        ],
        "version": "0.147.0",
    },
    "grok": {
        # Grok SELF-UPDATES and repoints its launcher (measured: 1.0.4 -> 1.0.5
        # mid-session). Both the download and the launcher are registered, so an
        # updated binary is still RECOGNISED as the provider rather than falling
        # through to the committed-mock branch.
        "path_globs": ["~/.grok/downloads/grok-linux-*"],
        "launcher_globs": ["~/.grok/bin/grok"],
        "version": "1.0.4",
    },
}

# An operator's own answer, which always wins over discovery. Written by hand for
# a non-standard install; the format mirrors a DISCOVERY entry but takes literal
# paths:
#
#   {"claude": {"path": "/opt/claude/bin/claude", "version": "2.1.240"}}
#
# Kept out of source for the same reason verified versions are: a pin whose only
# remedy is editing the package is a pin someone eventually turns off.
PROVIDERS_FILE = os.environ.get("AI_OPS_PROVIDERS_FILE") or os.path.expanduser(
    "~/.config/ai-ops/providers.json"
)


def _load_operator_providers() -> dict[str, dict[str, object]]:
    try:
        with open(PROVIDERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _expand(globs: object) -> list[str]:
    """Every existing executable matching these globs, newest first.

    Sorted by mtime so `path` -- the one consumers treat as primary -- is the
    most recently installed build, which is the one a self-updating CLI actually
    runs. All the others stay registered for recognition.
    """
    import glob as _glob

    found: list[str] = []
    for pattern in (globs or []):
        for hit in _glob.glob(os.path.expanduser(str(pattern))):
            if os.path.isfile(hit) and os.access(hit, os.X_OK):
                found.append(os.path.abspath(hit))
    found = sorted(set(found), key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def discover_providers() -> dict[str, dict[str, object]]:
    """Resolve every provider's install on THIS machine.

    Operator config wins; discovery fills the rest; a provider found nowhere is
    reported with no paths rather than omitted, so `doctor` and `capabilities`
    can say "not installed" instead of silently not mentioning it.
    """
    operator = _load_operator_providers()
    out: dict[str, dict[str, object]] = {}
    for name, spec in DISCOVERY.items():
        override = operator.get(name) or {}
        if override.get("path"):
            paths = [str(override["path"])]
        else:
            paths = _expand(spec.get("path_globs"))
        launcher = override.get("launcher")
        if not launcher:
            launchers = _expand(spec.get("launcher_globs"))
            launcher = launchers[0] if launchers else None
        out[name] = {
            "path": paths[0] if paths else None,
            "paths": paths,
            "launcher": launcher,
            "version": override.get("version") or spec.get("version"),
        }
    for name, override in operator.items():
        if name in out or not isinstance(override, dict):
            continue
        # A provider the operator added that this build has no discovery rule
        # for. Registered rather than ignored: silently dropping it would look
        # exactly like a typo in the name.
        out[name] = {
            "path": override.get("path"),
            "paths": [override["path"]] if override.get("path") else [],
            "launcher": override.get("launcher"),
            "version": override.get("version"),
        }
    return out


PINNED_PROVIDERS: dict[str, dict[str, object]] = discover_providers()

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
# OpenCode pin directly. Either may be None when OpenCode is not installed here,
# which is now a normal state rather than an impossible one -- callers check.
PINNED_OPENCODE = PINNED_PROVIDERS.get("opencode", {}).get("version")
PINNED_BINARY = PINNED_PROVIDERS.get("opencode", {}).get("path")


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


def pinned_for_path(path: str) -> tuple[str, dict[str, object]] | None:
    """Which pinned provider, if any, this executable IS.

    Compared by realpath so a symlinked launcher resolves to the same identity as
    the file it points at -- both Grok and the installed agent-ops launcher are
    symlinks, and treating those as different binaries would silently downgrade a
    live provider to mock.

    Checks EVERY registered path, not just the primary. A self-updating CLI keeps
    several versions installed, and an unrecognised real provider binary is
    treated as a committed mock: fail-closed for credentials, but it would run a
    real agent without the live gate.
    """
    target = os.path.realpath(path)
    for name, rec in PINNED_PROVIDERS.items():
        candidates = list(rec.get("paths") or [])
        for key in ("path", "launcher"):
            value = rec.get(key)
            if value:
                candidates.append(str(value))
        for pinned in candidates:
            if os.path.exists(pinned) and os.path.realpath(pinned) == target:
                return name, rec
    return None
