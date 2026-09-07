"""Deterministic tests for CI evidence indexing and hosted capability validation (goal.md §3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.runtime import (
    REQUIRED_CONFORMANCE_TESTS,
    build_ci_evidence_index,
    compute_conformance_report_hash,
    issue_qualification,
    validate_hosted_check_result,
)
from vibereview.runtime.ci_evidence import main as ci_evidence_main
from helpers.conformance_fixtures import (
    current_fingerprint_for,
    fabricated_passing_report,
    fabricated_probe,
)


# --- §3.1: Hosted capability check return-code table ---


@pytest.mark.parametrize(
    ("probe_rc", "qualify_rc", "qual_exists", "expected_ok"),
    [
        # Usable probe (0)
        (0, 0, False, True),
        (0, 0, True, True),
        (0, 4, False, False),
        (0, 5, False, False),
        (0, 2, False, False),
        # Unavailable probe (2) - fail closed
        (2, 2, False, True),
        (2, 3, False, True),
        (2, 4, False, True),
        (2, 0, False, False),  # Unexpected qualify success when probe unavailable
        (2, 5, False, False),  # Internal error must fail
        (2, 2, True, False),   # Qualification artifact forbidden
        # Blocked probe (3) - fail closed
        (3, 3, False, True),
        (3, 2, False, True),
        (3, 4, False, True),
        (3, 0, False, False),  # Unexpected qualify success when probe blocked
        (3, 5, False, False),  # Internal error must fail
        (3, 3, True, False),   # Qualification artifact forbidden
        # Unexpected probe codes
        (1, 2, False, False),
        (5, 2, False, False),
        (127, 2, False, False),
    ],
)
def test_hosted_capability_check_return_code_table(
    probe_rc: int, qualify_rc: int, qual_exists: bool, expected_ok: bool
):
    ok, msg = validate_hosted_check_result(probe_rc, qualify_rc, qual_exists)
    assert ok is expected_ok, f"Failed for probe_rc={probe_rc}, qualify_rc={qualify_rc}: {msg}"


def test_cli_validate_hosted_check_success_and_failure(tmp_path: Path, capsys):
    # Success case: probe 2, qualify 2, no artifact
    rc = ci_evidence_main([
        "validate-hosted-check",
        "--probe-rc", "2",
        "--qualify-rc", "2",
        "--qualification-artifact", str(tmp_path / "absent.json"),
    ])
    assert rc == 0
    assert "[HOSTED-CHECK VALID]" in capsys.readouterr().out

    # Failure case: internal error qualify 5
    rc_fail = ci_evidence_main([
        "validate-hosted-check",
        "--probe-rc", "2",
        "--qualify-rc", "5",
    ])
    assert rc_fail == 1
    assert "[HOSTED-CHECK ERROR]" in capsys.readouterr().err


# --- §3.2: CI evidence index generation ---


def _setup_artifacts(target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    probe = fabricated_probe()
    report = fabricated_passing_report(probe)
    fingerprint = current_fingerprint_for(probe)
    qualification = issue_qualification(report, fingerprint)

    (target_dir / "sandbox-probe.json").write_text(probe.model_dump_json(indent=2), encoding="utf-8")
    (target_dir / "sandbox-conformance-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (target_dir / "confinement-qualification.json").write_text(qualification.model_dump_json(indent=2), encoding="utf-8")
    return probe, report, fingerprint, qualification


def test_build_ci_evidence_index_valid(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, report, fingerprint, qualification = _setup_artifacts(art_dir)

    junit_file = tmp_path / "junit.xml"
    junit_file.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="10" failures="0" errors="0" skipped="1">
    <testcase classname="test_foo" name="test_1" />
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    index = build_ci_evidence_index(
        artifact_dir=art_dir,
        git_sha="abcdef1234567890",
        workflow_run_id="987654321",
        workflow_run_attempt="1",
        runner_identity="self-hosted-vibereview-1",
        network_policy="deny",
        junit_xml=junit_file,
    )

    assert index["tested_sha"] == "abcdef1234567890"
    assert index["workflow_run_id"] == "987654321"
    assert index["runner_identity"] == "self-hosted-vibereview-1"
    assert index["conformance_report_hash"] == report.report_hash
    assert index["test_counts"]["conformance_required_total"] == len(REQUIRED_CONFORMANCE_TESTS)
    assert index["test_counts"]["conformance_required_passed"] == len(REQUIRED_CONFORMANCE_TESTS)
    assert index["test_counts"]["pytest"]["selected"] == 10
    assert index["test_counts"]["pytest"]["passed"] == 9
    assert index["test_counts"]["pytest"]["skipped"] == 1
    assert "sandbox-probe.json" in index["artifact_hashes"]
    assert "confinement-qualification.json" in index["artifact_hashes"]

    index_path = art_dir / "ci-evidence-index.json"
    assert index_path.is_file()
    saved = json.loads(index_path.read_text(encoding="utf-8"))
    assert saved["conformance_report_hash"] == report.report_hash


def test_build_ci_evidence_index_rejects_tampered_report(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    _setup_artifacts(art_dir)

    report_path = art_dir / "sandbox-conformance-report.json"
    data = json.loads(report_path.read_text(encoding="utf-8"))
    data["all_required_passed"] = False
    report_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="Report hash mismatch"):
        build_ci_evidence_index(
            artifact_dir=art_dir,
            git_sha="sha",
            workflow_run_id="1",
            workflow_run_attempt="1",
            runner_identity="runner",
        )


def test_build_ci_evidence_index_missing_required_file(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    _setup_artifacts(art_dir)
    (art_dir / "confinement-qualification.json").unlink()

    with pytest.raises(FileNotFoundError, match="Required qualification artifact missing"):
        build_ci_evidence_index(
            artifact_dir=art_dir,
            git_sha="sha",
            workflow_run_id="1",
            workflow_run_attempt="1",
            runner_identity="runner",
        )


def test_cli_build_index_command(tmp_path: Path, capsys):
    art_dir = tmp_path / "artifacts"
    _setup_artifacts(art_dir)

    rc = ci_evidence_main([
        "build-index",
        "--artifact-dir", str(art_dir),
        "--git-sha", "12345678",
        "--workflow-run-id", "999",
        "--workflow-run-attempt", "1",
        "--runner-identity", "linux-sandbox",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tested_sha"] == "12345678"
