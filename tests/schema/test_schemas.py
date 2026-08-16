#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
from ai_ops.errors import Refuse  # noqa: E402
from ai_ops.schema import validate  # noqa: E402

SCHEMAS = ROOT / "schemas"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = ROOT / "project-profiles" / "example.json"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(schema_name: str, instance, expect_ok: bool, label: str) -> int:
    try:
        validate(instance, schema_name)
        ok = True
        errors = []
    except Refuse as exc:
        ok = False
        errors = [str(exc)]
    if ok != expect_ok:
        print(f"FAIL {label}: ok={ok} expected {expect_ok} errors={errors}")
        return 1
    print(f"ok - {label}")
    return 0


def main() -> int:
    rc = 0
    rc += check("task-envelope.schema.json", load(FIXTURES / "envelope-ok.json"), True, "envelope ok")
    rc += check(
        "task-envelope.schema.json",
        load(FIXTURES / "envelope-object-commands.json"),
        True,
        "envelope object commands",
    )
    rc += check(
        "task-envelope.schema.json",
        load(FIXTURES / "envelope-string-commands.json"),
        False,
        "envelope string commands rejected",
    )
    rc += check("project-profile.schema.json", load(PROFILE), True, "example profile")
    rc += check(
        "model.schema.json",
        {"id": "opencode-go/glm-5.3", "provider": "opencode", "model_family": "glm"},
        True,
        "model with family",
    )
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
