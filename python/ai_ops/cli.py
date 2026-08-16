from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import identity, job, lease, state
from .errors import RailError, Refuse
from .profile import load_profile
from .provider import resolve_provider
from .registry import model_record
from .schema import validate
from .state import StateRoot, read_json


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


def _print_job(record: dict[str, Any]) -> None:
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


def cmd_models(ns: argparse.Namespace) -> int:
    profile = load_profile(_profile_path(ns))
    print("profile allowlist:")
    for m in (profile.get("models") or {}).get("allow") or []:
        rec = model_record(m)
        print(f"  {m}  family={rec.get('model_family')}")
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


def cmd_run_like(ns: argparse.Namespace, mode: str, role: str, directory: str, prompt: str, envelope: dict[str, Any] | None) -> int:
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
    )
    _print_job(rec)
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
    env = None
    prompt = ns.prompt
    if ns.envelope:
        env = json.loads(open(ns.envelope, encoding="utf-8").read())
        validate(env, "task-envelope.schema.json")
        prompt = env.get("goal") or prompt
    rec = job.run_job(
        profile_path=_profile_path(ns),
        state_path=_state_path(ns),
        mode="readonly",
        role=ns.role,
        worktree=os.path.abspath(ns.dir),
        prompt=prompt or "review",
        provider_path=ns.provider or os.environ.get("AI_OPS_PROVIDER") or "",
        envelope=env,
    )
    _print_job(rec)
    parent = (env or {}).get("parent_job")
    if parent and rec["status"] == "ok":
        ev = open(rec["artifacts"]["events"], "rb").read()
        from . import events as evmod

        try:
            verdict, findings = evmod.extract_review_verdict(ev)
        except Exception as exc:
            print(f"ai-opencode: review not attachable: {exc}", file=sys.stderr)
            return 1
        job.attach_review(
            state_path=_state_path(ns),
            subject_job=parent,
            reviewer_record=rec,
            verdict=verdict,
            findings=findings,
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


def cmd_status(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    path = os.path.join(root.job_dir(ns.job), "result.json")
    rec = read_json(path)
    print(json.dumps(rec, indent=2))
    return 0


def cmd_promote(ns: argparse.Namespace) -> int:
    root = StateRoot(_state_path(ns))
    rev = read_json(os.path.join(root.job_dir(ns.review), "result.json"))
    ev = open(rev["artifacts"]["events"], "rb").read()
    from . import events as evmod

    verdict, findings = evmod.extract_review_verdict(ev)
    rec = job.attach_review(
        state_path=_state_path(ns),
        subject_job=ns.subject,
        reviewer_record=rev,
        verdict=verdict,
        findings=findings,
    )
    print(json.dumps({"status": rec["status"], "job": rec["job_id"]}, indent=2))
    return 0 if rec["status"] == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-opencode")
    p.add_argument("--profile")
    p.add_argument("--state")
    p.add_argument("--provider")
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
    sc.set_defaults(func=cmd_scout)

    rv = sub.add_parser("review")
    rv.add_argument("dir")
    rv.add_argument("role")
    rv.add_argument("prompt", nargs="?")
    rv.add_argument("--envelope")
    rv.set_defaults(func=cmd_review)

    wr = sub.add_parser("write")
    wr.add_argument("dir")
    wr.add_argument("role")
    wr.add_argument("--envelope", required=True)
    wr.add_argument("--token")
    wr.set_defaults(func=cmd_write)

    rn = sub.add_parser("run")
    rn.add_argument("--envelope", required=True)
    rn.set_defaults(func=cmd_run)

    ls = sub.add_parser("lease")
    ls.add_argument("action", choices=["acquire", "release", "show"])
    ls.add_argument("--dir", required=True)
    ls.add_argument("--owner")
    ls.add_argument("--owner-pid")
    ls.add_argument("--token")
    ls.add_argument("--mode")
    ls.set_defaults(func=cmd_lease)

    st = sub.add_parser("status")
    st.add_argument("job")
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
