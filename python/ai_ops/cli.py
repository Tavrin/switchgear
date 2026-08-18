from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

from . import identity, job, jobstate, lease, state
from .errors import RailError, Refuse
from .profile import load_profile
from .provider import resolve_provider
from .registry import model_record
from .schema import validate
from .state import StateRoot, atomic_write_json, read_json


def _die(msg: str, code: int = 1) -> None:
    print(f"ai-opencode: REFUSING — {msg}", file=sys.stderr)
    raise SystemExit(code)


def _state_path(ns: argparse.Namespace) -> str:
    p = ns.state or os.environ.get("AI_OPS_STATE")
    if not p:
        _die("state root required (--state or AI_OPS_STATE); provision it first")
    return os.path.abspath(p)


def _profile_path(ns: argparse.Namespace) -> str:
    if ns.profile:
        return os.path.abspath(ns.profile)
    envp = os.environ.get("AI_OPS_PROFILE")
    if envp:
        return os.path.abspath(envp)
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "project-profiles", "example.json"))
    return here


def _print_job(record: dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        # Stable machine-readable contract for programmatic callers (atelier
        # adapters, MCP wrappers, CI). Keep these keys additive-only.
        print(json.dumps({
            "job_id": record["job_id"],
            "status": record["status"],
            "mode": record["mode"],
            "role": record["role"],
            "model": record["model"]["id"],
            "dir": record["dir"],
            "exit": record.get("exit"),
            "error": record.get("error"),
            "artifacts": record.get("artifacts") or {},
            "freeze": record.get("freeze"),
            "review": record.get("review"),
            "provider_calls": record.get("provider_calls"),
            # Measured, from the provider's own per-step figures. A caller
            # deciding whether to keep delegating needs the real number, and it
            # is already in the persisted record.
            "cost_usd": record.get("cost_usd"),
            # Provenance for a continued session: a caller reading only this
            # record would otherwise not know the model already had context.
            "resumed": record.get("resumed"),
        }, indent=2))
        return
    print(f"model={record['model']['id']}")
    print(f"dir={record['dir']}")
    print(f"exit={record.get('exit')}")
    arts = record.get("artifacts") or {}
    print(f"result={arts.get('events')}")
    print(f"meta={record.get('job_id')}")
    print(f"job={record['job_id']}")


def _emit(payload: dict, ns, text=None) -> None:
    """Print a result honouring --json, so every command answers the same way.

    Added because the flag was handled ad hoc: `state`/`lease` never checked it,
    while `cancel`/`promote` ignored it and printed JSON unconditionally. A tool
    driven by agents cannot have per-command output conventions.
    """
    if getattr(ns, "json", False):
        print(json.dumps(payload, indent=2))
        return
    if text is not None:
        print(text(payload))
        return
    for key, value in payload.items():
        print(f"{key}={value}")


def _persist_review_report(
    state_path: str, job_id: str, report: dict, review_of: str | None = None
) -> None:
    """Write the reviewer's verdict onto its own result.json, durably.

    Read-modify-write under the same atomic writer the record uses. Kept
    non-fatal-adjacent: the caller decides the exit code; this just makes sure
    the outcome outlives the sandbox home.

    `review_of` records WHICH subject was reviewed, on the reviewer's own record,
    unconditionally -- before promotion is even attempted. Without it there is no
    durable link from a review back to its subject unless promotion SUCCEEDED
    (only the subject gets `review.reviewer_job`), so a review whose promotion
    has not happened yet is indistinguishable on disk from a free-standing one.
    Retention cannot protect an unpromoted chain it cannot see.
    """
    path = os.path.join(StateRoot(state_path).job_dir(job_id), "result.json")
    rec = read_json(path)
    rec["review"] = report
    rec["review_of"] = review_of
    validate(rec, "result.schema.json")
    atomic_write_json(path, rec)


def cmd_state(ns: argparse.Namespace) -> int:
    if ns.action == "provision":
        path = state.provision(os.path.abspath(ns.dir))
        _emit({"state": path}, ns, text=lambda d: f"state={d['state']}")
        return 0
    # Unreachable: argparse `choices` rejects anything else first. Kept as
    # defence in depth, but naming the valid actions so it is still actionable
    # if it ever does fire.
    _die("unknown state action (valid: provision)")
    return 1


def _reachability(provider_id: str, _cache: dict[str, str] = {}) -> str:
    """Whether a provider's credential is actually installed on this machine.

    The registry is a catalogue of what the rail KNOWS, which is not the same as
    what it can REACH -- ids can be listed for a provider whose key was never
    installed here. Reporting that at listing time beats discovering it at job
    time, and keeps the registry honest without pruning entries that are correct
    but unprovisioned.
    """
    if provider_id in _cache:
        return _cache[provider_id]
    from . import provider as provmod
    from .registry import provider_record

    try:
        prec = provider_record(provider_id)
        name = prec.get("credential") or provider_id
        if provmod.load_provider_credential(name):
            verdict = "reachable"
        else:
            # Name where a credential SHOULD go, not where lookup ended up:
            # credential_path() falls back to the legacy single-file path when
            # the per-provider file is absent, which is exactly the case here,
            # so reporting it would tell the operator to install the key in the
            # one place that cannot hold a second provider.
            want = os.environ.get("AI_OPS_PROVIDER_CREDENTIAL_FILE") or os.path.join(
                provmod.CREDENTIAL_DIR, name
            )
            verdict = f"UNREACHABLE (install a credential at {want}, mode 600)"
    except Refuse as exc:
        verdict = f"UNREACHABLE ({exc})"
    _cache[provider_id] = verdict
    return verdict


def cmd_models(ns: argparse.Namespace) -> int:
    """What this profile may use, and -- with --live -- what actually exists.

    The registry curates identity (family/vendor), never availability. Model
    versions churn weekly, so asking the provider is the only way to know what
    can be called today; a hand-maintained list is stale the day after it is
    written.
    """
    profile = load_profile(_profile_path(ns))
    allow = (profile.get("models") or {}).get("allow") or []

    if getattr(ns, "live", False):
        import subprocess

        from .adapters import get_adapter
        from .compat import PINNED_PROVIDERS
        from .sandbox import build_credential_refresh_argv

        rows = []
        for pname, prec in sorted(PINNED_PROVIDERS.items()):
            adapter = get_adapter(pname)
            binary = prec.get("path")
            if not binary or not os.path.exists(binary):
                continue
            argv = adapter.list_models_argv([binary])
            if not argv:
                rows.append({"provider": pname, "listable": False,
                             "note": "this CLI offers no model-list command"})
                continue
            import tempfile

            home = tempfile.mkdtemp(prefix="aiops-models-")
            auth = os.path.expanduser(f"~/.{pname}")
            try:
                full = build_credential_refresh_argv(
                    auth_dir=auth if os.path.isdir(auth) else home,
                    synth_home=home, provider_argv=argv,
                )
                out = subprocess.run(
                    full, env={"PATH": "/usr/bin:/bin", "HOME": home,
                               "LANG": "C.UTF-8", "TERM": "dumb"},
                    capture_output=True, text=True, timeout=60,
                    stdin=subprocess.DEVNULL,
                ).stdout or ""
            except Exception as exc:
                rows.append({"provider": pname, "listable": True,
                             "error": type(exc).__name__})
                continue
            found = []
            for line in out.splitlines():
                tok = line.strip().lstrip("*-").strip().split()[0] if line.strip() else ""
                if "/" in tok or (tok and pname == "grok" and tok.startswith("grok")):
                    found.append(tok if "/" in tok else f"{pname}/{tok}")
            detail = []
            for m in sorted(set(found)):
                try:
                    rec = model_record(m)
                    detail.append({"id": m, "model_family": rec["model_family"],
                                   "vendor_family": rec["vendor_family"],
                                   "identity_source": rec["identity_source"],
                                   "in_profile_allowlist": m in allow})
                except Refuse as exc:
                    detail.append({"id": m, "usable": False, "reason": str(exc)})
            rows.append({"provider": pname, "listable": True, "models": detail})
        note = (
            "discovery runs credential-free inside the sandbox, so pools that need "
            "a key to enumerate (e.g. opencode-go) may be under-reported; the rail "
            "keeps the credential controller-side by design"
        )
        if ns.json:
            print(json.dumps({"note": note, "providers": rows}, indent=2))
        else:
            for r in rows:
                if not r.get("listable"):
                    print(f"{r['provider']}: {r.get('note')}")
                    continue
                models = r.get("models") or []
                print(f"{r['provider']}: {len(models)} models available")
                for entry in models:
                    if not entry.get("usable", True):
                        ident = f"UNUSABLE ({entry.get('reason')})"
                    else:
                        ident = (f"family={entry['model_family']:9} "
                                 f"vendor={entry['vendor_family']:10} "
                                 f"{entry['identity_source']}")
                    mark = "" if entry.get("in_profile_allowlist") else "  (not in allowlist)"
                    print(f"  {entry['id']:34} {ident}{mark}")
            print(f"\nnote: {note}")
        return 0

    # The default path. It ignored --json entirely, in the one command an
    # orchestrator is most likely to call programmatically.
    from . import health as healthmod

    try:
        seen = healthmod.observe(_state_path(ns))
        by_model = {m["model"]: m for m in seen["models"]}
    except Exception:
        # Health is an observation over past jobs. A state root that does not
        # exist yet is not a reason to refuse to list models.
        by_model = {}

    entries = []
    for m in allow:
        rec = model_record(m)
        entry = {
            "id": m,
            "model_family": rec.get("model_family"),
            "vendor_family": rec.get("vendor_family"),
            "identity_source": rec.get("identity_source"),
            "reachability": _reachability(rec.get("provider") or ""),
        }
        obs = by_model.get(m)
        if obs:
            # REPORTED, never enforced: an agent testing a fix for a failing
            # model must not be refused from testing its own fix.
            entry["health"] = {
                "ok": obs["ok"], "failed": obs["failed"],
                "failure_ratio": obs["failure_ratio"],
                "unhealthy": obs["unhealthy"],
                "last_failure": obs["last_failure"],
            }
        entries.append(entry)

    if ns.json:
        print(json.dumps({"allow": entries}, indent=2))
        return 0

    print("profile allowlist:")
    for entry in entries:
        print(f"  {entry['id']}  family={entry['model_family']}  "
              f"vendor={entry['vendor_family']}  [{entry['identity_source']}]  "
              f"{entry['reachability']}")
        h = entry.get("health")
        if h and h["unhealthy"]:
            print(f"      WARN recent jobs: {h['failed']} failed / "
                  f"{h['ok'] + h['failed']} (last: {h['last_failure']}) — reported, not enforced")
    return 0


def cmd_lease(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    ident = identity.inspect_worktree(os.path.abspath(ns.dir))
    if ns.action == "acquire":
        owner = ns.owner or "controller"
        pid = int(ns.owner_pid or os.getppid())
        tok = lease.acquire(root, ident, owner, pid, ns.mode or "bounded-write")
        _emit(
            {
                "lease": tok["lease_uuid"],
                "file": os.path.join(root.leases, lease.identity_key(ident), "token.json"),
            },
            ns,
        )
        return 0
    if ns.action == "release":
        if not ns.token:
            _die("release requires --token (the uuid printed by `lease acquire`)")
        lease.release(root, ident, ns.token, ns.owner or "controller")
        _emit({"released": True, "dir": ident.realpath}, ns,
              text=lambda _d: "released")
        return 0
    if ns.action == "show":
        print(json.dumps(lease.load_token(root, ident), indent=2))
        return 0
    _die("unknown lease action (valid: acquire, release, show)")
    return 1


# --- background jobs ----------------------------------------------------------
#
# agent-ops blocked for the whole job, so every long run had to be hand-
# backgrounded by its caller. A launch returns a job id immediately; the caller
# then polls `status` and reads `logs --format digest` only if something looks
# wrong. That is the same detached-job shape atelier's codex lane already
# expects, and unlike a stdout pipe it survives the caller going away.


def _launch_dir(state_path: str) -> str:
    d = os.path.join(state_path, "launch")
    os.makedirs(d, exist_ok=True)
    return d


def launch_background(ns: argparse.Namespace) -> int:
    """Re-exec this CLI detached, and hand back the job id at once."""
    from .state import new_job_id

    state_path = _state_path(ns)
    job_id = new_job_id()
    ldir = _launch_dir(state_path)
    out_path = os.path.join(ldir, f"{job_id}.out")
    err_path = os.path.join(ldir, f"{job_id}.err")

    # Drop --background from the child's argv or it would launch forever.
    argv = [a for a in sys.argv[1:] if a != "--background"]
    child_env = dict(os.environ)
    child_env["AI_OPS_JOB_ID"] = job_id
    # The child re-execs this CLI without --background, so it cannot otherwise
    # tell it was launched detached. That distinction decides whether a full
    # concurrency queue refuses immediately (foreground: a caller at a terminal
    # wants to be told, not stalled) or waits for a slot.
    child_env["AI_OPS_BACKGROUND_CHILD"] = "1"

    with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, "-s", os.path.join(os.path.dirname(__file__), "__main__.py"), *argv],
            stdout=out_fh,
            stderr=err_fh,
            env=child_env,
            # Its own session: the job outlives this process, which is the whole
            # point, and it also keeps the worker's process group separate so a
            # cancel kills the job and not the caller.
            start_new_session=True,
            close_fds=True,
        )
    from .lease import _boot_id, _starttime

    meta = {
        "job_id": job_id,
        "pid": proc.pid,
        "starttime": _starttime(proc.pid),
        "boot_id": _boot_id(),
    }
    atomic_write_json(os.path.join(ldir, f"{job_id}.json"), meta)

    info = {
        "job_id": job_id,
        "state": "launched",
        "pid": proc.pid,
        "events": os.path.join(state_path, "jobs", job_id, "evidence", "events.jsonl"),
        "launch_stderr": err_path,
    }
    print(json.dumps(info, indent=2) if getattr(ns, "json", False)
          else "\n".join(f"{k}={v}" for k, v in info.items()))
    return 0


