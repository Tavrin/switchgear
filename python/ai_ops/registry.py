from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import Refuse

_ROOT = Path(__file__).resolve().parents[2]


def load_models() -> dict[str, Any]:
    path = _ROOT / "models" / "registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_commands() -> dict[str, Any]:
    path = _ROOT / "commands" / "registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def registry_digest() -> str:
    """Digest of the controller-owned model registry as it is right now.

    Frozen onto a subject job so a later edit to models/registry.json cannot
    relabel model families to manufacture reviewer independence.
    """
    from .digest import sha256_bytes

    path = _ROOT / "models" / "registry.json"
    return sha256_bytes(path.read_bytes())


def model_record(model_id: str) -> dict[str, Any]:
    reg = load_models()
    deny = reg.get("deny") or []
    for pat in deny:
        if pat.endswith("/*") and model_id.startswith(pat[:-1]):
            raise Refuse(f"model '{model_id}' is denied by controller registry")
        if model_id == pat:
            raise Refuse(f"model '{model_id}' is denied by controller registry")
    rec = (reg.get("models") or {}).get(model_id)
    if not rec:
        raise Refuse(f"model '{model_id}' is not in the controller registry")
    out = dict(rec)
    out["id"] = model_id
    out.setdefault("provider", model_id.split("/", 1)[0] if "/" in model_id else "opencode")
    return out


def provider_record(provider_id: str) -> dict[str, Any]:
    """Upstream + credential name for a provider, from the controller registry."""
    reg = load_models()
    rec = (reg.get("providers") or {}).get(provider_id)
    if not rec:
        raise Refuse(f"provider '{provider_id}' is not in the controller registry")
    if not rec.get("upstream"):
        raise Refuse(f"provider '{provider_id}' has no upstream")
    return rec


def wire_model_names(model_id: str) -> set[str]:
    """Names a provider may legitimately put in the request body.

    Registry ids are provider-qualified (`openrouter/anthropic/claude-...`), but
    a client may send either the full id or the provider-stripped suffix
    (`anthropic/claude-...`). Accept both rather than guessing, so the broker's
    model pin cannot be defeated by a naming convention.
    """
    names = {model_id}
    if "/" in model_id:
        names.add(model_id.split("/", 1)[1])
    return names


def command_record(verb: str) -> dict[str, Any]:
    reg = load_commands()
    rec = reg.get(verb)
    if not rec:
        raise Refuse(f"command verb '{verb}' is not in the controller registry")
    argv = rec.get("argv") or []
    if not argv or not os_isabs(argv[0]):
        raise Refuse(f"command {verb} must have an absolute executable")
    return rec


def os_isabs(p: str) -> bool:
    import os

    return os.path.isabs(p)
