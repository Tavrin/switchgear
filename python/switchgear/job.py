from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

from . import broker as brokermod
from . import commands as cmdlib
from . import delegate as delegatemod
from . import events, identity, jobstate, lease, process, provider, review, sandbox, sessions, state
from .digest import sha256_json
from .errors import DirtyWorktree, ProviderError, Refuse
from .paths import require_disjoint, safe_rmtree
from .policy import CompiledPolicy, compile_policy
from .profile import load_profile
from .registry import model_record, provider_record as registry_provider, registry_digest, wire_model_names
from .schema import validate
from .state import StateRoot, atomic_write_json, new_job_id, read_json


# Contract version of the durable job record. See the note where it is written.
SCHEMA_VERSION = 1

# A caller's own identifiers, carried through and handed back. Bounded so a
# record cannot be used as a side-channel store, and NEVER read for policy,
# routing or permissions -- see _correlation().
CORRELATION_MAX_KEYS = 16
CORRELATION_MAX_KEY = 64
CORRELATION_MAX_VALUE = 512


def _correlation(envelope: dict | None) -> dict[str, str] | None:
    """Validate and pass through the caller's own identifiers.

    Switchgear job ids are uuids. A supervisor driving a dozen jobs holds the
    id-to-intent map only in its own context, so losing that context leaves a
    state root of anonymous uuids -- and recovery, deduplication and cancelling a
    whole wave are all downstream of not having this.

    It is deliberately inert. Switchgear persists it and returns it and does
    nothing else with it: the moment a caller-supplied string could influence a
    model, a path or a limit, it would be an input to the security boundary
    rather than a label on it. Bounded in count and length because it is written
    into a record this tool guarantees the shape of.
    """
    raw = (envelope or {}).get("correlation")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise Refuse(
            "correlation must be an object of string keys to string values, "
            'e.g. {"workflow": "nightly", "task": "T-91"}'
        )
    if len(raw) > CORRELATION_MAX_KEYS:
        raise Refuse(
            f"correlation has {len(raw)} keys, limit is {CORRELATION_MAX_KEYS}. "
            "It labels a job; it is not a place to store the caller's state."
        )
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise Refuse(
                f"correlation key and value must both be strings; got "
                f"{type(k).__name__} -> {type(v).__name__}"
            )
        if len(k) > CORRELATION_MAX_KEY or len(v) > CORRELATION_MAX_VALUE:
            raise Refuse(
                f"correlation entry {k[:CORRELATION_MAX_KEY]!r} exceeds the limit "
                f"({CORRELATION_MAX_KEY}-char key, {CORRELATION_MAX_VALUE}-char value)."
            )
        out[k] = v
    return out


def _now() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _runner_record(
    *, job_id: str, worktree: str, harness: str, mode: str, role: str,
    model: dict[str, Any], session_store_id: str | None = None,
) -> dict[str, Any]:
    """Launch identity and attribution persisted before provider execution.

    Keeping fixture construction on this path matters: a crashed job has no
    result record to correct an incomplete runner record after the fact.

    What this record attests is what the job was LAUNCHED to run, not that it
    ran. It is written when the job directory is created, which is before the
    lease check, so a bounded write refused for a missing lease still leaves one
    behind. That ordering is deliberate and should not be 'fixed' by moving the
    write later: this is also the job's liveness marker, and a job that died
    between directory creation and the lease check would then have no record at
    all, reporting `unknown` where it can currently report `died`. A consumer
    reads `state` for what happened and this for what it was configured with.
    """
    return {
        "pid": os.getpid(),
        "starttime": lease._starttime(os.getpid()),
        "boot_id": lease._boot_id(),
        "job_id": job_id,
        "session_store_id": session_store_id,
        "dir": worktree,
        "harness": harness,
        "mode": mode,
        "role": role,
        "model": model,
        # Kept as the legacy harness key because projections of running jobs
        # resolve their adapter from it. It does not name the model pool here.
        "provider": harness,
        # A result-less job must keep the vocabulary chosen by the binary that
        # launched it. Without this start-time fact, upgrading the installed
        # package underneath a running job relabelled its live projection and
        # could later disagree with the events.v<N>.jsonl it persisted.
        "events_normalized_version": NORMALIZED_EVENTS_VERSION,
    }


def _timeout(policy: CompiledPolicy) -> int:
    env = os.environ.get("AI_OPENCODE_TIMEOUT")
    if env is not None and env != "":
        if not env.isdigit():
            raise Refuse(f"AI_OPENCODE_TIMEOUT must be a non-negative integer, got {env!r}")
        val = int(env)
    else:
        val = policy.timeout_s
    if val < policy.timeout_min or val > policy.timeout_max:
        raise Refuse(f"timeout '{val}' must be {policy.timeout_min}..{policy.timeout_max} (0 is not a disable)")
    return val


MIN_FREE_BYTES = int(os.environ.get("SWITCHGEAR_MIN_FREE_BYTES") or 2 * 1024 * 1024 * 1024)


def _require_disk_headroom(path: str) -> None:
    """Refuse to START a write job when the filesystem is already low.

    A worker can write into its worktree as fast as the disk allows (measured:
    500MB in 0.1s), and bwrap offers no aggregate quota for a bind mount, so
    this bounds only the STARTING condition. Real aggregate containment needs a
    size-limited filesystem for the worktree -- see docs.
    """
    import shutil

    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        raise Refuse(
            f"only {free // (1024*1024)}MB free on the worktree filesystem; "
            f"bounded-write needs {MIN_FREE_BYTES // (1024*1024)}MB headroom "
            "(set SWITCHGEAR_MIN_FREE_BYTES to override)"
        )


