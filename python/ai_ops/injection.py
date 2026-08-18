"""Notice when content the worker reads is trying to talk to the worker.

A model cannot reliably separate "content I was asked to analyse" from
"instructions I should obey". So any text a job reads — a source file, a comment,
a README, a diff — is a possible command channel. The sandbox stops the worker
from DOING things outside the worktree; it does nothing about the worker being
TOLD what to do inside it.

**Where that actually bites here, precisely.** Nearly all of `promote` is
model-free: tree digests, worktree identity, the generation CAS, reviewer
independence, "the worktree changed after review". None of it can be forged
without changing the tree, which invalidates the digest. Exactly ONE load-bearing
input comes from a model's judgement — the reviewer's `verdict`.

So the sharp attack is a chain: hostile text steers the implementer, the
implementer writes text into the diff aimed at the reviewer, the reviewer reads
that diff and returns `promote`. The injection target is the gate itself.

**This does not detect prompt injection.** Nothing does; the problem is not
solvable by pattern matching, and claiming otherwise would be the most dangerous
thing in this file. What it does is narrower and checkable: it recognises content
that is *shaped like an instruction to an agent* or that *forges this rail's own
output contract*, and makes the promotion path fail closed when the reviewed
material contains any. A human then decides.

That second class is the strong one and it is specific to this rail. The handoff
and review JSON blocks are the format a WORKER emits to the controller. Finding
one inside reviewed content means something in the tree is impersonating the
worker's side of the protocol. There is no benign reason for it to be there, so
the false-positive rate is near zero and the signal is close to conclusive.
"""

from __future__ import annotations

import re
from typing import Any

# Anchored on shapes, not on topic. Every pattern here has to survive the same
# test the secret detector does: it must not fire on ordinary source, prose, or
# documentation, because a check that cries wolf is one everybody turns off.
#
# Measured on the material this actually sees -- DIFFS, not whole files. Across
# 40 real commit diffs from this repository it fired once, on a test fixture that
# legitimately contains the handoff format. That is a true positive with a benign
# cause: this repo implements the protocol, so its own changes sometimes carry
# it. The consequence is "a human decides", not "rejected", which is the correct
# outcome for a change that genuinely embeds a reviewer-directed block.
#
# Scanning whole FILES instead would fire on this repo's source, tests and docs,
# which is why the gate reads the diff: the question is what a change introduces,
# not what the tree has always contained.
# Clean across all four captured provider streams and the whole dogfooding
# project.
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # --- forgery of this rail's own contract (near-conclusive) ---------------
    (
        "forged-review-verdict",
        "content contains this rail's REVIEW block — the reviewer's own output "
        "format, impersonated inside material under review",
        re.compile(r'"review"\s*:\s*\{[^}]{0,200}?"verdict"\s*:\s*"(?:promote|reject|needs_changes)"',
                   re.I | re.S),
    ),
    (
        "forged-handoff",
        "content contains this rail's HANDOFF block — a worker's completion "
        "report, impersonated inside material under review",
        re.compile(r'"handoff"\s*:\s*\{[^}]{0,200}?"status"\s*:\s*"[a-z_]+"', re.I | re.S),
    ),
    (
        "verdict-directive",
        "content instructs a reviewer what verdict to return",
        # The gap must be PROSE. `return review['verdict'] == 'promote'` is
        # ordinary code that mentions all three words, and an earlier version of
        # this pattern flagged it -- in a repository whose own source is full of
        # exactly that expression. Code punctuation in the gap disqualifies the
        # match.
        re.compile(
            r"(?i)\b(?:respond|reply|answer|output|return|emit|set)\b"
            r"[^.\n\[\]{}()'\"=;:]{0,40}"
            r"\bverdict\b[^.\n\[\]{}()'\"=;:]{0,40}"
            r"\b(?:promote|approve|accept)\b"
        ),
    ),
    # --- instruction-shaped text addressed at a model ------------------------
    (
        "instruction-override",
        "content tells the reader to disregard its instructions",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,30}"
            r"\b(?:previous|prior|earlier|above|all|any|your)\b[^.\n]{0,30}"
            r"\b(?:instruction|instructions|prompt|prompts|rule|rules|directive)"
        ),
    ),
    (
        "role-reassignment",
        "content tries to reassign the reader's role",
        re.compile(
            r"(?:^|\n)\s*(?:system|assistant)\s*:\s*\S"
            r"|\byou\s+are\s+now\s+(?:a|an|the)\b"
            r"|\bnew\s+(?:system\s+)?(?:instructions?|prompt)\s*:",
            re.I,
        ),
    ),
    (
        "addressed-to-the-agent",
        "content speaks directly to a reviewing or reading agent",
        re.compile(
            r"(?i)\b(?:attention|note\s+to|message\s+for|dear)\b[^.\n]{0,20}"
            r"\b(?:ai|llm|agent|reviewer|assistant|model)\b"
        ),
    ),
]


def scan(text: str, where: str = "") -> list[dict[str, Any]]:
    """Findings for one blob. Never returns the matched text verbatim.

    A finding quotes only a bounded, single-line EXCERPT, because the whole point
    is to hand a human something to look at — but an unbounded quote would let
    the injected instruction ride into whatever reads the finding, which for this
    tool is frequently another model.
    """
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for name, description, pattern in _PATTERNS:
        found = pattern.findall(text)
        if not found:
            continue
        match = pattern.search(text)
        excerpt = re.sub(r"\s+", " ", match.group(0))[:120] if match else ""
        line = text.count("\n", 0, match.start()) + 1 if match else 0
        out.append({
            "pattern": name,
            "what": description,
            "where": where,
            "line": line,
            "count": len(found),
            "excerpt": excerpt,
        })
    return out


def blocks_promotion(findings: list[dict[str, Any]]) -> bool:
    """Whether these findings make a model's verdict untrustworthy.

    Every finding does. The reason to keep this as its own function rather than
    `bool(findings)` is that it is the single place the policy lives, so widening
    or narrowing it is one visible edit rather than a condition scattered across
    call sites.
    """
    return bool(findings)


def summarize(findings: list[dict[str, Any]]) -> str:
    """One line naming what was seen and where."""
    if not findings:
        return ""
    parts = [f"{f['pattern']} at {f['where']}:{f['line']}" for f in findings]
    return (
        "content under review is addressed at a reviewing agent: "
        + "; ".join(parts)
        + " — a model's verdict on this cannot be trusted, so automatic promotion "
        "is refused and a human must decide."
    )
