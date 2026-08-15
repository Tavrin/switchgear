#!/usr/bin/env python3
"""Small JSON helper for the Stage-0 shell wrappers. Not a public API."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(path: str, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _walk(obj: Any, dotted: str) -> Any:
    cur = obj
    if not dotted:
        return cur
    for part in dotted.split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(part)
            cur = cur[part]
        elif isinstance(cur, list):
            cur = cur[int(part)]
        else:
            raise KeyError(part)
    return cur


def expand_vars(value: str) -> str:
    home = os.environ.get("HOME", "")
    return value.replace("${HOME}", home).replace("$HOME", home)


_FAMILY_HINTS = (
    (re.compile(r"qwen", re.I), "qwen", "alibaba"),
    (re.compile(r"glm", re.I), "glm", "zhipu"),
    (re.compile(r"kimi", re.I), "kimi", "moonshot"),
    (re.compile(r"deepseek", re.I), "deepseek", "deepseek"),
)


def infer_family(model_id: str) -> tuple[str | None, str | None]:
    for rx, family, vendor in _FAMILY_HINTS:
        if rx.search(model_id):
            return family, vendor
    return None, None


def expand_model(profile: dict[str, Any], model_id: str) -> dict[str, Any]:
    catalog = (profile.get("models") or {}).get("catalog") or []
    for entry in catalog:
        if entry.get("id") == model_id:
            out = dict(entry)
            out.setdefault("provider", profile.get("provider") or "opencode")
            return out
    family, vendor = infer_family(model_id)
    out = {"id": model_id, "provider": profile.get("provider") or "opencode"}
    if family:
        out["model_family"] = family
    if vendor:
        out["vendor_family"] = vendor
    return out


def deny_matches(model_id: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/*"):
            if model_id.startswith(pat[:-1]):
                return True
        elif model_id == pat:
            return True
    return False


def validate_instance(
    schema: dict[str, Any], instance: Any, schema_path: str | None = None
) -> list[str]:
    try:
        import jsonschema  # type: ignore

        kwargs = {}
        if schema_path:
            schema_dir = Path(schema_path).resolve().parent
            store = {}
            for p in schema_dir.glob("*.json"):
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                store[p.name] = loaded
                store[str(p)] = loaded
            kwargs["resolver"] = jsonschema.RefResolver(
                base_uri=schema_dir.as_uri() + "/",
                referrer=schema,
                store=store,
            )
        jsonschema.Draft7Validator(schema, **kwargs).validate(instance)
        return []
    except ImportError:
        return _structural_validate(schema, instance)
    except Exception as exc:  # jsonschema.ValidationError
        return [str(exc)]


def _structural_validate(schema: dict[str, Any], instance: Any) -> list[str]:
    errors: list[str] = []
    if schema.get("type") == "object" and not isinstance(instance, dict):
        return ["not an object"]
    for key in schema.get("required", []):
        if key not in instance:
            errors.append(f"missing {key}")
    props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False and isinstance(instance, dict):
        for key in instance:
            if key not in props:
                errors.append(f"unknown field {key}")
    if "commands" in props and isinstance(instance, dict) and "commands" in instance:
        cmds = instance["commands"]
        if not isinstance(cmds, list):
            errors.append("commands must be an array")
        else:
            for i, item in enumerate(cmds):
                if isinstance(item, str):
                    errors.append(f"commands[{i}] must be an object, not a string")
                elif not isinstance(item, dict) or "verb" not in item:
                    errors.append(f"commands[{i}] needs verb")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"value not in enum: {instance!r}")
    return errors


def cmd_get(args: list[str]) -> int:
    data = _load(args[0])
    path = args[1] if len(args) > 1 else ""
    default = args[2] if len(args) > 2 else None
    try:
        val = _walk(data, path) if path else data
    except (KeyError, IndexError, ValueError):
        if default is None:
            return 2
        print(default, end="")
        return 0
    if val is None:
        print("")
        return 0
    if isinstance(val, bool):
        print("true" if val else "false")
    elif isinstance(val, (dict, list)):
        json.dump(val, sys.stdout)
        print()
    else:
        print(val)
    return 0


def cmd_getj(args: list[str]) -> int:
    data = _load(args[0])
    path = args[1] if len(args) > 1 else ""
    try:
        val = _walk(data, path) if path else data
    except (KeyError, IndexError, ValueError):
        print("null")
        return 2
    json.dump(val, sys.stdout)
    print()
    return 0


def cmd_write(args: list[str]) -> int:
    path = args[0]
    obj = json.loads(sys.stdin.read() or "null")
    _dump(path, obj)
    return 0


def cmd_validate(args: list[str]) -> int:
    schema = _load(args[0])
    instance = _load(args[1])
    errors = validate_instance(schema, instance, args[0])
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    return 0


def cmd_expand_model(args: list[str]) -> int:
    profile = _load(args[0])
    model_id = args[1]
    json.dump(expand_model(profile, model_id), sys.stdout)
    print()
    return 0


def cmd_allowed(args: list[str]) -> int:
    profile = _load(args[0])
    model_id = args[1]
    models = profile.get("models") or {}
    allow = models.get("allow") or []
    deny = models.get("deny") or []
    if deny_matches(model_id, deny):
        print("deny")
        return 1
    if allow and model_id not in allow:
        print("not-allowlisted")
        return 1
    print("ok")
    return 0


def cmd_merge_runtime(args: list[str]) -> int:
    """Merge profile external_read + state_glob into a runtime JSON on stdout."""
    runtime = _load(args[0])
    extra = json.loads(args[1]) if len(args) > 1 else []
    state_glob = args[2] if len(args) > 2 else ""

    def add_globs(node: dict[str, Any]) -> None:
        ext = node.setdefault("permission", {}).setdefault("external_directory", {})
        ext.setdefault("*", "deny")
        ext.setdefault("/tmp/opencode/**", "allow")
        if state_glob:
            ext[state_glob] = "allow"
        for g in extra:
            ext[g] = "allow"
        agent = node.get("agent") or {}
        for spec in agent.values():
            if isinstance(spec, dict):
                aext = spec.setdefault("permission", {}).setdefault(
                    "external_directory", {}
                )
                aext.setdefault("*", "deny")
                if state_glob:
                    aext[state_glob] = "allow"
                for g in extra:
                    aext[g] = "allow"

    add_globs(runtime)
    json.dump(runtime, sys.stdout)
    print()
    return 0


def cmd_extract_handoff(args: list[str]) -> int:
    """Read provider event stream; write handoff JSON to stdout. Exit 2 if missing."""
    raw = Path(args[0]).read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return 2
    texts: list[str] = []
    # Try JSONL / concatenated JSON objects first.
    decoder = json.JSONDecoder()
    idx = 0
    blob = raw.strip()
    try:
        while idx < len(blob):
            while idx < len(blob) and blob[idx].isspace():
                idx += 1
            if idx >= len(blob):
                break
            obj, end = decoder.raw_decode(blob, idx)
            idx = end
            if isinstance(obj, dict):
                for key in ("text", "message", "content"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        texts.append(val)
                    elif isinstance(val, list):
                        for part in val:
                            if isinstance(part, dict) and isinstance(
                                part.get("text"), str
                            ):
                                texts.append(part["text"])
                            elif isinstance(part, str):
                                texts.append(part)
    except json.JSONDecodeError:
        # truncated / malformed
        if not texts:
            print("malformed", file=sys.stderr)
            return 3

    if not texts:
        # last non-empty line as text
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return 2
        texts.append(lines[-1])

    last = texts[-1].strip()
    # Strip markdown fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", last, re.S)
    if fence:
        last = fence.group(1).strip()
    try:
        parsed = json.loads(last)
        if isinstance(parsed, dict) and parsed.get("summary"):
            json.dump(parsed, sys.stdout)
            print()
            return 0
        # JSON without a summary is not a handoff (meta/status events).
        return 2
    except json.JSONDecodeError:
        pass
    json.dump({"summary": last, "status": "awaiting_review"}, sys.stdout)
    print()
    return 0


def cmd_independence(args: list[str]) -> int:
    """Compute independence booleans. argv: subject_model.json reviewer_model.json subject_job reviewer_job"""
    subject = _load(args[0])
    reviewer = _load(args[1])
    subject_job = args[2]
    reviewer_job = args[3]
    out = {
        "different_job": subject_job != reviewer_job,
        "different_model": subject.get("id") != reviewer.get("id"),
        "different_family": (subject.get("model_family") or "")
        != (reviewer.get("model_family") or "")
        and bool(subject.get("model_family") and reviewer.get("model_family")),
        "different_provider": (subject.get("provider") or "")
        != (reviewer.get("provider") or ""),
    }
    json.dump(out, sys.stdout)
    print()
    return 0


def cmd_required_unmet(args: list[str]) -> int:
    """argv: profile.json independence.json -> print unmet required keys, exit 1 if any."""
    profile = _load(args[0])
    indep = _load(args[1])
    policy = ((profile.get("review") or {}).get("independence")) or {}
    unmet = []
    for key in (
        "different_job",
        "different_model",
        "different_family",
        "different_provider",
    ):
        if policy.get(key) == "required" and not indep.get(key):
            unmet.append(key)
    json.dump(unmet, sys.stdout)
    print()
    return 1 if unmet else 0


COMMANDS = {
    "get": cmd_get,
    "getj": cmd_getj,
    "write": cmd_write,
    "validate": cmd_validate,
    "expand-model": cmd_expand_model,
    "allowed": cmd_allowed,
    "merge-runtime": cmd_merge_runtime,
    "extract-handoff": cmd_extract_handoff,
    "independence": cmd_independence,
    "required-unmet": cmd_required_unmet,
}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: jsonutil.py <cmd> ...", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        return 2
    return COMMANDS[cmd](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
