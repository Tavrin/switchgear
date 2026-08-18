from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

from . import identity, job, lease, state
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
        }, indent=2))
        return
    print(f"model={record['model']['id']}")
    print(f"dir={record['dir']}")
    print(f"exit={record.get('exit')}")
    arts = record.get("artifacts") or {}
    print(f"result={arts.get('events')}")
    print(f"meta={record.get('job_id')}")
    print(f"job={record['job_id']}")


def cmd_state(ns: argparse.Namespace) -> int:
    if ns.action == "provision":
        path = state.provision(os.path.abspath(ns.dir))
        print(f"state={path}")
        return 0
    _die("unknown state action")
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
    profile = load_profile(_profile_path(ns))
    print("profile allowlist:")
    for m in (profile.get("models") or {}).get("allow") or []:
        rec = model_record(m)
        reach = _reachability(rec.get("provider") or "")
        print(f"  {m}  family={rec.get('model_family')}  {reach}")
    return 0


def cmd_lease(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    ident = identity.inspect_worktree(os.path.abspath(ns.dir))
    if ns.action == "acquire":
        owner = ns.owner or "controller"
        pid = int(ns.owner_pid or os.getppid())
        tok = lease.acquire(root, ident, owner, pid, ns.mode or "bounded-write")
        print(f"lease={tok['lease_uuid']}")
        print(f"file={os.path.join(root.leases, lease.identity_key(ident), 'token.json')}")
        return 0
    if ns.action == "release":
        if not ns.token:
            _die("release requires --token")
        lease.release(root, ident, ns.token, ns.owner or "controller")
        print("released")
        return 0
    if ns.action == "show":
        tok = lease.load_token(root, ident)
        print(json.dumps(tok, indent=2))
        return 0
    _die("unknown lease action")
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
        print(json.dumps({"job": ns.job, "state": "not_running"}, indent=2))
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
    print(json.dumps({"job": ns.job, "state": "cancelled", "pid": pid}, indent=2))
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
    if rec["status"] == "dirty":
        return 2
    if rec["status"] in {"timeout"}:
        return 124
    if rec["status"] in {"provider_error", "review_failed", "refused"}:
        return 1
    return 0


def cmd_scout(ns: argparse.Namespace) -> int:
    return cmd_run_like(ns, "readonly", "scout", ns.dir, ns.prompt, None)


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
        files = identity.changed_files(ident)
    except Refuse:
        diff, files = "", []
    if diff:
        prompt = (
            f"{prompt or 'Review this change.'}\n\n"
            f"Changed files: {', '.join(files)}\n\n"
            "The complete uncommitted diff follows. Review THIS; do not go looking\n"
            "for it yourself.\n\n"
            f"```diff\n{diff}\n```\n"
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
    )
    _print_job(rec, getattr(ns, "json", False))
    parent = (env or {}).get("parent_job")
    if parent and rec["status"] == "ok":
        ev = open(rec["artifacts"]["events"], "rb").read()
        from . import events as evmod

        try:
            verdict, findings, reviewed_files = evmod.extract_review_verdict(ev)
        except Exception as exc:
            print(f"ai-opencode: review not attachable: {exc}", file=sys.stderr)
            return 1
        job.attach_review(
            state_path=_state_path(ns),
            subject_job=parent,
            reviewer_record=rec,
            verdict=verdict,
            findings=findings,
            reviewed_files=reviewed_files,
        )
    if rec["status"] == "dirty":
        return 2
    return 0 if rec["status"] == "ok" else 1


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
    profile = load_profile(_profile_path(ns)) if getattr(ns, "profile", None) else {}
    adapter = get_adapter((rec.get("provider") or profile.get("provider") or "opencode"))
    parsed, _ = parse_lenient(raw)
    return rec, adapter.normalize(parsed)


def _live_state(ns, rec: dict, jd: str) -> str:
    """What is this job doing right now?

    result.json only exists once a job is over, so its absence cannot mean
    "running" -- a job killed before it wrote one would poll as running forever,
    which is the worst answer this command can give an orchestrator. The launch
    record is the authority for a backgrounded job, and it is consulted even when
    the job directory does not exist: a job killed early may never have created
    one, and reporting "unknown" there loses the launch we know happened.
    """
    if rec:
        return str(rec.get("status"))
    meta_path = os.path.join(_launch_dir(_state_path(ns)), f"{ns.job}.json")
    if os.path.isfile(meta_path):
        from .lease import _alive

        try:
            meta = read_json(meta_path)
            if _alive(int(meta["pid"]), meta.get("starttime", ""), meta.get("boot_id", "")):
                return "running"
            return "cancelled" if meta.get("cancelled") else "died"
        except Exception:
            return "unknown"
    return "running" if os.path.isdir(jd) else "unknown"


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
    rec, norm = _projection(ns)
    fin = next((n for n in norm if n["event"] == "finished"), {})
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
    }
    print(json.dumps(out, indent=2) if ns.json else "\n".join(f"{k}={v}" for k, v in out.items()))
    return 0


