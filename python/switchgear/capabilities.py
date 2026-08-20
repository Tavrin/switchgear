"""What this tool can do, with the source of each claim made explicit.

Part of this tool's audience is an AI agent that has never seen it before and
cannot read the docs mid-task. Such a caller needs to find out what commands
exist, which providers and models it may use, what `effort` values are real, and
what a refusal will look like -- without guessing.

Commands and flags are DERIVED by walking the argparse parser. The provider
roster comes from the adapter registry crossed with version pins, and each
provider's effort entry comes from its adapter's `effort_support()`. Limit values
come from the operator budget and the concurrency control; uid-boundary state is
measured at call time; an optional profile block comes from the loaded profile.

Other claims are DECLARED constants maintained by hand in this module: the
refusal prefix, guarantee and exit-code descriptions; fixed tool, containment,
vocabulary and deprecation descriptions; and explanatory notes. They cannot be
made derived merely by living beside derived fields, so the emitted
`provenance` block labels both classes for a caller.

A hand-maintained capability document is stale the day after it is written --
which is not a hypothetical here, the model registry had already drifted to
listing 18 ids where the provider served 26.

The honesty test lives in the test suite: the provider set this reports must
equal `adapters._ADAPTERS`, so adding a fifth provider without wiring it in fails
CI rather than producing a confident, incomplete answer.
"""

from __future__ import annotations

import argparse
from typing import Any

# The refusal contract, stated once here because a caller needs it BEFORE it
# hits one. Kept beside the exit table it refers to.
REFUSAL_CONTRACT = {
    "stderr_prefix": "switchgear: REFUSING — ",
    "guarantee": (
        "Every refusal is a single line on stderr with this prefix and names a "
        "remedy. Treat any refusal as fail-closed: no work was promoted."
    ),
    "exit_codes": {
        "0": ("the job did its work (ok, awaiting_review, "
              "awaiting_external_review), or an informational command answered"),
        "1": "refusal or provider error",
        "2": (
            "dirty — worktree integrity changed during the job; also argparse "
            "usage errors before any job starts. A dirty refusal/outcome has "
            "this refusal prefix or a job record; argparse prints `usage:` and "
            "creates no job"
        ),
        "124": "timed out",
    },
}


# Machine-readable labels for the mixed origin of the capability document.
# The previous module docstring called every field derived while the refusal
# contract directly above it was a literal. That made the self-description hide
# the exact stale-copy hazard it warns callers about.
CAPABILITY_PROVENANCE = {
    "derived": {
        "commands": "argparse parser, including each command's flags",
        "providers": (
            "adapter registry plus version pins; effort comes from each adapter"
        ),
        "limits.budget_file,daily_usd,max_provider_calls_per_job": (
            "operator budget configuration"
        ),
        "limits.max_concurrent_jobs": "concurrency control",
        "containment.uid_boundary": "runtime capability probe",
        "profile": "loaded project profile, when available",
    },
    "declared": {
        "tool": "constant maintained in capabilities.py",
        "refusals": "REFUSAL_CONTRACT maintained in capabilities.py",
        "limits.note": "explanatory contract text maintained in capabilities.py",
        "containment.backend,platform,note,elsewhere": (
            "platform contract text maintained in capabilities.py"
        ),
        "vocabulary": "contract text maintained in capabilities.py",
        "deprecated": "compatibility contract maintained in capabilities.py",
        "provenance": "these source labels are maintained in capabilities.py",
    },
}


