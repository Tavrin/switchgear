from __future__ import annotations

import json
import os
from typing import Any

from .compat import PINNED_BINARY
from .errors import Refuse
from .paths import reject_symlinks


def resolve_provider(explicit: str | None) -> tuple[list[str], bool]:
    """Return (argv, is_live). Never PATH-lookup.

    "Live" means: this executable IS one of the pinned provider binaries. It used
    to mean "is it the pinned OpenCode binary", which classified every other real
    agent CLI as a committed mock -- fail-closed for credentials, but it would
    have run a real agent without the live gate and without a broker.
    """
    allow_live = os.environ.get("SWITCHGEAR_ALLOW_LIVE_PROVIDER") == "1"
    path = explicit or os.environ.get("SWITCHGEAR_PROVIDER")
    if not path:
        raise Refuse("provider path required (--provider or SWITCHGEAR_PROVIDER); no PATH lookup")
    if not os.path.isabs(path):
        raise Refuse("provider path must be absolute")
    path = reject_symlinks(path, "provider") if os.path.exists(path) else path
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise Refuse(f"provider is missing or not executable: {path}")
    from .compat import pinned_for_path

    match = pinned_for_path(path)
    is_live = match is not None
    if is_live:
        name, _rec = match
        if not allow_live:
            raise Refuse(
                f"refusing live provider {name} without SWITCHGEAR_ALLOW_LIVE_PROVIDER=1"
            )
        # The version check deliberately does NOT run here: executing the
        # provider on the host to ask its version hands a hostile binary
        # controller-side execution with the full inherited environment, before
        # any boundary exists. job.run_job runs the probe inside bwrap instead.
        return [path], True
    # committed mock: python script or executable
    if path.endswith(".py"):
        return ["/usr/bin/python3", path], False
    return [path], False


def assert_pinned_version(
    returncode: int, stdout: bytes, timed_out: bool, provider: str = "opencode"
) -> str:
    """Validate `--version` output captured from inside the sandbox.

    Each provider prints its version its own way: OpenCode emits a bare
    `1.18.18`, Grok emits `grok 1.0.4 (d846eb93d9) [stable]`. So the pinned
    version must be FOUND in the output rather than equal to it -- while still
    being a real check, which is why an absent pin refuses instead of passing.
    """
    from .compat import accepted_versions, version_token

    if timed_out:
        raise Refuse("provider version probe timed out")
    if returncode != 0:
        raise Refuse(f"provider version probe exited {returncode}")
    text = stdout.decode("utf-8", "replace").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ver = lines[-1] if lines else ""
    accepted = accepted_versions(provider)
    if not accepted:
        raise Refuse(f"no pinned version for provider {provider!r}; refusing to run it live")
    seen = version_token(ver)
    if not seen or seen not in accepted:
        # Codex, Claude Code and Grok self-update often, so this fires as routine
        # maintenance rather than as an alarm. Say how to clear it: a refusal
        # whose only remedy is editing source is one that gets switched off.
        raise Refuse(
            f"{provider} version {ver!r} has not been verified on this machine "
            f"(verified: {', '.join(accepted)}). Run "
            f"`switchgear providers verify --provider {provider}` to check this "
            "build against the adapter's contract and record it."
        )
    return ver


CREDENTIAL_ENV = "OPENCODE_API_KEY"
def probe_provider(binary: str | None, args: list[str], *, timeout: int = 30) -> str | None:
    """Run a provider binary INSIDE bwrap and return its stdout, or None.

    `--version` and `--help` look harmless, which is why they were being run
    directly on the host with the caller's entire environment inherited. Three
    places in this repo state the rule they broke -- HANDOFF's invariant list,
    THREAT-MODEL, and a comment in job.py -- all saying the provider is never
    executed outside the sandbox, not even for a version string.

    Returns a `(unreadable: ...)` marker rather than raising when the binary
    exists but will not run: a binary that is present and broken is a different
    diagnosis from an absent one, and telling those apart is doctor's job.
    """
    import subprocess
    import tempfile

    from .sandbox import build_probe_argv

    if not binary or not os.path.exists(binary):
        return None
    home = tempfile.mkdtemp(prefix="aiops-probe-")
    try:
        argv = build_probe_argv(
            synth_home=home,
            provider_argv=[os.path.realpath(binary), *args],
        )
        out = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            # Constructed, never inherited: the whole point is that the host
            # environment does not reach the provider.
            env={"PATH": "/usr/bin:/bin", "HOME": home},
        )
        return out.stdout or ""
    except Exception as exc:
        return f"(unreadable: {type(exc).__name__})"
    finally:
        from .paths import safe_rmtree

        try:
            safe_rmtree(home, must_be_under=tempfile.gettempdir(),
                        label="provider probe workspace")
        except Exception:
            pass


