"""Explicit runtime-only factories for offline and qualified live engines.

These adapters do not alter scientific DTOs.  The DeepSeek REST factory binds
one exact Bubblewrap backend to one stored-qualification gate and requires
explicit authorization.  The CLI adapter implementations are inert protocol
boundaries: every CLI live factory remains unconditionally fail-closed until a
machine-local attestation issuer and revalidation path exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import stat
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from vibereview.ids import Sha256

from . import credential_exec_shim, external_worker, trusted_launcher
from .confinement import NetworkPolicy
from .credentials import (
    EngineCredentialProvider,
    EnvironmentFileCredentialProvider,
    LockedJsonTokenCredentialProvider,
)
from .execution import (
    _LIVE_FACTORY_TOKEN,
    BubblewrapExecutionBackend,
    LauncherConfiguration,
    SubprocessEngine,
    TemporaryWorkspaceBackend,
)
from .hashing import canonical_json_bytes, hash_file, hash_json
from .live_execution import StoredSandboxQualificationGate
from .qualification_store import default_qualification_root
from .records import RuntimeModel
from .request_compiler import RequestCompilationPolicy
from .subprocess import SubprocessPolicy


MAX_LIVE_TRANSPORT_BYTES = 524_288
ADAPTER_VERSION = "1"
DEEPSEEK_CHAT_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})


class EngineUnavailableError(RuntimeError):
    """A requested live adapter lacks a required, current capability proof."""


class LiveExecutionAuthorization(RuntimeModel):
    """Explicit caller acknowledgement required by every live factory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledgement: Literal["LIVE_EXTERNAL_EXECUTION_AUTHORIZED"]


class CliProvider(StrEnum):
    KIMI = "kimi"
    AGY = "agy"
    CODEX = "codex"


class CliProtocol(StrEnum):
    KIMI_ACP = "kimi_acp"
    AGY_PRINT_JSON = "agy_print_json"
    CODEX_EXEC_JSON = "codex_exec_json"


class CliCredentialDelivery(StrEnum):
    FIXED_ENV = "fixed_env"
    DEDICATED_HOME_TOKEN = "dedicated_home_token"


