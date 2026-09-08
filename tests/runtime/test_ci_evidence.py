"""Deterministic tests for CI evidence indexing and hosted capability validation (goal.md §3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibereview.runtime import (
    CONFORMANCE_SUITE_VERSION,
    REQUIRED_CONFORMANCE_TESTS,
    ConfinementLevel,
    NetworkPolicy,
    QualificationFingerprint,
    SandboxProbeStatus,
    build_ci_evidence_index,
    compute_confinement_code_fingerprint,
    compute_conformance_report_hash,
    compute_profile_hash,
    issue_qualification,
    validate_hosted_check_result,
)
import vibereview.runtime.ci_evidence as ci_evidence_module
from vibereview.runtime.ci_evidence import main as ci_evidence_main
from vibereview.runtime.hashing import hash_file
from helpers.conformance_fixtures import (
    fabricated_passing_report,
    fabricated_probe,
)


_SYNTHETIC_PLATFORM_FINGERPRINT = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _synthetic_platform_probe(monkeypatch):
    """CI-evidence unit tests must never invoke a real Bubblewrap executable."""

    monkeypatch.setattr(
        ci_evidence_module,
        "probe_platform_capabilities",
        lambda _path: SimpleNamespace(fingerprint=_SYNTHETIC_PLATFORM_FINGERPRINT),
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


def _write_artifacts(target_dir: Path, probe, report, qualification) -> None:
    (target_dir / "sandbox-probe.json").write_text(
        probe.model_dump_json(indent=2), encoding="utf-8"
    )
    (target_dir / "sandbox-conformance-report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    (target_dir / "confinement-qualification.json").write_text(
        qualification.model_dump_json(indent=2), encoding="utf-8"
    )


def _rehash_report(report):
    return report.model_copy(
        update={"report_hash": compute_conformance_report_hash(report)}
    )


def _setup_artifacts(
    target_dir: Path,
    *,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
):
    target_dir.mkdir(parents=True, exist_ok=True)
    executable = target_dir.parent / "test-bwrap"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    executable = executable.resolve()
    probe = fabricated_probe(executable_hash=hash_file(executable)).model_copy(
        update={"executable_path": executable}
    )
    report = fabricated_passing_report(probe, network_policy=network_policy)
    fingerprint = QualificationFingerprint(
        backend_executable_identity=str(executable),
        backend_executable_hash=hash_file(executable),
        confinement_code_fingerprint=compute_confinement_code_fingerprint(),
        profile_hash=compute_profile_hash(network_policy),
        platform_capability_fingerprint=_SYNTHETIC_PLATFORM_FINGERPRINT,
        network_policy=network_policy,
        conformance_suite_version=CONFORMANCE_SUITE_VERSION,
    )
    qualification = issue_qualification(report, fingerprint)

    _write_artifacts(target_dir, probe, report, qualification)
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


def test_build_ci_evidence_index_rejects_unqualified_record(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    _write_artifacts(
        art_dir,
        probe,
        report,
        qualification.model_copy(update={"qualified": False}),
    )

    with pytest.raises(ValueError, match="is not marked qualified"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_rehashed_failed_summary(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    failed_report = _rehash_report(
        report.model_copy(update={"all_required_passed": False})
    )
    _write_artifacts(
        art_dir,
        probe,
        failed_report,
        qualification.model_copy(
            update={"conformance_report_hash": failed_report.report_hash}
        ),
    )

    with pytest.raises(ValueError, match="does not pass all required cases"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_rehashed_all_failed_required_cases(
    tmp_path: Path,
):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    cases = tuple(
        case.model_copy(
            update={"passed": False, "diagnostic": "adversarial failure"}
        )
        for case in report.cases
    )
    failed_report = _rehash_report(report.model_copy(update={"cases": cases}))
    _write_artifacts(
        art_dir,
        probe,
        failed_report,
        qualification.model_copy(
            update={"conformance_report_hash": failed_report.report_hash}
        ),
    )

    with pytest.raises(ValueError, match="contains failed required cases"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


@pytest.mark.parametrize("omission", ["declaration", "execution"])
def test_build_ci_evidence_index_rejects_missing_report_case(
    tmp_path: Path,
    omission: str,
):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    if omission == "declaration":
        changed = report.model_copy(
            update={"required_case_ids": report.required_case_ids[:-1]}
        )
        expected = "required-case declaration is not exact"
    else:
        changed = report.model_copy(update={"cases": report.cases[:-1]})
        expected = "did not execute exactly the required cases"
    changed = _rehash_report(changed)
    _write_artifacts(
        art_dir,
        probe,
        changed,
        qualification.model_copy(
            update={"conformance_report_hash": changed.report_hash}
        ),
    )

    with pytest.raises(ValueError, match=expected):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


@pytest.mark.parametrize(
    "passed_cases",
    [
        REQUIRED_CONFORMANCE_TESTS[:-1],
        (*REQUIRED_CONFORMANCE_TESTS, "test_untrusted_extra_case"),
        tuple(reversed(REQUIRED_CONFORMANCE_TESTS)),
    ],
)
def test_build_ci_evidence_index_rejects_inexact_qualification_case_evidence(
    tmp_path: Path,
    passed_cases: tuple[str, ...],
):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    _write_artifacts(
        art_dir,
        probe,
        report,
        qualification.model_copy(update={"required_cases_passed": passed_cases}),
    )

    with pytest.raises(ValueError, match="passed-case declaration is not exact"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_nonusable_bound_probe(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    blocked_probe = probe.model_copy(update={"status": SandboxProbeStatus.BLOCKED})
    blocked_report = _rehash_report(
        report.model_copy(update={"probe": blocked_probe})
    )
    _write_artifacts(
        art_dir,
        blocked_probe,
        blocked_report,
        qualification.model_copy(
            update={"conformance_report_hash": blocked_report.report_hash}
        ),
    )

    with pytest.raises(ValueError, match="Sandbox probe is not usable"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


@pytest.mark.parametrize("changed_field", ["argv", "stderr"])
def test_build_ci_evidence_index_rejects_probe_command_drift(
    tmp_path: Path,
    changed_field: str,
):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    commands = list(probe.commands)
    replacement = (
        (*commands[-1].argv, "--adversarial")
        if changed_field == "argv"
        else "adversarial diagnostic"
    )
    commands[-1] = commands[-1].model_copy(update={changed_field: replacement})
    changed_probe = probe.model_copy(update={"commands": tuple(commands)})
    _write_artifacts(art_dir, changed_probe, report, qualification)

    with pytest.raises(ValueError, match="Standalone probe does not match"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_current_executable_hash_drift(
    tmp_path: Path,
):
    art_dir = tmp_path / "artifacts"
    probe, _, _, _ = _setup_artifacts(art_dir)
    assert probe.executable_path is not None
    probe.executable_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="executable hash does not match"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_missing_current_executable(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, _, _, _ = _setup_artifacts(art_dir)
    assert probe.executable_path is not None
    probe.executable_path.unlink()

    with pytest.raises(ValueError, match="sandbox executable is unavailable"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend_name", "not-bubblewrap"),
        ("backend_version", "0.0-adversarial"),
        ("confinement_level", ConfinementLevel.TEST_ONLY),
        ("backend_executable_identity", "/not/the/probed/executable"),
        ("backend_executable_hash", "sha256:" + "1" * 64),
        ("confinement_code_fingerprint", "sha256:" + "2" * 64),
        ("profile_hash", "sha256:" + "3" * 64),
        ("platform_capability_fingerprint", "sha256:" + "4" * 64),
        ("network_policy", NetworkPolicy.HOST),
        ("conformance_suite_version", "0.0-adversarial"),
    ],
)
def test_build_ci_evidence_index_rejects_qualification_fingerprint_drift(
    tmp_path: Path,
    field: str,
    value,
):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    _write_artifacts(
        art_dir,
        probe,
        report,
        qualification.model_copy(update={field: value}),
    )

    with pytest.raises(ValueError, match=f"Qualification {field} does not match"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_self_consistent_stale_suite(tmp_path: Path):
    art_dir = tmp_path / "artifacts"
    probe, report, _, qualification = _setup_artifacts(art_dir)
    stale_report = _rehash_report(
        report.model_copy(update={"suite_version": "0.0-adversarial"})
    )
    _write_artifacts(
        art_dir,
        probe,
        stale_report,
        qualification.model_copy(
            update={
                "conformance_report_hash": stale_report.report_hash,
                "conformance_suite_version": stale_report.suite_version,
            }
        ),
    )

    with pytest.raises(ValueError, match="suite version is not current"):
        build_ci_evidence_index(art_dir, "sha", "1", "1", "runner")


def test_build_ci_evidence_index_rejects_requested_network_policy_mismatch(
    tmp_path: Path,
    capsys,
):
    art_dir = tmp_path / "artifacts"
    _setup_artifacts(art_dir, network_policy=NetworkPolicy.DENY)

    with pytest.raises(ValueError, match="Requested network policy does not match"):
        build_ci_evidence_index(
            art_dir, "sha", "1", "1", "runner", network_policy="host"
        )

    rc = ci_evidence_main(
        [
            "build-index",
            "--artifact-dir",
            str(art_dir),
            "--git-sha",
            "sha",
            "--workflow-run-id",
            "1",
            "--workflow-run-attempt",
            "1",
            "--runner-identity",
            "runner",
            "--network-policy",
            "host",
        ]
    )
    assert rc == 1
    assert "Requested network policy does not match" in capsys.readouterr().err


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