def cmd_resume(ns: argparse.Namespace) -> int:
    """Continue a finished job's provider session with a new message.

    This is the steering primitive, and every provider implements it natively
    (codex `exec resume`, claude `--resume`, grok `--resume`, opencode
    `run --session`). It is deliberately NOT a live channel into a running
    sandbox: the resumed turn is a NEW bounded job with its own boundary,
    evidence and cost, which keeps the freeze/review chain reasoning about a
    complete record instead of one shaped by inputs it never saw.

    The message is delivered as the job's prompt, so the role instructions --
    and therefore the rail's authority over what the worker may do -- are
    reapplied exactly as on a first run.
    """
    if getattr(ns, "background", False):
        return launch_background(ns)
    from .adapters import get_adapter, parse_lenient

    root = StateRoot(_state_path(ns))
    prior = read_json(os.path.join(root.job_dir(ns.job), "result.json"))
    profile = load_profile(_profile_path(ns))
    adapter = get_adapter(profile.get("provider"))

    events_path = (prior.get("artifacts") or {}).get("events")
    session = None
    if events_path and os.path.isfile(events_path):
        with open(events_path, encoding="utf-8", errors="replace") as fh:
            parsed, _ = parse_lenient(fh.read())
        session = adapter.session_id(parsed)
    if not adapter.session_store_paths():
        _die(
            f"resume is not supported for provider {adapter.name!r}: its "
            "conversation-store location has not been measured, and resuming "
            "without it would start a FRESH conversation wearing the previous "
            "session's id -- a continuation in name only."
        )
    if not session:
        # Honest and specific: for Grok the id exists only in the terminal event,
        # so a job that died mid-run genuinely has nothing to resume from.
        _die(
            f"job {ns.job} has no resumable session id in its evidence "
            f"(provider {adapter.name}). A job that failed before its session id "
            "was emitted cannot be resumed; start a new job instead."
        )

    rec = job.run_job(
        profile_path=_profile_path(ns),
        state_path=_state_path(ns),
        mode=prior["mode"],
        role=prior["role"],
        worktree=prior["dir"],
        prompt=ns.message,
        provider_path=ns.provider or os.environ.get("AI_OPS_PROVIDER") or "",
        lease_token=getattr(ns, "token", None),
        job_id=os.environ.get("AI_OPS_JOB_ID") or None,
        resume_session=session,
        resumed_from=ns.job,
    )
    _print_job(rec, getattr(ns, "json", False))
    return jobstate.exit_code_for(rec["status"])