def _resolve_effort(requested: str | None, adapter, model: dict) -> str | None:
    """Validate a profile's effort request against what the MODEL can prove.

    Effort is profile-owned, never a caller flag, for the same reason model
    choice is: it is a cost and behaviour lever, and the profile is where those
    are fixed and validated. A bare --effort override would defeat the invariant
    the budget, the model allowlist and the command allowlist all rely on.

    Two separate facts are checked, because they come from different places and
    fail differently:

      * the PROVIDER must have an effort control at all -- mechanism, from the
        adapter;
      * the MODEL must have a measured set containing this value -- fact, from
        the controller registry.

    Refuses rather than silently dropping. Measured: OpenCode ACCEPTS an
    unrecognised effort and runs the job to completion at full price, so passing
    an unverified value through buys a job that quietly ran at the model's
    default while the record claims otherwise. A refusal naming its remedy is
    worth more than a lie in the evidence.
    """
    if not requested:
        return None
    from .adapters import EFFORT_SUPPORTED
    from .registry import effort_values

    support = adapter.effort_support()
    if support.get("status") != EFFORT_SUPPORTED:
        raise Refuse(
            f"provider {adapter.name!r} has no effort control; remove `effort` "
            "from this role."
        )

    model_id = model.get("id")
    values = effort_values(model)
    if values is None:
        raise Refuse(
            f"no effort values have been measured for model {model_id!r}, so the "
            "rail will not send one blindly. Effort is a per-MODEL fact — one "
            "provider was measured serving two models with different sets — so "
            "it cannot be inferred from the provider. Measure it (send a "
            "deliberate nonsense value; most providers answer with the accepted "
            f"list) and add `effort_values` to {model_id!r} in "
            "switchgear/data/models/registry.json, or drop `effort` from this role."
        )
    if requested not in values:
        raise Refuse(
            f"effort {requested!r} is not accepted by {model_id!r} "
            f"(measured: {', '.join(values)})"
        )
    return requested


def _started_at_iso(job_dir: str) -> str:
    """The job's real start, from the marker create_job_dirs wrote."""
    try:
        with open(os.path.join(job_dir, "started_at"), encoding="utf-8") as fh:
            return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(float(fh.read().strip())))
    except (OSError, ValueError):
        # No marker means no start time. Falling back to "now" would restore the
        # exact bug this replaces, so be honest and let the caller see the gap.
        return _now()


def _reclaim_sandbox_home(home: str, state_root: str) -> None:
    """Delete the per-job synthetic HOME once the record is written.

    A live provider populates it with ~150MB of npm cache and node_modules per
    job; it is never reused and nothing else ever reads it, so retaining it grew
    the state store without bound. Evidence and result.json are kept.
    """
    if os.environ.get("SWITCHGEAR_KEEP_SANDBOX_HOME") == "1":
        return
    from .paths import safe_rmtree

    # Guarded rather than bare: `home` is interpolated, and nothing but luck
    # stopped an empty or wrong value from pointing somewhere the rail does not
    # own. Reclaiming a home is best-effort, so a genuine failure is swallowed —
    # but a REFUSAL is not, because that means the path was wrong.
    try:
        safe_rmtree(home, must_be_under=state_root, label="sandbox home")
    except OSError:
        pass


CREDENTIAL_NAMES = ("auth.json", "credentials.json", ".credentials.json", "token.json")
CREDENTIAL_SUFFIXES = (".key", ".pem")


def _assert_no_credentials(store: str) -> None:
    """Refuse to persist a session store that has collected a credential.

    Session stores are chosen narrowly, but some sit right beside a credential
    on the host -- OpenCode keeps auth.json in the same data directory as its
    session database. Today our sandbox never holds a real credential for those
    providers, so nothing can be written; that is an observation about the
    current configuration, not a property of it. This turns a silent future leak
    into a loud refusal.
    """
    for base, _dirs, files in os.walk(store):
        for name in files:
            low = name.lower()
            if low in CREDENTIAL_NAMES or low.endswith(CREDENTIAL_SUFFIXES):
                raise Refuse(
                    f"refusing to persist session state: {os.path.join(base, name)} "
                    "looks like a credential. A session store must hold conversation "
                    "history only; delete it and narrow the adapter's "
                    "session_store_paths()."
                )


#: Contract version of the normalized event stream, and the filename it is
#: written under. The version is in the NAME as well as in every line: a
#: consumer that finds the file it knows how to read does not have to open it to
#: discover whether it can.
NORMALIZED_EVENTS_VERSION = 2
NORMALIZED_EVENTS_NAME = f"events.v{NORMALIZED_EVENTS_VERSION}.jsonl"


