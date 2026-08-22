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

import hashlib
import json
import re
import subprocess
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
    capture_provenance,
    capture_safety_findings,
)


class ContractFixturePack(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((PACK / "MANIFEST.json").read_text())

    def test_the_pack_exists_and_declares_the_versions_it_captured(self):
        """A consumer pins a pack; it must say which contract it froze."""
        versions = self.manifest["contract_versions"]
        from switchgear.cli import DIGEST_VERSION
        from switchgear.job import NORMALIZED_EVENTS_VERSION, SCHEMA_VERSION

        self.assertEqual(versions["normalized_events_version"],
                         NORMALIZED_EVENTS_VERSION,
                         "the pack was captured against a different event vocabulary")
        self.assertEqual(versions["result_schema_version"], SCHEMA_VERSION,
                         "the pack was captured against a different result schema")
        self.assertEqual(versions["digest_version"], DIGEST_VERSION,
                         "the pack was captured against a different digest format")

    def test_generator_records_honest_capture_provenance(self):
        from unittest import mock

        current = "00000000000000000000000000000000000000aa"
        with mock.patch(
            "capture_contract_fixtures.subprocess.run",
            side_effect=(
                types.SimpleNamespace(stdout=current + "\n"),
                types.SimpleNamespace(stdout=" M generator.py\n"),
            ),
        ) as run:
            provenance = capture_provenance()
        self.assertEqual(
            [call.args[0][3:] for call in run.call_args_list],
            [["rev-parse", "HEAD"],
             ["status", "--porcelain", "--untracked-files=all",
              "--", "python", "bin", "project-profiles", "tests/helpers"]],
            "fixture generator did not measure HEAD and capture-source dirtiness "
            "over the scope it publishes",
        )

        cap = Capture.__new__(Capture)
        written = {}
        cap.provenance = provenance
        cap._write = lambda scenario, name, payload: written.update({name: payload})
        cap.manifest({name: "scenario" for name in EXPECTED_MANIFEST_SCENARIOS})
        manifest = written["MANIFEST.json"]
        self.assertEqual(
            manifest["captured_from_commit"], current,
            "fixture generator did not record rev-parse HEAD",
        )
        self.assertIsInstance(
            manifest.get("captured_from_sources_dirty"), bool,
            "fixture generator omitted whether the capture SOURCES were dirty",
        )
        self.assertEqual(
            manifest.get("captured_from_sources_scope"),
            ["python", "bin", "project-profiles", "tests/helpers"],
            "the manifest does not say which paths its dirtiness fact covers",
        )
        self.assertEqual(
            manifest.get("capture_generator_sha256"),
            hashlib.sha256(
                (ROOT / "tests/helpers/capture_contract_fixtures.py").read_bytes()
            ).hexdigest(),
            "fixture generator digest does not identify the shipped helper",
        )
        occurrences = []
        for path in PACK.rglob("*.json"):
            if "captured_from_commit" in path.read_text():
                occurrences.append(path.relative_to(PACK).as_posix())
        self.assertEqual(
            occurrences, ["MANIFEST.json"],
            "fixture pack pins captured_from_commit outside its manifest",
        )

    def _assert_clean_capture_commit_contains_shipped_generator(self, manifest):
        dirty = manifest.get("captured_from_sources_dirty")
        self.assertIsInstance(
            dirty, bool,
            "fixture manifest does not say whether its producing tree was dirty",
        )
        generator = ROOT / "tests/helpers/capture_contract_fixtures.py"
        self.assertEqual(
            manifest.get("capture_generator_sha256"),
            hashlib.sha256(generator.read_bytes()).hexdigest(),
            "fixture manifest does not identify the shipped capture generator",
        )
        if dirty:
            return
        commit = manifest["captured_from_commit"]
        shown = subprocess.run(
            ["/usr/bin/git", "-C", str(ROOT), "show",
             f"{commit}:tests/helpers/capture_contract_fixtures.py"],
            capture_output=True,
        )
        self.assertEqual(
            shown.returncode, 0,
            "fixture manifest claims a clean capture but its commit does not "
            "contain the capture generator",
        )
        self.assertEqual(
            shown.stdout, generator.read_bytes(),
            "fixture manifest claims a clean capture whose recorded commit does "
            "not contain the generator as shipped",
        )

    def test_clean_capture_commit_contains_the_shipped_generator(self):
        self._assert_clean_capture_commit_contains_shipped_generator(self.manifest)

    def test_clean_provenance_guard_constructs_a_generator_mismatch(self):
        from unittest import mock

        claimed = dict(
            self.manifest,
            captured_from_sources_dirty=False,
            captured_from_commit="00000000000000000000000000000000000000bb",
            capture_generator_sha256=hashlib.sha256(
                (ROOT / "tests/helpers/capture_contract_fixtures.py").read_bytes()
            ).hexdigest(),
        )
        with mock.patch(
            "subprocess.run",
            return_value=types.SimpleNamespace(returncode=0, stdout=b"older helper"),
        ), self.assertRaises(AssertionError) as ctx:
            self._assert_clean_capture_commit_contains_shipped_generator(claimed)
        self.assertIn(
            "claims a clean capture whose recorded commit does not contain the "
            "generator as shipped",
            str(ctx.exception),
        )

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

    def test_every_result_names_its_declared_versioned_artifact(self):
        """A derived v1 record named the unused v2 artifact beside real v1."""
        for scenario, files in EXPECTED_SCENARIO_FILES.items():
            if "result.json" not in files:
                continue
            with self.subTest(scenario=scenario):
                directory = PACK / scenario
                record = json.loads((directory / "result.json").read_text())
                artifacts = record["artifacts"]
                version = artifacts["events_normalized_version"]
                named = Path(artifacts["events_normalized"]).name
                expected = f"events.v{version}.jsonl"
                self.assertEqual(
                    named, expected,
                    f"fixture {scenario} mislabels normalized artifact version "
                    f"{version} as {named}",
                )
                self.assertTrue(
                    (directory / named).is_file(),
                    f"fixture {scenario} names missing normalized artifact {named}",
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
        from switchgear.schema import validate_result

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
            validate_result(record)

    def test_v1_and_v2_results_conform_only_to_their_own_closed_schema(self):
        """The v1/v2 split must not be cosmetic or relaxed open."""
        from switchgear.errors import Refuse
        from switchgear.schema import validate, validate_result

        v1 = json.loads((PACK / "legacy_v1" / "result.json").read_text())
        v2 = json.loads((PACK / "ok" / "result.json").read_text())

        validate(v1, "result-v1.schema.json")
        validate(v2, "result.schema.json")
        validate_result(v1)
        validate_result(v2)
        with self.assertRaises(
            Refuse, msg="strict v2 schema accepted the historical v1 fixture"
        ):
            validate(v1, "result.schema.json")
        with self.assertRaises(
            Refuse, msg="strict v1 schema accepted the current v2 fixture"
        ):
            validate(v2, "result-v1.schema.json")

        absent = dict(v1)
        absent.pop("schema_version")
        validate_result(absent)
        invalid = dict(v2, schema_version=3)
        with self.assertRaises(Refuse) as ctx:
            validate_result(invalid)
        self.assertIn("unsupported result schema_version=3", str(ctx.exception))

    def test_legacy_v1_uses_the_real_down_projection_vocabulary(self):
        scenario = PACK / "legacy_v1"
        events = [
            json.loads(line) for line in (scenario / "events.v1.jsonl")
            .read_text().splitlines() if line.strip()
        ]
        self.assertTrue(events, "legacy_v1 captured no projected events")
        self.assertTrue(
            all(event["v"] == 1 for event in events),
            "legacy_v1 events were not stamped with v1",
        )
        finished = next(event for event in events if event["event"] == "finished")
        self.assertEqual(finished["status"], "completed_empty")
        self.assertNotIn(
            "final_text_state", finished,
            "legacy_v1 leaked the v2-only terminal field",
        )
        self.assertIn("DERIVED_BY_REAL_PROJECTION",
                      self.manifest["derivations"]["legacy_v1"])

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

    def test_provider_error_pins_transcript_vs_job_outcome_divergence(self):
        """Transcript completion must not imply provider execution succeeded."""
        scenario = PACK / "provider_error"
        record = json.loads((scenario / "result.json").read_text())
        finished = self._finished(scenario / "events.v2.jsonl")
        self.assertEqual(
            (finished["status"], finished["final_text_state"],
             record["execution"]["outcome"], record["status"], record["exit"]),
            ("completed", "empty", "provider_error", "provider_error", 7),
            "provider_error fixture no longer proves transcript/job divergence",
        )

    def test_harness_pool_aliases_are_surface_specific(self):
        """Bare provider has two historical meanings and is never canonical."""
        import contextlib
        import io

        import switchgear.cli as cli

        scenario = PACK / "ok"
        row = json.loads((scenario / "jobs-row.json").read_text())
        record = json.loads((scenario / "result.json").read_text())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_job(record, as_json=True)
        projected = json.loads(output.getvalue())

        self.assertEqual(row["provider"], row["pool"],
                         "jobs row provider alias no longer means pool")
        self.assertEqual(projected["provider"], projected["harness"],
                         "single-job provider alias no longer means harness")
        self.assertEqual(projected.get("pool"), record["model"]["provider"],
                         "single-job pool did not come from model.provider")

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


class ClosedDurableSchemaVersions(unittest.TestCase):
    """Pin each versioned closed durable shape to its declared version.

    Audit of a1e990f..04fd1a4 found one existing closed-schema field addition:
    result.session_store_id. session-binding.schema.json is new in that range,
    so binding_version 1 is legitimate. Registering properties by version makes
    the next same-version addition fail here instead of relying on review.
    """

    RESULT_V1_PROPERTIES = frozenset({
        "acceptance", "agent_directed_content", "artifacts", "attempt", "change",
        "correlation", "cost_usd", "delegation", "dir", "effort", "error",
        "execution", "exit", "finished", "freeze", "generation", "harness",
        "integrity", "job_id", "lease_uuid", "mode", "model", "policy_digest",
        "process", "profile_digest", "provider", "provider_calls", "queued_s",
        "resumed", "review", "review_of", "role", "schema_version",
        "secrets_suspected", "security", "spend_unrecorded", "started", "status",
    })
    RESULT_NESTED_CLOSED_SHAPES = {
        "$.security": frozenset({
            "containment", "credential", "identity", "network",
        }),
        "$.security.containment": frozenset({
            "backend", "ipc_namespace", "mount_namespace",
            "network_namespace", "pid_namespace", "uts_namespace",
        }),
        "$.security.identity": frozenset({"payload_uid", "uid_boundary"}),
        "$.security.credential": frozenset({"enters_worker", "posture"}),
        "$.security.network": frozenset({"broker_only", "direct"}),
        "$.execution": frozenset({"outcome"}),
        "$.change": frozenset({"state"}),
        "$.acceptance": frozenset({"state"}),
        "$.delegation": frozenset({"children", "denied", "denied_detail"}),
    }
    BASELINES = {
        "result-v1.schema.json": (
            "schema_version", 1,
            {"$": RESULT_V1_PROPERTIES, **RESULT_NESTED_CLOSED_SHAPES},
        ),
        "result.schema.json": (
            "schema_version", 2,
            {"$": RESULT_V1_PROPERTIES | {"session_store_id"},
             **RESULT_NESTED_CLOSED_SHAPES},
        ),
        "session-binding.schema.json": (
            "binding_version", 1,
            {
                "$": frozenset({
                    "binding_version", "created_at", "created_by_job", "harness",
                    "session_store_id", "worktree",
                }),
                "$.worktree": frozenset({
                    "common_dev", "common_git_dir", "common_ino", "git_dir",
                    "realpath", "st_dev", "st_ino",
                }),
            },
        ),
    }

    def _closed_shapes(self, schema: dict, path: str = "$") -> dict:
        """Return every closed object keyed by its instance JSON path."""
        shapes = {}
        properties = schema.get("properties")
        if schema.get("additionalProperties") is False:
            self.assertIsInstance(
                properties, dict,
                f"closed durable object {path} has no property map to register",
            )
            shapes[path] = frozenset(properties)
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict):
                    shapes.update(self._closed_shapes(child, f"{path}.{name}"))
        items = schema.get("items")
        if isinstance(items, dict):
            shapes.update(self._closed_shapes(items, f"{path}[]"))
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            shapes.update(self._closed_shapes(additional, f"{path}.*"))
        patterns = schema.get("patternProperties")
        if isinstance(patterns, dict):
            for pattern, child in patterns.items():
                if isinstance(child, dict):
                    shapes.update(
                        self._closed_shapes(child, f"{path}<pattern:{pattern}>")
                    )
        for keyword in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(schema.get(keyword) or []):
                if isinstance(child, dict):
                    shapes.update(
                        self._closed_shapes(child, f"{path}<{keyword}:{index}>")
                    )
        return shapes

    def _assert_registered_shape(self, name: str, schema: dict) -> None:
        version_field, expected_version, expected_shapes = self.BASELINES[name]
        version_schema = schema["properties"][version_field]
        actual_version = version_schema.get("const", version_schema.get("minimum"))
        self.assertEqual(
            actual_version, expected_version,
            f"{name} moved version; review the new closed shape and register its "
            "property set in the durable-schema guard",
        )
        actual_shapes = self._closed_shapes(schema)
        self.assertEqual(
            set(actual_shapes), set(expected_shapes),
            f"{name} gained or lost a closed object without moving version "
            f"{expected_version}",
        )
        for path, expected_properties in expected_shapes.items():
            self.assertEqual(
                actual_shapes[path], expected_properties,
                f"{name} closed object {path} gained or lost a property without "
                f"moving version {expected_version}",
            )

    def test_every_versioned_closed_durable_shape_is_registered(self):
        schema_dir = ROOT / "python" / "switchgear" / "data" / "schemas"
        for name in self.BASELINES:
            with self.subTest(schema=name):
                schema = json.loads((schema_dir / name).read_text())
                self._assert_registered_shape(name, schema)

    def test_the_guard_constructs_a_nested_same_version_property_addition(self):
        """A nested closed shape must not bypass the version guard."""
        schema_path = (
            ROOT / "python" / "switchgear" / "data" / "schemas"
            / "result.schema.json"
        )
        mutated = json.loads(schema_path.read_text())
        mutated["properties"]["security"]["properties"]["containment"][
            "properties"
        ]["future_unversioned_field"] = {"type": "string"}
        with self.assertRaises(AssertionError) as ctx:
            self._assert_registered_shape("result.schema.json", mutated)
        self.assertIn(
            "closed object $.security.containment gained or lost a property "
            "without moving version 2", str(ctx.exception)
        )

    def test_result_v2_delta_is_exactly_the_reviewed_session_lineage_field(self):
        schema_dir = ROOT / "python" / "switchgear" / "data" / "schemas"
        v1 = json.loads((schema_dir / "result-v1.schema.json").read_text())
        v2 = json.loads((schema_dir / "result.schema.json").read_text())
        self.assertEqual(
            set(v2["properties"]) - set(v1["properties"]),
            {"session_store_id"},
            "result v2 contains an unaudited property addition",
        )
        self.assertEqual(
            set(v1["properties"]) - set(v2["properties"]), set(),
            "result v2 removed a historical property",
        )


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

    def test_failed_attribution_cleanup_claim_is_attempted_and_reported(self):
        candidate = (ROOT / "docs/CONTRACT-V1-RC1-CANDIDATE.md").read_text()
        self.assertNotIn(
            "Refuse before provider execution and clean up the partial job directory",
            candidate,
            "candidate still guarantees best-effort attribution cleanup",
        )
        self.assertIn("attempt guarded cleanup", candidate)
        self.assertIn("report its exact path", candidate)

    def test_compatibility_alias_claim_uses_the_frozen_surface_mapping(self):
        architecture = (ROOT / "docs/ARCHITECTURE.md").read_text()
        self.assertNotIn(
            "alias of `harness` and always carries the same value", architecture,
            "architecture still contradicts jobs --json alias semantics",
        )
        self.assertIn("surface-specific compatibility alias", architecture)
        self.assertIn("canonical noun table in `INTEGRATION.md`", architecture)

        job_source = (ROOT / "python/switchgear/job.py").read_text()
        self.assertNotIn(
            "`upstream` names the service", job_source,
            "job record comment still calls the pool by the registry URL noun",
        )
        self.assertIn("model.provider names the model-serving pool", job_source)

    def test_historical_event_retention_claim_names_gc_deletion(self):
        candidate = (ROOT / "docs/CONTRACT-V1-RC1-CANDIDATE.md").read_text()
        self.assertNotIn(
            "`evidence/events.v1.jsonl` is never rewritten, migrated or deleted",
            candidate,
            "candidate still claims gc cannot delete a collected v1 artifact",
        )
        self.assertIn("never rewrites or migrates", candidate)
        self.assertIn("`gc --yes` collects an eligible job's", candidate)

    def test_candidate_reports_the_current_suite_count(self):
        """Derived, not pinned.

        This guard hardcoded the count, so it went stale on the very next round
        that added a test -- the same rot it exists to catch. It now counts the
        suites `tests/run.sh` actually executes and requires the candidate to
        state that number, so the claim cannot drift from the gate it cites.
        """
        import importlib.util
        import re as _re

        run_sh = (ROOT / "tests" / "run.sh").read_text()
        suites = _re.findall(r'python3 "\$ROOT/(tests/[^"]+\.py)"', run_sh)
        self.assertTrue(suites, "could not read the suites tests/run.sh runs")

        loader = unittest.TestLoader()
        total = 0
        for rel in suites:
            spec = importlib.util.spec_from_file_location(
                "counted_" + _re.sub(r"\W", "_", rel), ROOT / rel)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                continue  # a suite that guards its own import is counted as 0
            total += loader.loadTestsFromModule(module).countTestCases()

        candidate = (ROOT / "docs/CONTRACT-V1-RC1-CANDIDATE.md").read_text()
        self.assertIn(
            f"{total} tests, exit 0", candidate,
            f"the candidate does not report the current full-suite count ({total})",
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

    def test_gc_session_race_names_the_running_job_mechanism(self):
        for relative in (
            "docs/INTEGRATION.md", "docs/CONTRACT-V1-RC1-CANDIDATE.md",
        ):
            text = (ROOT / relative).read_text()
            compact = re.sub(r"\s+", " ", text.replace(">", ""))
            self.assertIn(
                "currently running read-only or otherwise session-using job",
                compact,
                f"{relative} omits the running session-use gc race",
            )
            self.assertIn(
                "No later reappearance and no resume is required", compact,
                f"{relative} still requires reappearance/resume for the gc race",
            )

    def test_every_published_vocabulary_separates_transcript_and_job_outcome(self):
        publications = (
            "python/switchgear/harnesses/__init__.py",
            "docs/INTEGRATION.md",
            "docs/OBSERVABILITY.md",
            "docs/CONTRACT-V1-RC1-CANDIDATE.md",
        )
        for relative in publications:
            text = (ROOT / relative).read_text()
            self.assertIn(
                "transcript", text,
                f"{relative} does not call finished.status a transcript interpretation",
            )
            self.assertIn(
                "result.execution.outcome", text,
                f"{relative} omits the authoritative provider-execution field",
            )
            self.assertIn(
                "result.status", text,
                f"{relative} omits the projected job-outcome field",
            )


class TerminalPrecedenceClaims(unittest.TestCase):
    """`sawTerminal: false` is never `completed` -- but it is not always `failed`.

    The cascade in `_finish_events` checks a provider error BEFORE truncation, so
    a run that stopped to ask for permission reports `needs_input` while never
    having closed its stream. Three documents and one code comment claimed the
    stronger, false version: that any run ending without a terminal event
    reports `failed`. The shipped `needs_input` fixture is the counterexample,
    and it is used as the proof rather than a constructed case.
    """

    def test_the_shipped_needs_input_fixture_has_sawterminal_false(self):
        finished = None
        for line in (PACK / "needs_input" / "events.v2.jsonl").read_text().splitlines():
            if line.strip() and json.loads(line)["event"] == "finished":
                finished = json.loads(line)
        self.assertIsNotNone(finished, "the needs_input fixture lost its terminal event")
        self.assertEqual(
            (finished["status"], finished["sawTerminal"]),
            ("needs_input", False),
            "the fixture no longer demonstrates needs_input beside sawTerminal:false",
        )

    def test_the_cascade_puts_the_error_branch_before_truncation(self):
        """Behavioural, not textual: an input-request error with no terminal
        event must yield needs_input, not the truncation failure."""
        from switchgear.harnesses.normalization import _finish_events

        [event] = _finish_events(
            {"turns": 1, "costUSD": 0.0, "tokens": 0},
            saw_terminal=False, run_ended=True,
            errored="permission required to write outside the worktree",
            last_text="",
        )
        self.assertEqual((event["status"], event["sawTerminal"]),
                         ("needs_input", False))
        [truncated] = _finish_events(
            {"turns": 1, "costUSD": 0.0, "tokens": 0},
            saw_terminal=False, run_ended=True, errored=None, last_text="hello",
        )
        self.assertEqual(truncated["status"], "failed",
                         "a terminal-less run with no error must still be failed")
        self.assertNotEqual(truncated["status"], "completed")

    def test_no_document_claims_every_terminal_less_run_is_failed(self):
        """The exact overclaim that was published in three places."""
        for rel in ("docs/CONTRACT-V1-RC1-CANDIDATE.md", "docs/INTEGRATION.md"):
            text = " ".join((ROOT / rel).read_text().split())
            self.assertNotIn(
                "which is **never** `completed`. A run that ended without "
                "the provider closing its stream reports `failed`", text,
                f"{rel} still says every terminal-less run reports failed",
            )
            self.assertIn(
                "needs_input", text,
                f"{rel} does not mention the branch that precedes truncation",
            )


class InodeReuseResidualClaims(unittest.TestCase):
    """The forced-inode-reuse residual is NOT confined to legacy migration.

    The consumer reproduced it on the minted-lineage path, and the candidate said
    it could not happen there. A residual documented in the wrong place is worse
    than an undocumented one: a reader checks the named path and concludes they
    are safe.
    """

    def test_minted_lineage_verification_cannot_distinguish_a_reproduced_workspace(self):
        """Structural: verification compares identity_core and nothing else, so
        a recreated workspace reproducing all of it is indistinguishable."""
        import inspect

        from switchgear import identity, sessions

        source = inspect.getsource(sessions.verify_lineage)
        self.assertIn("worktree_facts(ident)", source)
        fields = set(identity.identity_core(identity.WorktreeIdentity(
            realpath="/x", st_dev=1, st_ino=2, git_dir="/x/.git",
            common_git_dir="/x/.git", common_dev=1, common_ino=3,
            head="a" * 40, branch="main", linked_worktree=False,
        )))
        self.assertNotIn("head", fields)
        self.assertNotIn("branch", fields)
        self.assertEqual(
            fields,
            {"realpath", "st_dev", "st_ino", "git_dir", "common_git_dir",
             "common_dev", "common_ino"},
            "identity_core changed; the residual's description must be rechecked",
        )

    def test_the_candidate_does_not_confine_the_residual_to_legacy_migration(self):
        text = " ".join(
            (ROOT / "docs" / "CONTRACT-V1-RC1-CANDIDATE.md").read_text().split())
        self.assertNotIn(
            "the exposure that remains is confined to legacy migration", text,
            "the candidate still confines the inode-reuse residual to legacy "
            "migration; the consumer reproduced it on the minted-lineage path",
        )
        self.assertIn("NOT confined to legacy migration", text,
                      "the candidate does not state the residual's real scope")
        self.assertIn("indistinguishable", text,
                      "the candidate does not say why the verifier cannot tell")

    def test_no_document_claims_recreation_always_fails_closed(self):
        for rel in ("docs/CONTRACT-V1-RC1-CANDIDATE.md", "docs/INTEGRATION.md"):
            text = " ".join((ROOT / rel).read_text().split())
            self.assertNotIn(
                "What still fails closed is a workspace that was destroyed and "
                "recreated", text,
                f"{rel} still claims every recreated workspace fails closed",
            )


class CandidateDocumentConformance(unittest.TestCase):
    """The rc1 candidate cites tests by name as proof of its claims.

    A proof table naming tests that do not exist is worse than no table: it
    reads as evidence while proving nothing. This is the same defect class as a
    comment describing a check that does not exist, which this repository grades
    hardest.
    """

    def test_every_test_the_candidate_cites_actually_exists(self):
        candidate = (ROOT / "docs" / "CONTRACT-V1-RC1-CANDIDATE.md").read_text()
        cited = set(re.findall(r"`(test_[a-z0-9_]+)`", candidate))
        self.assertTrue(cited, "the candidate cites no tests; the table vanished")

        defined = set()
        for path in sorted((ROOT / "tests").rglob("test_*.py")):
            defined.update(re.findall(r"^\s*def (test_[a-z0-9_]+)\(",
                                      path.read_text(), re.M))
        missing = sorted(cited - defined)
        self.assertEqual(
            missing, [],
            "docs/CONTRACT-V1-RC1-CANDIDATE.md cites tests that do not exist",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
