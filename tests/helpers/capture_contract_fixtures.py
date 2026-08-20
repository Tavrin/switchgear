#!/usr/bin/env python3
"""Capture the adapter contract fixture pack from REAL runs of the real rail.

Every file in the pack is what the code actually wrote, put through one
normalisation pass and nothing else. Nothing here hand-authors a record, and
that is a rule rather than a preference:

A valid capture puts `finished.status=completed` beside `final_text_state=empty`
beside `freeze.changed_files=["tracked.txt"]` -- a run that changed a file and
said nothing on the way out. A human writing that fixture by hand would "fix"
one of those three fields and destroy the exact signal the fixture exists to
carry. The same applies to the `needs_input` scenario, where the normalized
event says `needs_input` while `result.json` says `provider_error`: two
vocabularies disagreeing legitimately, which no author would invent.

So the captured artifact is the authority and this file is the suspect.

    $ python3 tests/helpers/capture_contract_fixtures.py [--out DIR]

It uses the committed mock provider by absolute path and costs nothing. It never
runs a live provider, and it refuses to write a pack containing an absolute
machine path or anything credential-shaped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "python" / "switchgear" / "__main__.py"
MOCK = ROOT / "tests" / "helpers" / "mock_provider.py"
EXAMPLE = ROOT / "project-profiles" / "example.json"
MAKE_REPO = ROOT / "tests" / "helpers" / "make-synthetic-repo"
PYTHON = "/usr/bin/python3"
GIT = "/usr/bin/git"

#: Bump when the MEANING of captured content changes. A new pack version is a
#: new directory, never an in-place reinterpretation of an old one -- a consumer
#: pins the directory it decoded.
PACK_VERSION = 1
PACK_NAME = f"adapter-v1-contract-fixtures.v{PACK_VERSION}"

#: One placeholder, used for every absolute prefix, so a decoder still sees the
#: path STRUCTURE (and therefore the version-bearing artifact filename) while no
#: machine path survives.
PLACEHOLDER = "<ABSOLUTE_PATH>"

#: Deliberately not normalised, and a consumer must not pin them: job ids,
#: timestamps, digests and costs are real values from a real run and change on
#: every re-capture. The freshness rule in AGENTS.md actively asks for
#: re-capture, so a fixture test that pinned them would break by design.
VOLATILE_NOTE = (
    "job_id, timestamps, digests, durations and costs are real values from the "
    "capture run and CHANGE on every re-capture. Assert the contract, never "
    "these values."
)

CREDENTIAL_SHAPED = re.compile(
    r"(auth\.json|credentials?/|\.pem\b|BEGIN [A-Z ]*PRIVATE KEY|"
    r"sk-[A-Za-z0-9]{16,}|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|"
    r"[\"']?(?:access_token|refresh_token|api_key|secret)[\"']?\s*[:=]|"
    r"Authorization\s*:\s*\S+)",
    re.I,
)

# A slash after whitespace/punctuation starts a POSIX absolute path. Slashes in
# model ids such as `pool/model` are preceded by a word character and do not.
# The placeholder's `>` is excluded so its preserved suffix stays admissible.
ABSOLUTE_PATH_SHAPED = re.compile(
    r"(?<![A-Za-z0-9_.<>/-])/(?!/)(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?"
)

COMPLETE_JOB_FILES = frozenset({
    "result.json", "runner.json", "events.v2.jsonl", "jobs-row.json",
    "logs-digest.json", "logs-normalized.json",
})

# This is the one declaration shared by capture and verification. Letting the
# generator and pack test each carry their own list would allow the same missing
# scenario to disappear from both and make the fixture gate vacuous again.
EXPECTED_SCENARIO_FILES = {
    scenario: COMPLETE_JOB_FILES
    for scenario in (
        "ok", "empty_final_text_with_change", "awaiting_external_review",
        "provider_error", "needs_input", "dirty",
    )
}
EXPECTED_SCENARIO_FILES["crashed_launch_only"] = frozenset({
    "runner.json", "jobs-row.json",
})
EXPECTED_ROOT_FILES = frozenset({
    "MANIFEST.json", "gc_plan.json", "gc_applied.json",
})
EXPECTED_MANIFEST_SCENARIOS = frozenset(EXPECTED_SCENARIO_FILES) | frozenset(
    name for name in EXPECTED_ROOT_FILES if name != "MANIFEST.json"
)


def capture_safety_findings(text: str) -> list[str]:
    """Machine-path and credential shapes forbidden in a published pack."""
    findings = [f"absolute path {match.group(0)!r}"
                for match in ABSOLUTE_PATH_SHAPED.finditer(text)]
    findings.extend(
        f"credential-shaped {match.group(0)!r}"
        for match in CREDENTIAL_SHAPED.finditer(text)
    )
    return findings


class Capture:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-fixtures-"))
        self.state = self.tmp / "state"
        self.profile = self.tmp / "profile.json"
        self.budget = self.tmp / "budget.json"
        vals = dict(
            line.split("=", 1)
            for line in subprocess.run(
                ["bash", str(MAKE_REPO), str(self.tmp / "syn")],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            if "=" in line
        )
        self.primary = Path(vals["PRIMARY"])
        self.wt = Path(vals["WT"])
        self.wt2 = Path(vals["WT2"])
        profile = json.loads(EXAMPLE.read_text())
        profile.update(write_enabled=True, commands={"probe": True})
        self.profile.write_text(json.dumps(profile, indent=2) + "\n")
        self._run("state", "provision", str(self.state))

    # ---- driving the real CLI ---------------------------------------------

    def _run(self, *args: str, env: dict[str, str] | None = None,
             timeout: int = 180) -> subprocess.CompletedProcess:
        base = os.environ.copy()
        for key in list(base):
            if key.startswith("OPENCODE_"):
                base.pop(key, None)
        base.pop("SWITCHGEAR_ALLOW_LIVE_PROVIDER", None)
        base["SWITCHGEAR_GC_MIN_AGE_S"] = "0"
        if env:
            base.update(env)
        return subprocess.run(
            [PYTHON, str(MAIN), "--profile", str(self.profile),
             "--state", str(self.state), "--provider", str(MOCK), *args],
            capture_output=True, text=True, env=base, timeout=timeout,
        )

    def _envelope(self, cwd: Path, mode: str, role: str) -> str:
        path = self.tmp / f"envelope-{mode}-{role}-{cwd.name}.json"
        path.write_text(json.dumps({
            "goal": "capture a contract fixture",
            "context": "hermetic fixture capture against the committed mock",
            "constraints": [], "done_when": ["the record is written"],
            "non_goals": [], "risk_threshold": "incorrect behavior only",
            "stop_condition": "stop when the record exists",
            "expansion_rule": "report and wait",
            "mode": mode, "role": role, "cwd": str(cwd),
        }, indent=2) + "\n")
        return str(path)

    def _lease(self, wt: Path) -> str:
        out = self._run("--json", "lease", "acquire", "--dir", str(wt),
                        "--mode", "bounded-write")
        return json.loads(out.stdout)["lease"]

    # ---- normalisation -----------------------------------------------------

    def _scrub(self, value: Any) -> Any:
        """Replace absolute prefixes; preserve every semantic field verbatim."""
        if isinstance(value, str):
            for prefix in (str(self.tmp.resolve()), str(self.tmp),
                           str(ROOT.resolve()), str(ROOT)):
                value = value.replace(prefix, PLACEHOLDER)
            return value
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        return value

    def _write(self, scenario: str, name: str, payload: Any) -> None:
        target = self.out / scenario if scenario else self.out
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(
            json.dumps(self._scrub(payload), indent=2, sort_keys=True) + "\n"
        )

    def _write_jsonl(self, scenario: str, name: str, rows: list[Any]) -> None:
        target = self.out / scenario
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(
            "".join(json.dumps(self._scrub(r), separators=(",", ":")) + "\n"
                    for r in rows)
        )

    # ---- one job -> one scenario directory ---------------------------------

    def _capture_job(self, scenario: str, job_id: str, *, worktree: Path) -> None:
        expected = EXPECTED_SCENARIO_FILES[scenario]
        jd = self.state / "jobs" / job_id
        res = jd / "result.json"
        if "result.json" in expected:
            if not res.is_file():
                raise RuntimeError(
                    f"scenario {scenario} produced no required result.json"
                )
            self._write(scenario, "result.json", json.loads(res.read_text()))
        elif res.is_file():
            raise RuntimeError(
                f"scenario {scenario} unexpectedly produced result.json"
            )
        runner = jd / "runner.json"
        if not runner.is_file():
            raise RuntimeError(
                f"scenario {scenario} produced no required runner.json"
            )
        self._write(scenario, "runner.json", json.loads(runner.read_text()))
        norms = sorted(jd.glob("evidence/events.v*.jsonl"))
        expected_norms = {name for name in expected if name.startswith("events.v")}
        if {norm.name for norm in norms} != expected_norms:
            raise RuntimeError(
                f"scenario {scenario} normalized artifacts were "
                f"{sorted(norm.name for norm in norms)}, expected "
                f"{sorted(expected_norms)}"
            )
        for norm in norms:
            self._write_jsonl(scenario, norm.name, [
                json.loads(line) for line in norm.read_text().splitlines()
                if line.strip()
            ])
        rows = json.loads(self._run("--json", "jobs", "--worktree",
                                    str(worktree), "--all").stdout)
        row = next((r for r in rows["jobs"] if r["job_id"] == job_id), None)
        if row is None:
            raise RuntimeError(
                f"scenario {scenario} produced no required jobs --json row"
            )
        self._write(scenario, "jobs-row.json", row)
        if "logs-digest.json" in expected:
            digest = self._run("--json", "logs", job_id)
            if digest.returncode != 0:
                raise RuntimeError(
                    f"scenario {scenario} digest projection failed: "
                    f"{digest.stderr.strip()}"
                )
            self._write(scenario, "logs-digest.json", json.loads(digest.stdout))
            normalized = self._run("--json", "logs", job_id,
                                   "--format", "normalized")
            if normalized.returncode != 0:
                raise RuntimeError(
                    f"scenario {scenario} normalized projection failed: "
                    f"{normalized.stderr.strip()}"
                )
            self._write(scenario, "logs-normalized.json",
                        json.loads(normalized.stdout))

        actual = {path.name for path in (self.out / scenario).iterdir()
                  if path.is_file()}
        if actual != set(expected):
            raise RuntimeError(
                f"scenario {scenario} captured {sorted(actual)}, expected "
                f"{sorted(expected)}"
            )

    # ---- the scenarios -----------------------------------------------------

    def scenario_ok(self) -> None:
        """Clean bounded write that also emits closing text."""
        token = self._lease(self.wt)
        proc = self._run("--json", "write", str(self.wt), "implement",
                         "--envelope", self._envelope(self.wt, "bounded-write", "implement"),
                         "--token", token,
                         env={"SWITCHGEAR_WRITE": "1",
                              "SWITCHGEAR_MOCK_BEHAVIOR": "edit-inside-verbose"})
        rec = json.loads(proc.stdout)
        assert rec["status"] == "awaiting_review", rec["status"]
        self._capture_job("ok", rec["job_id"], worktree=self.wt)
        return rec["job_id"]

    def scenario_completed_empty_with_change(self) -> None:
        """The combination a hand-written fixture would have destroyed."""
        token = self._lease(self.wt)
        proc = self._run("--json", "write", str(self.wt), "implement",
                         "--envelope", self._envelope(self.wt, "bounded-write", "implement"),
                         "--token", token,
                         env={"SWITCHGEAR_WRITE": "1",
                              "SWITCHGEAR_MOCK_BEHAVIOR": "edit-inside"})
        rec = json.loads(proc.stdout)
        assert rec["status"] == "awaiting_review", rec["status"]
        assert rec["freeze"]["changed_files"], "the point of this scenario"
        self._capture_job("empty_final_text_with_change", rec["job_id"],
                          worktree=self.wt)

    def scenario_external(self) -> None:
        """Operator set acceptance=external: same freeze, different authority."""
        self.budget.write_text(json.dumps({"acceptance": "external"}) + "\n")
        token = self._lease(self.wt2)
        proc = self._run("--json", "write", str(self.wt2), "implement",
                         "--envelope", self._envelope(self.wt2, "bounded-write", "implement"),
                         "--token", token,
                         env={"SWITCHGEAR_WRITE": "1",
                              "SWITCHGEAR_BUDGET_FILE": str(self.budget),
                              "SWITCHGEAR_MOCK_BEHAVIOR": "edit-inside-verbose"})
        rec = json.loads(proc.stdout)
        assert rec["status"] == "awaiting_external_review", rec["status"]
        assert rec["freeze"] is not None, "P3: external writes carry the freeze"
        self._capture_job("awaiting_external_review", rec["job_id"],
                          worktree=self.wt2)
        return rec["job_id"]

    def scenario_provider_error(self) -> None:
        proc = self._run("--json", "scout", str(self.primary), "probe",
                         env={"SWITCHGEAR_MOCK_BEHAVIOR": "exit-nonzero"})
        rec = json.loads(proc.stdout)
        assert rec["status"] == "provider_error", rec["status"]
        self._capture_job("provider_error", rec["job_id"], worktree=self.primary)

    def scenario_needs_input(self) -> None:
        """Event vocabulary and record vocabulary disagree, both correctly."""
        proc = self._run("--json", "scout", str(self.primary), "probe",
                         env={"SWITCHGEAR_MOCK_BEHAVIOR": "needs-input"})
        rec = json.loads(proc.stdout)
        assert rec["status"] == "provider_error", rec["status"]
        self._capture_job("needs_input", rec["job_id"], worktree=self.primary)

    def scenario_dirty(self) -> None:
        """Integrity changed DURING the job -- reproduced, not simulated.

        The worker cannot do this: a readonly worktree is bind-mounted read
        only. So the fixture reproduces the real-world case the `dirty` outcome
        exists to report -- something on the HOST moved the worktree's git
        identity while the job was running.
        """
        proc = self._run("--json", "scout", str(self.wt2), "probe", "--background",
                         env={"SWITCHGEAR_MOCK_BEHAVIOR": "slow-stream",
                              "SWITCHGEAR_MOCK_EXTRA": "6"})
        job_id = json.loads(proc.stdout)["job_id"]
        events = self.state / "jobs" / job_id / "evidence" / "events.jsonl"
        deadline = time.time() + 30
        while time.time() < deadline and not (
                events.is_file() and events.stat().st_size > 0):
            time.sleep(0.05)
        subprocess.run([GIT, "-C", str(self.wt2), "commit", "--allow-empty",
                        "-m", "host-side commit during a readonly job"],
                       check=True, capture_output=True)
        self._run("wait", job_id, "--timeout", "120")
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        assert rec["status"] == "dirty", rec["status"]
        self._capture_job("dirty", job_id, worktree=self.wt2)

    def scenario_crashed_launch_only(self) -> None:
        """A launch that died before writing a result: attribution, no record."""
        proc = self._run("--json", "scout", str(self.primary), "probe", "--background",
                         env={"SWITCHGEAR_MOCK_BEHAVIOR": "hang"})
        job_id = json.loads(proc.stdout)["job_id"]
        runner = self.state / "jobs" / job_id / "runner.json"
        deadline = time.time() + 30
        while time.time() < deadline and not runner.is_file():
            time.sleep(0.05)
        rec = json.loads(runner.read_text())
        os.kill(int(rec["pid"]), 9)
        deadline = time.time() + 30
        while time.time() < deadline:
            rows = json.loads(self._run("--json", "jobs", "--worktree",
                                        str(self.primary), "--all").stdout)
            row = next((r for r in rows["jobs"] if r["job_id"] == job_id), None)
            if row and row["state"] == "died":
                break
            time.sleep(0.2)
        assert row and row["state"] == "died", row
        assert not (self.state / "jobs" / job_id / "result.json").is_file()
        self._capture_job("crashed_launch_only", job_id, worktree=self.primary)

    def scenario_gc(self, protected_external: str) -> None:
        """A dry-run plan and an applied run: different shapes, both needed."""
        plan = self._run("--json", "gc", "--older-than", "0s", "--include-sessions")
        planned = json.loads(plan.stdout)
        assert any(p["job_id"] == protected_external for p in planned["protected"]), \
            "the external-review job must be protected"
        self._write("", "gc_plan.json", planned)
        applied = self._run("--json", "gc", "--older-than", "0s", "--yes",
                            "--include-sessions")
        self._write("", "gc_applied.json", json.loads(applied.stdout))

    # ---- pack metadata -----------------------------------------------------

    def manifest(self, scenarios: dict[str, str]) -> None:
        from switchgear.job import NORMALIZED_EVENTS_VERSION
        from switchgear.cli import DIGEST_VERSION

        if set(scenarios) != set(EXPECTED_MANIFEST_SCENARIOS):
            raise RuntimeError(
                f"manifest scenario set {sorted(scenarios)}, expected the "
                f"declared set {sorted(EXPECTED_MANIFEST_SCENARIOS)}"
            )

        commit = subprocess.run(
            [GIT, "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        self._write("", "MANIFEST.json", {
            "pack": PACK_NAME,
            "pack_version": PACK_VERSION,
            "_what": (
                "Durable records exactly as the rail wrote them, captured from "
                "real hermetic runs of the committed mock provider through the "
                "real code paths. Never hand-authored."
            ),
            "_regenerate": "python3 tests/helpers/capture_contract_fixtures.py",
            "captured_from_commit": commit,
            "contract_versions": {
                "result_schema_version": 1,
                "normalized_events_version": NORMALIZED_EVENTS_VERSION,
                "digest_version": DIGEST_VERSION,
            },
            "normalization": {
                "absolute_paths": (
                    f"every absolute prefix replaced by the single placeholder "
                    f"{PLACEHOLDER}, preserving the path structure after it so a "
                    f"decoder still sees the version-bearing artifact filename"
                ),
                "credentials": "none captured; the pack is asserted free of them",
                "semantic_fields": "preserved verbatim; never reconciled by hand",
            },
            "volatile_values": VOLATILE_NOTE,
            "scenarios": scenarios,
        })

    def assert_clean(self) -> None:
        """Refuse to ship a pack with a machine path or a credential in it."""
        bad: list[str] = []
        for path in sorted(self.out.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            bad.extend(
                f"{path.relative_to(self.out)}: {finding}"
                for finding in capture_safety_findings(text)
            )
        if bad:
            raise SystemExit("refusing to write the pack:\n  " + "\n  ".join(bad))

    def assert_complete(self) -> None:
        actual_dirs = {path.name for path in self.out.iterdir() if path.is_dir()}
        if actual_dirs != set(EXPECTED_SCENARIO_FILES):
            raise RuntimeError(
                f"captured scenario set {sorted(actual_dirs)}, expected "
                f"{sorted(EXPECTED_SCENARIO_FILES)}"
            )
        actual_root_files = {
            path.name for path in self.out.iterdir() if path.is_file()
        }
        if actual_root_files != set(EXPECTED_ROOT_FILES):
            raise RuntimeError(
                f"captured root files {sorted(actual_root_files)}, expected "
                f"{sorted(EXPECTED_ROOT_FILES)}"
            )

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / PACK_NAME))
    ns = ap.parse_args()
    out = Path(ns.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    sys.path.insert(0, str(ROOT / "python"))
    cap = Capture(out)
    try:
        cap.scenario_ok()
        cap.scenario_completed_empty_with_change()
        external = cap.scenario_external()
        cap.scenario_provider_error()
        cap.scenario_needs_input()
        cap.scenario_dirty()
        cap.scenario_crashed_launch_only()
        cap.scenario_gc(external)
        cap.manifest({
            "ok": "clean bounded write, closing text present -> awaiting_review",
            "empty_final_text_with_change": (
                "successful completion with NO closing assistant text beside a "
                "real frozen change: finished.status=completed, "
                "final_text_state=empty, change.state=frozen. Change presence "
                "comes from change/freeze, never from the terminal status."
            ),
            "awaiting_external_review": (
                "operator acceptance=external: identical freeze and evidence, "
                "exit 0, promote refused, the caller's gate decides"
            ),
            "provider_error": "the provider exited non-zero after a well-formed handoff",
            "needs_input": (
                "the normalized terminal event says needs_input while "
                "result.json says provider_error -- two vocabularies "
                "disagreeing legitimately, captured rather than reconciled"
            ),
            "dirty": (
                "worktree git identity moved on the HOST during a readonly job; "
                "exit 2, nothing promoted"
            ),
            "crashed_launch_only": (
                "runner.json with no result.json: the jobs row reads state=died "
                "and still carries launch attribution"
            ),
            "gc_plan.json": "dry run; protected entries each carry their reason",
            "gc_applied.json": (
                "the same selection applied -- the only shape carrying "
                "launch_artifacts_removed, which gc.plan() cannot produce"
            ),
        })
        cap.assert_complete()
        cap.assert_clean()
    finally:
        cap.close()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
