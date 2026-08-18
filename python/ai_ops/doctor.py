"""Find out what is broken BEFORE a job discovers it.

Every install and configuration problem in this rail was previously found the
same way: launch a job, watch it refuse, read the message, fix, retry. That is a
slow loop for a human and a dead end for an agent, which has no way to tell "you
configured this wrong" from "the model failed".

Each check answers three questions and no others: what was examined, what the
answer is, and -- when the answer is bad -- what to do about it. A check without
a remedy is a check that leaves the caller exactly where it started.

Checks REPORT; they never repair. Doctor runs when things are already wrong, and
a diagnostic that mutates state on the way past is one you cannot trust to tell
you what the state was.
"""

from __future__ import annotations

import os
from typing import Any, Callable

PASS = "pass"
WARN = "warn"
FAIL = "fail"


def _check(name: str, status: str, detail: str, remedy: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "remedy": remedy}


def check_sandbox() -> list[dict[str, Any]]:
    """The containment backend. There is deliberately no fallback: if this fails,
    nothing may run, so it is the one check that is unambiguously fatal."""
    import platform

    from .errors import Refuse
    from .sandbox import require_bwrap

    try:
        path = require_bwrap()
    except Refuse as exc:
        system = platform.system()
        if system == "Linux":
            remedy = ("install bubblewrap (Debian/Ubuntu: apt install bubblewrap). "
                      "There is no unsandboxed fallback and there will not be one.")
        else:
            # Telling a macOS user to apt-get bubblewrap is worse than saying
            # nothing: it sends them after a package that does not exist for
            # their OS, and hides that this is a platform limit rather than a
            # missing dependency.
            remedy = (
                f"agent-ops runs its containment on bubblewrap, which is "
                f"Linux-only — there is no {system} build to install, and the "
                "rail refuses to run without a boundary rather than degrading to "
                "an unsandboxed one. Run it inside a Linux VM or container "
                "(Lima, OrbStack, UTM, Docker) where every containment property "
                "holds unchanged. A native backend would have to be written and "
                "its containment re-proven on that platform; see "
                "docs/PORTABILITY.md."
            )
        return [_check("sandbox.backend", FAIL, str(exc), remedy)]
    return [_check("sandbox.backend", PASS, f"{path} present and trusted")]


def check_registry() -> list[dict[str, Any]]:
    """The controller-owned model registry, and an adapter for every provider."""
    from .adapters import _ADAPTERS, get_adapter
    from .errors import Refuse
    from .registry import load_models

    out: list[dict[str, Any]] = []
    try:
        reg = load_models()
        providers = sorted((reg.get("providers") or {}).keys())
        out.append(_check(
            "registry.models", PASS,
            f"{len(reg.get('models') or {})} curated ids, {len(providers)} model pools",
        ))
    except Exception as exc:
        return [_check(
            "registry.models", FAIL, f"{type(exc).__name__}: {exc}",
            "models/registry.json is unreadable or not valid JSON; restore it from "
            "git (`git checkout -- models/registry.json`).",
        )]

    for name in sorted(_ADAPTERS):
        try:
            get_adapter(name)
            out.append(_check(f"adapter.{name}", PASS, "resolvable"))
        except Refuse as exc:
            out.append(_check(
                f"adapter.{name}", FAIL, str(exc),
                f"provider '{name}' is registered but has no working adapter; this "
                "is a code bug, not a configuration one.",
            ))
    return out


