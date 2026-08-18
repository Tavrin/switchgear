from __future__ import annotations

import os
from typing import Sequence

from .env import BWRAP
from .errors import Refuse
from .identity import WorktreeIdentity
from .policy import CompiledPolicy

TRUSTED_BWRAP = BWRAP

# Fixed in-sandbox path for the brokered upstream socket.
BROKER_SOCKET_PATH = "/run/ai-ops-broker.sock"
BROKER_RELAY_PORT = 8_099


def require_bwrap() -> str:
    if not os.path.isfile(TRUSTED_BWRAP) or not os.access(TRUSTED_BWRAP, os.X_OK):
        raise Refuse("containment backend /usr/bin/bwrap is unavailable (no silent fallback)")
    if os.path.islink(TRUSTED_BWRAP):
        # allow only if it resolves to itself-owned system path
        real = os.path.realpath(TRUSTED_BWRAP)
        if real != TRUSTED_BWRAP and not real.startswith("/usr/"):
            raise Refuse("refusing untrusted bwrap symlink")
    return TRUSTED_BWRAP


def _exists(path: str) -> bool:
    return os.path.exists(path)


def build_credential_refresh_argv(
    *, auth_dir: str, synth_home: str, provider_argv: Sequence[str]
) -> list[str]:
    """A minimal sandbox for one job: letting a vendor CLI refresh its OWN session.

    These access tokens last about an hour and every vendor CLI refreshes its
    own file when it runs, so an operator who has not used the CLI recently
    finds the rail refusing work for a credential that is perfectly valid --
    "go run `grok models` first" is not a workable contract.

    So the controller triggers that refresh itself. Two properties keep it
    honest:

    - agent-ops still NEVER reads the refresh token. The CLI that owns the
      credential performs its own refresh; we only invoke it. That is why this
      generalises across providers instead of needing one hand-written OAuth
      flow per vendor, each of which would have to be guessed at.
    - The provider still never executes outside bwrap. This mount set is
      deliberately smaller than a job's: NO worktree, NO git dir, NO state
      store, and no prompt -- the CLI is given nothing to act on. The one
      addition is its own auth directory, bind-mounted READ-WRITE because
      writing the refreshed token back is the entire point.

    Network is NOT unshared here: reaching the issuer's token endpoint is what a
    refresh is. That is the same exposure as the operator running the command by
    hand, on a process with no repository and no instructions.
    """
    bwrap = require_bwrap()
    argv: list[str] = [
        bwrap,
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for src, dst in (
        ("/usr", "/usr"),
        ("/bin", "/bin"),
        ("/lib", "/lib"),
        ("/lib64", "/lib64"),
        ("/etc/resolv.conf", "/etc/resolv.conf"),
        ("/etc/ssl", "/etc/ssl"),
        ("/etc/ca-certificates", "/etc/ca-certificates"),
        ("/etc/hosts", "/etc/hosts"),
        ("/etc/nsswitch.conf", "/etc/nsswitch.conf"),
        ("/etc/passwd", "/etc/passwd"),
        ("/etc/group", "/etc/group"),
    ):
        if _exists(src):
            argv.extend(["--ro-bind", src, dst])
    argv.extend(["--bind", synth_home, synth_home])
    # The credential directory is bound INSIDE the synthetic HOME, under its own
    # name (~/.grok -> $HOME/.grok). These CLIs resolve their session relative to
    # $HOME, so binding it at its real host path leaves them reporting "not
    # authenticated" -- measured. Read-write, because writing the refreshed token
    # back is the entire point; and nothing else of the host home is visible.
    argv.extend(["--bind", auth_dir, os.path.join(synth_home, os.path.basename(auth_dir))])
    for extra in {os.path.dirname(os.path.realpath(p)) for p in provider_argv if os.path.isabs(p)}:
        if _exists(extra):
            argv.extend(["--ro-bind", extra, extra])
    argv.extend(["--chdir", synth_home])
    argv.append("--")
    argv.extend(list(provider_argv))
    return argv


def build_bwrap_argv(
    *,
    ident: WorktreeIdentity,
    policy: CompiledPolicy,
    synth_home: str,
    provider_argv: Sequence[str],
    command_binds: Sequence[str] | None = None,
    broker_socket: str | None = None,
) -> list[str]:
    bwrap = require_bwrap()
    argv: list[str] = [
        bwrap,
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    if broker_socket:
        # With the credential broker reachable over a bind-mounted unix socket,
        # the sandbox needs no network of its own. Unix sockets are filesystem
        # objects and keep working across a network namespace, so this removes
        # ALL outbound reachability except the one brokered upstream.
        argv.append("--unshare-net")
        argv.extend(["--bind", broker_socket, BROKER_SOCKET_PATH])
    # Without a broker the provider needs the host network to reach its API.
    for src, dst in (
        ("/usr", "/usr"),
        ("/bin", "/bin"),
        ("/lib", "/lib"),
        ("/lib64", "/lib64"),
        ("/etc/resolv.conf", "/etc/resolv.conf"),
        ("/etc/ssl", "/etc/ssl"),
        ("/etc/ca-certificates", "/etc/ca-certificates"),
        ("/etc/hosts", "/etc/hosts"),
        ("/etc/nsswitch.conf", "/etc/nsswitch.conf"),
        ("/etc/passwd", "/etc/passwd"),
        ("/etc/group", "/etc/group"),
    ):
        if _exists(src):
            argv.extend(["--ro-bind", src, dst])

    wt = ident.realpath
    if policy.mode == "readonly":
        argv.extend(["--ro-bind", wt, wt])
    else:
        argv.extend(["--bind", wt, wt])

    # Common git dir is never a worker-writable surface.
    common = ident.common_git_dir
    if os.path.isdir(common) or os.path.isfile(common):
        argv.extend(["--ro-bind", common, common])
    if ident.git_dir != common and os.path.exists(ident.git_dir):
        argv.extend(["--ro-bind", ident.git_dir, ident.git_dir])

    argv.extend(["--bind", synth_home, synth_home])
    argv.extend(["--chdir", wt])

    for extra in command_binds or ():
        if extra not in {wt, common, ident.git_dir, synth_home} and os.path.exists(extra):
            argv.extend(["--ro-bind", extra, extra])

    argv.append("--")
    argv.extend(list(provider_argv))
    return argv
