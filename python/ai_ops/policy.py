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
