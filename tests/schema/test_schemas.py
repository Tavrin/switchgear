#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
from jsonutil import validate_instance  # noqa: E402

SCHEMAS = ROOT / "schemas"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = ROOT / "project-profiles" / "example.json"
POLICIES = ROOT / "policies"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(schema_path: Path, instance, expect_ok: bool, label: str) -> int:
    errors = validate_instance(load(schema_path), instance, str(schema_path))
    ok = not errors
    if ok != expect_ok:
        print(f"FAIL {label}: ok={ok} expected {expect_ok} errors={errors}")
        return 1
    print(f"ok - {label}")
    return 0


def main() -> int:
    rc = 0
    env_schema = SCHEMAS / "task-envelope.schema.json"
    rc += check(env_schema, load(FIXTURES / "envelope-ok.json"), True, "envelope ok")
    rc += check(
        env_schema,
        load(FIXTURES / "envelope-object-commands.json"),
        True,
        "envelope object commands",
    )
    rc += check(
        env_schema,
        load(FIXTURES / "envelope-string-commands.json"),
        False,
        "envelope string commands rejected",
    )
    rc += check(
        SCHEMAS / "project-profile.schema.json",
        load(PROFILE),
        True,
        "example profile",
    )
    rc += check(SCHEMAS / "policy.schema.json", load(POLICIES / "readonly.json"), True, "readonly policy")
    rc += check(
        SCHEMAS / "policy.schema.json",
        load(POLICIES / "bounded-write.json"),
        True,
        "bounded-write policy",
    )
    model = {
        "id": "opencode-go/glm-5.3",
        "provider": "opencode",
        "model_family": "glm",
        "capabilities": ["write"],
        "cost_class": 2,
        "trust_stage": "unknown",
    }
    rc += check(SCHEMAS / "model.schema.json", model, True, "model with reserved slots")
    rc += check(
        SCHEMAS / "model.schema.json",
        {"id": "x", "provider": "opencode"},
        True,
        "model without optional slots",
    )
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