# A hard ceiling, enforced here rather than requested politely. 8 KiB is roughly
# 2k tokens: enough to diagnose a failed job, small enough that reading one by
# reflex cannot wreck a parent agent's context.
DIGEST_MAX_BYTES = 8192


def cmd_logs(ns: argparse.Namespace) -> int:
    jd, ev_path, _ = _job_paths(ns)
    if ns.format == "full":
        # Explicit only -- there is deliberately no default that lands here.
        if not os.path.isfile(ev_path):
            _die(f"no evidence stream at {ev_path}")
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
    print(blob)
    return 0


def cmd_promote(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    rev = read_json(os.path.join(root.job_dir(ns.review), "result.json"))
    validate(rev, "result.schema.json")
    ev = open(rev["artifacts"]["events"], "rb").read()
    from . import events as evmod

    verdict, findings, reviewed_files = evmod.extract_review_verdict(ev)
    rec = job.attach_review(
        state_path=_state_path(ns),
        subject_job=ns.subject,
        reviewer_record=rev,
        verdict=verdict,
        findings=findings,
        reviewed_files=reviewed_files,
    )
    print(json.dumps({"status": rec["status"], "job": rec["job_id"]}, indent=2))
    return 0 if rec["status"] == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-opencode")
    p.add_argument("--profile")
    p.add_argument("--state")
    p.add_argument("--provider")
    p.add_argument("--json", action="store_true", help="machine-readable output for programmatic callers")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("state")
    s.add_argument("action", choices=["provision"])
    s.add_argument("dir")
    s.set_defaults(func=cmd_state)

    m = sub.add_parser("models")
    m.set_defaults(func=cmd_models)

    sc = sub.add_parser("scout")
    sc.add_argument("dir")
    sc.add_argument("prompt")
    sc.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    sc.set_defaults(func=cmd_scout)

    rv = sub.add_parser("review")
    rv.add_argument("dir")
    rv.add_argument("role")
    rv.add_argument("prompt", nargs="?")
    rv.add_argument("--envelope")
    rv.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    rv.set_defaults(func=cmd_review)

    wr = sub.add_parser("write")
    wr.add_argument("dir")
    wr.add_argument("role")
    wr.add_argument("--envelope", required=True)
    wr.add_argument("--token")
    wr.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    wr.set_defaults(func=cmd_write)

    rn = sub.add_parser("run")
    rn.add_argument("--envelope", required=True)
    rn.add_argument("--token")
    rn.add_argument(
        "--background",
        action="store_true",
        help="launch detached; print the job id at once and poll with `status`",
    )
    rn.set_defaults(func=cmd_run)

    ls = sub.add_parser("lease")
    ls.add_argument("action", choices=["acquire", "release", "show"])
    ls.add_argument("--dir", required=True)
    ls.add_argument("--owner")
    ls.add_argument("--owner-pid")
    ls.add_argument("--token")
    ls.add_argument("--mode")
    ls.set_defaults(func=cmd_lease)

    cx = sub.add_parser("cancel")
    cx.add_argument("job")
    cx.set_defaults(func=cmd_cancel)

    lg = sub.add_parser("logs")
    lg.add_argument("job")
    lg.add_argument(
        "--format",
        choices=["digest", "full"],
        default="digest",
        help="digest (bounded, default) or full (unbounded raw stream; never for agent context)",
    )
    lg.set_defaults(func=cmd_logs)

    st = sub.add_parser("status")
    st.add_argument("job")
    st.add_argument(
        "--full",
        action="store_true",
        help="print the whole persisted record (only exists once the job has finished)",
    )
    st.set_defaults(func=cmd_status)

    pr = sub.add_parser("promote")
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
