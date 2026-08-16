from __future__ import annotations

import os
from typing import Mapping

# Dangerous families never forwarded.
_DROP_PREFIXES = (
    "GIT_",
    "OPENCODE_",
    "LD_",
    "PYTHON",
    "BASH_",
    "ENV",
    "SHELLOPTS",
    "GCONV_PATH",
    "NLSPATH",
    "HOSTALIASES",
    "LOCALDOMAIN",
    "RES_OPTIONS",
    "TERMINFO",
    "TERMPATH",
    "XDG_",
)

_KEEP = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
        "TMPDIR",
    }
)

TRUSTED_PATH = "/usr/bin:/bin"
GIT = "/usr/bin/git"
BWRAP = "/usr/bin/bwrap"
PYTHON = "/usr/bin/python3"


def git_clean_env() -> dict[str, str]:
    env = {
        "PATH": TRUSTED_PATH,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    return env


def allowlisted_env(
    *,
    home: str,
    extra: Mapping[str, str] | None = None,
    path: str = TRUSTED_PATH,
) -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": path,
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        "TMPDIR": os.path.join(home, "tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
    }
    if extra:
        env.update(extra)
    return env


def assert_no_host_secrets(env: Mapping[str, str]) -> None:
    for key in env:
        if key.startswith("OPENCODE_") and key not in {
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_DIR",
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_DISABLE_CLAUDE_CODE",
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS",
            "OPENCODE_DISABLE_AUTOUPDATE",
            "OPENCODE_PURE",
            "OPENCODE_FAKE_VCS",
        }:
            raise RuntimeError(f"refusing to forward {key}")
        if key == "OPENCODE_PERMISSION":
            raise RuntimeError("OPENCODE_PERMISSION must never be set")
