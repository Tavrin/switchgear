from __future__ import annotations

import os
from typing import Sequence

from .env import BWRAP
from .errors import Refuse
from .identity import WorktreeIdentity
from .policy import CompiledPolicy

TRUSTED_BWRAP = BWRAP

# Fixed in-sandbox path for the brokered upstream socket.
BROKER_SOCKET_PATH = "/run/switchgear-broker.sock"
# Where a delegation socket appears, when an operator has enabled delegation.
# Absent from the sandbox entirely otherwise -- not present-and-refusing, since
# a worker should not be able to tell that the feature exists.
DELEGATE_SOCKET_PATH = "/run/switchgear-delegate.sock"
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


def build_probe_argv(*, synth_home: str, provider_argv: Sequence[str]) -> list[str]:
    """The smallest sandbox that can ask a provider binary a question.

    Used for `--version` and `--help`. Those look harmless, which is exactly why
    they were being run straight on the host with the caller's whole environment
    inherited -- from `providers`, from `doctor`, and from `capabilities`, the
    command documented as safe to run when everything else is broken. Three
    separate places in this repo state the rule that makes that wrong:
    "Never execute the provider outside bwrap -- not even --version."

    The mount set is deliberately smaller than a job's and smaller than the
    credential-refresh one: no worktree, no git dir, no state store, no auth
    directory, no prompt. The binary is given nothing to act on and nothing to
    read. Network is unshared, because a version string needs none.
    """
    bwrap = require_bwrap()
    argv: list[str] = [
        bwrap,
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    for src, dst in (("/usr", "/usr"), ("/bin", "/bin"), ("/lib", "/lib"),
                     ("/lib64", "/lib64"), ("/etc/ssl", "/etc/ssl")):
        if _exists(src):
            argv.extend(["--ro-bind", src, dst])
    argv.extend(["--bind", synth_home, synth_home])
    argv.extend(["--setenv", "HOME", synth_home])
    argv.extend(["--setenv", "PATH", "/usr/bin:/bin"])
    # The provider itself, plus whatever else it needs to start (Codex ships
    # helper binaries beside its executable).
    for path in provider_argv:
        if os.path.isabs(path) and _exists(path):
            argv.extend(["--ro-bind", path, path])
            sibling = os.path.dirname(os.path.realpath(path))
            if _exists(sibling):
                argv.extend(["--ro-bind", sibling, sibling])
    argv.extend(["--chdir", synth_home])
    argv.append("--")
    argv.extend(list(provider_argv))
    return argv


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

    - switchgear still NEVER reads the refresh token. The CLI that owns the
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


def _traversable_ancestors(argv: list[str]) -> list[str]:
    """Ancestor dirs of every bind destination, so a non-root payload can reach them.

    bwrap creates the parents of a bind destination itself, as `drwx------` owned
    by the namespace root. With the uid boundary on, the payload runs as a
    different id and cannot traverse them -- measured: the provider binary was
    bind-mounted correctly and the worker still died with `Permission denied`
    opening it, because `/home` inside the sandbox was mode 700.

    Widening these to 0755 grants traversal, not content: they are empty tmpfs
    directories whose only contents are the binds we chose. Nothing becomes
    reachable that was not already mounted.
    """
    dests: list[str] = []
    for i, tok in enumerate(argv):
        if tok in ("--bind", "--ro-bind", "--dev-bind") and i + 2 < len(argv):
            dests.append(argv[i + 2])
    seen: dict[str, None] = {}
    for dest in dests:
        node = os.path.dirname(os.path.abspath(dest))
        chain = []
        while node not in ("/", ""):
            chain.append(node)
            node = os.path.dirname(node)
        for node in reversed(chain):
            seen.setdefault(node, None)
    return list(seen)


def build_bwrap_argv(
    *,
    ident: WorktreeIdentity,
    policy: CompiledPolicy,
    synth_home: str,
    provider_argv: Sequence[str],
    command_binds: Sequence[str] | None = None,
    broker_socket: str | None = None,
    delegate_socket: str | None = None,
    session_binds: Sequence[tuple[str, str]] | None = None,
    uid_boundary: bool = False,
    no_network: bool = False,
) -> list[str]:
    """Build the sandbox command line.

    `uid_boundary` adds the payload drop that makes the worker run as a subuid
    rather than as the invoking user. The bwrap flags that create the blocked
    user namespace are added by process.run_sandboxed instead, because they
    carry pipe fds it owns and the two cannot be separated. See userns.py for
    why the one-line version of this is a boundary in appearance only.
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
    if no_network and not broker_socket:
        # A sandbox with nothing to reach. Post-write gate commands take this:
        # they are built with no broker socket, and --unshare-net was only added
        # WHEN one was present -- so a verification command had host networking
        # inside a job whose worker had none.
        argv.append("--unshare-net")
    if broker_socket:
        # With the credential broker reachable over a bind-mounted unix socket,
        # the sandbox needs no network of its own. Unix sockets are filesystem
        # objects and keep working across a network namespace, so this removes
        # ALL outbound reachability except the one brokered upstream.
        argv.append("--unshare-net")
        argv.extend(["--bind", broker_socket, BROKER_SOCKET_PATH])
    if delegate_socket:
        # Same shape as the credential broker, for the same reason: the
        # capability stays controller-side and the worker gets a socket. It can
        # ask for a subagent; it cannot construct one, and it still cannot see
        # the CLI, the state root or any credential.
        argv.extend(["--bind", delegate_socket, DELEGATE_SOCKET_PATH])
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
    # Durable provider CONVERSATION state, bound over the per-job HOME so a
    # resumed job can find the session the previous one created. Deliberately
    # narrow: only the paths an adapter names, never the whole provider config
    # directory, which is where credentials live.
    for src, dst in session_binds or ():
        argv.extend(["--bind", src, dst])
    argv.extend(["--chdir", wt])

    for extra in command_binds or ():
        if extra not in {wt, common, ident.git_dir, synth_home} and os.path.exists(extra):
            argv.extend(["--ro-bind", extra, extra])

    if uid_boundary:
        # Computed from the finished bind list but SPLICED IN AT THE FRONT.
        # bwrap applies operations left to right, so a parent created after the
        # bind that needed it is too late -- the bind already made it, mode 700.
        # Measured exactly that way: the pre-creates were appended and the worker
        # still could not open its own provider binary.
        pre: list[str] = []
        for node in _traversable_ancestors(argv):
            pre.extend(["--perms", "0755", "--dir", node])
        # Spliced AFTER the tmpfs/proc/dev setup and BEFORE the binds. Both edges
        # were found by measurement: appended at the end, the binds had already
        # created the parents at 0700; inserted at the very front, `--tmpfs /tmp`
        # then mounted a fresh 0700 tmpfs straight over them, and a synthetic
        # HOME under /tmp was unreachable again.
        try:
            at = argv.index("--tmpfs") + 2
        except ValueError:
            at = 1
        argv[at:at] = pre

    argv.append("--")
    if uid_boundary:
        # Drop to the unmapped payload id. bwrap runs the payload as inside-0,
        # which the map points at the invoking user -- exactly the identity this
        # exists to escape -- so the drop happens here, immediately before the
        # provider, and setpriv clears the two capabilities that allowed it.
        from .userns import payload_prefix

        argv.extend(payload_prefix())
    argv.extend(list(provider_argv))
    return argv
