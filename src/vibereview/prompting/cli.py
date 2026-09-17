"""Command-line interface for the prompt-infrastructure slice."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence

from vibereview.runtime.hashing import hash_file

from .compiler import compile_prompt, write_compiled
from .models import PromptClass, PromptRegistry, PromptStatus, PromptingError
from .project import (
    init_project,
    load_prompt_profile,
    project_status,
    resolve_current,
)
from .registry import load_registry, parse_prompt_ref, verify_registry
from .runs import register_run

_CURRENT_FAMILIES: dict[str, tuple[str, str, str]] = {
    "project_brief": ("project", "project_brief", "md"),
    "protocol": ("project", "protocol", "md"),
    "structure": ("project", "structure", "md"),
    "prompt_profile": ("project", "prompt_profile", "yaml"),
    "outline": ("outline", "outline", "md"),
}


def _prompt_root() -> Path:
    from . import get_prompt_root

    return get_prompt_root()


def _canonical_ref(
    registry: PromptRegistry, ref: str, *, released_only: bool
) -> str:
    """Resolve ``REF`` or bare ``ID`` to an exact ``<id>@<version>`` key."""

    if "@" in ref:
        parse_prompt_ref(ref)
        if ref not in registry.prompts:
            raise PromptingError("PROMPT_NOT_FOUND", f"prompt {ref!r} is not registered")
        return ref
    candidates = [
        (key, entry)
        for key, entry in registry.prompts.items()
        if entry.id == ref and (entry.status is PromptStatus.RELEASED or not released_only)
    ]
    if not candidates:
        raise PromptingError("PROMPT_NOT_FOUND", f"prompt {ref!r} is not registered")
    candidates.sort(key=lambda item: tuple(int(p) for p in item[1].version.split(".")))
    return candidates[-1][0]


def _current_artifact(project_root: Path, role: str) -> Path:
    mapping = _CURRENT_FAMILIES.get(role)
    if mapping is None:
        raise PromptingError(
            "UNKNOWN_INPUT_ROLE", f"role {role!r} has no @current family mapping"
        )
    directory, family, extension = mapping
    current = resolve_current(project_root / directory, family, extension=extension)
    if current is None:
        raise PromptingError(
            "ARTIFACT_NOT_FOUND",
            f"no current {family}_v*.{extension} artifact in {project_root / directory}",
        )
    return current


def _project_relative(project_root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return path


def _cmd_prompts_list(args: argparse.Namespace) -> int:
    registry = load_registry()
    prompt_class = args.prompt_class.lower() if args.prompt_class else None
    status = args.status.upper() if args.status else None
    rows = []
    for ref, entry in sorted(registry.prompts.items()):
        if prompt_class is not None and entry.class_.value != prompt_class:
            continue
        if status is not None and entry.status.value != status:
            continue
        rows.append(
            f"{ref}  {entry.status.value}  {entry.class_.value}  "
            f"{entry.scientific_role}  {entry.sha256[:16]}"
        )
    if rows:
        print("\n".join(rows))
    return 0


def _cmd_prompts_show(args: argparse.Namespace) -> int:
    registry = load_registry()
    ref = _canonical_ref(registry, args.ref, released_only=False)
    entry = registry.prompts[ref]
    path = _prompt_root() / entry.path
    if not path.is_file():
        raise PromptingError("PROMPT_FILE_MISSING", f"{ref}: recorded file {entry.path!r} is missing")
    if hash_file(path) != entry.sha256:
        raise PromptingError("PROMPT_HASH_MISMATCH", f"{ref}: recorded hash does not match {path}")
    print(f"id: {entry.id}")
    print(f"version: {entry.version}")
    print(f"class: {entry.class_.value}")
    print(f"status: {entry.status.value}")
    print(f"scientific_role: {entry.scientific_role}")
    print(f"release_date: {entry.release_date}")
    print(f"change_note: {entry.change_note}")
    print(f"path: {entry.path}")
    print(f"sha256: {entry.sha256}")
    print()
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def _cmd_prompts_verify(args: argparse.Namespace) -> int:
    report = verify_registry()
    if report.ok:
        print("ok")
        return 0
    for issue in report.issues:
        print(f"{issue.path}: {issue.code}: {issue.message}")
    return 1


def _cmd_prompts_status(args: argparse.Namespace) -> int:
    registry = load_registry()
    by_class: dict[str, Counter[str]] = {}
    for entry in registry.prompts.values():
        counts = by_class.setdefault(entry.class_.value, Counter())
        counts[entry.status.value] += 1
    for prompt_class in sorted(by_class):
        counts = by_class[prompt_class]
        rendered = " ".join(
            f"{status.value}={counts.get(status.value, 0)}"
            for status in PromptStatus
        )
        print(f"{prompt_class}: {rendered}")
    focus_counts = Counter(entry.status.value for entry in registry.focus_modules.values())
    rendered = " ".join(
        f"{status.value}={focus_counts.get(status.value, 0)}" for status in PromptStatus
    )
    print(f"focus_modules: {rendered}")
    for name, bundle in sorted(registry.protocols.items()):
        print(f"protocol {name} ({bundle.status}):")
        for slot, pin in bundle.prompts.items():
            print(f"  {slot}: {pin}")
    return 0


def _parse_input_specs(project_root: Path, specs: Sequence[str]) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    for spec in specs:
        role, sep, value = spec.partition("=")
        if not sep or not role or not value:
            raise PromptingError(
                "UNKNOWN_INPUT_ROLE", f"--input expects ROLE=PATH, got {spec!r}"
            )
        if value == "@current":
            path = _current_artifact(project_root, role)
        else:
            path = _project_relative(project_root, Path(value))
        inputs[role] = path
    return inputs


def _compile_and_write(
    *,
    registry: PromptRegistry,
    project_root: Path,
    prompt_ref: str,
    compiled_prompt_id: str,
    inputs: dict[str, Path],
    focus_modules: Sequence[str],
    section_id: str | None,
    supplement: Path | None,
    out_dir: Path,
) -> int:
    previous_cwd = Path.cwd()
    out_dir = out_dir.resolve()
    os.chdir(project_root)
    try:
        result = compile_prompt(
            registry=registry,
            registry_root=_prompt_root(),
            prompt_ref=prompt_ref,
            compiled_prompt_id=compiled_prompt_id,
            inputs=inputs,
            focus_modules=focus_modules,
            section_id=section_id,
            supplement=supplement,
        )
        md_path, manifest_path = write_compiled(result, out_dir)
    finally:
        os.chdir(previous_cwd)
    print(md_path)
    print(manifest_path)
    return 0


def _cmd_prompts_compile(args: argparse.Namespace) -> int:
    registry = load_registry()
    project_root = Path(args.project).resolve()
    ref = _canonical_ref(registry, args.prompt_ref, released_only=True)
    entry = registry.prompts[ref]
    out_dir = (
        project_root / "deep_research" / "compiled"
        if entry.class_ is PromptClass.DEEP_RESEARCH
        else project_root / "run_manifests" / "compiled"
    )
    inputs = _parse_input_specs(project_root, args.input or [])
    supplement = (
        _project_relative(project_root, Path(args.supplement))
        if args.supplement
        else None
    )
    compiled_id = args.id or parse_prompt_ref(ref)[0]
    return _compile_and_write(
        registry=registry,
        project_root=project_root,
        prompt_ref=ref,
        compiled_prompt_id=compiled_id,
        inputs=inputs,
        focus_modules=args.focus or [],
        section_id=args.section,
        supplement=supplement,
        out_dir=out_dir,
    )


def _cmd_prompts_compile_dr(args: argparse.Namespace) -> int:
    project_root = Path(args.project).resolve()
    _, profile = load_prompt_profile(project_root)
    protocol = profile.get("protocol")
    protocol_id = protocol.get("id") if isinstance(protocol, dict) else None
    registry = load_registry()
    bundle = registry.protocols.get(protocol_id)
    if bundle is None:
        raise PromptingError(
            "PROMPT_NOT_FOUND", f"protocol {protocol_id!r} is not in the prompt registry"
        )
    prompt_ref = bundle.prompts.get("section_research")
    if prompt_ref is None:
        raise PromptingError(
            "PROMPT_NOT_FOUND",
            f"protocol {bundle.name!r} pins no 'section_research' prompt",
        )
    profile_modules = profile.get("focus_modules")
    if not isinstance(profile_modules, dict):
        profile_modules = {}
    focus_modules = list(profile_modules.get(args.section) or [])
    focus_modules.extend(args.focus or [])
    inputs = {
        "project_brief": _current_artifact(project_root, "project_brief"),
        "protocol": _current_artifact(project_root, "protocol"),
        "outline": _current_artifact(project_root, "outline"),
    }
    supplement = (
        _project_relative(project_root, Path(args.supplement))
        if args.supplement
        else None
    )
    compiled_id = args.id or f"DR_{args.section.replace('.', '_')}"
    return _compile_and_write(
        registry=registry,
        project_root=project_root,
        prompt_ref=prompt_ref,
        compiled_prompt_id=compiled_id,
        inputs=inputs,
        focus_modules=focus_modules,
        section_id=args.section,
        supplement=supplement,
        out_dir=project_root / "deep_research" / "compiled",
    )


def _cmd_project_init(args: argparse.Namespace) -> int:
    root = Path(args.path)
    project_id = args.project_id or root.name or "review_project"
    topic = args.topic or root.name
    init_project(
        root,
        project_id=project_id,
        working_topic=topic,
        core_processes=tuple(args.core_processes or ()),
        secondary_processes=tuple(args.secondary_processes or ()),
        future_outlook=tuple(args.future_outlook or ()),
        cutoff=args.cutoff,
    )
    print(root)
    return 0


def _cmd_project_status(args: argparse.Namespace) -> int:
    status = project_status(Path(args.project))
    print(f"project: {status.project_id}")
    print(f"protocol: {status.protocol}")
    for family, versions in status.artifact_versions.items():
        rendered = ", ".join(str(v) for v in versions) if versions else "-"
        print(f"{family}: {rendered}")
    runs = ", ".join(status.registered_runs) if status.registered_runs else "-"
    print(f"runs: {runs}")
    rendered = ", ".join(status.compiled_prompts) if status.compiled_prompts else "-"
    print(f"compiled prompts: {rendered}")
    return 0


def _cmd_research_register_output(args: argparse.Namespace) -> int:
    project_root = Path(args.project).resolve()
    manifest = register_run(
        project_root=project_root,
        run_id=args.run,
        compiled_prompt_path=Path(args.prompt),
        raw_output_path=Path(args.output),
        operator=args.operator or "",
        provider=args.provider,
        mode=args.mode,
        model=args.model,
        model_version=args.model_version,
        started_at=args.started_at,
        completed_at=args.completed_at,
    )
    print(project_root / "deep_research" / "runs" / manifest.run_id / "run.json")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibereview")
    commands = parser.add_subparsers(dest="command", required=True)

    prompts = commands.add_parser("prompts", help="prompt registry operations")
    prompt_commands = prompts.add_subparsers(dest="prompts_command", required=True)

    list_parser = prompt_commands.add_parser("list", help="list registered prompts")
    list_parser.add_argument(
        "--class",
        dest="prompt_class",
        choices=[cls.value for cls in PromptClass],
        type=str.lower,
    )
    list_parser.add_argument(
        "--status",
        choices=[status.value for status in PromptStatus],
        type=str.upper,
    )
    list_parser.set_defaults(func=_cmd_prompts_list)

    show_parser = prompt_commands.add_parser("show", help="show one prompt")
    show_parser.add_argument("ref")
    show_parser.set_defaults(func=_cmd_prompts_show)

    verify_parser = prompt_commands.add_parser("verify", help="verify the registry")
    verify_parser.set_defaults(func=_cmd_prompts_verify)

    status_parser = prompt_commands.add_parser("status", help="registry status summary")
    status_parser.set_defaults(func=_cmd_prompts_status)

    compile_parser = prompt_commands.add_parser("compile", help="compile a prompt")
    compile_parser.add_argument("prompt_ref")
    compile_parser.add_argument("--project", required=True)
    compile_parser.add_argument("--id")
    compile_parser.add_argument("--input", action="append", default=[])
    compile_parser.add_argument("--focus", action="append", default=[])
    compile_parser.add_argument("--section")
    compile_parser.add_argument("--supplement")
    compile_parser.set_defaults(func=_cmd_prompts_compile)

    compile_dr_parser = prompt_commands.add_parser(
        "compile-dr", help="compile the pinned section-research prompt"
    )
    compile_dr_parser.add_argument("--project", required=True)
    compile_dr_parser.add_argument("--section", required=True)
    compile_dr_parser.add_argument("--id")
    compile_dr_parser.add_argument("--focus", action="append", default=[])
    compile_dr_parser.add_argument("--supplement")
    compile_dr_parser.set_defaults(func=_cmd_prompts_compile_dr)

    project = commands.add_parser("project", help="review project operations")
    project_commands = project.add_subparsers(dest="project_command", required=True)

    init_parser = project_commands.add_parser("init", help="create a review project")
    init_parser.add_argument("path")
    init_parser.add_argument("--project-id", dest="project_id")
    init_parser.add_argument("--topic")
    init_parser.add_argument("--core-process", dest="core_processes", action="append")
    init_parser.add_argument("--secondary-process", dest="secondary_processes", action="append")
    init_parser.add_argument("--future-outlook", dest="future_outlook", action="append")
    init_parser.add_argument("--cutoff")
    init_parser.set_defaults(func=_cmd_project_init)

    project_status_parser = project_commands.add_parser(
        "status", help="summarize a review project"
    )
    project_status_parser.add_argument("--project", required=True)
    project_status_parser.set_defaults(func=_cmd_project_status)

    research = commands.add_parser("research", help="Deep Research run operations")
    research_commands = research.add_subparsers(
        dest="research_command", required=True
    )

    register_parser = research_commands.add_parser(
        "register-output", help="register a manually executed run"
    )
    register_parser.add_argument("--project", required=True)
    register_parser.add_argument("--run", required=True)
    register_parser.add_argument("--prompt", required=True)
    register_parser.add_argument("--output", required=True)
    register_parser.add_argument("--operator", default="")
    register_parser.add_argument("--provider", default="unknown")
    register_parser.add_argument("--mode", default="unknown")
    register_parser.add_argument("--model", default="unknown")
    register_parser.add_argument("--model-version", dest="model_version", default="unknown")
    register_parser.add_argument("--started-at", dest="started_at", default="unknown")
    register_parser.add_argument("--completed-at", dest="completed_at", default="unknown")
    register_parser.set_defaults(func=_cmd_research_register_output)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PromptingError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
