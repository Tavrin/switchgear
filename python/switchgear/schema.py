from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import Refuse

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - fail closed
    raise SystemExit("switchgear: REFUSING — jsonschema is required (no shallow fallback)") from exc

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _ROOT / "schemas"
_CACHE: dict[str, dict[str, Any]] = {}


def _store() -> dict[str, Any]:
    store: dict[str, Any] = {}
    for p in _SCHEMA_DIR.glob("*.json"):
        store[p.name] = json.loads(p.read_text(encoding="utf-8"))
    return store


def load_schema(name: str) -> dict[str, Any]:
    if name not in _CACHE:
        path = _SCHEMA_DIR / name
        if not path.is_file():
            raise Refuse(f"missing schema {name}")
        _CACHE[name] = json.loads(path.read_text(encoding="utf-8"))
    return _CACHE[name]


def validate(instance: Any, schema_name: str) -> None:
    schema = load_schema(schema_name)
    store = _store()
    resolver = jsonschema.RefResolver(
        base_uri=(_SCHEMA_DIR.resolve().as_uri() + "/"),
        referrer=schema,
        store=store,
    )
    try:
        jsonschema.Draft7Validator(schema, resolver=resolver).validate(instance)
    except jsonschema.ValidationError as exc:
        raise Refuse(f"{schema_name} validation failed: {exc.message}") from exc
