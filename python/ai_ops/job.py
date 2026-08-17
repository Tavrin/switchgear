from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from . import commands as cmdlib
from . import events, identity, lease, process, provider, review, sandbox, state
from .digest import sha256_json
from .errors import ProviderError, Refuse
from .paths import require_disjoint
from .policy import CompiledPolicy, compile_policy
from .profile import load_profile
from .registry import model_record, registry_digest
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
    job_id = new_job_id()
    dirs = state.create_job_dirs(root, job_id)
    lock_cm = None
    token_uuid = None
    if mode == "bounded-write" and policy.require_lease_for_write:
        # Refuse first if no lease exists at all, then require the caller to
        # actually present its token. Reading the token off disk and validating
        # it against itself is not authorization (finding N6).
        lease.load_token(root, ident)
        if not lease_token:
            raise Refuse("bounded-write requires the lease token (--token)")
        token_uuid = lease_token
        lock_cm = lease.WorkerLock(root, ident, token_uuid, job_id, mode)

    def _execute() -> dict[str, Any]:
        runtime = policy.to_opencode_runtime()
        mock_beh = os.environ.get("AI_OPS_MOCK_BEHAVIOR")
        if mock_beh:
            with open(os.path.join(dirs["home"], ".mock-behavior"), "w", encoding="utf-8") as fh:
                fh.write(mock_beh + "\n")
            extra = os.environ.get("AI_OPS_MOCK_EXTRA", "")
            if extra:
                with open(os.path.join(dirs["home"], ".mock-extra"), "w", encoding="utf-8") as fh:
                    fh.write(extra)
        env = provider.isolation_env(dirs["home"], runtime)
        prov_argv, _live = provider.resolve_provider(provider_path)
        agent = "ai-ops-bounded-write" if mode == "bounded-write" else "ai-ops-readonly"
        # Provider inside sandbox: mock gets dir via --dir
        inner = list(prov_argv) + [
            "run",
            "--pure",
            "--dir",
            ident.realpath,
            "--model",
            model["id"],
            "--agent",
            agent,
            "--format",
            "json",
            "--title",
            f"ai-opencode {role} {job_id}",
            prompt,
        ]
        # bind mock script if python
        extra_binds = [p for p in prov_argv if os.path.isabs(p) and os.path.exists(p)]
        if _live:
            # Validate the pinned version by running the provider INSIDE the
            # boundary. Never execute an untrusted provider on the host.
            probe = process.run_sandboxed(
                sandbox.build_bwrap_argv(
                    ident=ident,
                    policy=policy,
                    synth_home=dirs["home"],
                    provider_argv=list(prov_argv) + ["--version"],
                    command_binds=extra_binds,
                ),
                env=env,
                timeout_s=30,
            )
            provider.assert_pinned_version(probe.returncode, probe.stdout, probe.timed_out)
        bwrap_argv = sandbox.build_bwrap_argv(
            ident=ident,
            policy=policy,
            synth_home=dirs["home"],
            provider_argv=inner,
            command_binds=extra_binds,
        )
        before_id = identity.git_identity_digest(ident)
        before_tree = identity.tree_digest(ident)
        timeout = _timeout(policy)
        result = process.run_sandboxed(bwrap_argv, env=env, timeout_s=timeout)
        # process set is the bwrap pid ns; after return it is dead
        identity.assert_gitdir_pointer_intact(ident)
        after = identity.inspect_worktree(ident.realpath)
        if not identity.same_core(ident, after):
            raise Refuse("worktree identity changed during job")
        after_id = identity.git_identity_digest(ident)
        after_tree = identity.tree_digest(ident)
        id_changed = before_id != after_id

        ev_path = os.path.join(dirs["evidence"], "events.jsonl")
        with open(ev_path, "wb") as fh:
            fh.write(result.stdout)
        err_path = os.path.join(dirs["evidence"], "stderr")
        with open(err_path, "wb") as fh:
            fh.write(result.stderr)

        status = "ok"
        err = ""
        handoff = None
        if result.timed_out:
            status = "timeout"
            err = f"timed out after {timeout}s"
        elif id_changed:
            status = "dirty"
            err = "git identity changed"
        else:
            try:
                term = events.parse_event_stream(result.stdout, require_handoff=(mode == "bounded-write"))
                handoff = term.get("_handoff")
            except (ProviderError, Refuse) as exc:
                status = "provider_error"
                err = str(exc)
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
        if err:
            record["error"] = err
        else:
            record.pop("error", None)
        if handoff:
            hp = os.path.join(dirs["evidence"], "handoff.json")
            atomic_write_json(hp, handoff)
        if status == "awaiting_review":
            record["freeze"] = {
                "head": after.head,
                "tree_digest": after_tree,
                "policy_digest": policy.digest,
                "profile_digest": profile_digest,
                "independence": (policy.review or {}).get("independence") or {},
                "worktree": identity.identity_core(after),
                "model": model,
                # Pin the controller registry that resolved model families, so a
                # later edit cannot relabel two same-family models as independent.
                "models_registry_digest": registry_digest(),
            }
        # strip None error for schema
        if record.get("error") is None:
            record.pop("error", None)
        validate(record, "result.schema.json")
        atomic_write_json(os.path.join(dirs["job"], "result.json"), record)
        return record

    if lock_cm:
        with lock_cm:
            return _execute()
    return _execute()


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
            expected_files=identity.changed_files(live),
            generation=int(subject.get("generation") or 0),
        )
