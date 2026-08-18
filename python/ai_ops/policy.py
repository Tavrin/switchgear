from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .digest import sha256_json
from .errors import Refuse
from .registry import command_record, model_record


@dataclass(frozen=True)
class CompiledPolicy:
    mode: str
    write_enabled: bool
    containment: str
    tools: dict[str, str]
    review: dict[str, Any]
    commands: dict[str, Any]
    external_read: list[str]
    timeout_s: int
    timeout_min: int
    timeout_max: int
    require_linked_for_write: bool
    require_lease_for_write: bool
    allow_primary_for_readonly: bool
    roles: dict[str, dict[str, str]]
    models_allow: list[str]
    network: str
    digest: str

    def role(self, name: str) -> dict[str, str]:
        if name not in self.roles:
            raise Refuse(f"unknown role: {name}")
        return self.roles[name]

    def model_for_role(self, name: str) -> dict[str, Any]:
        spec = self.role(name)
        mid = spec["model"]
        if mid not in self.models_allow:
            raise Refuse(f"model '{mid}' is not allowed by the profile")
        return model_record(mid)

    def agent_definition(self, role: str, attachment_dir: str | None = None) -> str:
        """The OpenCode agent file: frontmatter policy + the prompt body.

        Generated from this compiled policy so there is ONE source of truth.
        Static agent markdown in the repo drifted from the runtime and was never
        actually loaded; this is what the provider really reads.

        The prompt matters for correctness, not just tone: a live provider never
        emits `handoff` or `review` objects of its own, so the structured result
        the rail requires has to be asked for here, in the model's own output.
        """
        bash = self.tools.get("bash", "deny")
        edit = self.tools.get("edit", "deny")
        head = [
            "---",
            f"description: agent-ops {self.mode} rail ({role})",
            "mode: primary",
            "permission:",
            f"  edit: {edit}",
            f"  bash: {bash}",
            "  read: allow",
            "  glob: allow",
            "  grep: allow",
            "  webfetch: deny",
            "  websearch: deny",
            "  task: deny",
            "  todowrite: deny",
            "  skill: deny",
            "  external_directory:",
        ]
        if attachment_dir:
            # The controller writes the material a job must read (the frozen
            # review diff) OUTSIDE the worktree, because writing it into the
            # worktree would mutate the very tree under review and break the
            # freeze. So exactly one directory outside the worktree is readable,
            # and the controller is its only writer.
            head.append(f'    "{attachment_dir}/**": allow')
        head += [
            '    "*": deny',
            "---",
            "",
        ]
        common = [
            "You are running inside an isolated sandbox on a single git worktree.",
            "You cannot reach anything outside it, and you must not try.",
            "Be concise. Do not narrate what you are about to do.",
            "",
        ]
        if self.mode == "bounded-write":
            body = common + [
                "Make the smallest change that satisfies the task. Edit files directly.",
                "Do not run git. Do not create commits. Do not touch .git.",
                "",
                "When you are done, end your final message with a fenced json block:",
                "",
                "```json",
                '{"handoff": {"summary": "<one line>", "status": "awaiting_review",',
                ' "changes": ["<repo-relative path>", "..."], "remaining_risks": ["..."],',
                ' "next_action": "review"}}',
                "```",
                "",
                "The block is mandatory: without it the job is rejected.",
            ]
        elif role == "review":
            body = common + [
                "The complete diff is given to you as an ATTACHED FILE named in the",
                "prompt. Read that file first, then review exactly that change.",
                "You have no shell and no git: do not try to obtain the diff",
                "yourself. Judge only whether the change is correct and safe.",
                "",
                "End your final message with a fenced json block:",
                "",
                "```json",
                '{"review": {"verdict": "promote|reject|needs_changes",',
                ' "reviewed_files": ["<every changed path you examined>"],',
                ' "findings": [{"severity": "high", "claim": "...", "evidence": "..."}]}}',
                "```",
                "",
                "reviewed_files must name every file the change touched; a review",
                "that omits one is rejected. Use 'promote' only if you would ship it.",
            ]
        else:
            body = common + [
                "Inspect and report. Never modify anything.",
                "Answer the question directly, citing file:line where useful.",
            ]
        return "\n".join(head + body) + "\n"

    def to_opencode_runtime(self) -> dict[str, Any]:
        bash = self.tools.get("bash", "deny") == "allow"
        edit = self.tools.get("edit", "deny") == "allow"
        agent = "ai-ops-bounded-write" if self.mode == "bounded-write" else "ai-ops-readonly"
        perm = {
            "bash": "deny" if not bash else "allow",
            "edit": "allow" if edit else "deny",
            "task": "deny",
            "skill": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "todowrite": "deny",
            "external_directory": {"*": "deny"},
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "tools": {
                "bash": bash,
                "write": edit,
                "edit": edit,
                "task": False,
            },
            "permission": perm,
            "plugin": [],
            "agent": {agent: {"permission": perm}},
        }


