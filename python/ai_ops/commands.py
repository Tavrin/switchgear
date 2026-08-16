from __future__ import annotations

from typing import Any, Sequence

from .errors import Refuse
from .registry import command_record


_FORBIDDEN_BASES = {
    "/bin/sh",
    "/usr/bin/sh",
    "/bin/bash",
    "/usr/bin/bash",
    "/usr/bin/env",
    "/bin/dash",
}


def resolve_command(verb: str, args: Sequence[str]) -> list[str]:
    rec = command_record(verb)
    argv0 = rec["argv"][0]
    if argv0 in _FORBIDDEN_BASES or argv0.endswith("/sh") or argv0.endswith("/bash"):
        raise Refuse(f"command {verb} uses a forbidden base executable")
    arity = rec.get("arity") or {}
    amin = int(arity.get("min", 0))
    amax = int(arity.get("max", 0))
    if not (amin <= len(args) <= amax):
        raise Refuse(f"command {verb} arity {len(args)} not in {amin}..{amax}")
    allow_opts = set(rec.get("allow_options") or [])
    allow_paths = bool(rec.get("allow_paths"))
    for a in args:
        if a.startswith("-") and a not in allow_opts:
            raise Refuse(f"option injection refused: {a}")
        if a.startswith("@") or a.startswith("--response-file") or a == "--":
            raise Refuse(f"response-file / separator refused: {a}")
        if not allow_paths and ("/" in a or a.startswith(".")):
            raise Refuse(f"path-bearing argument refused: {a}")
    return list(rec["argv"]) + list(args)
