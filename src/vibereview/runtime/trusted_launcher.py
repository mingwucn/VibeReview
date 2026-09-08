"""Trusted resource-limit launcher (goal.md §6).

Hierarchy:
  VibeReview supervisor → trusted RLIMIT launcher → qualified confinement backend → engine

Applies supported Linux RLIMITs before exec, writes an applied-limits report to
launcher/applied_limits.json, and execs the inner command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


# String codes matching ResourceLimitCode
MAX_OPEN_FILES = "max_open_files"
MAX_CPU_TIME = "max_cpu_time"
MAX_WRITABLE_SINGLE_FILE_BYTES = "max_writable_single_file_bytes"
MAX_ADDRESS_SPACE = "max_address_space"
MAX_PROCESS_COUNT = "max_process_count"


def _target_value(requested: int, hard: int) -> int:
    infinity = getattr(resource, "RLIM_INFINITY", -1) if resource is not None else -1
    if hard in (-1, infinity) or hard < 0:
        return requested
    return min(requested, hard)


def apply_limits(policy: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if resource is None:
        return records

    # 1. RLIMIT_NOFILE (authoritative)
    max_open_files = policy.get("max_open_files")
    if max_open_files is not None and hasattr(resource, "RLIMIT_NOFILE"):
        try:
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = _target_value(int(max_open_files), cur_hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, cur_hard))
            new_soft, new_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            records.append(
                {
                    "code": MAX_OPEN_FILES,
                    "requested_value": int(max_open_files),
                    "applied_soft_value": new_soft,
                    "applied_hard_value": new_hard,
                    "supported": True,
                    "authoritative": True,
                    "note": None,
                }
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Failed to set RLIMIT_NOFILE: {exc}") from exc

    # 2. RLIMIT_CPU (authoritative)
    max_cpu_seconds = policy.get("max_cpu_seconds")
    if max_cpu_seconds is not None and hasattr(resource, "RLIMIT_CPU"):
        try:
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_CPU)
            target = _target_value(int(max_cpu_seconds), cur_hard)
            resource.setrlimit(resource.RLIMIT_CPU, (target, cur_hard))
            new_soft, new_hard = resource.getrlimit(resource.RLIMIT_CPU)
            records.append(
                {
                    "code": MAX_CPU_TIME,
                    "requested_value": int(max_cpu_seconds),
                    "applied_soft_value": new_soft,
                    "applied_hard_value": new_hard,
                    "supported": True,
                    "authoritative": True,
                    "note": None,
                }
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Failed to set RLIMIT_CPU: {exc}") from exc

    # 3. RLIMIT_FSIZE (authoritative)
    max_writable_single_file_bytes = policy.get("max_writable_single_file_bytes")
    if max_writable_single_file_bytes is not None and hasattr(resource, "RLIMIT_FSIZE"):
        try:
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            target = _target_value(int(max_writable_single_file_bytes), cur_hard)
            resource.setrlimit(resource.RLIMIT_FSIZE, (target, cur_hard))
            new_soft, new_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            records.append(
                {
                    "code": MAX_WRITABLE_SINGLE_FILE_BYTES,
                    "requested_value": int(max_writable_single_file_bytes),
                    "applied_soft_value": new_soft,
                    "applied_hard_value": new_hard,
                    "supported": True,
                    "authoritative": True,
                    "note": None,
                }
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Failed to set RLIMIT_FSIZE: {exc}") from exc

    # 4. RLIMIT_AS (supported but non-authoritative)
    max_address_space_bytes = policy.get("max_address_space_bytes")
    if max_address_space_bytes is not None and hasattr(resource, "RLIMIT_AS"):
        try:
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_AS)
            target = _target_value(int(max_address_space_bytes), cur_hard)
            resource.setrlimit(resource.RLIMIT_AS, (target, cur_hard))
            new_soft, new_hard = resource.getrlimit(resource.RLIMIT_AS)
            records.append(
                {
                    "code": MAX_ADDRESS_SPACE,
                    "requested_value": int(max_address_space_bytes),
                    "applied_soft_value": new_soft,
                    "applied_hard_value": new_hard,
                    "supported": True,
                    "authoritative": False,
                    "note": "supported but non-authoritative across all platforms",
                }
            )
        except (OSError, ValueError) as exc:
            records.append(
                {
                    "code": MAX_ADDRESS_SPACE,
                    "requested_value": int(max_address_space_bytes),
                    "applied_soft_value": None,
                    "applied_hard_value": None,
                    "supported": False,
                    "authoritative": False,
                    "note": f"RLIMIT_AS unsupported or rejected: {exc}",
                }
            )

    # 5. RLIMIT_NPROC (defense-in-depth only, process monitor is authoritative)
    max_processes = policy.get("max_processes")
    if max_processes is not None and hasattr(resource, "RLIMIT_NPROC"):
        try:
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_NPROC)
            records.append(
                {
                    "code": MAX_PROCESS_COUNT,
                    "requested_value": int(max_processes),
                    "applied_soft_value": cur_soft,
                    "applied_hard_value": cur_hard,
                    "supported": True,
                    "authoritative": False,
                    "note": "defense in depth; monitor remains authoritative",
                }
            )
        except (OSError, ValueError) as exc:
            records.append(
                {
                    "code": MAX_PROCESS_COUNT,
                    "requested_value": int(max_processes),
                    "applied_soft_value": None,
                    "applied_hard_value": None,
                    "supported": False,
                    "authoritative": False,
                    "note": f"RLIMIT_NPROC unsupported: {exc}",
                }
            )

    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VibeReview Trusted RLIMIT Launcher")
    parser.add_argument("--policy", required=True, help="Path to launcher_policy.json")
    parser.add_argument("--report", required=True, help="Path to applied_limits.json")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute")

    args = parser.parse_args(argv)
    policy_path = Path(args.policy).resolve()
    report_path = Path(args.report).resolve()

    # Verify policy is from trusted launcher directory and not from engine-provided locations
    if policy_path.parent.name != "launcher":
        print(
            f"trusted_launcher: policy file must reside in launcher/, got {policy_path.parent}",
            file=sys.stderr,
        )
        return 125

    forbidden = {"bundle", "output", "scratch"}
    if any(part in forbidden for part in policy_path.parts):
        print(
            f"trusted_launcher: policy path contains forbidden directory component: {policy_path}",
            file=sys.stderr,
        )
        return 125

    if not policy_path.is_file():
        print(f"trusted_launcher: policy file not found: {policy_path}", file=sys.stderr)
        return 125

    try:
        policy_data = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(policy_data, dict):
            raise ValueError("policy root must be a JSON object")
    except Exception as exc:
        print(f"trusted_launcher: invalid policy file: {exc}", file=sys.stderr)
        return 125

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("trusted_launcher: no command to execute", file=sys.stderr)
        return 125

    try:
        records = apply_limits(policy_data)
    except Exception as exc:
        print(f"trusted_launcher: setrlimit failure before engine launch: {exc}", file=sys.stderr)
        return 125

    try:
        report_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"trusted_launcher: failed to write applied limits report: {exc}", file=sys.stderr)
        return 125

    # Exec inner command
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"trusted_launcher: exec failed: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
