from __future__ import annotations

import json
import os
from typing import Any

from .compat import PINNED_BINARY
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
        # The version check deliberately does NOT run here: executing the
        # provider on the host to ask its version hands a hostile binary
        # controller-side execution with the full inherited environment, before
        # any boundary exists. job.run_job runs the probe inside bwrap instead.
        return [path], True
    # committed mock: python script or executable
    if path.endswith(".py"):
        return ["/usr/bin/python3", path], False
    return [path], False


def assert_pinned_version(returncode: int, stdout: bytes, timed_out: bool) -> str:
    """Validate `--version` output captured from inside the sandbox."""
    from .compat import PINNED_OPENCODE

    if timed_out:
        raise Refuse("provider version probe timed out")
    if returncode != 0:
        raise Refuse(f"provider version probe exited {returncode}")
    text = stdout.decode("utf-8", "replace").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ver = lines[-1] if lines else ""
    if ver != PINNED_OPENCODE:
        raise Refuse(f"OpenCode version {ver!r} is outside the tested contract {PINNED_OPENCODE}")
    return ver


CREDENTIAL_ENV = "OPENCODE_API_KEY"
DEFAULT_CREDENTIAL_FILE = os.path.expanduser("~/.config/ai-ops/provider-credential")


def load_provider_credential() -> str | None:
    """Read the provider credential from an operator-owned file.

    Deliberately NOT taken from the controller's environment: the host env is
    where unrelated secrets live (work keys, cloud tokens), and this rail must
    forward exactly one credential and never a whole environment.

    RESIDUAL, accepted knowingly: the provider process receives a usable
    credential and has network access, so a hostile provider can exfiltrate it.
    Use a DEDICATED, separately-budgeted, independently revocable key -- never a
    personal or shared one. Stage 2 (a controller-side broker proxy addressed via
    provider options.baseURL, plus --unshare-net) removes this residual by never
    placing the credential inside the sandbox at all.
    """
    path = os.environ.get("AI_OPS_PROVIDER_CREDENTIAL_FILE") or DEFAULT_CREDENTIAL_FILE
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise Refuse(
            f"provider credential file {path} is group/world accessible "
            f"(mode {st.st_mode & 0o777:o}); chmod 600 it"
        )
    with open(path, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        return None
    return value


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

    # Live runs need a credential or the provider cannot reach any model. Only
    # ever injected when the operator has explicitly opted into a live provider.
    if os.environ.get("AI_OPS_ALLOW_LIVE_PROVIDER") == "1":
        cred = load_provider_credential()
        if cred:
            extra[CREDENTIAL_ENV] = cred

    env = allowlisted_env(home=synth_home, extra=extra)
    assert_no_host_secrets(env)
    return env
