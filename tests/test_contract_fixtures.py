#!/usr/bin/env python3
"""The committed contract fixture pack, and the `--json` shape it documents.

Two things this guards, both of which had already gone wrong:

* The pack must stay internally consistent and free of machine paths and
  credentials. It is the artifact an external consumer pins, and it ships in
  the repository, so a bad capture is published rather than merely wrong.

* `docs/INTEGRATION.md` publishes the `--json` object as "a stable object" and
  then lists its keys. That list had drifted by TWELVE keys -- `execution`,
  `integrity_outcome`, `change`, `acceptance`, `harness`, `provider`, `started`,
  `finished`, `effort`, `queued_s`, `delegation` and `resumed` were all emitted
  and none was documented. A published key set that nobody checks is a
  hand-maintained list, and this repository has already learned what those are
  worth.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "tests" / "fixtures" / "adapter-v1-contract-fixtures.v1"
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

from capture_contract_fixtures import (  # noqa: E402
    Capture,
    EXPECTED_ROOT_FILES,
    EXPECTED_MANIFEST_SCENARIOS,
    EXPECTED_SCENARIO_FILES,
    PLACEHOLDER,
    capture_safety_findings,
)


class ContractFixturePack(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((PACK / "MANIFEST.json").read_text())

    def test_the_pack_exists_and_declares_the_versions_it_captured(self):
        """A consumer pins a pack; it must say which contract it froze."""
        versions = self.manifest["contract_versions"]
        from switchgear.cli import DIGEST_VERSION
        from switchgear.job import NORMALIZED_EVENTS_VERSION

        self.assertEqual(versions["normalized_events_version"],
                         NORMALIZED_EVENTS_VERSION,
                         "the pack was captured against a different event vocabulary")
        self.assertEqual(versions["digest_version"], DIGEST_VERSION,
                         "the pack was captured against a different digest format")

    def test_no_machine_path_or_credential_survived_capture(self):
        """The pack ships. `tests/fixtures/` is exempt from the machine-path
        policy gate -- deliberately, since captured provider output is evidence
        -- so this is the only thing standing between a developer's home
        directory and a published fixture."""
        offenders = []
        for path in sorted(PACK.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            offenders.extend(
                f"{path.relative_to(PACK)}: {finding}"
                for finding in capture_safety_findings(text)
            )
        self.assertEqual(offenders, [], "the pack carries machine paths or credentials")

    def test_the_safety_scan_constructs_every_broadened_exposure(self):
        hostile = (
            '/ /var/lib/private /mnt/customer/data /opt/vendor/config '
            '{"access_token":"value","refresh_token":"value",'
            '"api_key":"value","secret":"value"} '
            'Authorization: Bearer-value'
        )
        findings = capture_safety_findings(hostile)
        for needle in (
            "absolute path '/'", "/var/lib/private", "/mnt/customer/data",
            "/opt/vendor/config",
            "access_token", "refresh_token", "api_key", "secret",
            "Authorization:",
        ):
            self.assertTrue(
                any(needle in finding for finding in findings),
                f"fixture safety scan missed constructed exposure {needle}",
            )
        self.assertEqual(
            capture_safety_findings(f'{PLACEHOLDER}/state/jobs/id'), [],
            "the documented absolute-path placeholder was rejected",
        )

    def test_declared_scenarios_and_files_match_the_pack_exactly(self):
        actual_dirs = {path.name for path in PACK.iterdir() if path.is_dir()}
        self.assertEqual(
            actual_dirs, set(EXPECTED_SCENARIO_FILES),
            "committed fixture scenarios differ from the one declared set",
        )
        actual_root_files = {path.name for path in PACK.iterdir() if path.is_file()}
        self.assertEqual(
            actual_root_files, set(EXPECTED_ROOT_FILES),
            "committed fixture root files differ from the one declared set",
        )
        self.assertEqual(
            set(self.manifest["scenarios"]), set(EXPECTED_MANIFEST_SCENARIOS),
            "manifest scenarios differ from the one declared scenario set",
        )
        for scenario, expected in EXPECTED_SCENARIO_FILES.items():
            actual = {path.name for path in (PACK / scenario).iterdir()
                      if path.is_file()}
            self.assertEqual(
                actual, set(expected),
                f"fixture scenario {scenario} is partial or has undeclared files",
            )

    def test_generator_refuses_every_partial_job_artifact_path(self):
        """The old generator skipped each missing artifact independently, so a
        successful run could publish a partial scenario without one error."""
        cases = {
            "result.json": "no required result.json",
            "runner.json": "no required runner.json",
            "events.v2.jsonl": "normalized artifacts were",
            "jobs-row.json": "no required jobs --json row",
            "logs-digest.json": "digest projection failed",
            "logs-normalized.json": "normalized projection failed",
        }
        for missing, expected_error in cases.items():
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                cap = Capture.__new__(Capture)
                cap.out = base / "out"
                cap.state = base / "state"
                cap.tmp = base
                job_id = "00000000-0000-4000-8000-000000000099"
                jd = cap.state / "jobs" / job_id
                (jd / "evidence").mkdir(parents=True)
                if missing != "result.json":
                    (jd / "result.json").write_text("{}")
                if missing != "runner.json":
                    (jd / "runner.json").write_text("{}")
                if missing != "events.v2.jsonl":
                    (jd / "evidence" / "events.v2.jsonl").write_text(
                        '{"event":"finished"}\n'
                    )

                def fake_run(*args, **_kwargs):
                    if "jobs" in args:
                        rows = [] if missing == "jobs-row.json" else [
                            {"job_id": job_id}
                        ]
                        return types.SimpleNamespace(
                            returncode=0, stdout=json.dumps({"jobs": rows}), stderr=""
                        )
                    if "normalized" in args:
                        if missing == "logs-normalized.json":
                            return types.SimpleNamespace(
                                returncode=1, stdout="",
                                stderr="constructed normalized projection failure",
                            )
                        return types.SimpleNamespace(
                            returncode=0, stdout="{}", stderr=""
                        )
                    if missing == "logs-digest.json":
                        return types.SimpleNamespace(
                            returncode=1, stdout="", stderr="constructed projection failure"
                        )
                    return types.SimpleNamespace(
                        returncode=0, stdout="{}", stderr=""
                    )

                cap._run = fake_run
                with self.assertRaisesRegex(
                    RuntimeError, expected_error,
                    msg=f"fixture generator silently accepted missing {missing}",
                ):
                    cap._capture_job(
                        "provider_error", job_id, worktree=Path("/synthetic")
                    )

    def test_every_captured_result_validates_against_the_shipped_schema(self):
        """Captured records are the contract. If one no longer validates, either
        the capture is stale or the schema moved under it."""
        from switchgear.schema import validate

        expected_results = {
            PACK / scenario / "result.json"
            for scenario, files in EXPECTED_SCENARIO_FILES.items()
            if "result.json" in files
        }
        actual_results = set(PACK.glob("*/result.json"))
        self.assertEqual(
            actual_results, expected_results,
            "the pack's result set does not cover every declared result scenario",
        )
        for result in sorted(actual_results):
            record = json.loads(result.read_text())
            validate(record, "result.schema.json")

    def test_the_empty_final_text_scenario_still_carries_a_real_change(self):
        """The whole reason fixtures are captured and never hand-written.

        A successful run that emitted no closing assistant text, beside a frozen
        change to a real file. An author writing this by hand would "correct"
        one of the two and destroy the signal. If a later change ever made the
        terminal status mean diff-emptiness, this breaks -- which is the
        regression worth guarding.
        """
        scenario = PACK / "empty_final_text_with_change"
        record = json.loads((scenario / "result.json").read_text())
        finished = self._finished(scenario / "events.v2.jsonl")
        self.assertEqual(
            (finished["status"], finished["final_text_state"],
             record["change"]["state"], bool(record["freeze"]["changed_files"])),
            ("completed", "empty", "frozen", True),
        )

    def test_needs_input_keeps_the_two_vocabularies_apart(self):
        """The normalized terminal event and the record legitimately disagree:
        `needs_input` parks the work back to the operator, while the record says
        the run produced no result. Captured rather than reconciled."""
        scenario = PACK / "needs_input"
        self.assertEqual(
            json.loads((scenario / "result.json").read_text())["status"],
            "provider_error")
        self.assertEqual(self._finished(scenario / "events.v2.jsonl")["status"],
                         "needs_input")

    def test_the_dirty_fixture_pins_the_two_meanings_of_exit(self):
        """`exit` on the record is the PROVIDER's; the CLI's comes from the
        status. A dirty job carries exit 0 while the CLI exits 2 and nothing was
        promoted, so a consumer reading the record's exit against the published
        exit table reads a refused job as a success."""
        from switchgear import jobstate

        record = json.loads((PACK / "dirty" / "result.json").read_text())
        self.assertEqual(record["status"], "dirty")
        self.assertEqual(record["exit"], 0, "the provider itself exited cleanly")
        self.assertEqual(jobstate.exit_code_for(record["status"]), 2,
                         "the CLI must still report dirty as 2")

    def test_the_crashed_launch_keeps_attribution_without_a_record(self):
        scenario = PACK / "crashed_launch_only"
        self.assertFalse((scenario / "result.json").exists(),
                         "the scenario is defined by having no result record")
        row = json.loads((scenario / "jobs-row.json").read_text())
        self.assertEqual(row["state"], "died")
        self.assertTrue(row.get("harness"), "attribution must survive the crash")

    def test_the_gc_pair_covers_both_shapes(self):
        """`launch_artifacts_removed` exists only in an applied run; a decoder
        needs both, which is why both are captured."""
        plan = json.loads((PACK / "gc_plan.json").read_text())
        applied = json.loads((PACK / "gc_applied.json").read_text())
        self.assertIn("sessions_skipped", plan)
        self.assertNotIn("launch_artifacts_removed", plan)
        self.assertIn("launch_artifacts_removed", applied)
        self.assertTrue(
            any(p["state"] == "awaiting_external_review" for p in plan["protected"]),
            "the protected external-review job is the point of the plan fixture")
        for entry in plan["protected"]:
            self.assertTrue(entry.get("reason"),
                            "every protected entry must say which rule kept it")

    def test_the_digest_fixtures_carry_their_format_version(self):
        digests = [
            PACK / scenario / "logs-digest.json"
            for scenario, files in EXPECTED_SCENARIO_FILES.items()
            if "logs-digest.json" in files
        ]
        self.assertTrue(digests, "the declared fixture contract has no digests")
        for digest in sorted(digests):
            envelope = json.loads(digest.read_text())
            self.assertIn("digest_v", envelope, f"{digest} has no digest version")
            self.assertIn("events_v", envelope, f"{digest} names no event vocabulary")
            for event in envelope["events"]:
                self.assertIn("digest_v", event,
                              f"{digest} has an unversioned digest line")

    def _finished(self, path: Path) -> dict:
        for line in path.read_text().splitlines():
            if line.strip():
                event = json.loads(line)
                if event["event"] == "finished":
                    return event
        self.fail(f"{path} has no finished event")


class PublishedJsonShape(unittest.TestCase):
    """The `--json` key set must equal what INTEGRATION.md publishes.

    One of the four test-coverage debts the stabilization plan recorded as
    accepted. It is paid here because the published list was found to be wrong
    by twelve keys -- accepted debt is not the same as harmless debt.
    """

    def test_documented_keys_equal_emitted_keys(self):
        import contextlib
        import io

        import switchgear.cli as cli

        record = json.loads((PACK / "ok" / "result.json").read_text())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_job(record, as_json=True)
        emitted = set(json.loads(buffer.getvalue()).keys())

        doc = (ROOT / "docs" / "INTEGRATION.md").read_text()
        block = doc.split("`--json` prints a stable object", 1)[1].split("```")[1]
        # `artifacts{...}` is documented with its nested keys inline. Those names
        # are not top level and must not be compared as if they were.
        nested = {"events", "events_normalized", "events_normalized_version",
                  "stderr", "handoff"}
        documented = {
            token.strip().split("{")[0].strip().rstrip("}")
            for token in re.split(r"[,\n]", block)
        }
        documented = {d for d in documented if d} - nested

        self.assertEqual(
            documented, emitted,
            "docs/INTEGRATION.md and cli._print_job disagree about the "
            "published --json key set",
        )


class PublishedBehaviorClaims(unittest.TestCase):
    def test_fixture_scenario_shape_claim_stays_true(self):
        fixture_doc = (ROOT / "docs" / "ADAPTER-V1-CONTRACT-FIXTURES.md").read_text()
        self.assertIn(
            "Every completed-job scenario holds", fixture_doc,
            "fixture doc again claims crashed_launch_only has completed artifacts",
        )
        self.assertIn(
            "deliberately holds only `runner.json` and its", fixture_doc,
            "fixture doc does not state the launch-only scenario's real shape",
        )

    def test_admission_order_claim_stays_true(self):
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
        compact = re.sub(r"\s+", " ", architecture)
        self.assertIn(
            "Admission: budget, concurrency slot, job directory and `runner.json`, "
            "disk headroom, exclusive lease.",
            compact,
            "architecture admission order differs from run_job's measured order",
        )

    def test_event_version_readback_claim_stays_true(self):
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
        for claim in (
            "result's recorded version",
            "start-time `runner.json` version",
            "Records predating both stamps default to v1",
            "neither record uses the installed version",
            "corrupt/non-object result is refused",
        ):
            self.assertIn(
                claim, architecture,
                f"architecture omits event-version read-back rule: {claim}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
