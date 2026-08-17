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