class CliCapabilityProof(RuntimeModel):
    """Non-authoritative candidate facts for a future machine-local proof.

    The executable source is runtime-only and excluded from dumps.  The
    expected hash, version and help hash can become cache/receipt semantics
    only after a trusted issuer and current revalidation path are implemented.
    Caller-created instances never enable a live CLI factory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: CliProvider
    protocol: CliProtocol
    executable_path: Path = Field(repr=False, exclude=True)
    executable_sha256: Sha256
    version: str
    help_sha256: Sha256
    noninteractive_verified: bool
    structured_output_verified: bool
    credential_delivery: CliCredentialDelivery
    tool_secret_canary_passed: bool
    refresh_required: bool = False

    @field_validator("executable_path")
    @classmethod
    def _absolute_executable(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("qualified CLI executable path must be absolute")
        return value

    @field_validator("version")
    @classmethod
    def _nonempty_version(cls, value: str) -> str:
        if not value or any(character in value for character in "\x00\r\n"):
            raise ValueError("qualified CLI version must be one non-empty line")
        return value

    def fingerprint(self) -> Sha256:
        return Sha256(
            hash_json(
                {
                    "provider": self.provider.value,
                    "protocol": self.protocol.value,
                    "executable_sha256": self.executable_sha256,
                    "version": self.version,
                    "help_sha256": self.help_sha256,
                    "noninteractive_verified": self.noninteractive_verified,
                    "structured_output_verified": self.structured_output_verified,
                    "credential_delivery": self.credential_delivery.value,
                    "tool_secret_canary_passed": self.tool_secret_canary_passed,
                    "refresh_required": self.refresh_required,
                }
            )
        )


def bounded_live_policy(*, timeout_seconds: float = 300.0) -> SubprocessPolicy:
    if timeout_seconds <= 5:
        raise ValueError("live timeout must leave time for trusted cleanup")
    return SubprocessPolicy(
        timeout_seconds=timeout_seconds,
        terminate_grace_seconds=2.0,
        max_stdout_bytes=65_536,
        max_stderr_bytes=65_536,
        max_proposal_bytes=MAX_LIVE_TRANSPORT_BYTES,
        max_writable_tree_bytes=67_108_864,
        max_writable_entries=1_024,
        max_writable_single_file_bytes=MAX_LIVE_TRANSPORT_BYTES,
        max_writable_directory_depth=12,
        max_open_files=256,
        max_processes=32,
        max_cpu_seconds=max(1, int(timeout_seconds)),
        max_address_space_bytes=2_147_483_648,
        writable_tree_scan_interval_seconds=0.05,
        inherited_environment_allowlist=("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE"),
    )


def _validate_live_envelope(
    policy: SubprocessPolicy, request_policy: RequestCompilationPolicy
) -> None:
    if policy.timeout_seconds <= 5:
        raise ValueError(
            "live subprocess timeout must exceed 5 seconds and preserve the "
            "trusted 3-second outer cleanup margin"
        )
    ceiling = bounded_live_policy()
    bounded_policy_fields = (
        "timeout_seconds", "terminate_grace_seconds", "max_stdout_bytes",
        "max_stderr_bytes", "max_proposal_bytes", "max_writable_tree_bytes",
        "max_writable_entries", "max_writable_single_file_bytes",
        "max_writable_directory_depth", "max_open_files", "max_processes",
        "max_cpu_seconds", "max_address_space_bytes",
    )
    for field in bounded_policy_fields:
        value = getattr(policy, field)
        maximum = getattr(ceiling, field)
        if value is None or maximum is None or value > maximum:
            raise ValueError(f"live subprocess policy relaxes {field}")
    if not set(policy.inherited_environment_allowlist).issubset(
        ceiling.inherited_environment_allowlist
    ):
        raise ValueError("live subprocess policy relaxes environment allowlist")
    if policy.allowed_output_files != ("proposal.json",):
        raise ValueError("live subprocess policy must allow only proposal.json")
    if (
        policy.writable_tree_scan_interval_seconds
        > ceiling.writable_tree_scan_interval_seconds
    ):
        raise ValueError("live subprocess policy relaxes writable-tree scan interval")
    request_ceiling = RequestCompilationPolicy()
    for field in RequestCompilationPolicy.model_fields:
        if getattr(request_policy, field) > getattr(request_ceiling, field):
            raise ValueError(f"live request policy relaxes {field}")


def _worker_path() -> Path:
    return Path(external_worker.__file__).resolve()


def _trusted_launcher_path() -> Path:
    return Path(trusted_launcher.__file__).resolve()


def _credential_shim_path() -> Path:
    return Path(credential_exec_shim.__file__).resolve()


def _launcher_configuration(**kwargs: object) -> LauncherConfiguration:
    """Pin every trusted script at factory construction and receipt identity."""

    worker = _worker_path()
    return LauncherConfiguration(
        worker_script=worker,
        worker_script_sha256=Sha256(hash_file(worker)),
        trusted_launcher_sha256=Sha256(hash_file(_trusted_launcher_path())),
        credential_exec_shim_sha256=Sha256(hash_file(_credential_shim_path())),
        **kwargs,
    )


def _outside_project(path: Path, project_root: Path, label: str) -> None:
    candidate = path.expanduser().resolve(strict=False)
    root = project_root.resolve()
    if candidate == root or root in candidate.parents:
        raise EngineUnavailableError(f"{label} must be outside the review project")


def _live_engine(
    *,
    name: str,
    version: str,
    mode: str,
    launcher: LauncherConfiguration,
    credential_provider: EngineCredentialProvider,
    policy: SubprocessPolicy,
    request_policy: RequestCompilationPolicy,
    qualification_root: Path | None,
    bwrap_path: Path | str | None,
    project_root: Path,
) -> SubprocessEngine:
    store_root = (qualification_root or default_qualification_root()).expanduser().resolve(
        strict=False
    )
    _outside_project(store_root, project_root, "qualification store")
    backend = BubblewrapExecutionBackend(
        launcher,
        bwrap_path=bwrap_path,
        network_policy=NetworkPolicy.HOST,
    )
    gate = StoredSandboxQualificationGate(
        backend,
        network_policy=NetworkPolicy.HOST,
        qualification_root=store_root,
    )
    return SubprocessEngine(
        launcher_configuration=launcher,
        modes=(mode,),
        policy=policy,
        credential_provider=credential_provider,
        backend=backend,
        compile_request=True,
        request_policy=request_policy,
        qualification_gate=gate,
        live=True,
        _live_factory_token=_LIVE_FACTORY_TOKEN,
        authorized_project_root=project_root,
        name=name,
        version=version,
    )


def build_offline_engine(
    proposal: dict,
    *,
    policy: SubprocessPolicy | None = None,
    request_policy: RequestCompilationPolicy | None = None,
) -> SubprocessEngine:
    """Build a deterministic, explicitly non-live fixture adapter."""

    raw = canonical_json_bytes(proposal)
    if len(raw) > MAX_LIVE_TRANSPORT_BYTES:
        raise ValueError("offline proposal exceeds the adapter byte bound")
    launcher = _launcher_configuration(
        worker_config={
            "mode": "offline",
            "fixture_path": "launcher/offline_proposal.json",
            "max_transport_bytes": MAX_LIVE_TRANSPORT_BYTES,
        },
        proposal_payloads={"offline_proposal.json": raw},
    )
    backend = TemporaryWorkspaceBackend(launcher)
    return SubprocessEngine(
        launcher_configuration=launcher,
        modes=("offline",),
        policy=policy or bounded_live_policy(timeout_seconds=30.0),
        backend=backend,
        compile_request=True,
        request_policy=request_policy or RequestCompilationPolicy(),
        live=False,
        name="offline-fixture",
        version=ADAPTER_VERSION,
    )


def build_live_deepseek_engine(
    *,
    authorization: LiveExecutionAuthorization,
    project_root: Path,
    model: str,
    api_key_env: str = "DEEPSEEK_API_KEY",
    qualification_root: Path | None = None,
    bwrap_path: Path | str | None = None,
    policy: SubprocessPolicy | None = None,
    request_policy: RequestCompilationPolicy | None = None,
) -> SubprocessEngine:
    if not isinstance(authorization, LiveExecutionAuthorization) or (
        authorization.acknowledgement != "LIVE_EXTERNAL_EXECUTION_AUTHORIZED"
    ):
        raise ValueError("explicit live-execution authorization is required")
    if model not in DEEPSEEK_CHAT_MODELS:
        raise ValueError("DeepSeek model is not in the adapter's verified allowlist")
    if api_key_env != "DEEPSEEK_API_KEY":
        raise ValueError("DeepSeek live factory requires DEEPSEEK_API_KEY")
    try:
        project_item = project_root.lstat()
    except OSError as exc:
        raise ValueError("live project root must be an existing directory") from exc
    if stat.S_ISLNK(project_item.st_mode) or not stat.S_ISDIR(project_item.st_mode):
        raise ValueError("live project root must be a non-symlink directory")
    resolved_project_root = project_root.resolve(strict=True)
    selected_policy = policy or bounded_live_policy()
    limits = SubprocessPolicy.model_validate(
        selected_policy.model_dump(mode="python")
    )
    selected_request_policy = request_policy or RequestCompilationPolicy()
    request_limits = RequestCompilationPolicy.model_validate(
        selected_request_policy.model_dump(mode="python")
    )
    _validate_live_envelope(limits, request_limits)
    launcher = _launcher_configuration(
        worker_config={
            "mode": "deepseek",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "model": model,
            "max_transport_bytes": MAX_LIVE_TRANSPORT_BYTES,
            "timeout_seconds": min(240.0, limits.timeout_seconds - 3.0),
            "max_tokens": 8_192,
        },
    )
    credentials = EnvironmentFileCredentialProvider(
        env_var=api_key_env,
        provider_id="deepseek-rest",
        fixed_file_name="credential",
    )
    return _live_engine(
        name="deepseek-rest",
        version=ADAPTER_VERSION,
        mode="deepseek",
        launcher=launcher,
        credential_provider=credentials,
        policy=limits,
        request_policy=request_limits,
        qualification_root=qualification_root,
        bwrap_path=bwrap_path,
        project_root=resolved_project_root,
    )


def build_live_kimi_engine(
    *,
    authorization: LiveExecutionAuthorization,
    project_root: Path,
    proof: CliCapabilityProof,
    credential_provider: LockedJsonTokenCredentialProvider,
    model: str,
    qualification_root: Path | None = None,
    bwrap_path: Path | str | None = None,
    policy: SubprocessPolicy | None = None,
    request_policy: RequestCompilationPolicy | None = None,
) -> SubprocessEngine:
    del authorization
    raise EngineUnavailableError(
        "Kimi CLI ACP is unavailable: no machine-local issued capability/auth "
        "attestation and zero-tool secret-isolation canary authority exists"
    )


def build_live_agy_engine(
    *,
    authorization: LiveExecutionAuthorization,
    project_root: Path,
    proof: CliCapabilityProof,
    credential_provider: LockedJsonTokenCredentialProvider,
    qualification_root: Path | None = None,
    bwrap_path: Path | str | None = None,
    policy: SubprocessPolicy | None = None,
    request_policy: RequestCompilationPolicy | None = None,
) -> SubprocessEngine:
    del authorization
    raise EngineUnavailableError(
        "Agy CLI is unavailable: its sterile dedicated credential/config and "
        "zero-tool capability have not been qualified"
    )


def build_live_codex_engine(
    *,
    authorization: LiveExecutionAuthorization,
    project_root: Path,
    proof: CliCapabilityProof,
    credential_provider: LockedJsonTokenCredentialProvider,
    model: str,
    qualification_root: Path | None = None,
    bwrap_path: Path | str | None = None,
    policy: SubprocessPolicy | None = None,
    request_policy: RequestCompilationPolicy | None = None,
) -> SubprocessEngine:
    del authorization
    raise EngineUnavailableError(
        "Codex CLI is unavailable: no machine-local issued capability/auth "
        "attestation and child-environment canary authority exists"
    )


class AdapterAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PilotEngineMatrix:
    """Honest current matrix: DeepSeek constructible, all CLI arms closed."""

    deepseek_primary: SubprocessEngine
    kimi_fallback: AdapterAvailability = AdapterAvailability.UNAVAILABLE
    agy_comparison: AdapterAvailability = AdapterAvailability.UNAVAILABLE
    codex_comparison: AdapterAvailability = AdapterAvailability.UNAVAILABLE

    def __post_init__(self) -> None:
        if self.deepseek_primary.name != "deepseek-rest":
            raise ValueError("matrix primary must be DeepSeek REST")
        if any(
            status is not AdapterAvailability.UNAVAILABLE
            for status in (
                self.kimi_fallback, self.agy_comparison, self.codex_comparison
            )
        ):
            raise ValueError("unqualified CLI adapters cannot be marked operational")

    @property
    def operational(self) -> tuple[SubprocessEngine, ...]:
        return (self.deepseek_primary,)


@dataclass(frozen=True)
class FivePaperComparisonPlan:
    """Exactly five independent paper keys crossed with Agy and Codex."""

    paper_keys: tuple[str, str, str, str, str]
    engines: PilotEngineMatrix

    def __post_init__(self) -> None:
        if len(set(self.paper_keys)) != 5 or any(not key for key in self.paper_keys):
            raise ValueError("comparison pilot requires five distinct non-empty paper keys")

    def independent_runs(self) -> tuple[tuple[str, SubprocessEngine], ...]:
        raise EngineUnavailableError(
            "five-paper comparison is unavailable until Agy and Codex capability "
            "attestations are qualified"
        )
