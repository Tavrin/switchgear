from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from . import broker as brokermod
from . import commands as cmdlib
from . import events, identity, lease, process, provider, review, sandbox, state
from .digest import sha256_json
from .errors import ProviderError, Refuse
from .paths import require_disjoint
from .policy import CompiledPolicy, compile_policy
from .profile import load_profile
from .registry import model_record, provider_record as registry_provider, registry_digest, wire_model_names
from .schema import validate
from .state import StateRoot, atomic_write_json, new_job_id, read_json


def _now() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


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


MIN_FREE_BYTES = int(os.environ.get("AI_OPS_MIN_FREE_BYTES") or 2 * 1024 * 1024 * 1024)


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
            "(set AI_OPS_MIN_FREE_BYTES to override)"
        )


def _reclaim_sandbox_home(home: str) -> None:
    """Delete the per-job synthetic HOME once the record is written.

    A live provider populates it with ~150MB of npm cache and node_modules per
    job; it is never reused and nothing else ever reads it, so retaining it grew
    the state store without bound. Evidence and result.json are kept.
    """
    import shutil

    if os.environ.get("AI_OPS_KEEP_SANDBOX_HOME") == "1":
        return
    shutil.rmtree(home, ignore_errors=True)


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
        if os.environ.get("AI_OPS_WRITE") != "1":
            raise Refuse("write is disabled (AI_OPS_WRITE is not 1)")
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

    # Before any work, and before any job directory exists: a budget checked
    # after the spend is an audit, not a control.
    quotamod.assert_within_budget(root.path)
    job_id = job_id or new_job_id()
    dirs = state.create_job_dirs(root, job_id)
    # Liveness record for EVERY job, not just backgrounded ones. Without it a
    # FOREGROUND job whose process died left a directory with no result.json,
    # and `status` reported "running" forever -- measured on a job abandoned five
    # hours earlier. pid alone is not identity (pids are recycled), hence
    # starttime and boot_id, the same triple the lease uses.
    try:
        atomic_write_json(
            os.path.join(dirs["job"], "runner.json"),
            {
                "pid": os.getpid(),
                "starttime": lease._starttime(os.getpid()),
                "boot_id": lease._boot_id(),
            },
        )
    except Exception:
        # Never fail a job because its liveness marker could not be written; the
        # status command degrades to "unknown" rather than lying.
        pass
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
        if os.environ.get("AI_OPS_ALLOW_LIVE_PROVIDER") == "1":
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
            upstream = os.environ.get("AI_OPS_PROVIDER_UPSTREAM") or prec["upstream"]
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
        mock_beh = os.environ.get("AI_OPS_MOCK_BEHAVIOR")
        if mock_beh:
            with open(os.path.join(dirs["home"], ".mock-behavior"), "w", encoding="utf-8") as fh:
                fh.write(mock_beh + "\n")
            extra = os.environ.get("AI_OPS_MOCK_EXTRA", "")
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
            attach_dir = os.path.join(dirs["home"], "ai-ops")
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
        if getattr(adapter, "credential_in_sandbox", False) and cred is not None and bk is not None:
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
                "(`ai-opencode providers` prints the path)."
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
        )
        # bind mock script if python
        extra_binds = [p for p in prov_argv if os.path.isabs(p) and os.path.exists(p)]
        # A provider may need more than its own executable (Codex ships helper
        # binaries beside it). Read-only, and only what the adapter names.
        extra_binds += [
            b for b in getattr(adapter, "extra_binds", lambda _a: [])(list(prov_argv))
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
        # Durable conversation state, per worktree per provider, so a later
        # `resume` can continue this session. It lives OUTSIDE the job directory
        # precisely because the job's sandbox home is reclaimed when the job
        # ends -- which is why the first resume attempt failed with "No
        # conversation found with session ID".
        session_binds = []
        for rel in getattr(adapter, "session_store_paths", lambda: [])():
            src = os.path.join(
                root.path, "sessions", lease.identity_key(ident), adapter.name, rel
            )
            os.makedirs(src, mode=0o700, exist_ok=True)
            _assert_no_credentials(src)
            session_binds.append((src, os.path.join(dirs["home"], rel)))

        bwrap_argv = sandbox.build_bwrap_argv(
            ident=ident,
            policy=policy,
            synth_home=dirs["home"],
            provider_argv=inner_prefix + inner,
            command_binds=extra_binds,
            broker_socket=broker_sock,
            session_binds=session_binds,
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
        result = process.run_sandboxed(
            bwrap_argv,
            env=env,
            timeout_s=timeout,
            stdout_path=ev_path,
            stderr_path=err_path,
        )
        # process set is the bwrap pid ns; after return it is dead

        identity.assert_gitdir_pointer_intact(ident)
        after = identity.inspect_worktree(ident.realpath)
        if not identity.same_core(ident, after):
            raise Refuse("worktree identity changed during job")
        after_id = identity.git_identity_digest(ident)
        after_tree = identity.tree_digest(ident)
        id_changed = before_id != after_id

        status = "ok"
        err = ""
        handoff = None
        if result.truncated:
            status = "provider_error"
            err = "provider output exceeded the capture bound (evidence truncated)"
        elif result.timed_out:
            status = "timeout"
            err = f"timed out after {timeout}s"
        elif id_changed:
            status = "dirty"
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
                status = "provider_error"
                err = verr
            if status == "ok" and result.returncode not in (0, None):
                # A well-formed handoff object is a claim by the provider, not
                # evidence of success. A crashed write is never promotable.
                status = "provider_error"
                err = f"provider exited {result.returncode}"

        if mode == "bounded-write" and status == "ok":
            # post-write commands inside the same sandbox
            for item in (envelope or {}).get("commands") or []:
                argv = cmdlib.resolve_command(item["verb"], item.get("args") or [])
                cmd_bwrap = sandbox.build_bwrap_argv(
                    ident=ident,
                    policy=policy,
                    synth_home=dirs["home"],
                    provider_argv=argv,
                    command_binds=extra_binds,
                )
                cr = process.run_sandboxed(cmd_bwrap, env=env, timeout_s=min(30, timeout))
                if cr.returncode != 0 or cr.timed_out:
                    status = "provider_error"
                    err = "post-write command failed"
                    break
            identity.assert_gitdir_pointer_intact(ident)
            after2 = identity.inspect_worktree(ident.realpath)
            if not identity.same_core(ident, after2):
                status = "dirty"
                err = "identity changed after commands"
            after = after2
            after_id = identity.git_identity_digest(ident)
            after_tree = identity.tree_digest(ident)
            if after_id != before_id:
                status = "dirty"
                err = "git identity changed"
            if status == "ok":
                status = "awaiting_review"

        record = {
            "job_id": job_id,
            "status": status,
            "mode": mode,
            "role": role,
            "model": model,
            "dir": ident.realpath,
            "exit": result.returncode,
            "started": _now(),
            "finished": _now(),
            "generation": 0,
            "attempt": int((envelope or {}).get("attempt") or 1),
            "integrity": {
                "git_identity_before": before_id,
                "git_identity_after": after_id,
                "tree_before": before_tree,
                "tree_after": after_tree,
                "identity_changed": id_changed,
            },
            "freeze": None,
            "artifacts": {
                "events": ev_path,
                "stderr": err_path,
                "handoff": os.path.join(dirs["evidence"], "handoff.json") if handoff else None,
            },
            "review": None,
            "process": {"pid": result.pid, "timed_out": result.timed_out},
            "policy_digest": policy.digest,
            "profile_digest": profile_digest,
            "error": err or None,
            "lease_uuid": token_uuid,
        }
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
        if status == "awaiting_review":
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
        except Exception:
            # Accounting must never fail a job that already ran and already cost
            # money: losing the record is bad, losing the work as well is worse.
            pass

        # strip None error for schema
        if record.get("error") is None:
            record.pop("error", None)
        validate(record, "result.schema.json")
        atomic_write_json(os.path.join(dirs["job"], "result.json"), record)
        _reclaim_sandbox_home(dirs["home"])
        return record

    if lock_cm:
        with lock_cm:
            return _execute_with_broker()
    return _execute_with_broker()


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
        return review.promote(
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
