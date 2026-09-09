"""Generic, engine-neutral integration for operator-supplied Git libraries."""

from .bibliography import load_bibliography, parse_bibtex_bytes, parse_bibtex_text
from .git_source import (
    PinnedGitSource,
    check_repository_clean,
    compute_content_sha256,
    enumerate_commit_blobs,
    inspect_configured_gitlink,
    inspect_repository_commit,
    sanitized_git_environment,
    snapshot_library_state,
    verify_library_integrity,
)
from .graph import LibraryGraphAdapter, ReadOnlyGraphAdapter
from .inventory import (
    build_library_inventory,
    classify_document,
    extract_citekey_candidate,
    extract_title_candidate,
    generate_inventory_markdown,
)
from .models import (
    BibEntryRecord,
    CommitMismatchError,
    CorpusImportResult,
    CorpusImportSource,
    CorpusIntegrityError,
    CorpusNotImportedError,
    CorpusLockManifest,
    CorpusLockPaper,
    CorpusSelectionDocument,
    CorpusSelectionError,
    CorpusSelectionManifest,
    DirtyWorkingTreeError,
    DocumentKind,
    ExcludedEntryRecord,
    GenerationLibraryManifest,
    GitlinkModeError,
    GraphSchemaReport,
    GraphSchemaStatus,
    LibraryConfig,
    LibraryDocumentRecord,
    LibraryError,
    LibraryNotFoundError,
    LibraryNotInitializedError,
    LibraryStateSnapshot,
    LibrarySourceObject,
    MetadataConflictReport,
    MetadataStatus,
    PathSecurityError,
    SourceMappingEntry,
    SourceMappingReport,
    SourceStatus,
    UnsupportedGitObjectError,
    UnsupportedGraphSchemaError,
    UpstreamIntegrityReport,
)
from .project_config import (
    ProjectLibraryConfig,
    ProjectMetadata,
    ProjectRetrievalConfig,
    ReviewProjectConfig,
    load_review_config,
)
from .resolver import resolve_source_mappings
from .retrieval import (
    CandidateHitOrigin,
    DeterministicTextRetriever,
    GraphAssistedRetriever,
    RawCandidateHit,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
    VerifiedCorpus,
)
from .evidence_task import (
    ASSESS_EVIDENCE_ADAPTER_CONTRACT_VERSION,
    AssessEvidencePromotionAdapter,
    AssessEvidencePromotionArtifact,
    EVIDENCE_TASK_ARTIFACT_ROOT,
    EVIDENCE_TASK_ARTIFACT_VERSION,
    run_assess_evidence_task,
)
from .retrieval_promotion import (
    CoupledEvidenceProposal,
    RetrievalPromotionDecision,
    RetrievalPromotionError,
    RetrievalPromotionRecord,
    RetrievalPromotionRequest,
    RetrievalPromotionResult,
    decisions_for_selected_candidates,
)
from .selection import (
    import_selected_corpus,
    load_corpus_lock,
    load_selection_manifest,
    save_selection_manifest,
    validate_selection_manifest,
    verify_corpus_lock,
)
from .pilot_setup import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_SCOPE_FACT_TEXT,
    PILOT_SETUP_VERSION,
    SyntheticPilotSetupResult,
    register_synthetic_pilot_setup,
)
from .pilot_controller import (
    MOCK_USAGE_UNAVAILABLE_REASON,
    PILOT_CONTROLLER_VERSION,
    PilotBudgetError,
    PilotDomainArtifacts,
    SyntheticPilotController,
    SyntheticPilotControllerError,
    SyntheticPilotControllerResult,
)

__all__ = [name for name in globals() if not name.startswith("_")]
