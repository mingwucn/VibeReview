"""CI evidence indexing and hosted capability-check validation (goal.md §3).

Provides:
- Strict validation of hosted capability-check exit codes against documented contracts.
- Independent CI evidence index generation verifying report integrity, required case coverage,
  qualification fingerprint, artifact hashes, and test counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .conformance import SandboxConformanceReport, compute_conformance_report_hash
from .confinement import (
    CONFORMANCE_SUITE_VERSION,
    REQUIRED_CONFORMANCE_TESTS,
    ConfinementLevel,
    ConfinementQualification,
    NetworkPolicy,
    SandboxProbeResult,
    SandboxProbeStatus,
    compute_confinement_code_fingerprint,
    compute_profile_hash,
    probe_platform_capabilities,
)
from .hashing import hash_file


def validate_hosted_check_result(
    probe_rc: int,
    qualify_rc: int,
    qualification_artifact_exists: bool,
) -> tuple[bool, str]:
    """Validate exit codes and artifacts from a hosted sandbox capability check.

    Result combinations (§3):
    1. Usable probe (0) and successful qualify (0):
       Hosted kernel allows sandbox profile (prepared-profile variant). Pass,
       noting it is a capability check only and not transferable qualification.
    2. Usable probe (0) but qualify failed (!= 0):
       Conformance failed or internal error occurred after probe indicated usability. Fail.
    3. Unavailable / blocked probe (2 or 3):
       Must fail closed: qualify MUST exit 2, 3, or 4, and no qualification
       artifact may exist. If so, pass.
       If qualify exited with any other code (e.g. 0 or 5), or if a qualification
       artifact was written, fail.
    4. Any unexpected probe exit code (!= 0, 2, 3): Fail.
    """
    if probe_rc == 0:
        if qualify_rc == 0:
            return (
                True,
                "Hosted kernel allows sandbox profile (prepared-profile variant); "
                "capability check passed (non-transferable runtime qualification).",
            )
        return (
            False,
            f"FAIL: Probe reported usable (exit 0), but qualify failed with unexpected exit {qualify_rc}.",
        )

    if probe_rc in (2, 3):
        if qualification_artifact_exists:
            return (
                False,
                "FAIL: Confinement qualification artifact must NOT be produced when probe is not usable.",
            )
        if qualify_rc in (2, 3, 4):
            probe_label = "unavailable" if probe_rc == 2 else "blocked"
            return (
                True,
                f"Fail-closed contract holds: probe was {probe_label} (exit {probe_rc}), "
                f"qualify exited {qualify_rc}, and no qualification artifact was written.",
            )
        return (
            False,
            f"FAIL: Probe was not usable (exit {probe_rc}), but qualify exited {qualify_rc}; "
            "expected fail-closed exit code in (2, 3, 4).",
        )

    return (
        False,
        f"FAIL: Probe exited with unexpected return code {probe_rc}; expected 0, 2, or 3.",
    )


def _compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_junit_counts(junit_xml_path: Path) -> dict[str, int]:
    try:
        tree = ET.parse(junit_xml_path)
        root = tree.getroot()
    except Exception as exc:
        return {"error": str(exc)}

    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0

    if root.tag == "testsuite":
        suites = [root]
    else:
        suites = root.findall(".//testsuite")
        if not suites and root.findall(".//testcase"):
            suites = [root]

    if suites:
        for s in suites:
            total_tests += int(s.attrib.get("tests", 0))
            total_failures += int(s.attrib.get("failures", 0))
            total_errors += int(s.attrib.get("errors", 0))
            total_skipped += int(s.attrib.get("skipped", 0))
    else:
        cases = root.findall(".//testcase")
        total_tests = len(cases)
        total_failures = len(root.findall(".//testcase/failure"))
        total_errors = len(root.findall(".//testcase/error"))
        total_skipped = len(root.findall(".//testcase/skipped"))

    failed = total_failures + total_errors
    passed = max(0, total_tests - failed - total_skipped)
    return {
        "selected": total_tests,
        "passed": passed,
        "failed": failed,
        "skipped": total_skipped,
    }


def _validate_qualification_artifacts(
    *,
    probe: SandboxProbeResult,
    report: SandboxConformanceReport,
    qualification: ConfinementQualification,
    requested_network_policy: NetworkPolicy,
) -> None:
    """Fail closed unless the three CI artifacts form one valid attestation."""

    if probe.status is not SandboxProbeStatus.USABLE:
        raise ValueError(f"Sandbox probe is not usable: {probe.status.value}")
    if probe != report.probe:
        raise ValueError("Standalone probe does not match the conformance report probe")
    if report.probe.status is not SandboxProbeStatus.USABLE:
        raise ValueError(
            f"Conformance report probe is not usable: {report.probe.status.value}"
        )
    if report.backend_name != report.probe.backend_name:
        raise ValueError("Conformance report backend does not match its probe")
    if report.backend_name != "bubblewrap":
        raise ValueError("Conformance report does not attest the Bubblewrap backend")
    if report.confinement_level is not ConfinementLevel.OS_SANDBOX:
        raise ValueError("Conformance report does not attest OS_SANDBOX confinement")
    if report.suite_version != CONFORMANCE_SUITE_VERSION:
        raise ValueError("Conformance report suite version is not current")
    if report.network_policy is not requested_network_policy:
        raise ValueError(
            "Requested network policy does not match the conformance report: "
            f"{requested_network_policy.value} != {report.network_policy.value}"
        )

    recomputed_report_hash = compute_conformance_report_hash(report)
    if recomputed_report_hash != report.report_hash:
        raise ValueError(
            f"Report hash mismatch: stored={report.report_hash}, "
            f"recomputed={recomputed_report_hash}"
        )
    if not report.all_required_passed:
        raise ValueError("Conformance report does not pass all required cases")

    required_cases = tuple(REQUIRED_CONFORMANCE_TESTS)
    if report.required_case_ids != required_cases:
        raise ValueError("Conformance report required-case declaration is not exact")
    report_case_ids = tuple(case.case_id for case in report.cases)
    if report_case_ids != required_cases:
        raise ValueError("Conformance report did not execute exactly the required cases")
    failed_cases = tuple(case.case_id for case in report.cases if not case.passed)
    if failed_cases:
        raise ValueError(
            f"Conformance report contains failed required cases: {failed_cases}"
        )

    if not qualification.qualified:
        raise ValueError("Confinement qualification is not marked qualified")
    if qualification.conformance_report_hash != report.report_hash:
        raise ValueError(
            "Qualification report hash mismatch: "
            f"{qualification.conformance_report_hash} != {report.report_hash}"
        )
    if qualification.required_cases_passed != required_cases:
        raise ValueError("Qualification passed-case declaration is not exact")

    executable_path = report.probe.executable_path
    executable_hash = report.probe.executable_hash
    if executable_path is None or executable_hash is None:
        raise ValueError("Usable conformance probe lacks executable identity evidence")
    try:
        resolved_executable = executable_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Qualified sandbox executable is unavailable") from exc
    if not resolved_executable.is_file():
        raise ValueError("Qualified sandbox executable is not a regular file")
    if hash_file(resolved_executable) != executable_hash:
        raise ValueError("Qualified sandbox executable hash does not match the probe")

    expected_fields: dict[str, Any] = {
        "backend_name": report.backend_name,
        "backend_version": report.probe.backend_version,
        "confinement_level": report.confinement_level,
        "backend_executable_identity": str(resolved_executable),
        "backend_executable_hash": executable_hash,
        "confinement_code_fingerprint": compute_confinement_code_fingerprint(),
        "profile_hash": compute_profile_hash(report.network_policy),
        "platform_capability_fingerprint": probe_platform_capabilities(
            resolved_executable
        ).fingerprint,
        "network_policy": report.network_policy,
        "conformance_suite_version": report.suite_version,
    }
    for field, expected in expected_fields.items():
        observed = getattr(qualification, field)
        if observed != expected:
            raise ValueError(
                f"Qualification {field} does not match the report/current runtime"
            )


def build_ci_evidence_index(
    artifact_dir: Path,
    git_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    runner_identity: str,
    network_policy: str = "deny",
    junit_xml: Path | None = None,
) -> dict[str, Any]:
    """Generate a validated CI evidence index from sandbox artifacts."""
    try:
        requested_network_policy = NetworkPolicy(network_policy)
    except ValueError as exc:
        raise ValueError(f"Unsupported CI evidence network policy: {network_policy!r}") from exc

    probe_file = artifact_dir / "sandbox-probe.json"
    report_file = artifact_dir / "sandbox-conformance-report.json"
    qual_file = artifact_dir / "confinement-qualification.json"

    if not probe_file.is_file():
        raise FileNotFoundError(f"Required probe artifact missing: {probe_file}")
    if not report_file.is_file():
        raise FileNotFoundError(f"Required report artifact missing: {report_file}")
    if not qual_file.is_file():
        raise FileNotFoundError(f"Required qualification artifact missing: {qual_file}")

    # 1. Parse and validate models
    probe = SandboxProbeResult.model_validate_json(probe_file.read_text(encoding="utf-8"))
    report = SandboxConformanceReport.model_validate_json(report_file.read_text(encoding="utf-8"))
    qualification = ConfinementQualification.model_validate_json(
        qual_file.read_text(encoding="utf-8")
    )

    # 2. Verify that the independently emitted artifacts form one current,
    # qualified, fully passing attestation rather than three merely parseable
    # JSON documents.
    _validate_qualification_artifacts(
        probe=probe,
        report=report,
        qualification=qualification,
        requested_network_policy=requested_network_policy,
    )

    # 3. Artifact hashes
    artifact_hashes: dict[str, str] = {}
    for p in sorted(artifact_dir.iterdir()):
        if p.is_file() and p.name != "ci-evidence-index.json":
            artifact_hashes[p.name] = _compute_file_sha256(p)

    test_counts: dict[str, Any] = {
        "conformance_required_total": len(REQUIRED_CONFORMANCE_TESTS),
        "conformance_required_passed": len(qualification.required_cases_passed),
    }
    if junit_xml and junit_xml.is_file():
        test_counts["pytest"] = _parse_junit_counts(junit_xml)

    index: dict[str, Any] = {
        "schema_version": "1.0",
        "tested_sha": git_sha,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "runner_identity": runner_identity,
        "network_policy": requested_network_policy.value,
        "probe_status": probe.status.value,
        "conformance_suite_version": report.suite_version,
        "conformance_report_hash": report.report_hash,
        "qualification_fingerprint": {
            "backend_executable_identity": qualification.backend_executable_identity,
            "backend_executable_hash": qualification.backend_executable_hash,
            "confinement_code_fingerprint": qualification.confinement_code_fingerprint,
            "profile_hash": qualification.profile_hash,
            "platform_capability_fingerprint": qualification.platform_capability_fingerprint,
        },
        "test_counts": test_counts,
        "artifact_hashes": artifact_hashes,
    }

    index_file = artifact_dir / "ci-evidence-index.json"
    index_file.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def _run_validate_hosted_check(args: argparse.Namespace) -> int:
    qual_exists = False
    if args.qualification_artifact:
        qual_exists = Path(args.qualification_artifact).is_file()

    ok, message = validate_hosted_check_result(
        args.probe_rc,
        args.qualify_rc,
        qual_exists,
    )
    if ok:
        print(f"[HOSTED-CHECK VALID] {message}")
        return 0
    else:
        print(f"[HOSTED-CHECK ERROR] {message}", file=sys.stderr)
        return 1


def _run_build_index(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir).resolve()
    junit_xml = Path(args.junit_xml).resolve() if args.junit_xml else None
    try:
        index = build_ci_evidence_index(
            artifact_dir=artifact_dir,
            git_sha=args.git_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            runner_identity=args.runner_identity,
            network_policy=args.network_policy,
            junit_xml=junit_xml,
        )
        print(json.dumps(index, indent=2))
        return 0
    except Exception as exc:
        print(f"Error building CI evidence index: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vibereview.runtime.ci_evidence",
        description="CI evidence indexer and hosted capability check validator.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-hosted-check",
        help="Validate return codes and artifact absence for hosted capability check.",
    )
    validate_parser.add_argument("--probe-rc", type=int, required=True, help="Exit code of probe")
    validate_parser.add_argument("--qualify-rc", type=int, required=True, help="Exit code of qualify")
    validate_parser.add_argument(
        "--qualification-artifact",
        type=str,
        default=None,
        help="Path to check for unexpected qualification artifact",
    )
    validate_parser.set_defaults(handler=_run_validate_hosted_check)

    index_parser = subparsers.add_parser(
        "build-index",
        help="Build a verified CI evidence index for a qualification run.",
    )
    index_parser.add_argument("--artifact-dir", type=str, required=True, help="Artifacts directory")
    index_parser.add_argument("--git-sha", type=str, required=True, help="Tested commit SHA")
    index_parser.add_argument("--workflow-run-id", type=str, required=True, help="Actions run ID")
    index_parser.add_argument("--workflow-run-attempt", type=str, required=True, help="Actions attempt")
    index_parser.add_argument("--runner-identity", type=str, required=True, help="Runner name/label")
    index_parser.add_argument(
        "--network-policy",
        choices=tuple(policy.value for policy in NetworkPolicy),
        default=NetworkPolicy.DENY.value,
        help="Network policy",
    )
    index_parser.add_argument("--junit-xml", type=str, default=None, help="Optional pytest JUnit XML")
    index_parser.set_defaults(handler=_run_build_index)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
