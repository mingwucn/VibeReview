"""Prompt-infrastructure slice: registry verification, deterministic compilation,
review-project artifact versioning, and Deep Research run registration.

Normative contract: ``docs/operations/review_prompt_protocol.md``. This package
is engineering provenance machinery only; it never creates canonical evidence.
"""

from __future__ import annotations

import os
from pathlib import Path

PROMPT_ROOT = Path(__file__).resolve().parent.parent / "prompts"
COMPILER_VERSION = "vibereview-prompt-compiler-1"
MANIFEST_FORMAT_VERSION = "vibereview-compiled-prompt-manifest-1"
RUN_FORMAT_VERSION = "vibereview-deep-research-run-1"


def get_prompt_root() -> Path:
    """The effective prompt root: ``VIBEREVIEW_PROMPT_ROOT`` when set, else PROMPT_ROOT."""

    override = os.environ.get("VIBEREVIEW_PROMPT_ROOT")
    return Path(override) if override else PROMPT_ROOT

from .compiler import (  # noqa: E402
    CANONICAL_ROLE_ORDER,
    compile_prompt,
    extract_outline_section,
    load_manifest,
    revalidate_manifest_inputs,
    validate_compiled_against_manifest,
    write_compiled,
)
from .models import (  # noqa: E402
    CompiledPromptManifest,
    CompileResult,
    ComponentRef,
    DeepResearchRunManifest,
    FocusModuleEntry,
    InputRef,
    PromptClass,
    PromptingError,
    PromptRegistry,
    PromptRegistryEntry,
    PromptStatus,
    ProtocolBundle,
    RegistryIssue,
    ResolvedPrompt,
    SupplementRef,
    VerificationReport,
    parse_prompt_ref,
)
from .project import (  # noqa: E402
    PACKAGE_TEMPLATE_DIR,
    REQUIRED_DIRECTORIES,
    ProjectStatus,
    init_project,
    list_versions,
    load_prompt_profile,
    migrate_prompt_profile,
    project_status,
    render_project_template,
    resolve_current,
    write_versioned_artifact,
)
from .registry import (  # noqa: E402
    load_registry,
    resolve_focus_module,
    resolve_prompt,
    verify_registry,
)
from .runs import register_run  # noqa: E402

__all__ = [
    "PROMPT_ROOT",
    "COMPILER_VERSION",
    "MANIFEST_FORMAT_VERSION",
    "RUN_FORMAT_VERSION",
    "get_prompt_root",
    "CANONICAL_ROLE_ORDER",
    "PACKAGE_TEMPLATE_DIR",
    "REQUIRED_DIRECTORIES",
    "CompiledPromptManifest",
    "CompileResult",
    "ComponentRef",
    "DeepResearchRunManifest",
    "FocusModuleEntry",
    "InputRef",
    "ProjectStatus",
    "PromptClass",
    "PromptingError",
    "PromptRegistry",
    "PromptRegistryEntry",
    "PromptStatus",
    "ProtocolBundle",
    "RegistryIssue",
    "ResolvedPrompt",
    "SupplementRef",
    "VerificationReport",
    "compile_prompt",
    "extract_outline_section",
    "init_project",
    "list_versions",
    "load_manifest",
    "load_prompt_profile",
    "load_registry",
    "migrate_prompt_profile",
    "parse_prompt_ref",
    "project_status",
    "register_run",
    "render_project_template",
    "resolve_current",
    "resolve_focus_module",
    "resolve_prompt",
    "revalidate_manifest_inputs",
    "validate_compiled_against_manifest",
    "verify_registry",
    "write_compiled",
    "write_versioned_artifact",
]
