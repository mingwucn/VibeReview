"""Sandbox qualification issuance, gate, storage, and CLI tests (goal.md §6).

The deterministic tests use fabricated conformance fixtures only; the single
end-to-end test is gated behind ``requires_bwrap`` + ``sandbox_conformance``
and executes the real conformance suite on a usable host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vibereview.runtime import (
    CONFORMANCE_SUITE_VERSION,
    QUALIFICATION_STORE_ENV_VAR,
    REQUIRED_CONFORMANCE_TESTS,
    BubblewrapExecutionBackend,
    ConfinementLevel,
    LauncherConfiguration,
    NetworkPolicy,
    SandboxNotQualifiedError,
    SandboxProbeStatus,
    StoredQualification,
    compute_conformance_report_hash,
    compute_current_qualification_fingerprint,
    compute_storage_fingerprint,
    issue_qualification,
    load_qualification_for_fingerprint,
    probe_sandbox_capabilities,
    require_real_engine_qualification,
    run_sandbox_conformance,
    store_qualification,
)
from vibereview.runtime import sandbox as sandbox_cli
from vibereview.runtime.conformance import CONFORMANCE_WORKER_PATH
from vibereview.runtime.confinement import (
    NETWORK_NAMESPACE_PROBE,
    PID_NAMESPACE_PROBE,
    USER_MOUNT_NAMESPACE_PROBE,
)

from helpers.conformance_fixtures import (
    current_fingerprint_for,
    fabricated_passing_report,
    fabricated_probe,
)
from helpers.sandbox_gate import require_usable_sandbox


class _OSBackend:
    confinement_level = ConfinementLevel.OS_SANDBOX


def _valid_gate_inputs():
    probe = fabricated_probe()
    report = fabricated_passing_report(probe)
    fingerprint = current_fingerprint_for(probe)
    qualification = issue_qualification(report, fingerprint)
    return probe, report, fingerprint, qualification


# --- §6.1: report hash ---


def test_report_hash_is_stable_and_excludes_itself():
    probe = fabricated_probe()
    report = fabricated_passing_report(probe)

    assert compute_conformance_report_hash(report) == report.report_hash
    # The excluded-field pattern: rehashing never depends on the stored field.
    rehashed = report.model_copy(update={"report_hash": "sha256:" + "9" * 64})
    assert compute_conformance_report_hash(rehashed) == report.report_hash
    # Any payload change invalidates the hash.
    changed = report.model_copy(update={"all_required_passed": False})
    assert compute_conformance_report_hash(changed) != report.report_hash


# --- §6.2: issuance ---


def test_issue_qualification_binds_report_and_fingerprint():
    probe, report, fingerprint, qualification = _valid_gate_inputs()

    assert qualification.qualified is True
    assert qualification.backend_name == report.backend_name
    assert qualification.backend_version == report.probe.backend_version
    assert qualification.confinement_level == report.confinement_level
    assert qualification.network_policy == report.network_policy
    assert qualification.conformance_suite_version == report.suite_version
    assert qualification.conformance_report_hash == report.report_hash
    assert qualification.backend_executable_identity == fingerprint.backend_executable_identity
    assert qualification.backend_executable_hash == fingerprint.backend_executable_hash
    assert qualification.confinement_code_fingerprint == fingerprint.confinement_code_fingerprint
    assert qualification.profile_hash == fingerprint.profile_hash
    assert (
        qualification.platform_capability_fingerprint
        == fingerprint.platform_capability_fingerprint
    )
    assert tuple(sorted(qualification.required_cases_passed)) == tuple(
        sorted(REQUIRED_CONFORMANCE_TESTS)
    )
    assert qualification.qualified_at


def test_issue_qualification_excludes_non_required_cases_from_evidence():
    probe = fabricated_probe()
    report = fabricated_passing_report(probe)
    extra_case = report.cases[0].model_copy(update={"case_id": "test_extra_unrelated_case"})
    report = report.model_copy(update={"cases": report.cases + (extra_case,)})
    report = report.model_copy(update={"report_hash": compute_conformance_report_hash(report)})
    fingerprint = current_fingerprint_for(probe)

    qualification = issue_qualification(report, fingerprint)
    assert "test_extra_unrelated_case" not in qualification.required_cases_passed


def test_issue_qualification_rejects_unusable_reports():
    probe = fabricated_probe()
    fingerprint = current_fingerprint_for(probe)

    not_passed = fabricated_passing_report(probe).model_copy(
        update={"all_required_passed": False}
    )
    with pytest.raises(
        SandboxNotQualifiedError, match="conformance report does not pass all required cases"
    ):
        issue_qualification(not_passed, fingerprint)

    missing_required = fabricated_passing_report(probe).model_copy(
        update={"required_case_ids": ("test_canary_read_denied",)}
    )
    with pytest.raises(SandboxNotQualifiedError, match="does not cover the required"):
        issue_qualification(missing_required, fingerprint)

    failed_case_report = fabricated_passing_report(probe)
    cases = tuple(
        case.model_copy(update={"passed": False})
        if case.case_id == "test_bundle_immutable"
        else case
        for case in failed_case_report.cases
    )
    failed_case_report = failed_case_report.model_copy(update={"cases": cases})
    failed_case_report = failed_case_report.model_copy(
        update={"report_hash": compute_conformance_report_hash(failed_case_report)}
    )
    with pytest.raises(SandboxNotQualifiedError, match="missing passed required cases"):
        issue_qualification(failed_case_report, fingerprint)

    tampered_hash = fabricated_passing_report(probe).model_copy(
        update={"report_hash": "sha256:" + "9" * 64}
    )
    with pytest.raises(SandboxNotQualifiedError, match="report hash mismatch"):
        issue_qualification(tampered_hash, fingerprint)

    wrong_suite_fp = fingerprint.model_copy(update={"conformance_suite_version": "0.0"})
    with pytest.raises(SandboxNotQualifiedError, match="suite version does not match"):
        issue_qualification(fabricated_passing_report(probe), wrong_suite_fp)

    wrong_policy_fp = fingerprint.model_copy(update={"network_policy": NetworkPolicy.HOST})
    with pytest.raises(SandboxNotQualifiedError, match="network policy does not match"):
        issue_qualification(fabricated_passing_report(probe), wrong_policy_fp)

    wrong_exe_fp = fingerprint.model_copy(
        update={"backend_executable_hash": "sha256:" + "f" * 64}
    )
    with pytest.raises(SandboxNotQualifiedError, match="probe executable does not match"):
        issue_qualification(fabricated_passing_report(probe), wrong_exe_fp)


# --- §6.3: gate rejects every mismatch class ---


def test_gate_accepts_matching_qualification():
    probe, report, fingerprint, qualification = _valid_gate_inputs()
    require_real_engine_qualification(
        _OSBackend(), qualification, fingerprint, current_probe=probe, report=report
    )


def test_gate_rejects_every_mismatch_class():
    probe, report, fingerprint, qualification = _valid_gate_inputs()

    def expect(match, *, backend=None, qual=None, fp=None, probe_arg=None, report_arg=None):
        with pytest.raises(RuntimeError, match=match):
            require_real_engine_qualification(
                backend or _OSBackend(),
                qual or qualification,
                fp or fingerprint,
                current_probe=probe_arg or probe,
                report=report_arg or report,
            )

    class _TestOnlyBackend:
        confinement_level = ConfinementLevel.TEST_ONLY

    expect("does not allow real engine", backend=_TestOnlyBackend())
    expect(
        "marked not qualified",
        qual=qualification.model_copy(update={"qualified": False}),
    )
    expect(
        "current sandbox probe is not usable",
        probe_arg=fabricated_probe(status=SandboxProbeStatus.UNAVAILABLE),
    )
    expect(
        "user/mount namespace capability not available",
        probe_arg=fabricated_probe(failing_stages=(USER_MOUNT_NAMESPACE_PROBE,)),
    )
    expect(
        "PID namespace capability not available",
        probe_arg=fabricated_probe(failing_stages=(PID_NAMESPACE_PROBE,)),
    )
    expect(
        "network namespace capability not available",
        probe_arg=fabricated_probe(failing_stages=(NETWORK_NAMESPACE_PROBE,)),
    )
    expect(
        "backend confinement level does not match the qualification",
        qual=qualification.model_copy(
            update={"confinement_level": ConfinementLevel.ENGINE_NATIVE_SANDBOX}
        ),
    )
    expect(
        "backend executable identity mismatch",
        fp=fingerprint.model_copy(update={"backend_executable_identity": "/opt/other/bwrap"}),
    )
    expect(
        "backend executable hash mismatch",
        fp=fingerprint.model_copy(update={"backend_executable_hash": "sha256:" + "1" * 64}),
    )
    expect(
        "confinement code fingerprint mismatch",
        fp=fingerprint.model_copy(
            update={"confinement_code_fingerprint": "sha256:" + "2" * 64}
        ),
    )
    expect(
        "sandbox profile hash mismatch",
        fp=fingerprint.model_copy(update={"profile_hash": "sha256:" + "3" * 64}),
    )
    expect(
        "platform capability fingerprint mismatch",
        fp=fingerprint.model_copy(
            update={"platform_capability_fingerprint": "sha256:" + "4" * 64}
        ),
    )
    expect(
        "network policy mismatch",
        fp=fingerprint.model_copy(update={"network_policy": NetworkPolicy.HOST}),
    )
    expect(
        "conformance suite version mismatch",
        fp=fingerprint.model_copy(update={"conformance_suite_version": "9.9"}),
    )

    tampered_report = report.model_copy(update={"report_hash": "sha256:" + "5" * 64})
    expect("conformance report hash mismatch", report_arg=tampered_report)

    expect(
        "qualification report hash does not resolve",
        qual=qualification.model_copy(
            update={"conformance_report_hash": "sha256:" + "6" * 64}
        ),
    )

    failed_report = report.model_copy(update={"all_required_passed": False})
    failed_report = failed_report.model_copy(
        update={"report_hash": compute_conformance_report_hash(failed_report)}
    )
    failed_qual = qualification.model_copy(
        update={"conformance_report_hash": failed_report.report_hash}
    )
    expect(
        "conformance report does not pass all required cases",
        qual=failed_qual,
        report_arg=failed_report,
    )

    incomplete_cases = tuple(
        case.model_copy(update={"passed": False})
        if case.case_id == "test_canary_write_denied"
        else case
        for case in report.cases
    )
    incomplete_report = report.model_copy(update={"cases": incomplete_cases})
    incomplete_report = incomplete_report.model_copy(
        update={"report_hash": compute_conformance_report_hash(incomplete_report)}
    )
    incomplete_qual = qualification.model_copy(
        update={"conformance_report_hash": incomplete_report.report_hash}
    )
    expect(
        "conformance report is missing passed required cases",
        qual=incomplete_qual,
        report_arg=incomplete_report,
    )

    expect(
        "qualification is missing required conformance tests",
        qual=qualification.model_copy(
            update={"required_cases_passed": ("test_canary_read_denied",)}
        ),
    )


def test_gate_denies_host_policy_without_network_namespace_requirement():
    """HOST-policy qualifications do not require the network namespace probe."""
    probe = fabricated_probe(failing_stages=(NETWORK_NAMESPACE_PROBE,))
    report = fabricated_passing_report(probe, network_policy=NetworkPolicy.HOST)
    fingerprint = current_fingerprint_for(probe, network_policy=NetworkPolicy.HOST)
    qualification = issue_qualification(report, fingerprint)
    require_real_engine_qualification(
        _OSBackend(), qualification, fingerprint, current_probe=probe, report=report
    )


# --- §6.4: machine-local storage ---


def test_storage_round_trip_and_fingerprint_invalidation(tmp_path: Path):
    probe, report, fingerprint, qualification = _valid_gate_inputs()

    path = store_qualification(qualification, report, root=tmp_path)
    assert path.is_file()
    assert path.parent == tmp_path
    assert path.name == compute_storage_fingerprint(fingerprint).removeprefix("sha256:") + ".json"

    loaded = load_qualification_for_fingerprint(fingerprint, root=tmp_path)
    assert loaded is not None
    assert loaded.qualification == qualification
    assert loaded.report == report

    drifted = fingerprint.model_copy(update={"profile_hash": "sha256:" + "7" * 64})
    assert compute_storage_fingerprint(drifted) != compute_storage_fingerprint(fingerprint)
    assert load_qualification_for_fingerprint(drifted, root=tmp_path) is None


def test_storage_env_override_and_corrupt_entry(tmp_path: Path, monkeypatch):
    probe, report, fingerprint, qualification = _valid_gate_inputs()

    monkeypatch.setenv(QUALIFICATION_STORE_ENV_VAR, str(tmp_path / "env-store"))
    path = store_qualification(qualification, report)
    assert path.parent == tmp_path / "env-store"
    loaded = load_qualification_for_fingerprint(fingerprint)
    assert isinstance(loaded, StoredQualification)

    path.write_text("{ not json", encoding="utf-8")
    assert load_qualification_for_fingerprint(fingerprint) is None


# --- §6.6: CLI exit codes (deterministic, monkeypatched probe/suite) ---


class _StubBubblewrapExecutionBackend:
    """Minimal test-local stub for CLI unit testing without bwrap or OS calls."""

    confinement_level = ConfinementLevel.OS_SANDBOX
    instances: list[_StubBubblewrapExecutionBackend] = []

    def __init__(
        self,
        launcher: LauncherConfiguration,
        *,
        bwrap_path: Path | str | None = None,
        network_policy: NetworkPolicy = NetworkPolicy.DENY,
        execution_root_parent: Path | None = None,
    ):
        self._launcher = launcher
        self._bwrap_path = Path(bwrap_path) if bwrap_path else Path("/stub/bin/bwrap")
        self._network_policy = network_policy
        self._execution_root_parent = execution_root_parent
        _StubBubblewrapExecutionBackend.instances.append(self)

    @property
    def bwrap_path(self) -> Path:
        return self._bwrap_path

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network_policy


def _patch_cli(monkeypatch, tmp_path: Path, *, report=None, probe=None):
    _StubBubblewrapExecutionBackend.instances.clear()
    probe = probe if probe is not None else fabricated_probe()
    report = report if report is not None else fabricated_passing_report(probe)
    fingerprint = current_fingerprint_for(probe)
    monkeypatch.setattr(sandbox_cli, "probe_sandbox_capabilities", lambda: probe)
    monkeypatch.setattr(
        sandbox_cli,
        "run_sandbox_conformance",
        lambda network_policy=NetworkPolicy.DENY, probe=None, bwrap_path=None: report,
    )
    monkeypatch.setattr(
        sandbox_cli,
        "BubblewrapExecutionBackend",
        _StubBubblewrapExecutionBackend,
    )
    monkeypatch.setattr(
        sandbox_cli,
        "compute_current_qualification_fingerprint",
        lambda backend, policy: fingerprint,
    )
    monkeypatch.setenv(QUALIFICATION_STORE_ENV_VAR, str(tmp_path / "store"))
    return probe, report, fingerprint


def test_cli_probe_usable_exit_zero(tmp_path: Path, monkeypatch, capsys):
    _patch_cli(monkeypatch, tmp_path)
    code = sandbox_cli.main(["probe", "--json", "--output-dir", str(tmp_path / "out")])
    assert code == 0
    assert (tmp_path / "out" / "sandbox-probe.json").is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "usable"


def test_cli_probe_unavailable_and_blocked_exit_codes(tmp_path: Path, monkeypatch, capsys):
    _patch_cli(
        monkeypatch,
        tmp_path,
        probe=fabricated_probe(status=SandboxProbeStatus.UNAVAILABLE),
    )
    assert sandbox_cli.main(["probe"]) == 2
    capsys.readouterr()

    _patch_cli(monkeypatch, tmp_path, probe=fabricated_probe(status=SandboxProbeStatus.BLOCKED))
    assert sandbox_cli.main(["probe"]) == 3


def test_cli_qualify_success_and_artifacts(tmp_path: Path, monkeypatch, capsys):
    _, report, fingerprint = _patch_cli(monkeypatch, tmp_path)
    out_dir = tmp_path / "artifacts"
    code = sandbox_cli.main(
        ["qualify", "--network-policy", "deny", "--json", "--output-dir", str(out_dir)]
    )
    assert code == 0
    assert (out_dir / "sandbox-probe.json").is_file()
    assert (out_dir / "sandbox-conformance-report.json").is_file()
    assert (out_dir / "confinement-qualification.json").is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["conformance_report_hash"] == report.report_hash

    loaded = load_qualification_for_fingerprint(fingerprint, root=tmp_path / "store")
    assert loaded is not None
    assert loaded.report.report_hash == report.report_hash

    status_code = sandbox_cli.main(["status", "--json"])
    assert status_code == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["qualification_match"] is True
    assert status_payload["report_hash"] == report.report_hash


def test_cli_qualify_probe_not_usable_exit_codes(tmp_path: Path, monkeypatch, capsys):
    _patch_cli(
        monkeypatch,
        tmp_path,
        probe=fabricated_probe(status=SandboxProbeStatus.UNAVAILABLE),
    )
    assert sandbox_cli.main(["qualify"]) == 2
    capsys.readouterr()

    _patch_cli(monkeypatch, tmp_path, probe=fabricated_probe(status=SandboxProbeStatus.BLOCKED))
    assert sandbox_cli.main(["qualify"]) == 3


def test_cli_qualify_conformance_failure_exit_four(tmp_path: Path, monkeypatch, capsys):
    probe = fabricated_probe()
    report = fabricated_passing_report(probe).model_copy(
        update={"all_required_passed": False}
    )
    _patch_cli(monkeypatch, tmp_path, probe=probe, report=report)
    assert sandbox_cli.main(["qualify", "--output-dir", str(tmp_path / "out")]) == 4
    assert (tmp_path / "out" / "sandbox-conformance-report.json").is_file()
    assert not (tmp_path / "out" / "confinement-qualification.json").exists()


def test_cli_status_without_qualification(tmp_path: Path, monkeypatch, capsys):
    _patch_cli(monkeypatch, tmp_path)
    assert sandbox_cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["qualification_match"] is False


def test_cli_subprocess_probe_without_bwrap_exits_two(tmp_path: Path):
    env = dict(os.environ)
    env["PATH"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "vibereview.runtime.sandbox", "probe", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "unavailable"


def test_cli_qualify_and_status_without_bwrap_discovery(tmp_path: Path, monkeypatch, capsys):
    """Hermetic CLI tests pass when bwrap executable is completely absent from host/PATH."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    _, report, fingerprint = _patch_cli(monkeypatch, tmp_path)

    out_dir = tmp_path / "artifacts"
    code = sandbox_cli.main(
        ["qualify", "--network-policy", "deny", "--json", "--output-dir", str(out_dir)]
    )
    assert code == 0
    assert (out_dir / "sandbox-probe.json").is_file()
    assert (out_dir / "sandbox-conformance-report.json").is_file()
    assert (out_dir / "confinement-qualification.json").is_file()

    # Verify status also operates hermetically without bwrap
    capsys.readouterr()
    status_code = sandbox_cli.main(["status", "--json"])
    assert status_code == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["qualification_match"] is True
    assert status_payload["report_hash"] == report.report_hash