def check_providers() -> list[dict[str, Any]]:
    """Each pinned provider binary: installed, and at a version this rail accepts.

    An unaccepted version is a WARN, never a FAIL. Codex, Claude Code and Grok
    self-update on their own schedule -- an updated binary is the normal state of
    a healthy machine, not a broken install, and `providers verify` is one command
    away. Treating it as fatal would make doctor cry wolf every week.
    """
    from .compat import PINNED_PROVIDERS, accepted_versions, version_token
    from .provider import installed_version

    out: list[dict[str, Any]] = []
    for name, rec in sorted(PINNED_PROVIDERS.items()):
        binary = rec.get("launcher") or rec.get("path")
        installed = installed_version(binary)
        accepted = accepted_versions(name)
        if not installed:
            from .compat import DISCOVERY, PROVIDERS_FILE

            where = binary or "nowhere it was looked for"
            looked = (DISCOVERY.get(name) or {}).get("path_globs") or []
            out.append(_check(
                f"provider.{name}", WARN, f"not installed ({where})",
                f"install the {name} CLI, or ignore this if you do not use it — "
                "an absent provider only blocks jobs that ask for it. Searched: "
                + (", ".join(str(g) for g in looked) or "no discovery rule") +
                f". If it lives somewhere else, name the path in {PROVIDERS_FILE} "
                'as {"' + name + '": {"path": "/abs/path", "version": "x.y.z"}}.',
            ))
        elif version_token(installed) in accepted:
            out.append(_check(f"provider.{name}", PASS, f"{installed} (accepted)"))
        else:
            out.append(_check(
                f"provider.{name}", WARN,
                f"installed {installed}, accepted {accepted}",
                f"run `ai-opencode providers verify --provider {name}` — it checks "
                "the CLI surface the adapter depends on and records the result.",
            ))
    return out


def check_credentials() -> list[dict[str, Any]]:
    """Whether each provider's credential is usable, without ever reading it out.

    Reports the credential's CLASS, SOURCE PATH and EXPIRY only. The token itself
    is never placed in the output -- doctor's output is exactly the thing someone
    pastes into a bug report.
    """
    from .credentials import load_credential
    from .errors import Refuse
    from .registry import load_models, provider_record

    out: list[dict[str, Any]] = []
    reg = load_models()
    for pool in sorted((reg.get("providers") or {}).keys()):
        try:
            prec = provider_record(pool)
        except Refuse as exc:
            out.append(_check(f"credential.{pool}", FAIL, str(exc),
                              "fix the provider entry in models/registry.json"))
            continue
        try:
            # on_expired left unset: doctor must not trigger a token refresh as a
            # side effect of being asked a question.
            cred = load_credential(pool, prec)
        except Refuse as exc:
            # load_credential's refusals already name the remedy (re-login, mode
            # 600, expired session), so repeating a generic one would be noise.
            out.append(_check(f"credential.{pool}", WARN, str(exc), ""))
            continue
        except Exception as exc:
            out.append(_check(
                f"credential.{pool}", WARN, f"{type(exc).__name__}: {exc}",
                f"inspect the credential source for '{pool}'",
            ))
            continue

        if cred is None:
            from . import provider as provmod

            # Where a credential SHOULD go, not where lookup ended up: the legacy
            # single-file fallback cannot hold a second provider, so naming it
            # would send the operator to the one path that will not work. Same
            # reasoning as _reachability in the CLI.
            want = os.environ.get("AI_OPS_PROVIDER_CREDENTIAL_FILE") or os.path.join(
                provmod.CREDENTIAL_DIR, prec.get("credential") or pool
            )
            out.append(_check(
                f"credential.{pool}", WARN, "no credential installed",
                f"install a key for '{pool}' at {want} with mode 600, or ignore "
                "this if you do not use this pool.",
            ))
            continue

        detail = f"{cred.cls} from {cred.source}"
        if cred.expires_at:
            import time

            left = cred.expires_at - time.time()
            if left <= 0:
                out.append(_check(
                    f"credential.{pool}", WARN, f"{detail} — EXPIRED",
                    f"run any {pool} command so its own CLI refreshes the session.",
                ))
                continue
            detail += f", {int(left // 60)}m remaining"
        out.append(_check(f"credential.{pool}", PASS, detail))
    return out


