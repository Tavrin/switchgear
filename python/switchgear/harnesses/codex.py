"""Codex CLI: `codex exec`.

Moved out of adapters.py unchanged; see harnesses/base.py."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .base import EFFORT_SUPPORTED, HarnessAdapter, TERMINAL_FAILED
from .normalization import (
    _clip,
    _error_message,
    _extract_handoff,
    _extract_review,
    _finish_events,
    _num,
    parse_lenient,
)


class CodexAdapter(HarnessAdapter):
    """Codex CLI 0.147.x, headless (`exec --json`).

    Read off a real captured stream (tests/fixtures/codex-real-scout.jsonl).
    Measured shapes -- a much smaller vocabulary than the others:

      thread.started   {thread_id}          <- the session id, on the FIRST event
      turn.started     {}
      item.started     {item:{id,type,...}}
      item.completed   {item:{id,type,...}} <- type agent_message{text}
                                              or command_execution{command,...}
      turn.completed   {usage{input_tokens,cached_input_tokens,output_tokens,
                              reasoning_output_tokens}}

    Two consequences worth stating because they differ from every sibling:

    - `thread_id` is on the FIRST event, not the last (Grok's sessionId is only
      on its terminal event). Resume information is therefore available from the
      moment the job starts.
    - Codex reports NO COST, only tokens. costUSD is 0.0 rather than invented,
      which is why an orchestrator lane would set reportsCost false for this provider.
    """

    name = "codex"
    # FULL tier: measured -- Codex accepts a placeholder session file and sends
    # that token as Bearer, so the broker holds the real one.
    credential_in_sandbox = False

    # A structurally valid but meaningless token. Codex reads its session file at
    # startup and puts the token in an Authorization header; the broker replaces
    # both that header and the ChatGPT-Account-ID derived from it.
    PLACEHOLDER_ACCOUNT = "00000000-0000-0000-0000-000000000000"

    def _placeholder_token(self) -> str:
        import base64 as _b64
        import time as _time

        def seg(d):
            return _b64.urlsafe_b64encode(
                json.dumps(d, separators=(",", ":")).encode()
            ).decode().rstrip("=")

        now = int(_time.time())
        return ".".join([
            seg({"alg": "RS256", "typ": "JWT"}),
            seg({
                "iss": "https://auth.openai.com", "aud": "broker-placeholder",
                "sub": self.PLACEHOLDER_ACCOUNT, "exp": now + 86400, "iat": now,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": self.PLACEHOLDER_ACCOUNT,
                    "chatgpt_plan_type": "placeholder",
                    "user_id": self.PLACEHOLDER_ACCOUNT,
                },
            }),
            "YnJva2VyLXBsYWNlaG9sZGVy",
        ])

    def effort_support(self) -> dict[str, Any]:
        """`-c model_reasoning_effort=<v>`. The CLI forwards anything; the API
        rejects an unknown value with a 400 that enumerates the set FOR THAT
        MODEL -- which is how the per-model nature of this was discovered."""
        return {"status": EFFORT_SUPPORTED, "flag": "-c model_reasoning_effort=",
                "validates": "api"}
    def required_flags(self) -> list[str]:
        """CLI surface this adapter's argv depends on.

        `providers verify` checks a NEW build still offers these, which is a
        real contract check rather than trust in a version number -- and it
        costs nothing, so it can run on every self-update.
        """
        return ["exec", "--json", "--sandbox", "--model", "--skip-git-repo-check"]

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """How to ask this CLI what models it can actually serve.

        None means it offers no such command -- reported honestly rather
        than guessed, since a stale hardcoded list is the problem this
        exists to avoid.
        """
        return None

    def session_store_paths(self) -> list[str]:
        """HOME-relative paths holding CONVERSATION state, not credentials.

        Provider session history lives inside the provider's HOME, and every
        job gets a fresh synthetic HOME that is reclaimed afterwards -- so
        without persisting these, a resumed job finds nothing and the CLI
        answers "No conversation found with session ID". Measured per
        provider; an empty list means resume is refused for it rather than
        silently starting a fresh conversation dressed as a continuation.

        Measured via `codex doctor`: rollout/session files live under
        ~/.codex/sessions.
        """
        return [".codex/sessions"]

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
        # --skip-git-repo-check: the sandbox mounts the git dir read-only and
        # Codex's own check is redundant with the rail's worktree identity work.
        wire = model_id.split("/", 1)[1] if "/" in model_id else model_id
        # Codex runs model-generated shell commands under its OWN sandbox, nested
        # inside ours. read-only for a scout; workspace-write when the job is
        # allowed to edit. Ours remains the real boundary either way -- for a
        # readonly job the worktree is a read-only mount, so even a wrong value
        # here cannot grant writes.
        sandbox_mode = "workspace-write" if agent.endswith("bounded-write") else "read-only"
        argv = list(provider_argv) + ["exec"]
        if resume_session:
            # Codex resumes through a SUBCOMMAND rather than a flag:
            # `codex exec resume <session> <prompt>`. Same headless --json
            # stream, so the normalizer and the whole evidence path are unchanged.
            argv += ["resume", resume_session]
        argv += ["--skip-git-repo-check", "--json", "--sandbox", sandbox_mode,
                 "--model", wire]
        if effort:
            # Codex takes this as a config override rather than a flag. `-c
            # key=value` is one argv pair, so the value cannot be split off into
            # a position where it would read as a prompt.
            argv += ["-c", f"model_reasoning_effort={effort}"]
        if attach_dir:
            argv += ["--add-dir", attach_dir]
        return argv + [prompt]

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "switchgear-bounded-write" if mode == "bounded-write" else "switchgear-readonly"

    def refresh_argv(self, provider_argv: list[str]) -> list[str] | None:
        """A cheap, no-model command that makes the CLI refresh its own session."""
        return list(provider_argv) + ["doctor"]

    def extra_binds(self, provider_argv: list[str]) -> list[str]:
        """Codex is not one file: it needs its sibling helpers.

        Binding only the executable produced a job that authenticated, streamed
        and answered -- while telling the user "the workspace execution tool is
        unavailable", because codex-code-mode-host was missing. A provider that
        cannot read a file is useless for scout or review, so the whole vendor
        directory (bin/, codex-path/ with its bundled rg, codex-resources/) is
        bound read-only.
        """
        import os as _os

        out = []
        for arg in provider_argv:
            if _os.path.isabs(arg) and _os.path.exists(arg):
                # .../vendor/<triple>/bin/codex -> .../vendor/<triple>
                vendor = _os.path.dirname(_os.path.dirname(_os.path.realpath(arg)))
                if _os.path.isdir(_os.path.join(vendor, "bin")):
                    out.append(vendor)
        return out

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Write Codex's own config + a placeholder session into the sandbox.

        CODEX_HOME defaults to $HOME/.codex, so the synthetic HOME already
        isolates it (measured: host auth, 3 MCP servers, 1,173 rollouts and the
        state DB are all absent inside). It is still set explicitly rather than
        relying on that default holding.

        `supports_websockets = false` is load-bearing: by default Codex reaches
        inference over `wss://api.openai.com/v1/responses`, which IGNORES the
        base-url redirect and which an HTTP broker cannot proxy at all. Forcing
        the HTTP transport is what makes this provider brokerable.
        """
        import os as _os

        from ..env import allowlisted_env, assert_no_host_secrets

        codex_home = _os.path.join(synth_home, ".codex")
        _os.makedirs(codex_home, exist_ok=True)
        extra = {"CODEX_HOME": codex_home}
        if broker_base_url:
            base = broker_base_url.rstrip("/")
            with open(_os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(
                    'model_provider = "broker"\n'
                    f'chatgpt_base_url = "{base}"\n'
                    "[model_providers.broker]\n"
                    'name = "broker"\n'
                    f'base_url = "{base}"\n'
                    'wire_api = "responses"\n'
                    "requires_openai_auth = true\n"
                    "supports_websockets = false\n"
                )
            auth = _os.path.join(codex_home, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "OPENAI_API_KEY": None,
                        "tokens": {
                            "id_token": self._placeholder_token(),
                            "access_token": self._placeholder_token(),
                            "account_id": self.PLACEHOLDER_ACCOUNT,
                        },
                        "last_refresh": "2026-01-01T00:00:00.000000000Z",
                    },
                    fh,
                )
            _os.chmod(auth, 0o600)
        env = allowlisted_env(home=synth_home, extra=extra)
        assert_no_host_secrets(env)
        return env

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """Redirected by its config file, written in isolation_env."""
        return runtime

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """No agent-file mechanism: the role instructions go in the prompt.

        Same text the OpenCode agent file carries (policy.role_instructions), so
        the handoff contract a worker is told and the one the rail validates
        cannot drift apart.
        """
        if not instructions:
            return prompt
        return f"{instructions.strip()}\n\n---\n\n{prompt}"


    def extract_review(self, raw: bytes):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        return _extract_review(self.full_text(parsed))

    def full_text(self, events: Iterable[dict[str, Any]]) -> str:
        """Unclipped agent_message text, for contract extraction."""
        out = []
        for ev in events:
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                if item.get("text"):
                    out.append(str(item["text"]))
        return "\n".join(out)

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if ev.get("type") == "thread.started" and isinstance(ev.get("thread_id"), str):
                return ev["thread_id"]
        return None

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        events = list(events)
        out: list[dict[str, Any]] = []
        sid = self.session_id(events)
        if sid:
            out.append({"event": "status", "sessionId": sid})

        turns = 0
        last_text = ""
        usage: dict[str, Any] = {}
        saw_terminal = False
        errored: str | None = None

        for ev in events:
            ty = ev.get("type")
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ty == "turn.started":
                turns += 1
            elif ty == "turn.completed":
                saw_terminal = True
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            elif ty in ("turn.failed", "error"):
                errored = _error_message(ev) if ty == "error" else str(
                    ev.get("error") or "turn failed"
                )
            elif ty == "item.completed":
                itype = item.get("type")
                if itype == "error":
                    # An error ITEM is Codex reporting that something it needed
                    # was unavailable. Measured: a run whose code-mode host was
                    # missing emitted one, then answered "I can't inspect the
                    # file" -- and still closed its turn normally, so the run
                    # read as `completed`. That is the claims-done-with-evidence-
                    # of-failure shape; record it as the failure it is.
                    errored = str(item.get("message") or "provider reported an error item")
                elif itype == "agent_message" and item.get("text"):
                    last_text = str(item["text"])
                    out.append({"event": "text", "content": _clip(last_text)})
                elif itype == "command_execution":
                    out.append({
                        "event": "tool",
                        "name": "command_execution",
                        "target": _clip(str(item.get("command") or ""), 200),
                    })
                elif itype:
                    out.append({
                        "event": "tool",
                        "name": str(itype),
                        "target": _clip(str(item.get("path") or item.get("id") or ""), 200),
                    })

        counters = {
            "turns": turns,
            # Codex reports NO cost, only tokens. Report 0.0 rather than invent a
            # number; the token counts are real.
            "costUSD": 0.0,
            "tokens": int(
                _num(usage.get("input_tokens"))
                + _num(usage.get("output_tokens"))
                + _num(usage.get("cached_input_tokens"))
                + _num(usage.get("reasoning_output_tokens"))
            ),
        }
        out.extend(
            _finish_events(
                counters,
                saw_terminal=saw_terminal,
                run_ended=run_ended,
                errored=errored,
                last_text=last_text,
            )
        )
        return out

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        fin = next(
            (n for n in self.normalize(parsed, run_ended=True) if n["event"] == "finished"), None
        )
        if fin is None:
            return None, "no terminal provider event"
        if fin["status"] == TERMINAL_FAILED:
            return None, fin.get("exitSummary") or "provider run failed"
        if require_handoff:
            return _extract_handoff(self.full_text(parsed))
        return None, None