def compile_policy(profile: dict[str, Any], mode: str) -> CompiledPolicy:
    if mode not in {"readonly", "bounded-write"}:
        raise Refuse(f"unknown mode {mode}")
    write_enabled = bool(profile.get("write_enabled"))
    if mode == "bounded-write" and not write_enabled:
        raise Refuse("profile write_enabled is false")
    tools = {
        "bash": "deny",
        "edit": "allow" if mode == "bounded-write" else "deny",
        "write": "allow" if mode == "bounded-write" else "deny",
        "task": "deny",
        "skill": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "todowrite": "deny",
    }
    review = profile.get("review") or {
        "required_after_write": True,
        "independence": {
            "different_job": "required",
            "different_model": "required",
            "different_family": "preferred",
            "different_provider": "optional",
        },
    }
    cmds: dict[str, Any] = {}
    for verb, enabled in (profile.get("commands") or {}).items():
        if enabled:
            cmds[verb] = command_record(verb)
    # Profile deny is enforced by dropping denied ids out of allow at compile time.
    models = profile.get("models") or {}
    denied = models.get("deny") or []
    allow = [m for m in (models.get("allow") or []) if m not in denied]
    wt = profile.get("worktree") or {}
    payload = {
        "mode": mode,
        "write_enabled": write_enabled,
        "containment": "bwrap",
        "tools": tools,
        "review": review,
        "commands": {k: v.get("argv") for k, v in cmds.items()},
        "external_read": [],
        "timeout_s": int(profile.get("timeout_s") or 600),
        "timeout_min": int(profile.get("timeout_min") or 1),
        "timeout_max": int(profile.get("timeout_max") or 1800),
        "require_linked_for_write": bool(wt.get("require_linked_worktree_for_write", True)),
        "require_lease_for_write": bool(wt.get("require_lease_for_write", True)),
        "allow_primary_for_readonly": bool(wt.get("allow_primary_for_readonly", True)),
        "roles": profile.get("roles") or {},
        "models_allow": allow,
        "network": "provider-required",
    }
    digest = sha256_json(payload)
    return CompiledPolicy(
        mode=mode,
        write_enabled=write_enabled,
        containment="bwrap",
        tools=tools,
        review=review,
        commands=cmds,
        external_read=[],
        timeout_s=int(profile.get("timeout_s") or 600),
        timeout_min=int(profile.get("timeout_min") or 1),
        timeout_max=int(profile.get("timeout_max") or 1800),
        require_linked_for_write=bool(wt.get("require_linked_worktree_for_write", True)),
        require_lease_for_write=bool(wt.get("require_lease_for_write", True)),
        allow_primary_for_readonly=bool(wt.get("allow_primary_for_readonly", True)),
        roles=profile.get("roles") or {},
        models_allow=allow,
        network="provider-required",
        digest=digest,
    )