def test_cli_qualify_unavailable_or_blocked_never_constructs_backend(tmp_path: Path, monkeypatch, capsys):
    """Non-usable probes must return documented exit codes without constructing backend or store records."""
    out_dir = tmp_path / "artifacts"

    # UNAVAILABLE probe -> exit 2
    _patch_cli(monkeypatch, tmp_path, probe=fabricated_probe(status=SandboxProbeStatus.UNAVAILABLE))
    assert sandbox_cli.main(["qualify", "--output-dir", str(out_dir)]) == 2
    assert len(_StubBubblewrapExecutionBackend.instances) == 0
    assert not (out_dir / "confinement-qualification.json").exists()
    assert not (tmp_path / "store").exists()

    # BLOCKED probe -> exit 3
    _patch_cli(monkeypatch, tmp_path, probe=fabricated_probe(status=SandboxProbeStatus.BLOCKED))
    assert sandbox_cli.main(["qualify", "--output-dir", str(out_dir)]) == 3
    assert len(_StubBubblewrapExecutionBackend.instances) == 0
    assert not (out_dir / "confinement-qualification.json").exists()


def test_cli_qualify_conformance_failure_never_stores_qualification(tmp_path: Path, monkeypatch, capsys):
    """Failed conformance suite must not issue or persist a qualification record."""
    probe = fabricated_probe()
    failing_report = fabricated_passing_report(probe).model_copy(
        update={"all_required_passed": False}
    )
    _, _, fingerprint = _patch_cli(monkeypatch, tmp_path, probe=probe, report=failing_report)
    out_dir = tmp_path / "artifacts"

    rc = sandbox_cli.main(["qualify", "--output-dir", str(out_dir)])
    assert rc == 4
    assert not (out_dir / "confinement-qualification.json").exists()
    assert load_qualification_for_fingerprint(fingerprint, root=tmp_path / "store") is None