def _write_normalized_events(evidence_dir: str, adapter, raw: bytes | str) -> str | None:
    """Project the raw provider stream into the normalized vocabulary, on disk.

    `evidence/events.jsonl` is the provider's stdout byte for byte. That is the
    right thing for it to be -- it is the forensic record, and normalizing on the
    way in would mean the only durable copy had already been through our own
    parser. But it left the adapter seam invisible from outside: the caller
    contract told integrators to tail that file and map it themselves, so every
    consumer had to learn four providers' event shapes, which is the exact
    knowledge this tool exists to absorb.

    So: raw stays raw, and this is the public shape beside it. Same normalize()
    the digest and `status` already use, so the two cannot drift.

    Returns None if the projection could not be written, and the record says so
    rather than naming a file that is not there -- a missing artifact must never
    be inferred to be an empty one. `logs --format normalized` recomputes from
    the raw stream, so nothing is lost.
    """
    from .adapters import parse_lenient

    path = os.path.join(evidence_dir, NORMALIZED_EVENTS_NAME)
    try:
        # The captured stream is bytes -- `errors="replace"` rather than strict
        # because a projection must not fail on a provider that emitted one bad
        # byte, and the raw stream keeps the original either way.
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        parsed, _ = parse_lenient(text)
        normalized = adapter.normalize(parsed, run_ended=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for ev in normalized:
                fh.write(json.dumps(dict(ev, v=NORMALIZED_EVENTS_VERSION),
                                    separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path
    except Exception as exc:  # noqa: BLE001 - a projection must never fail a job
        print(
            f"switchgear: WARNING — could not write the normalized event stream "
            f"({exc}). The raw stream is unaffected; recompute with "
            f"`switchgear logs --format normalized`.",
            file=sys.stderr,
        )
        return None


def _security_facts(
    *,
    bwrap_argv: list[str],
    uid_boundary: dict | None,
    broker_socket: str | None,
    credential_in_sandbox: bool,
) -> dict[str, Any]:
    """What containment this job ACTUALLY got, not what it asked for.

    Every field here is read back off the argv that was really constructed, or
    off the capability probe that really ran. That direction matters: a policy
    says what was wanted, and until now the record said nothing at all about what
    was achieved. A caller could not distinguish a job that ran under a uid
    boundary from one on a machine that cannot establish one, because both
    requested the same thing and neither was recorded.

    So a consumer can state a REQUIREMENT ("brokered credential, no direct
    network, real uid boundary") and test it against the outcome, instead of
    knowing how this tool builds namespaces. Do not populate any of it from the
    policy: the moment one field reports intent, none of them can be trusted.
    """
    flags = set(bwrap_argv)
    return {
        "containment": {
            "backend": os.path.basename(bwrap_argv[0]) if bwrap_argv else None,
            # bwrap always creates a mount namespace; the rest are per-flag.
            "mount_namespace": True,
            "pid_namespace": "--unshare-pid" in flags,
            "ipc_namespace": "--unshare-ipc" in flags,
            "uts_namespace": "--unshare-uts" in flags,
            "network_namespace": "--unshare-net" in flags,
        },
        "identity": {
            # "same-user" is the honest answer for bounded-write, and for a
            # readonly job on a machine without subuid ranges or the setuid map
            # helpers. Those two cases are indistinguishable in the policy and
            # must not be indistinguishable here.
            "uid_boundary": "subuid" if uid_boundary else "same-user",
            "payload_uid": (uid_boundary or {}).get("payload_uid"),
        },
        "credential": {
            "posture": (
                "in-sandbox-access-token" if credential_in_sandbox
                else "brokered" if broker_socket
                else "none"
            ),
            "enters_worker": bool(credential_in_sandbox),
        },
        "network": {
            # Both halves are required: a network namespace with no broker is
            # unreachable, and a broker without the namespace is not the only
            # route out.
            "broker_only": bool(broker_socket) and "--unshare-net" in flags,
            "direct": "--unshare-net" not in flags,
        },
    }


def run_job(
    *,
    profile_path: str,
    state_path: str,
    mode: str,
    role: str,
    worktree: str,
    prompt: str,
    provider_path: str,
    envelope: Optional[dict[str, Any]] = None,
    lease_token: Optional[str] = None,
    job_id: Optional[str] = None,
    attachments: Optional[dict[str, str]] = None,
    resume_session: Optional[str] = None,
    resumed_from: Optional[str] = None,
    session_store_id: Optional[str] = None,
    legacy_git_identity_after: Optional[str] = None,
) -> dict[str, Any]:
    profile = load_profile(profile_path)
    profile_digest = sha256_json(profile)
    policy = compile_policy(profile, mode)
    spec = policy.role(role)
    if spec["mode"] != mode:
        raise Refuse(f"role {role} is mode {spec['mode']}, not {mode}")
    model = model_record(spec["model"])
    if spec["model"] not in policy.models_allow:
        raise Refuse(f"model '{spec['model']}' is not allowed by the profile")

    ident = identity.inspect_worktree(worktree)
    if mode == "bounded-write":
        if os.environ.get("SWITCHGEAR_WRITE") != "1":
            raise Refuse("write is disabled (SWITCHGEAR_WRITE is not 1)")
        if policy.require_linked_for_write:
            identity.require_linked(ident)
    elif not policy.allow_primary_for_readonly and not ident.linked_worktree:
        raise Refuse("primary checkout not allowed for this readonly profile")

    root = StateRoot(state_path)
    # The synthetic HOME is bind-mounted writable and lives under the state root.
    # If the state root overlapped the target, that writable bind would nest
    # inside the --ro-bind and defeat readonly containment on the host.
    require_disjoint(root.path, ident.realpath, "state root", "worktree")
    require_disjoint(root.path, ident.common_git_dir, "state root", "git dir")
    # A background launch picks the id in the PARENT so it can hand the caller a
    # job id and a log path immediately, before any work starts. create_job_dirs
    # still creates the directory exclusively, so a collision is still a failure.
    from .adapters import get_adapter
    from . import quota as quotamod

    adapter = get_adapter(profile.get("provider"))

    # Second containment layer: run the worker as a uid that is not the invoking
    # user, so the kernel refuses a write even to something that became reachable
    # by mistake. READONLY only -- a bounded-write worker must produce files the
    # controller then reads and commits, and files written by a subuid are owned
    # by that subuid with no way to hand them back without privileges we do not
    # have. Claiming the boundary for a lane where it breaks ownership would
    # trade a real property for a broken one. Scout and review are readonly,
    # which is most jobs and includes the review gate itself.
    from . import userns as usernsmod

    uid_boundary = None
    if mode == "readonly":
        _cap = usernsmod.capability()
        if _cap.get("available"):
            uid_boundary = _cap
    # Validated here rather than at argv-build time: an unusable effort request
    # must cost nothing, so it is refused before the job directory exists and
    # before the budget is touched.
    effort = _resolve_effort(spec.get("effort"), adapter, model)
    # Same reasoning as effort: a malformed label must cost nothing, so it is
    # refused before the job directory exists and before the budget is touched.
    correlation = _correlation(envelope)

    # Before any work, and before any job directory exists: a budget checked
    # after the spend is an audit, not a control.
    quotamod.assert_within_budget(root.path)
    job_id = job_id or new_job_id()
    legacy_resume = bool(resume_session) and session_store_id is None
    if session_store_id is None:
        session_store_id = sessions.new_session_store_id()
    else:
        sessions.require_session_store_id(session_store_id)

    # ONE enforcement point, so --background is covered automatically (it just
    # re-execs this CLI) rather than special-cased per command.
    from . import concurrency

    queued_s = concurrency.acquire(
        root.path, job_id,
        wait=os.environ.get("SWITCHGEAR_BACKGROUND_CHILD") == "1",
    )
    dirs = state.create_job_dirs(root, job_id)
    # Launch attribution for EVERY job, not just backgrounded ones. Without it a
    # FOREGROUND job whose process died left a directory with no result.json,
    # and `status` reported "running" forever -- measured on a job abandoned five
    # hours earlier. pid alone is not identity (pids are recycled), hence
    # starttime and boot_id, the same triple the lease uses.
    # The lineage id has to survive a controller crash before provider startup:
    # otherwise the durable conversation exists but no record can identify
    # which store belongs to the job. A failed attribution write therefore
    # refuses the launch instead of spending with an unidentifiable store.
    try:
        atomic_write_json(
            os.path.join(dirs["job"], "runner.json"),
            _runner_record(
                job_id=job_id,
                worktree=ident.realpath,
                harness=adapter.name,
                mode=mode,
                role=role,
                model=model,
                session_store_id=session_store_id,
            ),
        )
    except Exception:
        # Attribution failure now refuses the launch. create_job_dirs has
        # already made a job that gc must protect as unknown, so leaving it here
        # would strand permanent litter on every refused launch.
        concurrency.release(root.path, job_id)
        try:
            safe_rmtree(
                dirs["job"], must_be_under=root.jobs,
                label=f"job {job_id} after runner attribution failure",
            )
        except Exception:
            # Cleanup must not replace the attribution error: that is the cause
            # the caller can remedy, while the leftover directory is visible
            # evidence an operator can remove deliberately.
            pass
        raise
    lock_cm = None
    token_uuid = None
    if mode == "bounded-write":
        if policy.require_lease_for_write:
            # Refuse first if no lease exists at all, then require the caller to
            # actually present its token. Reading the token off disk and
            # validating it against itself is not authorization (finding N6).
            lease.load_token(root, ident)
            if not lease_token:
                raise Refuse("bounded-write requires the lease token (--token)")
            token_uuid = lease_token
            lock_cm = lease.WorkerLock(root, ident, token_uuid, job_id, mode)
        else:
            # Lock-free mode still needs mutual exclusion: promotion takes the
            # same flock, and an unserialized writer could move the tree between
            # the live sample and the status write (luna-2).
            lock_cm = lease.WorktreeLock(root, ident)
        _require_disk_headroom(ident.realpath)

    def _execute_with_broker() -> dict[str, Any]:
        """Run the job, fronting the provider with a credential broker when live.

        The credential stays in the controller process; the sandbox is given a
        loopback URL and a placeholder key. See broker.py.
        """
        cred = None
        prec: dict[str, Any] = {}
        provider_id = model.get("provider") or "opencode-go"
        if os.environ.get("SWITCHGEAR_ALLOW_LIVE_PROVIDER") == "1":
            from . import credentials as credmod

            prec = registry_provider(provider_id)
            # Resolved by the provider's declared credential class: an
            # operator-installed API key, or the access token out of a CLI's own
            # OAuth session. Either way it stays controller-side.
            # If the access token has gone stale, let the provider's OWN CLI
            # refresh it before giving up. These tokens last about an hour and
            # every vendor CLI refreshes on use, so refusing would send the
            # operator off to run a command by hand for a credential that is
            # perfectly valid.
            prov_argv_for_refresh, _ = provider.resolve_provider(provider_path)
            cred = credmod.load_credential(
                provider_id,
                prec,
                on_expired=lambda: credmod.refresh_via_own_cli(
                    provider_id, prec, list(prov_argv_for_refresh), adapter
                ),
            )
            upstream = os.environ.get("SWITCHGEAR_PROVIDER_UPSTREAM") or prec["upstream"]
            if not cred:
                # Fail closed. Falling through to the no-broker path here would
                # silently drop --unshare-net (the namespace is requested only
                # when there is a broker socket to bind), leaving a live provider
                # process on the host network. It could not reach a model without
                # a credential, so this bought nothing and cost the strongest
                # containment property the rail has.
                raise Refuse(
                    f"live provider requested but no credential for {provider_id!r} "
                    f"at {provider.credential_path(prec.get('credential') or provider_id)} "
                    "(refusing: an unbrokered live job would run on the host network)"
                )
        if not cred:
            return _execute(None, None, {}, provider_id)
        sock = os.path.join(dirs["job"], "broker.sock")
        from . import quota as quotamod

        with brokermod.CredentialBroker(
            cred,
            upstream=upstream,
            allowed_models=wire_model_names(model["id"]),
            unix_socket=sock,
            # The worker is not this user under the uid boundary, so 0600 would
            # lock it out of its own broker.
            socket_mode=0o666 if uid_boundary else 0o600,
            max_calls=quotamod.max_provider_calls(),
            allowed_paths=tuple(prec.get("allowed_paths") or ()) or None,
            allowed_get_paths=tuple(prec.get("allowed_get_paths") or ()) or None,
            auth_header=prec.get("auth_header") or "authorization",
            auth_scheme=prec.get("auth_scheme", "Bearer"),
        ) as bk:
            return _execute(bk, cred, prec, provider_id)

    def _execute(bk, cred=None, prec=None, provider_id=None) -> dict[str, Any]:
        runtime = policy.to_opencode_runtime()
        broker_url = f"http://127.0.0.1:{sandbox.BROKER_RELAY_PORT}" if bk is not None else None
        if broker_url:
            # How a provider is redirected at the broker is provider-specific:
            # OpenCode takes it in its config, Grok takes it in the environment.
            # Both go through the adapter so neither leaks into the lifecycle.
            runtime = adapter.broker_runtime(runtime, broker_url, model["id"])
        mock_beh = os.environ.get("SWITCHGEAR_MOCK_BEHAVIOR")
        if mock_beh:
            with open(os.path.join(dirs["home"], ".mock-behavior"), "w", encoding="utf-8") as fh:
                fh.write(mock_beh + "\n")
            extra = os.environ.get("SWITCHGEAR_MOCK_EXTRA", "")
            if extra:
                with open(os.path.join(dirs["home"], ".mock-extra"), "w", encoding="utf-8") as fh:
                    fh.write(extra)
        # Material the job must READ but that cannot live in the worktree.
        #
        # The review diff used to be concatenated into the prompt, which is one
        # element of argv. The kernel caps a SINGLE argument at MAX_ARG_STRLEN
        # (128KiB), and worktree_diff caps at 200KB, so any real repo with a
        # large uncommitted change failed at exec with E2BIG before the provider
        # ever started -- measured on a large private repository, and reproduced here with a
        # 229KB diff. A file has no such limit.
        #
        # It goes in the sandbox HOME rather than the worktree: writing it into
        # the tree under review would mutate the very thing being frozen.
        attach_dir = None
        if attachments:
            attach_dir = os.path.join(dirs["home"], "switchgear")
            os.makedirs(attach_dir, mode=0o700, exist_ok=True)
            for name, content in attachments.items():
                # Flat basenames only: an attachment name is controller-supplied
                # today, and keeping it incapable of traversal means it stays
                # safe if that ever changes.
                safe = os.path.basename(name)
                with open(os.path.join(attach_dir, safe), "w", encoding="utf-8") as fh:
                    fh.write(content)

        agent_name = adapter.agent_name(mode)
        provider.write_agent_definition(
            dirs["home"], agent_name, policy.agent_definition(role, attach_dir)
        )
        # Fallback tier: a provider whose CLI validates its session locally
        # (Grok) needs its access token inside the sandbox. Only for such an
        # adapter, and only with a live credential and a broker in play, so the
        # token still has no egress except the one brokered upstream.
        if adapter.credential_in_sandbox and cred is not None and bk is not None:
            adapter.write_sandbox_credential(dirs["home"], provider_id, prec or {})
        env = adapter.isolation_env(dirs["home"], runtime, broker_url)
        prov_argv, _live = provider.resolve_provider(provider_path)
        # The profile names a provider BINARY and --provider supplies one; if
        # they disagree the rail would build (say) Claude's argv for the Codex
        # executable. It used to surface as a confusing VERSION error naming the
        # wrong provider and recommending a verify command that cannot help --
        # and which, if forced through, would record a foreign version and let
        # the mismatch actually run. Name it for what it is.
        from .compat import pinned_for_path

        _pinned = pinned_for_path(prov_argv[-1]) if prov_argv else None
        if _pinned and _pinned[0] != adapter.name:
            raise Refuse(
                f"provider mismatch: the profile declares provider "
                f"{adapter.name!r} but --provider is the {_pinned[0]!r} binary "
                f"({prov_argv[-1]}). Use that provider's own profile, or point "
                f"--provider at the {adapter.name!r} binary "
                "(`switchgear providers` prints the path)."
            )
        # Providers without an agent-file mechanism get the SAME role
        # instructions in their prompt. One source (policy.role_instructions),
        # two delivery paths -- a worker told a different contract from the one
        # the rail validates fails in a way that looks like a model problem.
        job_prompt = adapter.compose_prompt(prompt, policy.role_instructions(role))
        if attach_dir:
            lines = [job_prompt.rstrip(), "", "Attached files (read these first):"]
            for name in sorted(attachments or {}):
                safe = os.path.basename(name)
                full = os.path.join(attach_dir, safe)
                lines.append(f"  {full}  ({os.path.getsize(full)} bytes)")
            lines += [
                "",
                "You have no shell and no git. Do not go looking for the change",
                "yourself: the attached file IS the change under review.",
            ]
            job_prompt = "\n".join(lines)
        # Provider inside sandbox: mock gets dir via --dir
        inner = adapter.argv(
            provider_argv=list(prov_argv),
            worktree=ident.realpath,
            model_id=model["id"],
            agent=agent_name,
            role=role,
            job_id=job_id,
            prompt=job_prompt,
            attach_dir=attach_dir,
            resume_session=resume_session,
            effort=effort,
        )
        # bind mock script if python
        extra_binds = [p for p in prov_argv if os.path.isabs(p) and os.path.exists(p)]
        # A provider may need more than its own executable (Codex ships helper
        # binaries beside it). Read-only, and only what the adapter names.
        extra_binds += [
            b for b in adapter.extra_binds(list(prov_argv))
            if os.path.exists(b)
        ]
        broker_sock = None
        if bk is not None:
            relay = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_relay.py")
            extra_binds = list(extra_binds) + [relay]
            broker_sock = bk.unix_socket
            inner_prefix = [
                "/usr/bin/python3", relay,
                "--socket", sandbox.BROKER_SOCKET_PATH,
                "--port", str(sandbox.BROKER_RELAY_PORT), "--",
            ]
        else:
            inner_prefix = []
        if _live:
            # Validate the pinned version by running the provider INSIDE the
            # boundary. Never execute an untrusted provider on the host.
            probe = process.run_sandboxed(
                sandbox.build_bwrap_argv(
                    ident=ident,
                    policy=policy,
                    synth_home=dirs["home"],
                    provider_argv=adapter.version_argv(list(prov_argv)),
                    command_binds=extra_binds,
                ),
                env=env,
                timeout_s=30,
            )
            provider.assert_pinned_version(
                probe.returncode, probe.stdout, probe.timed_out, adapter.name
            )
        # Durable conversation state follows this job's controller-minted
        # lineage. Fresh jobs never discover or adopt a store by filesystem
        # identity; only an explicit resume reaches an existing lineage.
        session_binds = []
        for rel in adapter.session_store_paths():
            src = os.path.join(
                root.path, "sessions", session_store_id, adapter.name, rel
            )
            os.makedirs(src, mode=0o700, exist_ok=True)
            _assert_no_credentials(src)
            session_binds.append((src, os.path.join(dirs["home"], rel)))

        # In-sandbox delegation, off unless an operator turned it on. Readonly
        # children only, and only in this job's own worktree: a nested writer
        # would need a second worktree, and creating one is orchestration.
        deleg_policy = delegatemod.policy_for(quotamod.load_budget())
        deleg_broker = None
        deleg_sock = None
        if deleg_policy["enabled"] and mode == "readonly":
            deleg_sock = os.path.join(dirs["job"], "delegate.sock")
            deleg_broker = delegatemod.DelegationBroker(
                unix_socket=deleg_sock,
                parent_job=job_id,
                worktree=ident.realpath,
                state_path=root.path,
                profile_path=profile_path,
                provider_path=provider_path,
                roles=deleg_policy["roles"],
                max_children=deleg_policy["max_children"],
                max_depth=deleg_policy["max_depth"],
                depth=int(os.environ.get("SWITCHGEAR_DELEGATION_DEPTH") or 0),
                # Same reasoning as the credential broker: under the uid boundary
                # the worker is not this user, and 0600 would lock it out of its
                # own socket. Safe because the job directory is 0700 and ours.
                socket_mode=0o666 if uid_boundary else 0o600,
            )

        bwrap_argv = sandbox.build_bwrap_argv(
            ident=ident,
            policy=policy,
            synth_home=dirs["home"],
            provider_argv=inner_prefix + inner,
            command_binds=extra_binds,
            broker_socket=broker_sock,
            delegate_socket=deleg_sock,
            session_binds=session_binds,
            uid_boundary=bool(uid_boundary),
        )
        before_id = identity.git_identity_digest(ident)
        before_tree = identity.tree_digest(ident)
        before_fp = identity.dirty_fingerprints(ident)
        timeout = _timeout(policy)
        # Evidence is STREAMED, not written after the fact: the provider's stdout
        # lands in evidence/events.jsonl as it arrives, so the record is readable
        # while the job runs. That is the whole basis for observing a delegated
        # agent mid-run -- previously there was literally nothing on disk until
        # the process exited.
        #
        # It also strengthens the existing "persist before the integrity asserts"
        # property rather than weakening it: those asserts can raise (a worker
        # that redirected .git), and now the record is already durable no matter
        # where the job dies, including a controller crash. The worker still
        # cannot tamper with it -- the job dir is not bind-mounted into the
        # sandbox, and stdout is a pipe, so the worker can append but never seek
        # back over what it already emitted.
        ev_path = os.path.join(dirs["evidence"], "events.jsonl")
        err_path = os.path.join(dirs["evidence"], "stderr")
        if uid_boundary:
            # The worker runs as a different id now, so the directories the
            # controller made FOR it have to be usable by it. Evidence and the
            # job directory itself are deliberately not in this list.
            usernsmod.grant_payload_access(
                [dirs["home"]] + [src for src, _dst in session_binds]
            )
            # And git must stop refusing a repository it no longer owns.
            env = dict(env)
            env.update(usernsmod.payload_env())

        if deleg_broker is not None:
            with deleg_broker:
                result = process.run_sandboxed(
                    bwrap_argv,
                    env=env,
                    timeout_s=timeout,
                    stdout_path=ev_path,
                    stderr_path=err_path,
                    userns=uid_boundary,
                )
            # Read after the socket is torn down, so the counts cannot still be
            # moving while they are being written into the record.
            delegation_summary = deleg_broker.summary()
        else:
            delegation_summary = None
            result = process.run_sandboxed(
                bwrap_argv,
                env=env,
                timeout_s=timeout,
                stdout_path=ev_path,
                stderr_path=err_path,
                userns=uid_boundary,
            )
        # process set is the bwrap pid ns; after return it is dead

        identity.assert_gitdir_pointer_intact(ident)
        after = identity.inspect_worktree(ident.realpath)
        if not identity.same_core(ident, after):
            # NOT a bare Refuse. This raised before the record was built, so no
            # result.json was written and the job later read `died`/`unknown` --
            # and the caller got exit 1, a generic refusal. Four lines below,
            # the WEAKER integrity violation (`id_changed`) sets status="dirty"
            # and exit 2, which is what the published exit table promises for
            # "worktree integrity changed during the job". The stronger
            # violation reported as the vaguer failure.
            raise DirtyWorktree(
                "worktree identity changed during the job: the directory this "
                "job was running in is no longer the same worktree (device, "
                "inode or git dir moved). Nothing was promoted. Check whether "
                "something recreated or replaced the worktree while the job ran."
            )
        after_id = identity.git_identity_digest(ident)
        after_tree = identity.tree_digest(ident)
        id_changed = before_id != after_id

        # Four independent facts, recorded separately, with `status` derived from
        # them at the end. Previously one variable answered all four questions,
        # so a job that both errored AND left the tree dirty reported only
        # whichever branch ran first. See jobstate.project_status for the
        # precedence, which is unchanged.
        execution = jobstate.EXECUTION_COMPLETED
        integrity_outcome = jobstate.INTEGRITY_CLEAN
        change_state = jobstate.CHANGE_NONE
        acceptance = jobstate.ACCEPTANCE_NOT_REQUIRED
        err = ""
        handoff = None
        if result.truncated:
            execution = jobstate.EXECUTION_PROVIDER_ERROR
            err = "provider output exceeded the capture bound (evidence truncated)"
        elif result.timed_out:
            execution = jobstate.EXECUTION_TIMEOUT
            err = f"timed out after {timeout}s"
        elif id_changed:
            integrity_outcome = jobstate.INTEGRITY_DIRTY
            err = "git identity changed"
        else:
            # The RESULT path goes through the adapter, not a hardcoded event
            # vocabulary: OpenCode's terminal event is step_finish, Grok's is
            # `end`. A live Grok scout produced a complete, correct stream that
            # the OpenCode parser called "no terminal provider event".
            handoff, verr = adapter.validate_result(
                result.stdout, require_handoff=(mode == "bounded-write")
            )
            if verr:
                execution = jobstate.EXECUTION_PROVIDER_ERROR
                err = verr
            if execution == jobstate.EXECUTION_COMPLETED and result.returncode not in (0, None):
                # A well-formed handoff object is a claim by the provider, not
                # evidence of success. A crashed write is never promotable.
                execution = jobstate.EXECUTION_PROVIDER_ERROR
                err = f"provider exited {result.returncode}"

        _clean_so_far = (
            execution == jobstate.EXECUTION_COMPLETED
            and integrity_outcome == jobstate.INTEGRITY_CLEAN
        )
        if mode == "bounded-write" and _clean_so_far:
            # post-write commands inside the same sandbox
            for item in (envelope or {}).get("commands") or []:
                argv = cmdlib.resolve_command(item["verb"], item.get("args") or [])
                cmd_bwrap = sandbox.build_bwrap_argv(
                    ident=ident,
                    policy=policy,
                    synth_home=dirs["home"],
                    provider_argv=argv,
                    command_binds=extra_binds,
                    # A gate command verifies the tree; it has no reason to
                    # reach the network, and without this it had MORE reach
                    # than the worker whose output it is checking.
                    no_network=True,
                )
                cr = process.run_sandboxed(cmd_bwrap, env=env, timeout_s=min(30, timeout))
                if cr.returncode != 0 or cr.timed_out:
                    execution = jobstate.EXECUTION_PROVIDER_ERROR
                    err = "post-write command failed"
                    break
            identity.assert_gitdir_pointer_intact(ident)
            after2 = identity.inspect_worktree(ident.realpath)
            if not identity.same_core(ident, after2):
                integrity_outcome = jobstate.INTEGRITY_DIRTY
                err = "identity changed after commands"
            after = after2
            after_id = identity.git_identity_digest(ident)
            after_tree = identity.tree_digest(ident)
            if after_id != before_id:
                integrity_outcome = jobstate.INTEGRITY_DIRTY
                err = "git identity changed"
            if (execution == jobstate.EXECUTION_COMPLETED
                    and integrity_outcome == jobstate.INTEGRITY_CLEAN):
                # A bounded write that got this far has a delta the controller
                # will freeze, and nothing has accepted it yet. WHO decides that
                # is operator-owned: by default this tool's own interlock review,
                # or the caller when an operator has said so. The freeze and the
                # evidence are identical either way -- what changes is only who
                # may declare the change acceptable.
                change_state = jobstate.CHANGE_FROZEN
                acceptance = (
                    jobstate.ACCEPTANCE_AWAITING_REVIEW
                    if jobstate.acceptance_authority() == jobstate.ACCEPTANCE_INTERLOCK
                    else jobstate.ACCEPTANCE_AWAITING_EXTERNAL
                )

        status = jobstate.project_status(
            execution=execution,
            integrity=integrity_outcome,
            acceptance=acceptance,
            change=change_state,
        )

        record = {
            # Bumped only for a CHANGE THAT BREAKS A READER. Additive keys do not
            # move it -- the CLI already promises additive-only JSON, and a
            # version that increments on every addition tells a consumer nothing.
            # Absent means 1: records written before this existed are still valid
            # and must stay readable.
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "session_store_id": session_store_id,
            # The one word callers already branch on, DERIVED from the four facts
            # below rather than assigned directly. Its values and its precedence
            # are unchanged.
            "status": status,
            # The four facts `status` used to answer all at once. Separate
            # because they are independent: a job can complete cleanly and still
            # be awaiting a decision, and one that both errored and left a dirty
            # tree used to report only whichever branch happened to run first.
            "execution": {"outcome": execution},
            "change": {"state": change_state},
            "acceptance": {"state": acceptance},
            "mode": mode,
            "role": role,
            # The ADAPTER that ran this job, which is not derivable from the
            # model: `opencode-go/glm-5.3` is served by the `opencode` binary, so
            # model.provider names the pool and this names the code that can read
            # the stream back. Without it every projection fell back to the
            # ambient profile and, failing that, silently to "opencode".
            "provider": adapter.name,
            # Same value, settled noun. `provider` meant two different things in
            # one record -- this key (the executable that ran the job) and
            # model.provider (the pool that served the model) -- so a reader had to
            # know which sense applied from context. `harness` names the runtime,
            # `upstream` names the service. `provider` stays as an alias: nothing
            # that reads it has to change, and there is one source for both.
            "harness": adapter.name,
            "model": model,
            # What the provider was actually TOLD to use, not what the profile
            # asked for -- null means no effort was sent, which is the honest
            # record when a role declares none.
            "effort": effort,
            # Time spent waiting for a concurrency slot. Recorded separately so
            # queueing shows up as queueing rather than silently inflating the
            # job's apparent duration.
            "queued_s": queued_s,
            "dir": ident.realpath,
            "exit": result.returncode,
            # The REAL start, read back from the marker written at job creation.
            # Both of these used to be _now() on adjacent lines, so `started` was
            # the moment the record was built: identical to `finished` in 33 of
            # 33 real records, including jobs that ran for three minutes. Any
            # consumer deriving duration or ordering from the record got zero.
            "started": _started_at_iso(dirs["job"]),
            "finished": _now(),
            "generation": 0,
            "attempt": int((envelope or {}).get("attempt") or 1),
            "integrity": {
                # The verdict over the digests below, so a caller does not have
                # to compare them itself to learn what this job already decided.
                "outcome": integrity_outcome,
                "git_identity_before": before_id,
                "git_identity_after": after_id,
                "tree_before": before_tree,
                "tree_after": after_tree,
                "identity_changed": id_changed,
            },
            "freeze": None,
            "artifacts": {
                "events": ev_path,
                # The public shape. `events` above is the provider's own bytes
                # and is forensic evidence, not a contract.
                "events_normalized": _write_normalized_events(
                    dirs["evidence"], adapter, result.stdout
                ),
                "events_normalized_version": NORMALIZED_EVENTS_VERSION,
                "stderr": err_path,
                "handoff": os.path.join(dirs["evidence"], "handoff.json") if handoff else None,
            },
            "review": None,
            "process": {"pid": result.pid, "timed_out": result.timed_out},
            "security": _security_facts(
                bwrap_argv=bwrap_argv,
                uid_boundary=uid_boundary,
                broker_socket=broker_sock,
                credential_in_sandbox=bool(
                    adapter.credential_in_sandbox and cred is not None and bk is not None
                ),
            ),
            # What this job's worker asked its delegation socket for, including
            # what it was refused. A worker probing its own boundary is a fact
            # about that worker and belongs in the evidence, not just in a log.
            "delegation": delegation_summary,
            "policy_digest": policy.digest,
            "profile_digest": profile_digest,
            "error": err or None,
            "lease_uuid": token_uuid,
        }
        if correlation:
            record["correlation"] = correlation
        if resume_session:
            # A resumed job is a NEW job with its own sandbox, evidence and
            # cost -- but it is not independent history, and a reviewer reading
            # only this record would miss that the model already had context.
            record["resumed"] = {"session": resume_session, "from_job": resumed_from}
        if err:
            record["error"] = err
        else:
            record.pop("error", None)
        if handoff:
            hp = os.path.join(dirs["evidence"], "handoff.json")
            atomic_write_json(hp, handoff)
        if bk is not None:
            # Recorded before the record is persisted; mutating it afterwards
            # left provider_calls null on disk.
            record["provider_calls"] = {
                "forwarded": bk.forwarded,
                # `denied` is POLICY only (path/model/ceiling) -- the security
                # number. `transport` is upstream-unreachable, kept apart so a
                # network blip cannot masquerade as, or hide, a real denial.
                "denied": len(bk.denials),
                "denied_detail": sorted(set(bk.denials))[:10],
                "transport": len(bk.transport_errors),
            }
        # Bind the evidence to the fact it substantiates. Keying this on the
        # projected status let external acceptance assert change.state=frozen
        # while persisting freeze:null -- a claim the record did not substantiate.
        if change_state == jobstate.CHANGE_FROZEN:
            record["freeze"] = {
                "head": after.head,
                "tree_digest": after_tree,
                "policy_digest": policy.digest,
                "profile_digest": profile_digest,
                "independence": (policy.review or {}).get("independence") or {},
                "worktree": identity.identity_core(after),
                "model": model,
                # This job's OWN delta, not the cumulative worktree state.
                "changed_files": identity.delta_paths(
                    before_fp, identity.dirty_fingerprints(ident)
                ),
                # Pin the controller registry that resolved model families, so a
                # later edit cannot relabel two same-family models as independent.
                "models_registry_digest": registry_digest(),
            }
        # What this job actually cost, from the provider's own per-step figures
        # rather than an estimate. It is the only spend number the rail has that
        # is measured, it is what the daily ceiling counts, and it belongs in the
        # persisted record -- computing it after the write would leave the file
        # and the returned record disagreeing.
        try:
            from .adapters import get_adapter, parse_lenient

            parsed, _ = parse_lenient(result.stdout.decode("utf-8", "replace"))
            fin = next(
                (n for n in get_adapter(profile.get("provider")).normalize(
                    parsed, run_ended=True
                 )
                 if n["event"] == "finished"),
                {},
            )
            record["cost_usd"] = float(fin.get("costUSD") or 0.0)
            quotamod.record_spend(root.path, job_id, model["id"], record["cost_usd"])
        except Exception as exc:
            # Accounting must never fail a job that already ran and already cost
            # money: losing the record is bad, losing the work as well is worse.
            #
            # But it must not fail SILENTLY either. spend.jsonl is the only thing
            # assert_within_budget reads, so a swallowed failure here means the
            # daily ceiling quietly stops counting this job — under-counting a
            # money limit, invisibly. Flag it on the record and say so.
            record["spend_unrecorded"] = f"{type(exc).__name__}: {exc}"[:200]
            print(
                f"switchgear: WARNING — this job's cost was NOT recorded to the "
                f"ledger ({type(exc).__name__}). The daily budget is now "
                "under-counting; check the state root is writable.",
                file=sys.stderr,
            )

        # Does the worker's own output contain something that looks like a
        # secret? The rail guards credentials going IN and was indifferent to
        # what comes OUT -- and that output is read by `logs`, quoted into review
        # prompts, and kept indefinitely.
        #
        # FLAG, never destroy: evidence is audit material and a rail that
        # silently rewrites the bytes it recorded is worth less than one that
        # records honestly and points at the problem. Status is untouched.
        try:
            from . import secrets as secretscan

            found = secretscan.scan_files({
                "evidence/events.jsonl": ev_path,
                "evidence/stderr": os.path.join(dirs["evidence"], "stderr"),
            })
            if found:
                record["secrets_suspected"] = found
                print(f"switchgear: WARNING — {secretscan.summarize(found)}",
                      file=sys.stderr)

            from . import injection as injectionmod

            # Advisory HERE, blocking at the promote gate. A scout reading a repo
            # that talks to agents is worth knowing about even when nothing is
            # being promoted.
            addressed = injectionmod.scan(
                open(ev_path, encoding="utf-8", errors="replace").read(2_000_000),
                "evidence/events.jsonl",
            )
            if addressed:
                record["agent_directed_content"] = addressed
                print(f"switchgear: WARNING — {injectionmod.summarize(addressed)}",
                      file=sys.stderr)
        except Exception:
            # Advisory only. A scanner that could fail the job would turn a
            # completed, already-paid-for run into a loss.
            pass

        # strip None error for schema
        if record.get("error") is None:
            record.pop("error", None)
        validate(record, "result.schema.json")
        atomic_write_json(os.path.join(dirs["job"], "result.json"), record)
        _reclaim_sandbox_home(dirs["home"], root.path)
        return record

    def _prepare_session_lineage() -> None:
        # This runs after a write lease is established but before credentials,
        # version probes, or the provider. A refused resume therefore cannot
        # mount, read, or mutate the store it failed to verify.
        if resume_session:
            if legacy_resume:
                sessions.migrate_legacy(
                    root, session_store_id, adapter.name, job_id, ident,
                    legacy_git_identity_after,
                )
            else:
                sessions.verify_lineage(root, session_store_id, adapter.name, ident)
        else:
            sessions.create_lineage(root, session_store_id, adapter.name, job_id, ident)

    try:
        if lock_cm:
            with lock_cm:
                _prepare_session_lineage()
                return _execute_with_broker()
        _prepare_session_lineage()
        return _execute_with_broker()
    finally:
        # The slot goes back however the job ended. A crashed job's marker is
        # also reclaimed by the liveness check, so this is belt and braces
        # rather than the only path.
        concurrency.release(root.path, job_id)


def attach_review(
    *,
    state_path: str,
    subject_job: str,
    reviewer_record: dict[str, Any],
    verdict: str,
    findings: list[dict[str, Any]] | None,
    reviewed_files: list[str] | None = None,
) -> dict[str, Any]:
    root = StateRoot(state_path)
    subject_path = os.path.join(root.job_dir(subject_job), "result.json")
    subject = read_json(subject_path)
    validate(subject, "result.schema.json")
    validate(reviewer_record, "result.schema.json")
    freeze = subject.get("freeze") or {}
    smodel = freeze.get("model") or subject["model"]
    rmodel = reviewer_record["model"]

    # The reviewer must have been a real, clean, read-only inspection of the
    # subject's own worktree. Everything below is attested by the REVIEWER's
    # record; nothing is copied out of the subject's freeze and compared to
    # itself. See docs/REVIEW-6d217a6.md finding N1.
    if reviewer_record["job_id"] == subject_job:
        raise Refuse("a job cannot review itself")
    if reviewer_record.get("mode") != "readonly":
        raise Refuse("reviewer job must be a readonly job")
    if reviewer_record.get("status") != "ok":
        raise Refuse(f"reviewer job did not succeed (status {reviewer_record.get('status')})")
    if os.path.realpath(reviewer_record.get("dir") or "") != os.path.realpath(subject["dir"]):
        raise Refuse("reviewer did not inspect the subject worktree")

    rint = reviewer_record.get("integrity") or {}
    reviewed_before = rint.get("tree_before")
    reviewed_after = rint.get("tree_after")
    if not reviewed_before or not reviewed_after:
        raise Refuse("reviewer record carries no tree evidence")
    if reviewed_before != reviewed_after:
        raise Refuse("tree changed underneath the reviewer")

    registry_now = registry_digest()
    indep = review.independence(smodel, rmodel, subject_job, reviewer_record["job_id"])
    policy_indep = freeze.get("independence") or {}
    unmet = review.unmet_required(policy_indep, indep)
    artifact = {
        "subject_job": subject_job,
        "reviewer_job": reviewer_record["job_id"],
        "model": rmodel,
        "role": reviewer_record["role"],
        "independence": indep,
        "verdict": verdict,
        "findings": findings or [],
        "subject_head": freeze.get("head"),
        "subject_tree_digest": freeze.get("tree_digest"),
        "subject_policy_digest": freeze.get("policy_digest"),
        # What the reviewer actually looked at.
        "reviewed_dir": reviewer_record["dir"],
        "reviewed_tree_digest": reviewed_before,
        "models_registry_digest": registry_now,
        "required_unmet": unmet,
    }
    live = identity.inspect_worktree(subject["dir"])
    # Sample the live tree and commit the promotion under one exclusive hold, so
    # a concurrent bounded-write worker cannot move the tree in between.
    with lease.PromotionLock(root, live):
        live_tree = identity.tree_digest(live)
        artifact["reviewed_files"] = reviewed_files

        # The material the reviewer actually read, recomputed here rather than
        # threaded through the CLI. Safe to recompute because promote refuses if
        # the tree moved since the review, so this IS what was reviewed -- and
        # deriving it from the tree means a caller cannot pass a sanitised copy.
        try:
            # The FULL diff the reviewer was given, not a shorter slice of it.
            # This used to be a bare [:2_000_000] while cmd_review attaches up to
            # identity.MAX_DIFF_BYTES (5MB) to the reviewer -- so content placed
            # past 2MB was read by the reviewer and invisible to this gate. The
            # gate is sold as failing CLOSED; above 2MB it failed open.
            reviewed_content = identity.worktree_diff(live)
        except Exception:
            # Never lose a promotion to the scan's own failure; but an
            # unavailable diff means the scan proves nothing, so say so rather
            # than passing an empty string that reads as "clean".
            reviewed_content = None

        return review.promote(
            reviewed_content=reviewed_content,
            subject_path=subject_path,
            review_artifact=artifact,
            live_head=live.head,
            live_tree_digest=live_tree,
            # The subject's OWN frozen change, not the live cumulative worktree
            # delta: the worktree persists across jobs, so the live set is the
            # union of everything uncommitted and would credit one job with
            # another job's work.
            expected_files=freeze.get("changed_files"),
            generation=int(subject.get("generation") or 0),
        )
