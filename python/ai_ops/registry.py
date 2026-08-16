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
    out.setdefault("provider", "opencode")
    return out


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
