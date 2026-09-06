"""Operator CLI for sandbox probing, qualification, and status (spec §6.6).

``python -m vibereview.runtime.sandbox probe`` classifies the current host's
namespace support. ``qualify`` runs the deterministic conformance suite and,
when every required case passes, issues and stores a
:class:`ConfinementQualification` bound to the current host and code
fingerprints. ``status`` re-probes the host and reports whether a stored
qualification matches the current fingerprint.

Exit codes:

- ``0``: probe USABLE; qualification issued/stored; or status reported.
- ``2``: sandbox unavailable (bubblewrap missing or unusable kernel state).
- ``3``: sandbox blocked by host policy (e.g. AppArmor userns restriction).
- ``4``: conformance failed (a required case failed, or the report could not
  be certified into a qualification).
- ``5``: internal error (unexpected exception).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .conformance import CONFORMANCE_WORKER_PATH, run_sandbox_conformance
from .confinement import (
    NetworkPolicy,
    SandboxNotQualifiedError,
    SandboxProbeStatus,
    compute_current_qualification_fingerprint,
    issue_qualification,
    probe_sandbox_capabilities,
)
from .execution import BubblewrapExecutionBackend, LauncherConfiguration
from .qualification_store import (
    compute_storage_fingerprint,
    default_qualification_root,
    load_qualification_for_fingerprint,
    store_qualification,
)

if TYPE_CHECKING:
    from .conformance import SandboxConformanceReport

EXIT_OK = 0
EXIT_UNAVAILABLE = 2
EXIT_BLOCKED = 3
EXIT_CONFORMANCE_FAILED = 4
EXIT_INTERNAL_ERROR = 5

_PROBE_ARTIFACT = "sandbox-probe.json"
_REPORT_ARTIFACT = "sandbox-conformance-report.json"
_QUALIFICATION_ARTIFACT = "confinement-qualification.json"


def _probe_exit_code(status: SandboxProbeStatus) -> int:
    if status is SandboxProbeStatus.BLOCKED:
        return EXIT_BLOCKED
    return EXIT_UNAVAILABLE


def _write_artifact(output_dir: Path, name: str, json_text: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    path.write_text(json_text + "\n", encoding="utf-8")
    return path


def _run_probe(argv: argparse.Namespace) -> int:
    probe = probe_sandbox_capabilities()
    if argv.json:
        print(probe.model_dump_json(indent=2))
    else:
        print(f"Sandbox probe status: {probe.status.value}")
        print(f"  diagnostic: {probe.diagnostic}")
        if probe.executable_path is not None:
            print(f"  bubblewrap: {probe.executable_path} (version {probe.backend_version})")
        for command in probe.commands:
            print(f"  [{command.name}] exit={command.exit_code} stderr={command.stderr}")
    if argv.output_dir is not None:
        path = _write_artifact(argv.output_dir, _PROBE_ARTIFACT, probe.model_dump_json(indent=2))
        if not argv.json:
            print(f"  probe artifact: {path}")
    if probe.status is not SandboxProbeStatus.USABLE:
        return _probe_exit_code(probe.status)
    return EXIT_OK


def _failing_case_lines(report: SandboxConformanceReport) -> list[str]:
    lines = []
    for case in report.cases:
        if case.case_id in report.required_case_ids and not case.passed:
            lines.append(f"  FAIL {case.case_id}: {case.diagnostic}")
    return lines


def _run_qualify(argv: argparse.Namespace) -> int:
    network_policy = (
        NetworkPolicy.HOST
        if argv.network_policy == "host"
        else NetworkPolicy.DENY
    )
    probe = probe_sandbox_capabilities()
    if argv.output_dir is not None:
        _write_artifact(argv.output_dir, _PROBE_ARTIFACT, probe.model_dump_json(indent=2))
    if probe.status is not SandboxProbeStatus.USABLE:
        if argv.json:
            print(probe.model_dump_json(indent=2))
        else:
            print(f"Sandbox not usable: {probe.status.value}: {probe.diagnostic}")
        return _probe_exit_code(probe.status)

    report = run_sandbox_conformance(network_policy=network_policy, probe=probe)
    if argv.output_dir is not None:
        _write_artifact(argv.output_dir, _REPORT_ARTIFACT, report.model_dump_json(indent=2))
    if not report.all_required_passed:
        if argv.json:
            print(report.model_dump_json(indent=2))
        else:
            print("Sandbox conformance FAILED; required cases that did not pass:")
            for line in _failing_case_lines(report):
                print(line)
        return EXIT_CONFORMANCE_FAILED

    backend = BubblewrapExecutionBackend(
        LauncherConfiguration(worker_script=CONFORMANCE_WORKER_PATH),
        network_policy=network_policy,
    )
    fingerprint = compute_current_qualification_fingerprint(backend, network_policy)
    try:
        qualification = issue_qualification(report, fingerprint)
    except SandboxNotQualifiedError as exc:
        if argv.json:
            print(report.model_dump_json(indent=2))
        else:
            print(f"Sandbox conformance report could not be certified: {exc}")
        return EXIT_CONFORMANCE_FAILED

    storage_fingerprint = compute_storage_fingerprint(fingerprint)
    stored_path = store_qualification(qualification, report)
    if argv.output_dir is not None:
        _write_artifact(
            argv.output_dir, _QUALIFICATION_ARTIFACT, qualification.model_dump_json(indent=2)
        )
    if argv.json:
        print(qualification.model_dump_json(indent=2))
    else:
        print("Sandbox qualification issued.")
        print(f"  storage fingerprint: {storage_fingerprint}")
        print(f"  report hash: {report.report_hash}")
        print(f"  qualified at: {qualification.qualified_at}")
        print(f"  stored at: {stored_path}")
        print(f"  store root: {default_qualification_root()}")
    return EXIT_OK


def _run_status(argv: argparse.Namespace) -> int:
    probe = probe_sandbox_capabilities()
    if probe.status is not SandboxProbeStatus.USABLE:
        if argv.json:
            print(probe.model_dump_json(indent=2))
        else:
            print(f"Sandbox not usable: {probe.status.value}: {probe.diagnostic}")
        return _probe_exit_code(probe.status)

    network_policy = (
        NetworkPolicy.HOST
        if argv.network_policy == "host"
        else NetworkPolicy.DENY
    )
    backend = BubblewrapExecutionBackend(
        LauncherConfiguration(worker_script=CONFORMANCE_WORKER_PATH),
        network_policy=network_policy,
    )
    fingerprint = compute_current_qualification_fingerprint(backend, network_policy)
    stored = load_qualification_for_fingerprint(fingerprint)
    matched = stored is not None
    if argv.json:
        payload: dict[str, object] = {
            "probe": probe.model_dump(mode="json"),
            "current_storage_fingerprint": compute_storage_fingerprint(fingerprint),
            "qualification_match": matched,
        }
        if matched:
            payload["qualified_at"] = stored.qualification.qualified_at
            payload["report_hash"] = stored.report.report_hash
        print(json.dumps(payload, indent=2))
    else:
        print(f"Sandbox probe status: {probe.status.value}")
        if matched:
            print("Stored qualification matches the current host and code fingerprints.")
            print(f"  qualified at: {stored.qualification.qualified_at}")
            print(f"  report hash: {stored.report.report_hash}")
            print(f"  store root: {default_qualification_root()}")
        else:
            print("No stored qualification matches the current host and code fingerprints.")
            print("Run `python -m vibereview.runtime.sandbox qualify` to qualify this host.")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vibereview.runtime.sandbox",
        description="Probe, qualify, and inspect the VibeReview sandbox on this host.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    probe_cmd = subcommands.add_parser("probe", help="Classify host sandbox capability.")
    probe_cmd.add_argument("--json", action="store_true", help="Emit the probe result as JSON.")
    probe_cmd.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write sandbox-probe.json into this directory.",
    )
    probe_cmd.set_defaults(handler=_run_probe)

    qualify_cmd = subcommands.add_parser(
        "qualify", help="Run the conformance suite and issue a stored qualification."
    )
    qualify_cmd.add_argument("--json", action="store_true", help="Emit JSON output.")
    qualify_cmd.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write probe/report/qualification artifacts into this directory.",
    )
    qualify_cmd.add_argument(
        "--network-policy",
        choices=("deny", "host"),
        default="deny",
        help="Network policy the qualification covers (default: deny).",
    )
    qualify_cmd.set_defaults(handler=_run_qualify)

    status_cmd = subcommands.add_parser(
        "status", help="Report probe status and stored-qualification match."
    )
    status_cmd.add_argument("--json", action="store_true", help="Emit JSON output.")
    status_cmd.add_argument(
        "--network-policy",
        choices=("deny", "host"),
        default="deny",
        help="Network policy fingerprint to look up (default: deny).",
    )
    status_cmd.set_defaults(handler=_run_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except SandboxNotQualifiedError as exc:
        print(f"Sandbox not qualified: {exc}", file=sys.stderr)
        return EXIT_CONFORMANCE_FAILED
    except Exception as exc:  # noqa: BLE001 - CLI boundary: any failure is exit 5.
        print(f"Internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