def cmd_cancel(ns: argparse.Namespace) -> int:
    state_path = _state_path(ns)
    meta_path = os.path.join(_launch_dir(state_path), f"{ns.job}.json")
    if not os.path.isfile(meta_path):
        _die(f"no background launch record for {ns.job}")
    meta = read_json(meta_path)
    from .lease import _alive

    pid = int(meta["pid"])
    # pid + starttime + boot_id, not pid alone: pids are recycled, and killing
    # whatever now holds a recorded pid is how a cancel becomes an outage.
    if not _alive(pid, meta.get("starttime", ""), meta.get("boot_id", "")):
        _emit({"job": ns.job, "state": "not_running"}, ns)
        return 0

    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError as exc:
        _die(f"cancel failed: {exc}")
    deadline = time.time() + 5
    while time.time() < deadline and _alive(pid, meta.get("starttime", ""), meta.get("boot_id", "")):
        time.sleep(0.1)
    if _alive(pid, meta.get("starttime", ""), meta.get("boot_id", "")):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    meta["cancelled"] = True
    atomic_write_json(meta_path, meta)
    _emit({"job": ns.job, "state": "cancelled", "pid": pid}, ns)
    return 0


def cmd_run_like(ns: argparse.Namespace, mode: str, role: str, directory: str, prompt: str, envelope: dict[str, Any] | None) -> int:
    if getattr(ns, "background", False):
        return launch_background(ns)
    rec = job.run_job(
        profile_path=_profile_path(ns),
        state_path=_state_path(ns),
        mode=mode,
        role=role,
        worktree=os.path.abspath(directory),
        prompt=prompt,
        provider_path=ns.provider or os.environ.get("AI_OPS_PROVIDER") or "",
        envelope=envelope,
        lease_token=getattr(ns, "token", None),
        job_id=os.environ.get("AI_OPS_JOB_ID") or None,
    )
    _print_job(rec, getattr(ns, "json", False))
    return jobstate.exit_code_for(rec["status"])


def cmd_scout(ns: argparse.Namespace) -> int:
    return cmd_run_like(ns, "readonly", "scout", ns.dir, ns.prompt, None)


def _tracked_paths(ident) -> list[str]:
    """Paths git already tracks, so new files can be told apart from edits."""
    from .identity import _git_pinned

    try:
        out = _git_pinned(ident, "ls-files")
    except Exception:
        return []
    return [ln for ln in out.splitlines() if ln]


