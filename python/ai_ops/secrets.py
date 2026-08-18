"""Notice when a worker's own output contains something that looks like a secret.

The rail is careful about credentials going IN -- the broker keeps them out of
the sandbox entirely for three of four providers, and _assert_no_credentials
refuses to persist a session store that collected one. It was entirely
indifferent to what comes OUT. A worker that cats a .env, echoes an
Authorization header while debugging, or pastes a key into its own reasoning
writes that straight into evidence, which is then read by `logs`, quoted into
review prompts, and kept indefinitely.

This FLAGS and never destroys. The owner's decision, and the right one: evidence
is audit material, and a rail that silently rewrites the bytes it recorded is
worth less than one that records honestly and says "look here". A finding is
added to the record and a warning goes to stderr; the job's status is untouched
and the evidence stays byte-identical.

Designed for a low false-positive rate rather than coverage. A detector that
cries wolf on ordinary output trains everyone to ignore it, at which point it is
strictly worse than nothing -- so every pattern here is anchored to a shape that
is hard to produce by accident, and the rail's OWN placeholder is excluded by
name and covered by a test.
"""

from __future__ import annotations

import re
from typing import Any

# The value the broker writes into the sandbox in place of a real credential. It
# is DESIGNED to look like a credential to the provider, so it would trip a naive
# scanner on essentially every brokered job -- which would make the whole signal
# worthless. Excluded by name, with a test.
PLACEHOLDER = "broker-placeholder-not-a-credential"

# Each pattern is anchored on a vendor-published prefix or a structural shape.
# Deliberately NOT included: bare high-entropy strings, "password" near an
# equals sign, anything base64-shaped. Those are where false positives come
# from, and a git hash, a UUID or a content digest would trip every one of them.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Vendor key prefixes. sk-ant- before sk-, since the generic rule would
    # otherwise claim every Anthropic key and hide which vendor leaked.
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}")),
    ("xai-key", re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # A JWT: three base64url segments, and the first must decode to a JSON
    # header start ("{" -> "eyJ"). That prefix is what keeps this off ordinary
    # dotted identifiers.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # Private keys, in the armoured form that is unambiguous.
    ("private-key-pem", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    # A header line carrying an actual value. The value class excludes the
    # redaction forms people write in examples (`***`, `<token>`, `REDACTED`).
    # A header or assignment carrying an actual value. The optional quote matters:
    # `api_key="..."` is how it appears in config and code, and a value class
    # that stopped at the quote missed every quoted form. The negative lookahead
    # excludes the redaction spellings people write in examples and docs.
    ("auth-header", re.compile(
        r"(?i)\b(?:authorization|x-api-key|api[_-]?key)\b\s*[:=]\s*"
        r"[\"']?(?:bearer\s+)?"
        r"(?![\"']?(?:\*|<|redacted|placeholder|xxx|none|null)\b)"
        r"[A-Za-z0-9_\-\.]{20,}"
    )),
]


def scan(text: str, where: str = "") -> list[dict[str, Any]]:
    """Findings for one blob of text: pattern, where, and a count.

    The matched VALUE is never returned. The whole point is to avoid copying a
    secret into a second place, and a finding that quotes the secret would put it
    into result.json -- which is smaller, more portable and more likely to be
    pasted somewhere than the evidence file it came from.
    """
    if not text:
        return []
    # Strip the rail's own placeholder first, so a brokered job cannot trip the
    # detector on a value the rail itself inserted.
    haystack = text.replace(PLACEHOLDER, "")
    out: list[dict[str, Any]] = []
    for name, pattern in _PATTERNS:
        found = pattern.findall(haystack)
        if found:
            out.append({"pattern": name, "where": where, "count": len(found)})
    return out


def scan_files(paths: dict[str, str], limit: int = 4_000_000) -> list[dict[str, Any]]:
    """Scan several named files, tolerating anything unreadable.

    `paths` maps a label (used as `where`) to a filesystem path. A file that
    cannot be read is skipped rather than raising: this runs after the job's
    record is built, and a scanner that could fail the job would make an
    advisory check into a new way to lose a completed result.
    """
    out: list[dict[str, Any]] = []
    for where, path in sorted(paths.items()):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                out.extend(scan(fh.read(limit), where))
        except OSError:
            continue
    return out


def summarize(findings: list[dict[str, Any]]) -> str:
    """One stderr line naming what was seen and where, never the value."""
    if not findings:
        return ""
    parts = [f"{f['count']}x {f['pattern']} in {f['where']}" for f in findings]
    return (
        "possible secrets in worker output: " + "; ".join(parts) +
        " — evidence is kept byte-intact and the job status is unchanged; "
        "review the evidence and rotate anything real."
    )