def _walk_parser(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """Commands and their flags, read off the parser that actually parses them.

    Written this way so a flag cannot exist in the CLI and be absent here, or the
    reverse. The alternative -- a hand-listed table -- is the thing this command
    exists to replace.
    """
    out: list[dict[str, Any]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        # argparse keeps a subcommand's `help=` on the pseudo-action, separate
        # from the subparser's own `description`. Most commands here set the
        # former, so reading only the latter reported every command as
        # undocumented -- which a self-describing surface must not do.
        helps = {ca.dest: (ca.help or "") for ca in action._choices_actions}
        for name, sub in action.choices.items():
            flags = []
            for sa in sub._actions:
                if sa.dest == "help":
                    continue
                flags.append({
                    "name": (sa.option_strings[0] if sa.option_strings else sa.dest),
                    "positional": not sa.option_strings,
                    "required": bool(getattr(sa, "required", False)) or not sa.option_strings,
                    "help": sa.help or "",
                    "choices": list(sa.choices) if sa.choices else None,
                })
            desc = (sub.description or "").strip()
            out.append({
                "command": name,
                "help": helps.get(name) or (desc.splitlines()[0] if desc else ""),
                "flags": flags,
            })
    return sorted(out, key=lambda c: c["command"])


def _providers() -> list[dict[str, Any]]:
    """Every registered adapter, with what it can actually do here.

    The union with PINNED_PROVIDERS matters: an adapter with no pin cannot run
    live, and a pin with no adapter is a configuration error. Reporting both
    sides makes a half-added provider visible instead of silently absent.
    """
    from .adapters import EFFORT_SUPPORTED, _ADAPTERS
    from .compat import PINNED_PROVIDERS, accepted_versions
    from .provider import installed_version

    out = []
    for name in sorted(set(_ADAPTERS) | set(PINNED_PROVIDERS)):
        adapter = _ADAPTERS.get(name)
        pin = PINNED_PROVIDERS.get(name) or {}
        binary = pin.get("launcher") or pin.get("path")
        effort = adapter.effort_support() if adapter else {"status": "no adapter"}
        entry = {
            "provider": name,
            "has_adapter": adapter is not None,
            "pinned": bool(pin),
            "installed_version": installed_version(binary),
            "accepted_versions": accepted_versions(name),
            # A provider can only be resumed if its conversation store has been
            # MEASURED; an empty list means resume is refused rather than
            # silently starting a fresh conversation wearing the old session id.
            "can_resume": bool(adapter.session_store_paths()) if adapter else False,
            # False is the good case: the credential stays in the broker and the
            # sandbox only ever sees a placeholder.
            "credential_in_sandbox": bool(
                getattr(adapter, "credential_in_sandbox", False)) if adapter else None,
            "effort": effort,
        }
        if effort.get("status") == EFFORT_SUPPORTED:
            entry["effort_values"] = effort.get("values")
        out.append(entry)
    return out


def _uid_boundary_state() -> dict[str, Any]:
    """Whether a readonly worker runs as a uid that is not the caller's."""
    from .userns import capability

    cap = capability()
    if cap.get("available"):
        return {"available": True, "applies_to": "readonly jobs",
                "note": ("bounded-write deliberately excluded: files written by a "
                         "subuid could not be handed back to the controller")}
    return {"available": False, "reason": cap.get("reason"),
            "note": "mount and network isolation only; the worker runs as your uid"}


def describe(parser: argparse.ArgumentParser, profile: dict[str, Any] | None,
             state_path: str | None) -> dict[str, Any]:
    """The whole self-description. Every field traces to live code or config."""
    from . import quota as quotamod
    from .concurrency import limit as concurrency_limit

    budget = quotamod.load_budget()
    out: dict[str, Any] = {
        "tool": "switchgear",
        "commands": _walk_parser(parser),
        "providers": _providers(),
        "refusals": REFUSAL_CONTRACT,
        "provenance": CAPABILITY_PROVENANCE,
        "limits": {
            "budget_file": quotamod.budget_path(),
            "daily_usd": budget.get("daily_usd"),
            "max_provider_calls_per_job": quotamod.max_provider_calls(),
            "max_concurrent_jobs": concurrency_limit(),
            "note": ("Limits are operator-owned and absent means unlimited. They "
                     "are never profile-declared: a project that can raise its "
                     "own ceiling does not have one."),
        },
        "containment": {
            "backend": "bwrap",
            "platform": "linux-only",
            "note": ("The worktree is the only writable mount for bounded-write; "
                     "the git dir is read-only. Readonly jobs additionally run "
                     "under a subuid boundary where the machine can establish "
                     "one — see uid_boundary below, and `security` on each "
                     "record for what a given job actually got. Bounded-write "
                     "runs as the invoking user by design: the controller has to "
                     "read back and commit what the worker produced."),
            "elsewhere": ("Run inside a Linux VM or container — every containment "
                          "property holds unchanged. There is no unsandboxed "
                          "fallback. See docs/PORTABILITY.md."),
            # Reported rather than claimed: it depends on subuid allocation and
            # helper binaries that a given machine may not have.
            "uid_boundary": _uid_boundary_state(),
        },
    }
    # The nouns, from the executable contract rather than a document -- a reader
    # here is usually an agent that will never open docs/.
    out["vocabulary"] = {
        "harness": "the agent CLI that runs a job: claude, codex, grok, opencode",
        "pool": ("the service that serves a model, named by the prefix of a model "
                 "id (opencode-go/..., openrouter/...). This is `model.provider` "
                 "on a record. Its base URL is the registry's `upstream`"),
        "model": "the exact model id, pool-qualified",
        "role": "a profile-level purpose (scout, implement, review) that fixes model, mode and effort",
        "job": "one execution: one agent, one process, one sandbox, one record",
        "note": ("`provider` historically meant BOTH the harness and the pool, in "
                 "the same record. Prefer `harness` for the binary and "
                 "`model.provider` for the pool."),
    }
    out["deprecated"] = {
        # Kept working, named here so a caller can migrate deliberately instead
        # of discovering it from a doc that may be stale.
        "result.provider": "alias of result.harness; same value, retained indefinitely",
        "adapters.ProviderAdapter": "alias of harnesses.HarnessAdapter; same class object",
        "switchgear.adapters": "re-export shim for switchgear.harnesses",
    }
    if profile:
        out["profile"] = {
            "name": profile.get("name"),
            "provider": profile.get("provider"),
            "write_enabled": bool(profile.get("write_enabled")),
            "models_allow": (profile.get("models") or {}).get("allow") or [],
            # Roles carry model, mode and (optionally) effort -- the three things
            # a caller must know before dispatching.
            "roles": profile.get("roles") or {},
        }
    return out