def installed_version(binary: str | None) -> str | None:
    """The `--version` line this binary prints, or None if it is not installed.

    Shared by `providers`, `doctor` and `capabilities` so they cannot disagree
    about whether a provider is present. Runs inside the sandbox -- see
    probe_provider.
    """
    out = probe_provider(binary, ["--version"])
    if out is None:
        return None
    if out.startswith("(unreadable:"):
        return out
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else None


CREDENTIAL_DIR = os.path.expanduser("~/.config/switchgear/credentials")
DEFAULT_CREDENTIAL_FILE = os.path.expanduser("~/.config/switchgear/provider-credential")


def credential_path(name: str | None = None) -> str:
    """Where a provider's credential lives.

    Per-provider files under ~/.config/switchgear/credentials/<name> so each pool
    (opencode-go, openrouter, ...) is separately installable and separately
    revocable. SWITCHGEAR_PROVIDER_CREDENTIAL_FILE overrides for a single-provider
    setup; the legacy single-file path stays supported.
    """
    override = os.environ.get("SWITCHGEAR_PROVIDER_CREDENTIAL_FILE")
    if override:
        return override
    if name:
        per = os.path.join(CREDENTIAL_DIR, name)
        if os.path.isfile(per):
            return per
    return DEFAULT_CREDENTIAL_FILE


def load_provider_credential(name: str | None = None) -> str | None:
    """Read the provider credential from an operator-owned file.

    Deliberately NOT taken from the controller's environment: the host env is
    where unrelated secrets live (work keys, cloud tokens), and this rail must
    forward exactly one credential and never a whole environment.

    The credential never enters the sandbox. It is read here, in the controller,
    and held by the broker (broker.py), which attaches the Authorization header
    on the way upstream; the sandbox gets a placeholder and a loopback URL. That
    closes the earlier residual in which a hostile provider could exfiltrate a
    usable key. Still use a DEDICATED, separately-budgeted, independently
    revocable key -- the broker bounds what the key can be spent on, not what a
    compromised upstream could do with it.

    Returns None when no credential is configured. Callers that requested a live
    provider must treat that as a refusal, not as a fallback: without a broker
    the sandbox is not given a network namespace of its own.
    """
    path = credential_path(name)
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


def runtime_with_broker(runtime: dict[str, Any], base_url: str, model_id: str) -> dict[str, Any]:
    """Point the provider at the loopback broker with a placeholder key.

    The sandbox never receives the real credential: options.apiKey here is a
    literal placeholder that the broker overwrites on the way upstream.
    """
    out = dict(runtime)
    provider_id = model_id.split("/", 1)[0] if "/" in model_id else model_id
    providers = dict(out.get("provider") or {})
    entry = dict(providers.get(provider_id) or {})
    options = dict(entry.get("options") or {})
    # No "/v1" here: the registry upstream already carries the API version
    # (e.g. .../zen/go/v1), and the broker concatenates upstream + request path.
    # Appending it produced .../zen/go/v1/v1/chat/completions -> 404.
    options["baseURL"] = base_url.rstrip("/")
    options["apiKey"] = "broker-placeholder-not-a-credential"
    entry["options"] = options
    providers[provider_id] = entry
    out["provider"] = providers
    return out


def write_agent_definition(synth_home: str, agent: str, definition: str) -> str:
    """Materialise the generated agent file the provider will load."""
    d = os.path.join(synth_home, ".config", "opencode", "agent")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{agent}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(definition)
    return path


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

    # NOTE: the credential is deliberately NOT placed here. job.run_job runs a
    # controller-side broker and rewrites the runtime config to point at it, so
    # the sandbox holds a placeholder rather than a usable key.
    env = allowlisted_env(home=synth_home, extra=extra)
    assert_no_host_secrets(env)
    return env