def test_cli_internal_unexpected_error_exits_five(tmp_path: Path, monkeypatch, capsys):
    """Any unexpected exception inside CLI dispatch terminates with exit code 5."""
    _patch_cli(monkeypatch, tmp_path)

    def _broken_store(*args, **kwargs):
        raise OSError("Simulated disk failure")

    monkeypatch.setattr(sandbox_cli, "store_qualification", _broken_store)
    rc = sandbox_cli.main(["qualify"])
    assert rc == 5
    err = capsys.readouterr().err
    assert "Internal error: OSError: Simulated disk failure" in err


# --- §6.5: pytest suite and runner cannot drift apart ---


@pytest.mark.sandbox_conformance
def test_required_cases_match_pytest_conformance_names():
    source = (
        Path(__file__).resolve().parent / "test_subprocess_confinement.py"
    ).read_text(encoding="utf-8")
    pytest_names = set(re.findall(r"^def (test_\w+)\(", source, re.MULTILINE))
    missing = set(REQUIRED_CONFORMANCE_TESTS) - pytest_names
    assert not missing, f"required conformance cases missing from pytest suite: {missing}"


# --- §6.1-§6.6: gated end-to-end on a usable host ---


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_end_to_end_qualification_on_usable_host(tmp_path: Path):
    require_usable_sandbox()

    report = run_sandbox_conformance()
    assert report.all_required_passed, [
        (case.case_id, case.diagnostic) for case in report.cases if not case.passed
    ]
    assert report.suite_version == CONFORMANCE_SUITE_VERSION
    assert compute_conformance_report_hash(report) == report.report_hash

    backend = BubblewrapExecutionBackend(
        LauncherConfiguration(worker_script=CONFORMANCE_WORKER_PATH),
        network_policy=NetworkPolicy.DENY,
    )
    fingerprint = compute_current_qualification_fingerprint(backend, NetworkPolicy.DENY)
    qualification = issue_qualification(report, fingerprint)

    store_root = tmp_path / "store"
    stored_path = store_qualification(qualification, report, root=store_root)
    assert stored_path.is_file()
    loaded = load_qualification_for_fingerprint(fingerprint, root=store_root)
    assert loaded is not None

    probe = probe_sandbox_capabilities()
    require_real_engine_qualification(
        backend, qualification, fingerprint, current_probe=probe, report=report
    )

    artifacts = tmp_path / "artifacts"
    env = dict(os.environ)
    env[QUALIFICATION_STORE_ENV_VAR] = str(tmp_path / "cli-store")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vibereview.runtime.sandbox",
            "qualify",
            "--network-policy",
            "deny",
            "--json",
            "--output-dir",
            str(artifacts),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr
    for name in (
        "sandbox-probe.json",
        "sandbox-conformance-report.json",
        "confinement-qualification.json",
    ):
        assert (artifacts / name).is_file(), name

    status = subprocess.run(
        [sys.executable, "-m", "vibereview.runtime.sandbox", "status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["qualification_match"] is True

    # Surface the fingerprint for the milestone report.
    print(f"END_TO_END_STORAGE_FINGERPRINT={compute_storage_fingerprint(fingerprint)}")
    print(f"END_TO_END_REPORT_HASH={report.report_hash}")
