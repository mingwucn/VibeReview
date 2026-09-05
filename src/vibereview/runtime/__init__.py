"""Public runtime API through the MockEngine milestone."""

from .dto import (
    AssessClaimInput,
    AuditPropositionInput,
    CandidateClaimProposal,
    ClaimAssessmentProposal,
    DiscoveryProposalBundle,
    GenerateCandidateClaimsInput,
    SemanticAuditProposal,
    ThemeProposal,
)
from .engine import AgentEngine, MockEngine, MockResponse
from .kernel import ProjectRuntime
from .records import (
    AgentResult,
    AgentTask,
    AttemptOutcome,
    RuntimeConfig,
    RuntimeResult,
    TaskAttemptRecord,
    TaskSpec,
    TaskType,
    fallback_allowed,
)
from .repository import (
    CrashPoint,
    GenerationStore,
    InjectedCrash,
    PromotionPayload,
    StaleSnapshotError,
)
from .specs import TASK_SPECS
from .state import RepositorySnapshot

__all__ = [
    "AgentEngine",
    "AgentResult",
    "AgentTask",
    "AssessClaimInput",
    "AttemptOutcome",
    "AuditPropositionInput",
    "CandidateClaimProposal",
    "ClaimAssessmentProposal",
    "CrashPoint",
    "DiscoveryProposalBundle",
    "GenerateCandidateClaimsInput",
    "GenerationStore",
    "InjectedCrash",
    "MockEngine",
    "MockResponse",
    "ProjectRuntime",
    "PromotionPayload",
    "RepositorySnapshot",
    "RuntimeConfig",
    "RuntimeResult",
    "SemanticAuditProposal",
    "StaleSnapshotError",
    "TASK_SPECS",
    "TaskAttemptRecord",
    "TaskSpec",
    "TaskType",
    "ThemeProposal",
    "fallback_allowed",
]
