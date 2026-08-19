"""The harness seam: the contract every agent CLI adapter must satisfy.

Split out of a single 1,682-line adapters.py for review locality -- a Claude
protocol change should produce a Claude-shaped diff, not a diff in the file that
also holds Codex, Grok and OpenCode. Nothing here changed in the move.

This module deliberately imports no sibling: base <- normalization <- the four
harnesses <- the registry, in one direction only.
"""

from __future__ import annotations

from typing import Any, Iterable

TEXT_LIMIT = 400

TERMINAL_COMPLETED = "completed"
TERMINAL_EMPTY = "completed_empty"
TERMINAL_NEEDS_INPUT = "needs_input"
# Not one of the orchestrator's three success outcomes. A stream that stopped without a
# terminal event is the "claims done, evidence truncated" case, which their
# tripwires treat as suspicious -- over-reporting truncation is the right
# default, so this never reports as completed.
TERMINAL_FAILED = "failed"


EFFORT_SUPPORTED = "supported"
EFFORT_UNMEASURED = "unmeasured"
EFFORT_UNSUPPORTED = "unsupported"



class HarnessAdapter:
    """The interface a provider must implement, in one place.

    Adding a provider (a Mistral CLI, a new harness) used to mean knowing that
    fourteen methods existed and what each had to return, with nothing written
    down and nothing checking. A half-finished adapter registered happily and
    died of AttributeError partway through a job -- after the sandbox was built
    and, for a live provider, after money had been spent. Seven call sites
    papered over that with `hasattr`/`getattr` defaults, so a method you forgot
    to write looked exactly like a capability the provider genuinely lacks.

    So this class does two things and no more:

      * DEFAULTS for everything genuinely optional, expressed as the honest
        negative -- no session store, no refresh path, no effort control, no
        extra binds. A provider that simply cannot do a thing needs to write
        nothing at all.
      * REQUIRED methods that raise a message naming the adapter and what the
        method is for, instead of AttributeError from inside the job lifecycle.

    It deliberately does NOT try to share implementation bodies. Measured across
    the four existing adapters, only 12 lines are byte-identical: the provider
    shapes really are different, and a base class that pretended otherwise would
    have to be fought by every adapter after the first.

    `validate_adapter()` checks completeness at REGISTRATION, so the failure
    lands when the module is imported rather than mid-job.
    """

    #: Short provider id. Must match the key in _ADAPTERS and the profile's
    #: `provider` field.
    name: str = ""

    #: True only for a fallback-tier provider where the credential must exist
    #: INSIDE the sandbox. False means the broker holds it and the sandbox sees
    #: a placeholder -- the default, and the one to keep if at all possible.
    credential_in_sandbox: bool = False

    # ---- required: there is no sensible default for these ------------------

    def argv(
        self,
        *,
        provider_argv: list[str],
        worktree: str,
        model_id: str,
        agent: str,
        role: str,
        job_id: str,
        prompt: str,
        attach_dir: str | None = None,
        resume_session: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """The full command line to run INSIDE the sandbox."""
        raise self._missing("argv", "the command line to run inside the sandbox")

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        """This provider's raw events -> the normalized vocabulary at the top of
        this module. The single most important method: everything above this
        seam depends on it and nothing above it may know the provider's shape."""
        raise self._missing("normalize", "translation into the normalized vocabulary")

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        """(handoff, error) from the raw stream."""
        raise self._missing("validate_result", "deciding whether the run produced a result")

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        """The durable session identifier. Without it there is no resume."""
        raise self._missing("session_id", "the session id that makes resume possible")

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Environment for the sandboxed process. Must never pass host secrets."""
        raise self._missing("isolation_env", "the sandboxed process environment")

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """Point this provider at the broker instead of its real upstream."""
        raise self._missing("broker_runtime", "redirecting the provider through the broker")

    def required_flags(self) -> list[str]:
        """CLI surface argv() depends on. `providers verify` checks a new build
        still offers these -- a real contract check rather than trust in a
        version number."""
        raise self._missing("required_flags", "the CLI surface this adapter depends on")

    # ---- optional: the default is the honest negative ----------------------

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        """How to ask this CLI its version. `--version` for every provider so far."""
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "switchgear-bounded-write" if mode == "bounded-write" else "switchgear-readonly"

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """Providers with no agent-file mechanism must carry the role
        instructions in the prompt. Default assumes no such mechanism, which is
        the safe direction: a worker told a WEAKER contract than the one the rail
        validates fails in a way that looks like a model problem."""
        return f"{instructions}\n\n{prompt}" if instructions else prompt

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """None = this CLI cannot enumerate its models. Reported honestly rather
        than filled in with a hardcoded list that is stale a week later."""
        return None

    def session_store_paths(self) -> list[str]:
        """HOME-relative paths holding CONVERSATION state, never credentials.
        Empty = resume is refused for this provider, rather than silently
        starting a fresh conversation dressed as a continuation."""
        return []

    def refresh_argv(self, provider_argv: list[str]) -> list[str] | None:
        """A command that makes the CLI refresh its own expired session.
        None = the rail cannot refresh it; the operator is told to log in."""
        return None

    def extra_binds(self, provider_argv: list[str]) -> list[str]:
        """Extra read-only paths this CLI needs (helper binaries beside it)."""
        return []

    def effort_support(self) -> dict[str, Any]:
        """See EFFORT_* above. Default is `unsupported`, so a provider that
        never mentions effort cannot have one silently invented for it."""
        return {"status": EFFORT_UNSUPPORTED}

    def extract_review(self, raw: bytes):
        from ..events import extract_review_verdict

        return extract_review_verdict(raw)

    def full_text(self, events: Iterable[dict[str, Any]]) -> str:
        """Unclipped text for a human reader. Default reads the normalized
        `text` events, which every adapter must already produce."""
        return "".join(
            str(ev.get("content") or "")
            for ev in events
            if ev.get("event") == "text"
        )

    def write_sandbox_credential(self, *args: Any, **kwargs: Any) -> None:
        """Fallback-tier providers only: place a credential inside the sandbox.
        Doing nothing is correct for every full-tier provider."""
        return None

    # ---- machinery ---------------------------------------------------------

    def _missing(self, method: str, purpose: str) -> NotImplementedError:
        return NotImplementedError(
            f"adapter {type(self).__name__} does not implement {method}() — "
            f"it is required for {purpose}. See docs/ADDING-A-PROVIDER.md."
        )


#: Methods with no possible default. Checked at registration so a half-written
#: adapter fails at import, not partway through a job that already cost money.
#: The old name, bound to the same class. `provider` was doing two jobs at once
#: in this codebase -- the executable agent runtime (Claude Code, Codex, Grok,
#: OpenCode) and the API/credential/model service behind it (Anthropic, OpenAI,
#: xAI, OpenRouter, opencode-go). A result record literally carries both senses:
#: a top-level `provider` naming the binary and a `model.provider` naming the
#: pool, which is why the schema had to explain the difference in a description.
#:
#: The settled vocabulary is harness / upstream / model / target. This alias
#: exists so the rename costs no caller anything; it is not deprecated in the
#: sense of "will break", and there is no second class to drift.
ProviderAdapter = HarnessAdapter

REQUIRED_METHODS = (
    "argv",
    "normalize",
    "validate_result",
    "session_id",
    "isolation_env",
    "broker_runtime",
    "required_flags",
)


def validate_adapter(name: str, adapter: Any) -> None:
    """Refuse to register an adapter that cannot do the job.

    Checks the class actually OVERRIDES each required method rather than merely
    having the attribute -- inheriting the base's raising stub would otherwise
    pass a presence check and fail later, which is the exact failure this
    replaces.
    """
    from ..errors import Refuse

    if not isinstance(adapter, HarnessAdapter):
        raise Refuse(
            f"adapter for {name!r} must subclass HarnessAdapter so it inherits "
            "the documented defaults and the completeness check"
        )
    if getattr(adapter, "name", "") != name:
        raise Refuse(
            f"adapter registered as {name!r} calls itself "
            f"{getattr(adapter, 'name', None)!r}; the two must match or "
            "get_adapter and the profile's `provider` field will disagree"
        )
    missing = [
        m for m in REQUIRED_METHODS
        if getattr(type(adapter), m, None) is getattr(HarnessAdapter, m, None)
    ]
    if missing:
        raise Refuse(
            f"adapter {name!r} is incomplete: {', '.join(missing)} not implemented. "
            "See docs/ADDING-A-PROVIDER.md."
        )