def cmd_review(ns: argparse.Namespace) -> int:
    if getattr(ns, "background", False):
        return launch_background(ns)
    env = None
    prompt = ns.prompt
    if ns.envelope:
        env = json.loads(open(ns.envelope, encoding="utf-8").read())
        validate(env, "task-envelope.schema.json")
        prompt = env.get("goal") or prompt
    # Give the reviewer the controller's frozen diff. It has no shell and no git,
    # so without this it cannot see the change at all -- a live reviewer looped
    # 35 times and timed out trying.
    review_dir = os.path.abspath(ns.dir)
    try:
        ident = identity.inspect_worktree(review_dir)
        diff = identity.worktree_diff(ident)
        changed, ignored = identity.review_manifest(ident)
        # `git diff HEAD` is tracked-only, so a NEW file shows up in the changed
        # list with no content behind it. A live reviewer caught exactly that on
        # a real repo -- and for bounded-write it is the common case, since a
        # worker cannot stage and everything it creates is untracked. Append
        # added-file hunks so the reviewer can actually see what was written.
        tracked = set(_tracked_paths(ident))
        new_files = [p for p in changed if p not in tracked]
        if new_files:
            extra = identity.untracked_diff(ident, new_files)
            if extra.strip():
                diff = (diff + "\n" if diff else "") + extra
    except Refuse:
        diff, changed, ignored = "", [], []
    attachments = None
    if diff or changed:
        # The diff is ATTACHED (a file the reviewer reads), never inlined into
        # argv -- one argv element is capped at 128KiB (MAX_ARG_STRLEN).
        #
        # Only the ACTUAL change is listed: tracked-modified plus new non-ignored
        # untracked files. Ambient ignored files (.venv, .idea, __pycache__) are
        # reported as a COUNT with a small sample, never dumped -- dumping all of
        # them put 42,205 paths / 3.7MB in front of a reviewer that then burned
        # its whole timeout on noise. The integrity digest still covers them; the
        # reviewer just is not asked to read the environment.
        CHANGED_LIST_CAP = 500
        listed = changed[:CHANGED_LIST_CAP]
        header_lines = [f"# Changed files ({len(changed)} tracked + new source)"]
        header_lines += [f"#   {f}" for f in listed]
        if len(changed) > len(listed):
            header_lines.append(f"#   [+{len(changed) - len(listed)} more]")
        if ignored:
            sample = ", ".join(ignored[:15])
            header_lines.append(
                f"# ({len(ignored)} ignored/ambient files NOT shown: {sample}"
                + (", ..." if len(ignored) > 15 else "") + ")"
            )
        header = "\n".join(header_lines) + "\n#\n# Uncommitted diff (git diff HEAD) follows.\n\n"
        attachments = {"review-diff.patch": header + diff}

        # Inline a BOUNDED slice of the diff in the prompt as well as attaching
        # the whole thing. Two failures make this the right shape:
        #   - unbounded inlining blew the kernel's 128KiB single-argument limit
        #     and killed every review of a real repo (E2BIG);
        #   - attachment-only depends on the provider being able to READ a file
        #     outside its working directory, and a live Claude review answered
        #     "I don't have permission to read the attached diff file".
        # So the common case travels in the prompt, where no permission applies,
        # and the attachment carries the remainder for providers that can read it.
        INLINE_CAP = 60_000
        inline = diff[:INLINE_CAP]
        overflow = len(diff) > INLINE_CAP
        note = ""
        if "[diff truncated]" in diff:
            note = (
                "\n\nWARNING: the diff was TRUNCATED at the controller's cap. You are\n"
                "seeing part of the change. Say so in your findings and do not return\n"
                "'promote' on the strength of a partial diff."
            )
        shown = ", ".join(changed[:40]) or "(no tracked change)"
        more = len(changed) - min(len(changed), 40)
        tail = ""
        if overflow:
            tail = (
                f"\n\nNOTE: this is the first {INLINE_CAP} bytes of a {len(diff)}-byte diff."
                " The COMPLETE diff is in the attached file named below; read it before"
                " judging, and if you cannot, say so and do not return 'promote'."
            )
        prompt = (
            f"{prompt or 'Review this change.'}\n\n"
            f"Changed files ({len(changed)}): {shown}"
            + (f"  [+{more} more]" if more > 0 else "") + note
            + f"\n\nThe uncommitted diff follows.{tail}\n\n```diff\n{inline}\n```"
        )
    rec = job.run_job(
        profile_path=_profile_path(ns),
        state_path=_state_path(ns),
        mode="readonly",
        role=ns.role,
        worktree=review_dir,
        prompt=prompt or "review",
        provider_path=ns.provider or os.environ.get("AI_OPS_PROVIDER") or "",
        envelope=env,
        job_id=os.environ.get("AI_OPS_JOB_ID") or None,
        attachments=attachments,
    )
    parent = (env or {}).get("parent_job")
    if rec["status"] == "ok":
        # Persist the reviewer's OWN verdict onto its OWN record, ALWAYS -- not
        # only when a parent is being promoted. Bug #3: a standalone review left
        # review:null on disk, so the report existed only inside events.jsonl and
        # the human-readable outcome was lost once the sandbox home was reclaimed
        # (the workaround was AI_OPS_KEEP_SANDBOX_HOME). The verdict belongs on
        # the record so `status <job> --full` shows it.
        from .adapters import get_adapter

        ev = open(rec["artifacts"]["events"], "rb").read()
        try:
            # Through the adapter: only OpenCode emits structured review objects.
            # The strict OpenCode parser reported "no terminal provider event" for
            # a perfectly good Claude review -- the same provider-blindness that
            # was fixed for job results, in two places it had been missed.
            verdict, findings, reviewed_files = get_adapter(
                load_profile(_profile_path(ns)).get("provider")
            ).extract_review(ev)
        except Exception as exc:
            # A review that produced no parseable verdict is a real failure, but
            # the record must SAY so rather than silently reading null.
            report = {"verdict": None, "error": str(exc)}
            _persist_review_report(_state_path(ns), rec["job_id"], report, parent)
            print(f"ai-opencode: reviewer produced no verdict: {exc}", file=sys.stderr)
            return 1
        report = {"verdict": verdict, "findings": findings, "reviewed_files": reviewed_files}
        _persist_review_report(_state_path(ns), rec["job_id"], report, parent)
        rec["review"] = report
        if parent:
            job.attach_review(
                state_path=_state_path(ns),
                subject_job=parent,
                reviewer_record=rec,
                verdict=verdict,
                findings=findings,
                reviewed_files=reviewed_files,
            )
    # Printed AFTER the verdict is attached: printing first reported
    # "review": null for a review that had in fact produced one, so the caller's
    # JSON disagreed with the record on disk.
    _print_job(rec, getattr(ns, "json", False))
    # One shared mapping: this used to omit timeout->124, so `rc == 124` meant
    # something different for `review` than for `scout`.
    return jobstate.exit_code_for(rec["status"])


def cmd_write(ns: argparse.Namespace) -> int:
    env = json.loads(open(ns.envelope, encoding="utf-8").read())
    validate(env, "task-envelope.schema.json")
    if env.get("mode") != "bounded-write":
        _die("envelope mode must be bounded-write")
    if env.get("role") != ns.role:
        _die("envelope role mismatch")
    return cmd_run_like(ns, "bounded-write", ns.role, ns.dir, env.get("goal") or "write", env)


def cmd_run(ns: argparse.Namespace) -> int:
    env = json.loads(open(ns.envelope, encoding="utf-8").read())
    validate(env, "task-envelope.schema.json")
    mode = env["mode"]
    if mode == "bounded-write":
        ns.role = env["role"]
        ns.dir = env["cwd"]
        ns.envelope = ns.envelope
        return cmd_write(ns)
    return cmd_run_like(ns, mode, env["role"], env["cwd"], env.get("goal") or "", env)


# --- projections over the one stream ------------------------------------------
#
# evidence/events.jsonl is the single record; everything below is a view of it.
# The cheap views are the defaults, because the expensive one is unbounded and a
# delegating agent that reads it once has flooded its own context. A convention
# saying "please don't" would be violated, so the bounds are in the code.


def _job_paths(ns) -> tuple[str, str, str]:
    root = StateRoot(_state_path(ns))
    jd = root.job_dir(ns.job)
    return jd, os.path.join(jd, "evidence", "events.jsonl"), os.path.join(jd, "result.json")


