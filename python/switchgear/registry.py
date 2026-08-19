from __future__ import annotations

import json
import re
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


# Family -> vendor. This is the ONLY hardcoded identity table, and it is
# deliberately the slowest-moving fact available: model VERSIONS churn weekly
# (qwen3.7-max, qwen3.8-max, kimi-k2.6, kimi-k3 ...) while a family's owner
# essentially never changes. Listing individual model ids meant the registry was
# stale the day after it was written -- `opencode models` reports 26 ids where
# this file had hand-listed 18 across every provider.
FAMILY_VENDOR = {
    "claude": "anthropic",
    "gpt": "openai",
    "grok": "xai",
    "deepseek": "deepseek",
    "glm": "zhipu",
    "kimi": "moonshot",
    "qwen": "alibaba",
    "minimax": "minimax",
    "mimo": "xiaomi",
    "nemotron": "nvidia",
    "llama": "meta",
    "mistral": "mistralai",
    "gemini": "google",
    "hy": "tencent",
}

# Single-vendor CLIs: the provider itself settles the vendor, whatever the model
# is called. Multi-vendor pools (opencode-go, openrouter) are absent on purpose.
PROVIDER_VENDOR = {
    "grok": "xai",
    "codex": "openai",
    "claude": "anthropic",
}


def derive_identity(model_id: str, provider: str) -> dict[str, str]:
    """Work out family and vendor from the id, by controller-owned RULES.

    This is what lets a model released tomorrow work without a code change,
    while keeping the property the registry exists for: a project profile may
    NAME a model, it may never say what family that model belongs to. Reviewer
    independence rests on this metadata, so it stays derived here, in the
    controller, from rules the project cannot influence.

    Heuristic, and honest about it: `identity_source` is recorded on every record
    so an auditor can tell a derived classification from a curated one.
    """
    name = model_id.split("/")[-1].lower()
    # Family: the leading alphabetic run. qwen3.8-max -> qwen, glm-5.3 -> glm,
    # claude-sonnet-5 -> claude, deepseek-v4-flash -> deepseek.
    match = re.match(r"[a-z]+", name)
    family = match.group(0) if match else name

    # Vendor: the provider wins when it serves exactly one; then an explicit
    # vendor segment in the id (openrouter/anthropic/claude-...); then family.
    vendor = PROVIDER_VENDOR.get(provider)
    if not vendor:
        parts = model_id.split("/")
        if len(parts) >= 3 and parts[1] in FAMILY_VENDOR.values():
            vendor = parts[1]
    if not vendor:
        vendor = FAMILY_VENDOR.get(family)
    return {
        "model_family": family,
        "vendor_family": vendor or family,
        "identity_source": "derived",
    }


def effort_values(model_rec: dict[str, Any]) -> list[str] | None:
    """The effort values MEASURED for this model, or None if nobody has.

    Per model, never per provider. One provider was observed to serve two models
    with different sets -- the OpenAI API advertises `minimal` generically and
    gpt-5.6-sol refuses it -- so a provider-level list is wrong for some model in
    the pool, and wrong silently.

    None means unmeasured, and unmeasured means the rail refuses rather than
    guessing. That is deliberately the DEFAULT for a model nobody has curated:
    identity can be derived from an id by rule, but an accepted-value set cannot
    be, and inventing one is how a job ends up running at an effort nobody chose.
    """
    values = model_rec.get("effort_values")
    if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
        return list(values)
    return None


def model_record(model_id: str) -> dict[str, Any]:
    reg = load_models()
    deny = reg.get("deny") or []
    for pat in deny:
        if pat.endswith("/*") and model_id.startswith(pat[:-1]):
            raise Refuse(f"model '{model_id}' is denied by controller registry")
        if model_id == pat:
            raise Refuse(f"model '{model_id}' is denied by controller registry")
    provider = model_id.split("/", 1)[0] if "/" in model_id else "opencode"
    if provider not in (reg.get("providers") or {}):
        raise Refuse(
            f"model '{model_id}' names provider '{provider}', which is not in the "
            "controller registry"
        )
    rec = (reg.get("models") or {}).get(model_id)
    if rec:
        # A curated entry wins: it is how a family the rules get wrong is fixed.
        out = dict(rec)
        out.setdefault("identity_source", "registry")
    else:
        # Not curated is not unknown. Model versions churn far faster than anyone
        # edits this file, so identity is DERIVED rather than refused -- the
        # profile allowlist and the deny list still decide what may be used.
        out = derive_identity(model_id, provider)
    out["id"] = model_id
    out.setdefault("provider", provider)
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
