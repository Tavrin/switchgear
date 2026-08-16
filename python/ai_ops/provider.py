from __future__ import annotations

import json
import os
from typing import Any

from .compat import PINNED_BINARY, check_opencode_version
from .errors import Refuse
from .paths import reject_symlinks


def resolve_provider(explicit: str | None) -> tuple[list[str], bool]:
    """Return (argv, is_live). Never PATH-lookup."""
    allow_live = os.environ.get("AI_OPS_ALLOW_LIVE_PROVIDER") == "1"
    path = explicit or os.environ.get("AI_OPS_PROVIDER")
    if not path:
        raise Refuse("provider path required (--provider or AI_OPS_PROVIDER); no PATH lookup")
    if not os.path.isabs(path):
        raise Refuse("provider path must be absolute")
    path = reject_symlinks(path, "provider") if os.path.exists(path) else path
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise Refuse(f"provider is missing or not executable: {path}")
    is_live = os.path.realpath(path) == os.path.realpath(PINNED_BINARY)
    if is_live:
        if not allow_live:
            raise Refuse("refusing live OpenCode without AI_OPS_ALLOW_LIVE_PROVIDER=1")
        check_opencode_version(path)
        return [path], True
    # committed mock: python script or executable
    if path.endswith(".py"):
        return ["/usr/bin/python3", path], False
    return [path], False


def isolation_env(synth_home: str, runtime: dict[str, Any]) -> dict[str, str]:
    cfg_dir = os.path.join(synth_home, ".config", "opencode")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, "opencode.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(runtime, fh, indent=2)
        fh.write("\n")
    content = json.dumps(runtime, separators=(",", ":"))
    extra = {
        "OPENCODE_CONFIG": cfg_path,
        "OPENCODE_CONFIG_DIR": cfg_dir,
        "OPENCODE_CONFIG_CONTENT": content,
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_PURE": "1",
    }
    from .env import allowlisted_env, assert_no_host_secrets

    env = allowlisted_env(home=synth_home, extra=extra)
    assert_no_host_secrets(env)
    return env