def _projection(ns) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(record-or-{}, normalized events) -- works on a RUNNING job.

    Reads whatever the stream holds right now. result.json does not exist until
    the job finishes, so anything that insists on it cannot answer the question
    a poller is actually asking.
    """
    from .adapters import get_adapter, parse_lenient

    jd, ev_path, res_path = _job_paths(ns)
    rec: dict[str, Any] = {}
    if os.path.isfile(res_path):
        try:
            rec = read_json(res_path)
        except Exception:
            rec = {}
    raw = ""
    if os.path.isfile(ev_path):
        with open(ev_path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    # Resolve the adapter from what the JOB recorded, never from the ambient
    # profile if the job knows better.
    #
    # This was a real, silent wrong answer: reading a Claude job's logs with the
    # default profile normalized the stream with the OpenCode adapter, recognised
    # nothing, and reported `status: failed, turns: 0, "stream truncated ... not
    # evidence of completion"` for a job that had completed fine. A false failure
    # report, produced confidently. The job's own provider is a fact; the
    # caller's profile is a guess about it.
    provider = rec.get("provider")
    if not provider:
        # A running job has no result.json yet, which is exactly when logs and
        # status are used most -- so the runner record carries it too.
        try:
            provider = read_json(os.path.join(jd, "runner.json")).get("provider")
        except Exception:
            provider = None
    if not provider and getattr(ns, "profile", None):
        provider = (load_profile(_profile_path(ns)) or {}).get("provider")
    if not provider:
        _die(
            f"cannot tell which provider produced job {getattr(ns, 'job', '?')}: "
            "its record predates provider stamping and no --profile was given. "
            "Pass --profile with the profile the job ran under. Guessing would "
            "silently misread the stream and report a completed job as failed."
        )

    adapter = get_adapter(provider)
    parsed, _ = parse_lenient(raw)
    normalized = adapter.normalize(parsed, run_ended=bool(rec))

    # A non-empty stream that yields nothing recognisable is the signature of the
    # wrong adapter, not of a truncated run. Say so rather than reporting a
    # confident falsehood.
    if raw.strip() and not [n for n in normalized if n["event"] != "finished"]:
        print(
            f"ai-opencode: WARNING — the {provider!r} adapter recognised nothing in "
            f"{len(raw)} bytes of evidence. That usually means the stream was "
            "produced by a different provider; check --profile.",
            file=sys.stderr,
        )
    return rec, normalized


def cmd_providers(ns: argparse.Namespace) -> int:
    """List provider binaries, or verify a new build against its adapter.

    Codex, Claude Code and Grok self-update, often weekly. The rail refuses a
    build it has not seen, which is correct and would be intolerable if clearing
    it meant editing source -- so verification is one command, it runs real
    checks, and it records the result in an operator-owned file.
    """
    import subprocess

    from .adapters import get_adapter
    from .compat import PINNED_PROVIDERS, accepted_versions, record_verified, version_token
    from .provider import installed_version

    only = getattr(ns, "provider", None)
    rows = []
    for name, rec in sorted(PINNED_PROVIDERS.items()):
        if only and name != only:
            continue
        binary = rec.get("launcher") or rec.get("path")
        installed = installed_version(binary)
        accepted = accepted_versions(name)
        ok = bool(installed) and (version_token(installed) in accepted)
        row = {"provider": name, "installed": installed, "verified": accepted,
               # The path a caller must pass to --provider. NOT the launcher:
               # resolve_provider rejects symlink components (a symlink can be
               # repointed under you), so the resolved file is what works.
               "provider_path": os.path.realpath(binary) if binary and os.path.exists(binary) else None,
               "status": "ok" if ok else ("not installed" if not installed else "UNVERIFIED")}

        # Both spellings. `providers verify` is what every remedy in this
        # codebase reached for independently, and a remedy that errors is worse
        # than none -- so the parser accepts the phrasing the tool itself uses.
        wants_verify = bool(getattr(ns, "verify", False)) or getattr(ns, "action", None) == "verify"
        if wants_verify and installed and not ok:
            # The real check: does this build still offer the CLI surface the
            # adapter's argv depends on? A version number proves nothing; a
            # missing flag breaks every job for that provider.
            adapter = get_adapter(name)
            needed = adapter.required_flags()
            try:
                helptext = subprocess.run(
                    [os.path.realpath(binary), "--help"],
                    capture_output=True, text=True, timeout=30,
                    stdin=subprocess.DEVNULL,
                ).stdout or ""
                sub = ""
                for word in needed:
                    if not word.startswith("-"):
                        sub += subprocess.run(
                            [os.path.realpath(binary), word, "--help"],
                            capture_output=True, text=True, timeout=30,
                            stdin=subprocess.DEVNULL,
                        ).stdout or ""
                surface = helptext + sub
            except Exception as exc:
                row["status"] = f"verify failed: {type(exc).__name__}"
                rows.append(row)
                continue
            missing = [f for f in needed if f not in surface]
            if missing:
                row["status"] = "CONTRACT BROKEN"
                row["missing_flags"] = missing
            else:
                record_verified(name, installed)
                row["status"] = "verified"
                row["verified"] = accepted_versions(name)
                row["note"] = (
                    "CLI surface checked. The event VOCABULARY is not re-checked here "
                    "-- that needs a captured stream. If jobs start failing to parse, "
                    "re-capture the fixture for this provider."
                )
        rows.append(row)

    if ns.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['provider']:10} {str(r['installed']):28} {r['status']}")
            if r.get("provider_path"):
                print(f"           --provider {r['provider_path']}")
            if r.get("missing_flags"):
                print(f"           missing: {', '.join(r['missing_flags'])}")
    return 0 if all(r["status"] in ("ok", "verified", "not installed") for r in rows) else 1


def cmd_execution_profile(ns: argparse.Namespace) -> int:
    """The content pin an orchestrator records at first spawn.

    Published rather than left to the caller to compute: the caller cannot see
    which package this launcher execs without resolving the symlink itself, and
    a pin derived from a different walk than the one that actually runs is worse
    than no pin.
    """
    from .pinning import execution_profile

    prof = execution_profile()
    print(json.dumps(prof, indent=2) if ns.json
          else "\n".join(f"{k}={v}" for k, v in prof.items()))
    return 0


def cmd_capabilities(ns: argparse.Namespace) -> int:
    """Describe this tool to a caller that has never seen it.

    Everything is derived from live code: commands from the parser, providers
    from the adapter registry, effort from each adapter, limits from the budget.
    """
    from . import capabilities as capmod

    profile = None
    try:
        profile = load_profile(_profile_path(ns))
    except Exception:
        # A caller asking what the tool can do should get an answer even with a
        # broken or absent profile -- that is when they need it most.
        pass
    try:
        state_path = _state_path(ns)
    except SystemExit:
        state_path = None

    out = capmod.describe(build_parser(), profile, state_path)
    if ns.json:
        print(json.dumps(out, indent=2))
        return 0

    print("commands:")
    for c in out["commands"]:
        print(f"  {c['command']:20} {c['help']}")
    print("\nproviders:")
    for p in out["providers"]:
        bits = []
        bits.append("resume" if p["can_resume"] else "no-resume")
        eff = p["effort"].get("status")
        bits.append(f"effort:{eff}")
        if not p["has_adapter"]:
            bits.append("NO ADAPTER")
        if not p["pinned"]:
            bits.append("NOT PINNED (cannot run live)")
        ver = p["installed_version"] or "not installed"
        print(f"  {p['provider']:12} {ver:32} {', '.join(bits)}")
    print("\nexit codes:")
    for code, meaning in out["refusals"]["exit_codes"].items():
        print(f"  {code:>3}  {meaning}")
    print(f"\nrefusals: every one is one stderr line starting "
          f"{out['refusals']['stderr_prefix']!r} and names a remedy")
    lim = out["limits"]
    print(f"\nlimits: daily_usd={lim['daily_usd'] or 'unlimited'}  "
          f"calls/job={lim['max_provider_calls_per_job'] or 'unlimited'}  "
          f"concurrent={lim['max_concurrent_jobs'] or 'unlimited'}")
    if out.get("profile"):
        pr = out["profile"]
        print(f"\nprofile {pr['name']!r}: provider={pr['provider']} "
              f"write_enabled={pr['write_enabled']}")
        for role, spec in (pr["roles"] or {}).items():
            extra = f" effort={spec['effort']}" if spec.get("effort") else ""
            print(f"  role {role:12} {spec.get('model')} ({spec.get('mode')}){extra}")
    return 0


def cmd_gc(ns: argparse.Namespace) -> int:
    """Reclaim old jobs. Opt-in, dry-run by default, selector required.

    Evidence is audit material, so nothing here is automatic and nothing happens
    without both a selector and --yes.
    """
    from . import gc as gcmod
    from .joblist import parse_duration

    older = parse_duration(ns.older_than) if getattr(ns, "older_than", None) else None
    if (older is None and getattr(ns, "keep_last", None) is None
            and getattr(ns, "compact_ledger", False)):
        # Compacting the ledger removes no jobs, so it does not need a job
        # selector. Asking for one would push callers into passing a destructive
        # selector they did not want just to tidy the ledger.
        planned = {"jobs": [], "protected": [], "orphan_launch_records": [],
                   "sessions": [], "sessions_skipped": [], "bytes": 0}
    else:
        planned = gcmod.plan(
            _state_path(ns),
            older_than_s=older,
            keep_last=getattr(ns, "keep_last", None),
            include_sessions=bool(getattr(ns, "include_sessions", False)),
        )

    if getattr(ns, "compact_ledger", False):
        from . import quota as quotamod

        comp = quotamod.compact_ledger(_state_path(ns), apply=bool(ns.yes))
        planned["ledger"] = comp
        if not ns.json:
            if comp["compacted"]:
                verb = "folded" if comp["applied"] else "would fold"
                print(f"ledger: {verb} {comp['compacted']} entr(ies) from "
                      f"{len(comp['days'])} past day(s) into {comp['rollup']}, "
                      f"keeping {comp['kept']} from today")
            else:
                print("ledger: nothing to compact (only today's entries)")

    if not ns.yes:
        planned["dry_run"] = True
        if ns.json:
            print(json.dumps(planned, indent=2))
        else:
            mb = planned["bytes"] / (1024 * 1024)
            print(f"would remove {len(planned['jobs'])} job(s), {mb:.1f}MB")
            for c in planned["jobs"]:
                print(f"  {c['job_id'][:8]}  {c['state']:16} age {c['age_s']}s")
            if planned["orphan_launch_records"]:
                print(f"  + {len(planned['orphan_launch_records'])} orphaned launch record(s)")
            for sess in planned["sessions"]:
                print(f"  session {sess['key'][:8]} (worktree gone: {sess['worktree']})")
            for sk in planned["sessions_skipped"]:
                print(f"  SKIPPED session {sk['key'][:8]}: {sk['reason']}")
            if planned["protected"]:
                print(f"protected ({len(planned['protected'])}):")
                for pr in planned["protected"][:10]:
                    print(f"  {pr['job_id'][:8]}  {pr['reason']}")
            print("\nnothing was removed — add --yes to apply")
        return 0

    result = gcmod.apply(_state_path(ns), planned)
    result["dry_run"] = False
    if ns.json:
        print(json.dumps(result, indent=2))
    else:
        mb = result["bytes_freed"] / (1024 * 1024)
        print(f"removed {len(result['removed'])} job(s), freed {mb:.1f}MB")
        for k in result["kept"]:
            print(f"  kept {k['job_id'][:8]}: {k['reason']}")
    return 0


def cmd_doctor(ns: argparse.Namespace) -> int:
    """Check the install and report what to do about anything broken.

    Exit 0 on pass OR warn, 1 on any fail -- so CI can gate on this without a
    provider nobody uses turning the build red.
    """
    from . import doctor

    try:
        state = _state_path(ns)
    except Exception:
        state = None  # doctor must run on a machine with no state root at all
    out = doctor.run_all(state)

    if ns.json:
        print(json.dumps(out, indent=2))
    else:
        mark = {doctor.PASS: "ok  ", doctor.WARN: "WARN", doctor.FAIL: "FAIL"}
        for c in out["checks"]:
            print(f"{mark[c['status']]}  {c['name']:24} {c['detail']}")
            if c["remedy"]:
                print(f"        -> {c['remedy']}")
        n = out["counts"]
        print(f"\n{n[doctor.PASS]} pass, {n[doctor.WARN]} warn, {n[doctor.FAIL]} fail")
    return 1 if out["status"] == doctor.FAIL else 0


def cmd_jobs(ns: argparse.Namespace) -> int:
    """List jobs in a state root with their live state.

    Bounded by default: an agent must not be made to read 500 rows to find the
    one it cares about. `--all` removes the cap deliberately.
    """
    from . import joblist

    states = None
    if getattr(ns, "state_filter", None):
        states = {s.strip() for s in ns.state_filter.split(",") if s.strip()}
    since_s = joblist.parse_duration(ns.since) if getattr(ns, "since", None) else None
    limit = None if getattr(ns, "all", False) else ns.limit

    out = joblist.enumerate_jobs(
        _state_path(ns),
        states=states,
        since_s=since_s,
        worktree=getattr(ns, "worktree", None),
        limit=limit,
    )
    if ns.json:
        print(json.dumps(out, indent=2))
        return 0

    if not out["jobs"]:
        print("no jobs match")
        return 0
    for row in out["jobs"]:
        cost = f"${row['cost_usd']:.4f}" if row.get("cost_usd") else "—"
        elapsed = f"{row['elapsed_s']:.0f}s" if row.get("elapsed_s") is not None else "—"
        where = os.path.basename(row["dir"]) if row.get("dir") else "—"
        flag = " [resumed]" if row.get("resumed_from") else ""
        print(
            f"{row['job_id'][:8]}  {str(row['state']):16} {str(row['role'] or '—'):10} "
            f"{str(row['model'] or '—'):34} {elapsed:>7} {cost:>9}  {where}{flag}"
        )
    if out["truncated"]:
        print(f"... {out['total'] - len(out['jobs'])} more (--all to show, --limit N to change)")
    return 0


def cmd_quota(ns: argparse.Namespace) -> int:
    """What is left, and what we have spent.

    Deliberately reports two DIFFERENT things without blending them into one
    reassuring number: subscription pools that publish a reading (which this rail
    does not spend), and the measured spend of this state root (which it does).
    The pool agent-ops actually bills -- opencode-go -- publishes no quota at all,
    so an "overall remaining" figure would be an invention.
    """
    from . import quota as quotamod

    state_path = _state_path(ns)
    budget = quotamod.load_budget()
    today = quotamod.spent_since(state_path, quotamod.day_start())
    out: dict[str, Any] = {
        "budget_file": quotamod.budget_path(),
        "daily_usd": budget.get("daily_usd"),
        "spent_today_usd": round(today, 6),
        "max_provider_calls_per_job": quotamod.max_provider_calls(),
        "ledger": quotamod.ledger_path(state_path),
        "external": quotamod.external_all(),
    }
    if isinstance(out["daily_usd"], (int, float)) and out["daily_usd"] > 0:
        out["remaining_usd"] = round(float(out["daily_usd"]) - today, 6)
    if getattr(ns, "rollup", False):
        since = quotamod.day_start() if getattr(ns, "today", False) else 0.0
        out["rollup"] = quotamod.rollup(state_path, since)
    if ns.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"budget file      {out['budget_file']}")
    print(f"daily limit      {out['daily_usd'] if out['daily_usd'] else 'unlimited (no budget file)'}")
    print(f"spent today      ${out['spent_today_usd']:.6f}")
    if "remaining_usd" in out:
        print(f"remaining        ${out['remaining_usd']:.6f}")
    print(f"call ceiling     {out['max_provider_calls_per_job'] or 'none'}")
    for rec in out["external"]:
        stale = "  STALE" if rec["stale"] else ""
        age = f"{rec['age_s']}s old" if rec["age_s"] is not None else "no timestamp"
        print(f"{rec['provider']:16} min remaining {rec['min_remaining_percent']}%  ({age}){stale}")
    if not out["external"]:
        print("external         none published")
    roll = out.get("rollup")
    if roll:
        print(f"\nmeasured spend across {roll['jobs']} job(s): ${roll['total_usd']:.6f}")
        for row in roll["by_provider"]:
            note = "" if row["metered"] else "   (reports no cost; billed elsewhere)"
            print(f"  {row['name']:16} ${row['cost_usd']:>10.6f}  {row['jobs']:>3} jobs{note}")
        print("  by model:")
        for row in roll["by_model"]:
            print(f"    {row['name']:34} ${row['cost_usd']:>10.6f}  {row['jobs']:>3} jobs")
        if roll["unmetered_providers"]:
            print(f"  NOTE: {', '.join(roll['unmetered_providers'])} reported no cost, "
                  "so the total is a floor and not the whole bill.")
    return 0


def _fence_identity(ns) -> str | None:
    """pid identity as `linux-proc-start:<bootId>:<startTime>`.

    A pid alone is not an identity -- pids are recycled, which is why the launch
    record carries starttime and boot_id too. This is exactly the shape atelier's
    process fencing uses, so it is published rather than left to be rebuilt.
    """
    meta_path = os.path.join(_launch_dir(_state_path(ns)), f"{ns.job}.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        meta = read_json(meta_path)
        return f"linux-proc-start:{meta['boot_id']}:{meta['starttime']}"
    except Exception:
        return None


def _require_known_job(ns, jd: str) -> None:
    """Refuse to answer about a job that does not exist.

    `status` used to return rc=0 with state "unknown", and `logs` used to return
    rc=0 with a `progress` event -- so a typo'd id, or an id from a state store
    that has since been wiped, read as a healthy job that simply had not started.
    That is the same lying-poll class as a cancelled job reporting `running`: an
    orchestrator polling it would wait forever on nothing.
    """
    if os.path.isdir(jd):
        return
    if os.path.isfile(os.path.join(_launch_dir(_state_path(ns)), f"{ns.job}.json")):
        return  # launched, directory not created yet
    _die(f"no such job: {ns.job} (no job directory and no launch record under this state root)")


def _live_state(ns, rec: dict, jd: str) -> str:
    """Thin wrapper: the logic lives in jobstate so listings can reuse it."""
    return jobstate.live_state(_state_path(ns), ns.job, rec, jd)


def cmd_status(ns: argparse.Namespace) -> int:
    """The polling answer: ~30 tokens, and valid while the job is still running.

    This is the spinner equivalent. A parent agent should call this in a loop and
    reach for `logs --digest` only when something looks wrong -- never
    `logs --follow`, which is unbounded by construction.
    """
    if getattr(ns, "full", False):
        jd, _, res_path = _job_paths(ns)
        print(json.dumps(read_json(res_path), indent=2))
        return 0

    jd, ev_path, res_path = _job_paths(ns)
    _require_known_job(ns, jd)
    rec, norm = _projection(ns)
    # Counters come from whichever summary event is present: `progress` while the
    # job runs, `finished` once it is over.
    fin = next((n for n in norm if n["event"] in ("finished", "progress")), {})
    tools = [n for n in norm if n["event"] == "tool"]
    sid = next((n["sessionId"] for n in norm if n["event"] == "status"), None)

    state = _live_state(ns, rec, jd)

    started = None
    marker = os.path.join(jd, "started_at")
    if os.path.isfile(marker):
        try:
            started = float(open(marker, encoding="utf-8").read().strip())
        except ValueError:
            started = None
    # For a finished job, freeze elapsed at the last write to the stream; for a
    # running one, measure against now.
    if rec:
        ref = os.path.getmtime(ev_path) if os.path.isfile(ev_path) else None
    else:
        ref = time.time()
    out = {
        "job": ns.job,
        "state": state,
        "sessionId": sid,
        "turns": fin.get("turns", 0),
        "tools": len(tools),
        "last_tool": (tools[-1]["name"] if tools else None),
        "tokens": fin.get("tokens", 0),
        "costUSD": fin.get("costUSD", 0.0),
        "elapsed_s": round((ref - started), 1) if started and ref else None,
        # atelier's process-fence identity shape. It already has this triple in
        # the launch record; handing it over beats having the adapter re-derive
        # it, and re-derivation after the process is gone is impossible.
        "fence": _fence_identity(ns),
    }
    print(json.dumps(out, indent=2) if ns.json else "\n".join(f"{k}={v}" for k, v in out.items()))
    return 0


# A hard ceiling, enforced here rather than requested politely. 8 KiB is roughly
# 2k tokens: enough to diagnose a failed job, small enough that reading one by
# reflex cannot wreck a parent agent's context.
DIGEST_MAX_BYTES = 8192


def cmd_logs(ns: argparse.Namespace) -> int:
    jd, ev_path, _ = _job_paths(ns)
    _require_known_job(ns, jd)
    if ns.format == "full":
        # Explicit only -- there is deliberately no default that lands here.
        if not os.path.isfile(ev_path):
            _die(f"no evidence stream at {ev_path}")
        if ns.json:
            # Refused rather than wrapped. `full` is the raw provider stream,
            # unbounded and provider-shaped; buffering it into one JSON object
            # would defeat the only reason it exists (tailing a file) and would
            # hand an agent the context flood this command is careful to avoid.
            _die(
                "logs --format full is the raw, unbounded provider stream and is "
                "not available as a JSON object. Use the digest (the default) for "
                f"structured output, or read the file directly: {ev_path}"
            )
        with open(ev_path, "rb") as fh:
            sys.stdout.buffer.write(fh.read())
        return 0

    _rec, norm = _projection(ns)
    lines = [json.dumps(n, separators=(",", ":")) for n in norm]
    blob = "\n".join(lines)
    if len(blob.encode("utf-8")) > DIGEST_MAX_BYTES:
        kept: list[str] = []
        used = 0
        for line in lines:
            size = len(line.encode("utf-8")) + 1
            if used + size > DIGEST_MAX_BYTES:
                break
            kept.append(line)
            used += size
        dropped = len(lines) - len(kept)
        kept.append(
            json.dumps({"event": "truncated", "dropped_events": dropped,
                        "cap_bytes": DIGEST_MAX_BYTES})
        )
        blob = "\n".join(kept)
        norm = [json.loads(line) for line in kept]

    if ns.json:
        # The digest is already machine-readable as JSONL, so --json is not about
        # making it parseable -- it is about the ENVELOPE. A caller that asks
        # every command for JSON gets one object here too, with the truncation
        # state as a field rather than as a sentinel line it has to notice.
        truncated = norm and norm[-1].get("event") == "truncated"
        print(json.dumps({
            "job_id": ns.job,
            "format": "digest",
            "events": norm[:-1] if truncated else norm,
            "truncated": bool(truncated),
            "dropped_events": norm[-1].get("dropped_events", 0) if truncated else 0,
            "cap_bytes": DIGEST_MAX_BYTES,
        }, indent=2))
        return 0
    print(blob)
    return 0


def cmd_promote(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    rev = read_json(os.path.join(root.job_dir(ns.review), "result.json"))
    validate(rev, "result.schema.json")
    ev = open(rev["artifacts"]["events"], "rb").read()
    from . import events as evmod

    from .adapters import get_adapter

    verdict, findings, reviewed_files = get_adapter(
        load_profile(_profile_path(ns)).get("provider")
    ).extract_review(ev)
    rec = job.attach_review(
        state_path=_state_path(ns),
        subject_job=ns.subject,
        reviewer_record=rev,
        verdict=verdict,
        findings=findings,
        reviewed_files=reviewed_files,
    )
    _emit({"status": rec["status"], "job": rec["job_id"]}, ns)
    return jobstate.exit_code_for(rec["status"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-opencode",
        description=(
            "Run a coding agent inside a sandbox and keep evidence of what it did. "
            "Every command takes --json for a stable machine-readable contract. "
            "Exit codes: 0 ok, 1 refusal or error, 2 dirty worktree, 124 timeout. "
            "Refusals print `ai-opencode: REFUSING — ...` on stderr and name a remedy."
        ),
    )
    p.add_argument("--profile", help="project profile JSON (default: AI_OPS_PROFILE, then the bundled example)")
    p.add_argument("--state", help="state root holding jobs, leases and evidence (default: AI_OPS_STATE)")
    p.add_argument("--provider", help="absolute path to the provider binary; never a PATH lookup or a symlink")
    p.add_argument("--json", action="store_true", help="machine-readable output for programmatic callers")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("state", help="provision a state root")
    s.add_argument("action", choices=["provision"], help="only `provision` today")
    s.add_argument("dir", help="directory to provision; must NOT be inside a worktree you will run jobs on")
    s.set_defaults(func=cmd_state)

    m = sub.add_parser("models", help="models this profile allows, with family/vendor and reachability")
    m.add_argument("--live", action="store_true",
                   help="ask each installed provider what models it can actually serve")
    m.set_defaults(func=cmd_models)

    sc = sub.add_parser("scout", help="read-only inspection of a worktree")
    sc.add_argument("dir", help="worktree to inspect")
    sc.add_argument("prompt", help="what to ask the agent")
    sc.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    sc.set_defaults(func=cmd_scout)

    rv = sub.add_parser("review", help="read-only review of a worktree's uncommitted change")
    rv.add_argument("dir", help="worktree to review")
    rv.add_argument("role", help="role name from the profile (its model and mode)")
    rv.add_argument("prompt", nargs="?")
    rv.add_argument("--envelope")
    rv.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    rv.set_defaults(func=cmd_review)

    wr = sub.add_parser("write", help="bounded write in a leased worktree (needs AI_OPS_WRITE=1)")
    wr.add_argument("dir", help="leased worktree to write in")
    wr.add_argument("role", help="role name from the profile (must be a bounded-write role)")
    wr.add_argument("--envelope", required=True)
    wr.add_argument("--token")
    wr.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    wr.set_defaults(func=cmd_write)

    rn = sub.add_parser("run", help="dispatch by envelope; mode/role/cwd come from the envelope")
    rn.add_argument("--envelope", required=True)
    rn.add_argument("--token")
    rn.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    rn.set_defaults(func=cmd_run)

    ls = sub.add_parser("lease", help="worktree lease lifecycle: acquire, release, show")
    ls.add_argument("action", choices=["acquire", "release", "show"])
    ls.add_argument("--dir", required=True)
    ls.add_argument("--owner")
    ls.add_argument("--owner-pid")
    ls.add_argument("--token")
    ls.add_argument("--mode")
    ls.set_defaults(func=cmd_lease)

    pv = sub.add_parser("providers", help="installed provider binaries and whether their build is verified")
    pv.add_argument("action", nargs="?", choices=["verify"],
                    help="`verify` checks an unverified build against its "
                         "adapter's CLI surface and records it")
    pv.add_argument("--provider", help="limit to one provider")
    pv.add_argument("--verify", action="store_true",
                    help="same as the `verify` action")
    pv.set_defaults(func=cmd_providers)

    ep = sub.add_parser("execution-profile", help="content digest of the launcher and package, for pinning")
    ep.set_defaults(func=cmd_execution_profile)

    cp = sub.add_parser("capabilities",
                        help="describe this tool: commands, providers, limits, refusal contract")
    cp.set_defaults(func=cmd_capabilities)

    gp = sub.add_parser("gc", help="reclaim old jobs (opt-in; dry-run unless --yes)")
    gp.add_argument("--older-than", dest="older_than",
                    help="remove jobs older than this, e.g. 24h, 7d")
    gp.add_argument("--keep-last", dest="keep_last", type=int,
                    help="keep the N most recent jobs, remove the rest")
    gp.add_argument("--yes", action="store_true",
                    help="actually delete (without this, gc only reports)")
    gp.add_argument("--compact-ledger", dest="compact_ledger", action="store_true",
                    help="fold spend.jsonl history older than today into an "
                         "append-only spend-rollup.jsonl (totals are preserved)")
    gp.add_argument("--include-sessions", dest="include_sessions", action="store_true",
                    help="also remove session stores whose worktree is gone. Needs "
                         "--yes as well: a job directory can be recreated by "
                         "re-running the job, a conversation cannot")
    gp.set_defaults(func=cmd_gc)

    dr = sub.add_parser("doctor", help="check this install and report how to fix what is broken")
    dr.set_defaults(func=cmd_doctor)

    jb = sub.add_parser("jobs", help="list jobs in the state root with their live state")
    jb.add_argument("--state-filter", dest="state_filter",
                    help="comma-separated states to include, e.g. running,awaiting_review,died")
    jb.add_argument("--since", help="only jobs started within this window, e.g. 30m, 24h, 7d")
    jb.add_argument("--worktree", help="only jobs whose worktree resolves to this path")
    jb.add_argument("--limit", type=int, default=20, help="max rows, newest first (default 20)")
    jb.add_argument("--all", action="store_true", help="remove the row cap")
    jb.set_defaults(func=cmd_jobs)

    qt = sub.add_parser("quota", help="measured spend, budget limits, and published provider quota")
    qt.add_argument("--rollup", action="store_true",
                    help="aggregate measured spend by provider, model and day")
    qt.add_argument("--today", action="store_true",
                    help="with --rollup, limit the aggregation to the current UTC day")
    qt.set_defaults(func=cmd_quota)

    rs = sub.add_parser("resume", help="continue a job's provider session with a new message")
    rs.add_argument("job", help="the job whose provider session to continue")
    rs.add_argument("message", help="what to say to it")
    rs.add_argument("--token", help="lease token, required to resume a bounded-write job")
    rs.add_argument(
        "--background", action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    rs.set_defaults(func=cmd_resume)

    cx = sub.add_parser("cancel", help="stop a backgrounded job")
    cx.add_argument("job")
    cx.set_defaults(func=cmd_cancel)

    lg = sub.add_parser("logs", help="projections over a job's evidence stream")
    lg.add_argument("job")
    lg.add_argument(
        "--format",
        choices=["digest", "full"],
        default="digest",
        help="digest (bounded, default) or full (unbounded raw stream; never for agent context)",
    )
    lg.set_defaults(func=cmd_logs)

    st = sub.add_parser("status", help="cheap poll of one job: state, counters, cost (~30 tokens)")
    st.add_argument("job")
    st.add_argument(
        "--full",
        action="store_true",
        help="print the whole persisted record (only exists once the job has finished)",
    )
    st.set_defaults(func=cmd_status)

    pr = sub.add_parser("promote", help="promote a reviewed subject job under the worktree lock")
    pr.add_argument("--subject", required=True)
    pr.add_argument("--review", required=True)
    pr.set_defaults(func=cmd_promote)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
        return int(ns.func(ns))
    except Refuse as exc:
        print(f"ai-opencode: REFUSING — {exc}", file=sys.stderr)
        return 1
    except RailError as exc:
        print(f"ai-opencode: {exc}", file=sys.stderr)
        return exc.code