def check_state(state_path: str | None) -> list[dict[str, Any]]:
    """The state root: provisioned, writable, and with room to work."""
    import shutil

    from .errors import Refuse
    from .job import MIN_FREE_BYTES
    from .state import StateRoot

    if not state_path:
        return [_check(
            "state.root", WARN, "no state root given",
            "pass --state <abs path> or set AI_OPS_STATE to check it.",
        )]

    out: list[dict[str, Any]] = []
    try:
        root = StateRoot(state_path)
    except Refuse as exc:
        return [_check(
            "state.root", FAIL, str(exc),
            f"run `ai-opencode --state {state_path} state provision {state_path}`. "
            "A state root must be an absolute path outside every worktree it "
            "records jobs for, on a filesystem you own.",
        )]
    out.append(_check("state.root", PASS, f"provisioned at {root.path}"))

    probe = os.path.join(root.jobs, ".doctor-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.unlink(probe)
        out.append(_check("state.writable", PASS, "jobs directory is writable"))
    except OSError as exc:
        out.append(_check(
            "state.writable", FAIL, f"cannot write to {root.jobs}: {exc}",
            "fix ownership/permissions on the state root, or choose one you own.",
        ))

    free = shutil.disk_usage(root.path).free
    mb, need_mb = free // (1024 * 1024), MIN_FREE_BYTES // (1024 * 1024)
    if free < MIN_FREE_BYTES:
        out.append(_check(
            "state.disk", WARN, f"{mb}MB free, bounded-write needs {need_mb}MB",
            "free space on this filesystem, or raise AI_OPS_MIN_FREE_BYTES if you "
            "accept the risk — a worker can fill a disk faster than any poll "
            "interval catches.",
        ))
    else:
        out.append(_check("state.disk", PASS, f"{mb}MB free"))
    return out


def check_sessions(state_path: str | None) -> list[dict[str, Any]]:
    """Which providers can resume a conversation on this machine.

    Reported as INFO-shaped passes rather than warnings: a provider that declares
    no session store cannot resume, and that is a property of the provider, not a
    fault in this install.
    """
    from .adapters import _ADAPTERS

    out: list[dict[str, Any]] = []
    for name in sorted(_ADAPTERS):
        paths = _ADAPTERS[name].session_store_paths()
        out.append(_check(
            f"resume.{name}", PASS,
            f"session store declared ({len(paths)} path(s))" if paths
            else "no session store — jobs on this provider cannot be resumed",
        ))
    return out


def check_effort() -> list[dict[str, Any]]:
    """Which providers have an effort control, and which models have a measured set.

    Reported per MODEL because that is what it is: one provider was measured
    serving two models with different sets. An agent writing a profile needs to
    know that asking for `minimal` on gpt-5.6-sol will be refused, before it
    writes the role.
    """
    from .adapters import EFFORT_SUPPORTED, _ADAPTERS
    from .registry import effort_values, load_models

    out: list[dict[str, Any]] = []
    for name in sorted(_ADAPTERS):
        sup = _ADAPTERS[name].effort_support()
        if sup.get("status") != EFFORT_SUPPORTED:
            out.append(_check(f"effort.{name}", PASS, "no effort control"))
            continue
        note = {
            "client": "the CLI rejects a bad value itself",
            "api": "the API rejects a bad value",
            "none": "the CLI SILENTLY IGNORES an unknown value — the rail is the "
                    "only thing that can catch a mistake here",
        }.get(sup.get("validates"), "")
        out.append(_check(f"effort.{name}", PASS, f"{sup['flag']} — {note}"))

    models = (load_models().get("models") or {})
    measured = {k: effort_values(v) for k, v in models.items() if effort_values(v)}
    if measured:
        out.append(_check(
            "effort.measured_models", PASS,
            f"{len(measured)} model(s) with a measured set: "
            + "; ".join(f"{k} [{', '.join(v)}]" for k, v in sorted(measured.items())),
        ))
    unmeasured = sorted(k for k in models if not effort_values(models[k]))
    if unmeasured:
        out.append(_check(
            "effort.unmeasured_models", PASS,
            f"{len(unmeasured)} model(s) have no measured set, so `effort` is "
            "refused for them: " + ", ".join(unmeasured[:6])
            + (" ..." if len(unmeasured) > 6 else ""),
            "measure one by sending a deliberate nonsense value (most providers "
            "answer with the accepted list) and add `effort_values` to that "
            "model in models/registry.json.",
        ))
    return out


def check_health(state_path: str | None) -> list[dict[str, Any]]:
    """Models that have been failing recently. WARN at worst, never FAIL.

    Part of this tool's audience is an AI agent driving it, and an agent testing
    a fix FOR a failing model must not be refused from testing its own fix. So
    this reports and the rail never gates on it.
    """
    if not state_path:
        return []
    from . import health as healthmod

    lines = healthmod.warnings(state_path)
    if not lines:
        return [_check("health.models", PASS, "no model failing a majority of recent jobs")]
    return [
        _check(f"health.{line.split(':')[0]}", WARN, line,
               "check the provider's status, or route this role to another model. "
               "The rail does NOT refuse these jobs — this is a report.")
        for line in lines
    ]


def check_fixtures() -> list[dict[str, Any]]:
    """Whether each provider's captured stream still matches its installed build.

    `providers verify` checks that a new build offers the CLI FLAGS an adapter's
    argv depends on, and explicitly does NOT check the event VOCABULARY -- that
    needs a captured stream. These CLIs update weekly, so a normalization
    regression is plausible and would present as "jobs stopped working" with no
    obvious cause.

    WARN, never fail: a version bump usually changes nothing in the stream. It is
    a prompt to re-capture and re-run the tripwire tests, not a verdict.
    """
    import json as _json
    import os as _os

    from .compat import PINNED_PROVIDERS, version_token
    from .provider import installed_version

    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    manifest_path = _os.path.join(root, "tests", "fixtures", "MANIFEST.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = (_json.load(fh) or {}).get("fixtures") or {}
    except (OSError, ValueError):
        return [_check(
            "fixtures.manifest", WARN, f"unreadable: {manifest_path}",
            "the fixture manifest records which provider build produced each "
            "captured stream; without it vocabulary drift is invisible.",
        )]

    out: list[dict[str, Any]] = []
    for name, entry in sorted(manifest.items()):
        provider = entry.get("provider")
        captured = entry.get("captured_version")
        rec = PINNED_PROVIDERS.get(provider) or {}
        binary = rec.get("launcher") or rec.get("path")
        if not binary:
            continue  # not installed here; nothing to compare against
        current = version_token(installed_version(binary))
        if not captured:
            out.append(_check(
                f"fixtures.{provider}", WARN,
                f"{name} has no recorded capture version (installed: {current})",
                f"re-capture {name} from the current build to establish a "
                "baseline; until then vocabulary drift for this provider cannot "
                "be detected.",
            ))
        elif current and version_token(captured) != current:
            out.append(_check(
                f"fixtures.{provider}", WARN,
                f"{name} captured from {captured}, installed is {current}",
                f"re-capture {name} from {current} and re-run the tripwire tests. "
                "`providers verify` checks the CLI flags, not the event "
                "vocabulary, so this is the only thing that would catch a "
                "normalization regression.",
            ))
        else:
            out.append(_check(f"fixtures.{provider}", PASS,
                              f"{name} matches the installed build ({current})"))
    return out


CHECKS: list[tuple[str, Callable[..., list[dict[str, Any]]], bool]] = [
    ("sandbox", check_sandbox, False),
    ("registry", check_registry, False),
    ("providers", check_providers, False),
    ("credentials", check_credentials, False),
    ("state", check_state, True),
    ("effort", check_effort, False),
    ("health", check_health, True),
    ("fixtures", check_fixtures, False),
    ("sessions", check_sessions, True),
]


def run_all(state_path: str | None = None) -> dict[str, Any]:
    """Every check, with an overall verdict.

    A check that raises is itself reported as a FAIL rather than aborting the
    run: doctor is what you reach for when things are broken, so it must survive
    the breakage it was called to describe.
    """
    checks: list[dict[str, Any]] = []
    for name, fn, wants_state in CHECKS:
        try:
            checks.extend(fn(state_path) if wants_state else fn())
        except Exception as exc:  # a diagnostic must not die of what it diagnoses
            checks.append(_check(
                f"{name}.<crashed>", FAIL, f"{type(exc).__name__}: {exc}",
                "this check crashed; that is a bug in doctor itself.",
            ))
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in (PASS, WARN, FAIL)}
    return {
        "checks": checks,
        "counts": counts,
        # Warnings never decide the verdict, so CI can gate on failures alone
        # without an unused provider or an unfunded pool turning the build red.
        "status": FAIL if counts[FAIL] else (WARN if counts[WARN] else PASS),
    }
