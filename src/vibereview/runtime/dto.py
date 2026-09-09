"""Private invocation DTOs and sanitized engine-facing input/proposal DTOs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import (
    AggregateRelation,
    AggregateStrength,
    AuditClassVerdict,
    AuditProvenanceVerdict,
    ClaimDecision,
    EvidenceRelation,
    EvidenceStrength,
    EvidenceSufficiency,
    FinalClaimStatus,
    Origin,
    PropositionContentClass,
    RejectionBasis,
    RetrievalDispositionStatus,
    RetrievalIntent,
    ValidationCheckResult,
    WithinPaperConsistency,
)
from vibereview.ids import (
    ClaimId,
    ClaimPaperEvidenceId,
    CorpusFactId,
    EvidenceId,
    PaperId,
    ProcessFactId,
    PropositionId,
    QueryId,
    Sha256,
    SpanId,
    ThemeId,
    parse_query_id,
)
from vibereview.models import ComponentRelations, EvidenceQuality

from .records import RESOURCE_ID_PATTERN, RuntimeModel


class DTOModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ResourceRef = Annotated[str, Field(pattern=RESOURCE_ID_PATTERN)]

# These proposal-side limits intentionally mirror the deterministic retrieval
# boundary in ``vibereview.library.retrieval`` without introducing a runtime
# dependency on the optional library package.
MAX_RETRIEVAL_QUERY_CHARS = 4_096
MAX_RETRIEVAL_QUERY_UTF8_BYTES = 16_384
MAX_RETRIEVAL_QUERY_SCOPE_CLAIMS = 50
MAX_RETRIEVAL_QUERY_PROPOSALS = 600

# Runtime-local copies of the bounded deterministic-retrieval contract.  They
# intentionally match the optional library adapter without importing that
# higher layer into provider-neutral runtime DTOs.
MAX_ASSESS_EVIDENCE_CANDIDATES = 100
MAX_EVIDENCE_PROPOSAL_TEXT_CHARS = 16_384
MAX_EVIDENCE_PROPOSAL_LIMITATIONS = 100
MAX_EVIDENCE_PROPOSAL_LIMITATION_CHARS = 4_096

# Claim aggregation, assessment, revision, and final validation are executable
# Package C tasks.  Their DTOs therefore need schema-level finite envelopes,
# not just later semantic/canonical-ID capacity checks.
MAX_CLAIM_TASK_ITEMS = 500
MAX_CLAIM_TASK_TEXT_CHARS = 16_384

# Accepted draft tasks use deliberately smaller per-object budgets than the
# generation-owned artifact envelope in ``runtime.drafts``.  Keeping these
# limits local avoids a DTO -> artifact-store dependency while ensuring a
# schema-valid proposal cannot grow without bound before coupled validation.
MAX_DRAFT_TASK_ITEMS = 100
MAX_DRAFT_REFS_PER_ITEM = 100
MAX_DRAFT_TEXT_CHARS = 4_096
MAX_DRAFT_TEXT_UTF8_BYTES = 16_384
MAX_DRAFT_PROPOSAL_UTF8_BYTES = 4 * 1024 * 1024

# Package C's strict discovery wrappers use these tighter, finite envelopes.
# The legacy TaskSpecs remain source-compatible, but every newly introduced
# collection is bounded at schema-validation time before an engine result can
# reach a coupled adapter.
MAX_DISCOVERY_RESOURCES = 16
MAX_DISCOVERY_ITEMS = 100
MAX_DISCOVERY_LOCATORS_PER_ITEM = 16
MAX_DISCOVERY_TEXT_CHARS = 4_096
MAX_DISCOVERY_TEXT_UTF8_BYTES = 16_384
MAX_CONCEPT_SKETCH_TERMS = 100
MAX_DISCOVERY_PROPOSAL_UTF8_BYTES = 4 * 1024 * 1024

DraftTaskId = Annotated[
    str, Field(min_length=8, max_length=64, pattern=r"^TASK[0-9]{4,}$")
]
DraftLocalRef = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"),
]
DraftObjectRef = Annotated[str, Field(min_length=1, max_length=128)]
ClaimTaskText = Annotated[str, Field(max_length=MAX_CLAIM_TASK_TEXT_CHARS)]
NonEmptyClaimTaskText = Annotated[
    str, Field(min_length=1, max_length=MAX_CLAIM_TASK_TEXT_CHARS)
]


def _has_canonical_numeric_id(value: str, prefix: str) -> bool:
    suffix = value.removeprefix(prefix)
    return value.startswith(prefix) and len(suffix) >= 4 and suffix.isdigit()


def _validate_draft_text(value: str, *, label: str) -> None:
    if len(value.encode("utf-8")) > MAX_DRAFT_TEXT_UTF8_BYTES:
        raise ValueError(f"{label} exceeds its UTF-8 byte budget")


def _validate_unique(values: list[object], *, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class ParseDeepResearchInvocation(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    document_paths: Annotated[
        list[Path], Field(max_length=MAX_DISCOVERY_RESOURCES)
    ]


class ParseDeepResearchInput(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    document_resource_ids: Annotated[
        list[ResourceRef], Field(max_length=MAX_DISCOVERY_RESOURCES)
    ]


class AcceptedDiscoveryArtifactInvocation(DTOModel):
    """Private exact reference to one generation-owned discovery artifact."""

    artifact_kind: Literal["parse", "challenge"]
    owner_generation: Annotated[int, Field(ge=1)]
    source_generation: Annotated[int, Field(ge=0)]
    task_id: DraftTaskId
    semantic_task_key: Sha256
    artifact_hash: Sha256
    artifact_path: Path

    @model_validator(mode="after")
    def _artifact_identity_is_private_and_owned(
        self,
    ) -> "AcceptedDiscoveryArtifactInvocation":
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("discovery artifact generation lineage is not consecutive")
        if not self.artifact_path.is_absolute():
            raise ValueError("discovery artifact path must be absolute")
        if ".." in self.artifact_path.parts:
            raise ValueError("discovery artifact path must not contain '..' parts")
        return self


class AcceptedDiscoveryArtifactInput(DTOModel):
    """Sanitized engine-facing identity for a discovery artifact resource."""

    artifact_kind: Literal["parse", "challenge"]
    owner_generation: Annotated[int, Field(ge=1)]
    source_generation: Annotated[int, Field(ge=0)]
    task_id: DraftTaskId
    semantic_task_key: Sha256
    artifact_hash: Sha256
    resource_id: ResourceRef

    @model_validator(mode="after")
    def _artifact_generation_is_owned(self) -> "AcceptedDiscoveryArtifactInput":
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("discovery artifact generation lineage is not consecutive")
        return self


class CorpusChallengerInvocation(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    paper_ids: Annotated[list[PaperId], Field(max_length=MAX_DISCOVERY_ITEMS)]
    discovery_artifact: AcceptedDiscoveryArtifactInvocation | None = None
    paper_resource_paths: Annotated[
        list[Path], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    paper_source_hashes: Annotated[
        list[Sha256], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _optional_strict_inputs_are_coherent(self) -> "CorpusChallengerInvocation":
        _validate_unique(self.paper_ids, label="paper_ids")
        if self.discovery_artifact is not None and self.discovery_artifact.artifact_kind != "parse":
            raise ValueError("corpus challenger requires a parse discovery artifact")
        strict_lengths = (
            len(self.paper_resource_paths),
            len(self.paper_source_hashes),
        )
        if any(strict_lengths) and strict_lengths != (len(self.paper_ids),) * 2:
            raise ValueError(
                "paper resource paths and source hashes must exactly align with paper_ids"
            )
        return self


class CorpusChallengerInput(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    paper_ids: Annotated[list[PaperId], Field(max_length=MAX_DISCOVERY_ITEMS)]
    discovery_artifact: AcceptedDiscoveryArtifactInput | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    paper_resource_ids: Annotated[
        list[ResourceRef],
        Field(max_length=MAX_DISCOVERY_ITEMS, exclude_if=lambda value: not value),
    ] = Field(default_factory=list)
    paper_source_hashes: Annotated[
        list[Sha256],
        Field(max_length=MAX_DISCOVERY_ITEMS, exclude_if=lambda value: not value),
    ] = Field(default_factory=list)


class GenerateCandidateClaimsInvocation(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    existing_theme_ids: Annotated[
        list[ThemeId], Field(max_length=MAX_DISCOVERY_ITEMS)
    ]
    discovery_artifact: AcceptedDiscoveryArtifactInvocation | None = None
    challenge_artifact: AcceptedDiscoveryArtifactInvocation | None = None

    @model_validator(mode="after")
    def _optional_artifacts_are_a_pair(self) -> "GenerateCandidateClaimsInvocation":
        _validate_unique(self.existing_theme_ids, label="existing_theme_ids")
        if (self.discovery_artifact is None) != (self.challenge_artifact is None):
            raise ValueError("candidate generation requires both discovery artifacts or neither")
        if self.discovery_artifact is not None:
            assert self.challenge_artifact is not None
            if self.discovery_artifact.artifact_kind != "parse":
                raise ValueError("candidate generation discovery artifact must be parse")
            if self.challenge_artifact.artifact_kind != "challenge":
                raise ValueError("candidate generation challenge artifact must be challenge")
        return self


class GenerateCandidateClaimsInput(DTOModel):
    topic: Annotated[str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)]
    existing_theme_ids: Annotated[
        list[ThemeId], Field(max_length=MAX_DISCOVERY_ITEMS)
    ]
    discovery_artifact: AcceptedDiscoveryArtifactInput | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    challenge_artifact: AcceptedDiscoveryArtifactInput | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class GenerateRetrievalQueriesInvocation(DTOModel):
    claim_ids: Annotated[
        list[ClaimId],
        Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_SCOPE_CLAIMS),
    ]

    @model_validator(mode="after")
    def _claim_ids_are_unique(self) -> "GenerateRetrievalQueriesInvocation":
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("claim_ids must be unique")
        return self


class GenerateRetrievalQueriesInput(DTOModel):
    claim_ids: Annotated[
        list[ClaimId],
        Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_SCOPE_CLAIMS),
    ]


class _AssessEvidenceIdentity(DTOModel):
    source_generation: Annotated[int, Field(ge=0)]
    claim_id: ClaimId
    query_id: QueryId
    candidate_refs: Annotated[
        list[Sha256],
        Field(min_length=1, max_length=MAX_ASSESS_EVIDENCE_CANDIDATES),
    ]
    canonical_span_refs: Annotated[
        list[SpanId], Field(max_length=MAX_ASSESS_EVIDENCE_CANDIDATES)
    ]

    @model_validator(mode="after")
    def _query_and_candidates_are_exact(self) -> "_AssessEvidenceIdentity":
        encoded_claim, _, _ = parse_query_id(self.query_id)
        if encoded_claim != self.claim_id:
            raise ValueError("query_id must encode claim_id")
        if len(self.candidate_refs) != len(set(self.candidate_refs)):
            raise ValueError("candidate_refs must be unique")
        if len(self.canonical_span_refs) != len(set(self.canonical_span_refs)):
            raise ValueError("canonical_span_refs must be unique")
        return self


class AssessEvidenceInvocation(_AssessEvidenceIdentity):
    retrieval_ledger_path: Path


class AssessEvidenceInput(_AssessEvidenceIdentity):
    retrieval_ledger_resource_id: ResourceRef


class AggregatePaperEvidenceInvocation(DTOModel):
    claim_id: ClaimId
    paper_id: PaperId
    evidence_ids: Annotated[list[EvidenceId], Field(max_length=MAX_CLAIM_TASK_ITEMS)]


class AggregatePaperEvidenceInput(DTOModel):
    claim_id: ClaimId
    paper_id: PaperId
    evidence_ids: Annotated[list[EvidenceId], Field(max_length=MAX_CLAIM_TASK_ITEMS)]


class AssessClaimInvocation(DTOModel):
    claim_id: ClaimId
    claim_paper_evidence_ids: Annotated[
        list[ClaimPaperEvidenceId], Field(max_length=MAX_CLAIM_TASK_ITEMS)
    ]


class AssessClaimInput(DTOModel):
    claim_id: ClaimId
    claim_paper_evidence_ids: Annotated[
        list[ClaimPaperEvidenceId], Field(max_length=MAX_CLAIM_TASK_ITEMS)
    ]


class ReviseClaimInvocation(DTOModel):
    claim_id: ClaimId
    current_candidate_claim: NonEmptyClaimTaskText


class ReviseClaimInput(DTOModel):
    claim_id: ClaimId
    current_candidate_claim: NonEmptyClaimTaskText


class ValidateFinalClaimInvocation(DTOModel):
    claim_id: ClaimId
    proposed_final_claim: NonEmptyClaimTaskText


class ValidateFinalClaimInput(DTOModel):
    claim_id: ClaimId
    proposed_final_claim: NonEmptyClaimTaskText


class _PropositionSourceSelection(DTOModel):
    claim_packet_ids: Annotated[
        list[ClaimId], Field(max_length=MAX_DRAFT_TASK_ITEMS)
    ]
    corpus_fact_ids: Annotated[
        list[CorpusFactId], Field(max_length=MAX_DRAFT_TASK_ITEMS)
    ]
    process_fact_ids: Annotated[
        list[ProcessFactId], Field(max_length=MAX_DRAFT_TASK_ITEMS)
    ]

    @model_validator(mode="after")
    def _selected_sources_are_unique(self) -> "_PropositionSourceSelection":
        _validate_unique(self.claim_packet_ids, label="claim_packet_ids")
        _validate_unique(self.corpus_fact_ids, label="corpus_fact_ids")
        _validate_unique(self.process_fact_ids, label="process_fact_ids")
        if not (
            self.claim_packet_ids
            or self.corpus_fact_ids
            or self.process_fact_ids
        ):
            raise ValueError(
                "proposition source selection requires at least one claim, "
                "corpus, or process fact"
            )
        return self


class GeneratePropositionsInvocation(_PropositionSourceSelection):
    pass


class GeneratePropositionsInput(_PropositionSourceSelection):
    pass


class _AcceptedPropositionDraftIdentity(_PropositionSourceSelection):
    draft_kind: Literal["proposition_draft"]
    draft_owner_generation: Annotated[int, Field(ge=1)]
    draft_source_generation: Annotated[int, Field(ge=0)]
    draft_task_id: DraftTaskId
    draft_artifact_hash: Sha256
    draft_local_ref: DraftLocalRef

    @model_validator(mode="after")
    def _draft_generation_is_owned(self) -> "_AcceptedPropositionDraftIdentity":
        if self.draft_owner_generation != self.draft_source_generation + 1:
            raise ValueError(
                "draft_owner_generation must immediately follow draft_source_generation"
            )
        if _has_canonical_numeric_id(self.draft_local_ref, "PR"):
            raise ValueError("draft_local_ref must not be a canonical proposition ID")
        return self


class AuditPropositionInvocation(_AcceptedPropositionDraftIdentity):
    draft_artifact_path: Path

    @model_validator(mode="after")
    def _draft_path_is_private_and_absolute(self) -> "AuditPropositionInvocation":
        if not self.draft_artifact_path.is_absolute():
            raise ValueError("draft_artifact_path must be absolute")
        if ".." in self.draft_artifact_path.parts:
            raise ValueError("draft_artifact_path must not contain '..' parts")
        return self


class AuditPropositionInput(_AcceptedPropositionDraftIdentity):
    draft_resource_id: ResourceRef


class RenderProseInvocation(DTOModel):
    proposition_ids: Annotated[
        list[PropositionId], Field(min_length=1, max_length=MAX_DRAFT_TASK_ITEMS)
    ]

    @model_validator(mode="after")
    def _proposition_ids_are_unique(self) -> "RenderProseInvocation":
        _validate_unique(self.proposition_ids, label="proposition_ids")
        return self


class RenderProseInput(DTOModel):
    proposition_ids: Annotated[
        list[PropositionId], Field(min_length=1, max_length=MAX_DRAFT_TASK_ITEMS)
    ]

    @model_validator(mode="after")
    def _proposition_ids_are_unique(self) -> "RenderProseInput":
        _validate_unique(self.proposition_ids, label="proposition_ids")
        return self


class _AcceptedRenderedSentenceDraftIdentity(DTOModel):
    draft_kind: Literal["rendered_sentence_draft"]
    draft_owner_generation: Annotated[int, Field(ge=1)]
    draft_source_generation: Annotated[int, Field(ge=0)]
    draft_task_id: DraftTaskId
    draft_artifact_hash: Sha256
    draft_local_ref: DraftLocalRef
    source_proposition_ids: Annotated[
        list[PropositionId], Field(min_length=1, max_length=MAX_DRAFT_TASK_ITEMS)
    ]

    @model_validator(mode="after")
    def _draft_identity_is_exact(self) -> "_AcceptedRenderedSentenceDraftIdentity":
        if self.draft_owner_generation != self.draft_source_generation + 1:
            raise ValueError(
                "draft_owner_generation must immediately follow draft_source_generation"
            )
        if _has_canonical_numeric_id(self.draft_local_ref, "RS"):
            raise ValueError("draft_local_ref must not be a canonical sentence ID")
        _validate_unique(
            self.source_proposition_ids, label="source_proposition_ids"
        )
        return self


class AuditRenderedSentenceInvocation(_AcceptedRenderedSentenceDraftIdentity):
    draft_artifact_path: Path

    @model_validator(mode="after")
    def _draft_path_is_private_and_absolute(
        self,
    ) -> "AuditRenderedSentenceInvocation":
        if not self.draft_artifact_path.is_absolute():
            raise ValueError("draft_artifact_path must be absolute")
        if ".." in self.draft_artifact_path.parts:
            raise ValueError("draft_artifact_path must not contain '..' parts")
        return self


class AuditRenderedSentenceInput(_AcceptedRenderedSentenceDraftIdentity):
    draft_resource_id: ResourceRef


LocalRef = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"),
]
ObjectRef = Annotated[str, Field(min_length=1, max_length=128)]
DiscoveryText = Annotated[
    str, Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
]
DiscoveryTextList = Annotated[
    list[DiscoveryText], Field(max_length=MAX_CONCEPT_SKETCH_TERMS)
]


class ResourceTextLocator(DTOModel):
    """Exact Unicode code-point slice within one task-owned RES resource."""

    resource_id: ResourceRef
    resource_hash: Sha256
    start_offset: Annotated[int, Field(ge=0)]
    end_offset: Annotated[int, Field(gt=0)]
    source_span_hash: Sha256

    @model_validator(mode="after")
    def _offsets_are_ordered(self) -> "ResourceTextLocator":
        if self.end_offset <= self.start_offset:
            raise ValueError("resource locator end_offset must exceed start_offset")
        return self


class ThemeProposal(DTOModel):
    local_ref: LocalRef
    title: DiscoveryText
    description: DiscoveryText
    origin: Origin
    parent_ref: ObjectRef | None = None


class CandidateClaimProposal(DTOModel):
    local_ref: LocalRef
    theme_ref: ObjectRef
    candidate_claim: DiscoveryText
    origin: Origin
    origin_refs: Annotated[
        list[ObjectRef], Field(max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM)
    ]

    @model_validator(mode="after")
    def _origin_refs_are_unique(self) -> "CandidateClaimProposal":
        _validate_unique(self.origin_refs, label="origin_refs")
        return self


class DiscoverySourceBinding(DTOModel):
    target_ref: LocalRef
    locators: Annotated[
        list[ResourceTextLocator],
        Field(min_length=1, max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ]


class DiscoveryTerminologyProposal(DTOModel):
    local_ref: LocalRef
    term: DiscoveryText
    meaning: DiscoveryText
    locators: Annotated[
        list[ResourceTextLocator],
        Field(min_length=1, max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ]


class DiscoveryPaperCandidateProposal(DTOModel):
    local_ref: LocalRef
    citation: DiscoveryText
    relevance: DiscoveryText
    locators: Annotated[
        list[ResourceTextLocator],
        Field(min_length=1, max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ]


class DiscoveryFindingProposal(DTOModel):
    local_ref: LocalRef
    summary: DiscoveryText
    locators: Annotated[
        list[ResourceTextLocator],
        Field(min_length=1, max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ]


class PaperConceptSketchProposal(DTOModel):
    local_ref: LocalRef
    paper_ref: PaperId
    summary: DiscoveryText
    concepts: Annotated[
        list[DiscoveryText], Field(min_length=1, max_length=MAX_CONCEPT_SKETCH_TERMS)
    ]
    populations: DiscoveryTextList = Field(default_factory=list)
    methods: DiscoveryTextList = Field(default_factory=list)
    boundary_conditions: DiscoveryTextList = Field(default_factory=list)
    contradictions: DiscoveryTextList = Field(default_factory=list)
    locators: Annotated[
        list[ResourceTextLocator],
        Field(min_length=1, max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ]


DiscoveryCoverageStatus = Literal["covered", "partial", "missing", "contradicted"]
DiscoveryMissingDimension = Literal[
    "theme",
    "claim",
    "contradiction",
    "population",
    "method",
    "boundary_condition",
]


class DiscoveryCoverageFindingProposal(DTOModel):
    local_ref: LocalRef
    discovery_ref: LocalRef
    status: DiscoveryCoverageStatus
    paper_refs: Annotated[list[PaperId], Field(max_length=5)] = Field(
        default_factory=list
    )
    missing_dimensions: Annotated[
        list[DiscoveryMissingDimension], Field(max_length=6)
    ] = Field(default_factory=list)
    rationale: DiscoveryText
    locators: Annotated[
        list[ResourceTextLocator],
        Field(max_length=MAX_DISCOVERY_LOCATORS_PER_ITEM),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coverage_fields_are_coherent(self) -> "DiscoveryCoverageFindingProposal":
        _validate_unique(self.paper_refs, label="coverage paper_refs")
        _validate_unique(
            self.missing_dimensions, label="coverage missing_dimensions"
        )
        if self.status == "covered" and self.missing_dimensions:
            raise ValueError("covered discovery concepts cannot name missing dimensions")
        if self.status != "covered" and not self.missing_dimensions:
            raise ValueError(
                "non-covered discovery concepts require explicit missing dimensions"
            )
        if self.status in {"covered", "partial", "contradicted"} and not self.locators:
            raise ValueError("observed discovery coverage requires an exact paper locator")
        return self


class DiscoveryInputDispositionProposal(DTOModel):
    """Explicit inclusion/exclusion of one accepted discovery input item."""

    source_kind: Literal["parse", "challenge"]
    source_ref: LocalRef
    disposition: Literal["included", "excluded"]
    candidate_claim_refs: Annotated[
        list[LocalRef], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    reason: DiscoveryText

    @model_validator(mode="after")
    def _disposition_is_explicit_and_coherent(
        self,
    ) -> "DiscoveryInputDispositionProposal":
        _validate_unique(
            self.candidate_claim_refs, label="disposition candidate_claim_refs"
        )
        if self.disposition == "included" and not self.candidate_claim_refs:
            raise ValueError(
                "included discovery inputs require at least one candidate claim"
            )
        if self.disposition == "excluded" and self.candidate_claim_refs:
            raise ValueError(
                "excluded discovery inputs cannot name candidate claims"
            )
        return self


class DiscoveryProposalBundle(DTOModel):
    themes: Annotated[list[ThemeProposal], Field(max_length=MAX_DISCOVERY_ITEMS)] = (
        Field(default_factory=list)
    )
    claims: Annotated[
        list[CandidateClaimProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    source_bindings: Annotated[
        list[DiscoverySourceBinding], Field(max_length=2 * MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    terminology: Annotated[
        list[DiscoveryTerminologyProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    paper_candidates: Annotated[
        list[DiscoveryPaperCandidateProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    controversies: Annotated[
        list[DiscoveryFindingProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    gaps: Annotated[
        list[DiscoveryFindingProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    paper_concept_sketches: Annotated[
        list[PaperConceptSketchProposal], Field(max_length=5)
    ] = Field(default_factory=list)
    coverage_findings: Annotated[
        list[DiscoveryCoverageFindingProposal], Field(max_length=MAX_DISCOVERY_ITEMS)
    ] = Field(default_factory=list)
    input_dispositions: Annotated[
        list[DiscoveryInputDispositionProposal],
        Field(max_length=2 * MAX_DISCOVERY_ITEMS),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _proposal_payload_is_bounded(self) -> "DiscoveryProposalBundle":
        if (
            len(self.model_dump_json().encode("utf-8"))
            > MAX_DISCOVERY_PROPOSAL_UTF8_BYTES
        ):
            raise ValueError("discovery proposal bundle exceeds its UTF-8 byte budget")
        return self


class RetrievalQueryProposal(DTOModel):
    local_ref: LocalRef
    claim_ref: ObjectRef
    intent: RetrievalIntent
    query_text: Annotated[
        str, Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)
    ]

    @model_validator(mode="after")
    def _query_text_budget(self) -> "RetrievalQueryProposal":
        if len(self.query_text.encode("utf-8")) > MAX_RETRIEVAL_QUERY_UTF8_BYTES:
            raise ValueError(
                "retrieval query exceeds its deterministic UTF-8 byte budget"
            )
        return self


class RetrievalQueryProposalBundle(DTOModel):
    queries: Annotated[
        list[RetrievalQueryProposal],
        Field(max_length=MAX_RETRIEVAL_QUERY_PROPOSALS),
    ]


class EvidenceRecordProposal(DTOModel):
    local_ref: LocalRef
    claim_ref: ObjectRef
    retrieved_span_ref: ObjectRef
    paper_ref: ObjectRef
    relation_to_candidate: EvidenceRelation
    evidence_summary: str
    quality: EvidenceQuality
    assessment_note: str


class EvidenceRecordProposalBundle(DTOModel):
    evidence: list[EvidenceRecordProposal]


AssessEvidenceDisposition = Literal[
    RetrievalDispositionStatus.ASSESSED,
    RetrievalDispositionStatus.DUPLICATE,
    RetrievalDispositionStatus.REDUNDANT,
]


class EvidenceAssessmentProposal(DTOModel):
    """ID-less evidence fields coupled to one assessed retrieval candidate."""

    relation_to_candidate: EvidenceRelation
    evidence_summary: Annotated[
        str, Field(max_length=MAX_EVIDENCE_PROPOSAL_TEXT_CHARS)
    ]
    quality: EvidenceQuality
    assessment_note: Annotated[
        str, Field(max_length=MAX_EVIDENCE_PROPOSAL_TEXT_CHARS)
    ]

    @model_validator(mode="after")
    def _quality_diagnostics_are_bounded(self) -> "EvidenceAssessmentProposal":
        if len(self.quality.limitations) > MAX_EVIDENCE_PROPOSAL_LIMITATIONS:
            raise ValueError("evidence proposal has too many limitations")
        if any(
            len(item) > MAX_EVIDENCE_PROPOSAL_LIMITATION_CHARS
            for item in self.quality.limitations
        ):
            raise ValueError("evidence proposal limitation exceeds its text budget")
        return self


class EvidenceCandidateDecisionProposal(DTOModel):
    """One bounded disposition decision for a ledger-local candidate ref."""

    candidate_ref: Sha256
    status: AssessEvidenceDisposition
    reason: Annotated[
        str | None, Field(default=None, max_length=MAX_EVIDENCE_PROPOSAL_TEXT_CHARS)
    ]
    canonical_span_ref: SpanId | None = None
    evidence: EvidenceAssessmentProposal | None = None

    @model_validator(mode="after")
    def _evidence_is_coupled_exactly_once(self) -> "EvidenceCandidateDecisionProposal":
        if self.status is RetrievalDispositionStatus.ASSESSED:
            if self.canonical_span_ref is not None:
                raise ValueError("assessed candidate cannot name a canonical span")
            if self.evidence is None:
                raise ValueError(
                    "assessed candidate requires exactly one evidence proposal"
                )
        elif self.status is RetrievalDispositionStatus.DUPLICATE:
            if self.canonical_span_ref is None:
                raise ValueError("duplicate candidate requires a canonical span")
            if self.evidence is not None:
                raise ValueError("duplicate candidate cannot carry evidence")
        else:
            if self.canonical_span_ref is None and not self.reason:
                raise ValueError(
                    "redundant candidate requires a canonical span or reason"
                )
            if self.evidence is not None:
                raise ValueError("redundant candidate cannot carry evidence")
        return self


class AssessEvidenceProposalBundle(DTOModel):
    decisions: Annotated[
        list[EvidenceCandidateDecisionProposal],
        Field(min_length=1, max_length=MAX_ASSESS_EVIDENCE_CANDIDATES),
    ]

    @model_validator(mode="after")
    def _candidate_refs_are_unique(self) -> "AssessEvidenceProposalBundle":
        candidate_refs = [item.candidate_ref for item in self.decisions]
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("evidence decision candidate_refs must be unique")
        return self


class ClaimPaperEvidenceProposal(DTOModel):
    claim_ref: ObjectRef
    paper_ref: ObjectRef
    evidence_refs: Annotated[
        list[ObjectRef],
        Field(min_length=1, max_length=MAX_CLAIM_TASK_ITEMS),
    ]
    relation_to_candidate: AggregateRelation
    component_relations: ComponentRelations
    strength: EvidenceStrength
    within_paper_consistency: WithinPaperConsistency
    assessment_note: ClaimTaskText


class ClaimAssessmentProposal(DTOModel):
    claim_ref: ObjectRef
    aggregate_strength: AggregateStrength
    evidence_sufficiency: EvidenceSufficiency
    decision: ClaimDecision
    rejection_basis: RejectionBasis | None
    support_summary: ClaimTaskText
    contradiction_summary: ClaimTaskText
    qualification_summary: ClaimTaskText
    reason: ClaimTaskText

    @model_validator(mode="after")
    def _rejection_basis(self) -> "ClaimAssessmentProposal":
        if self.decision is ClaimDecision.REJECT and self.rejection_basis is None:
            raise ValueError("REJECT requires rejection_basis")
        if self.decision is not ClaimDecision.REJECT and self.rejection_basis is not None:
            raise ValueError("non-REJECT cannot have rejection_basis")
        return self


class RevisedClaimProposal(DTOModel):
    claim_ref: ObjectRef
    final_claim: NonEmptyClaimTaskText


class FinalPaperRelationProposal(DTOModel):
    claim_paper_evidence_ref: ObjectRef
    paper_ref: ObjectRef
    relation_to_final_claim: AggregateRelation


class FinalClaimValidationProposal(DTOModel):
    claim_ref: ObjectRef
    final_claim: NonEmptyClaimTaskText
    status: FinalClaimStatus
    paper_relations: Annotated[
        list[FinalPaperRelationProposal], Field(max_length=MAX_CLAIM_TASK_ITEMS)
    ]
    scope_check: ValidationCheckResult
    certainty_check: ValidationCheckResult
    causal_language_check: ValidationCheckResult
    numerical_claim_check: ValidationCheckResult
    notes: ClaimTaskText

    @model_validator(mode="after")
    def _valid_status_checks(self) -> "FinalClaimValidationProposal":
        if self.status is not FinalClaimStatus.VALID:
            return self
        if not self.paper_relations:
            raise ValueError("VALID final claim requires at least one paper relation")
        if self.scope_check is not ValidationCheckResult.PASS:
            raise ValueError("VALID final claim requires scope_check=pass")
        if self.certainty_check is not ValidationCheckResult.PASS:
            raise ValueError("VALID final claim requires certainty_check=pass")
        if self.causal_language_check not in {
            ValidationCheckResult.PASS,
            ValidationCheckResult.NOT_APPLICABLE,
        }:
            raise ValueError(
                "VALID final claim requires causal_language_check=pass or not_applicable"
            )
        if self.numerical_claim_check not in {
            ValidationCheckResult.PASS,
            ValidationCheckResult.NOT_APPLICABLE,
        }:
            raise ValueError(
                "VALID final claim requires numerical_claim_check=pass or not_applicable"
            )
        return self


class CitationBindingProposal(DTOModel):
    paper_ref: DraftObjectRef
    claim_ref: DraftObjectRef
    claim_paper_evidence_ref: DraftObjectRef


class PropositionProposal(DTOModel):
    local_ref: DraftLocalRef
    text: Annotated[str, Field(min_length=1, max_length=MAX_DRAFT_TEXT_CHARS)]
    content_class: PropositionContentClass
    claim_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]
    citation_bindings: Annotated[
        list[CitationBindingProposal], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]
    corpus_fact_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]
    process_fact_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]

    @model_validator(mode="after")
    def _exclusive_provenance(self) -> "PropositionProposal":
        if _has_canonical_numeric_id(self.local_ref, "PR"):
            raise ValueError("proposition local_ref must not be a canonical PR ID")
        _validate_draft_text(self.text, label="proposition text")
        _validate_unique(self.claim_refs, label="claim_refs")
        _validate_unique(self.corpus_fact_refs, label="corpus_fact_refs")
        _validate_unique(self.process_fact_refs, label="process_fact_refs")
        citation_keys = [
            (
                binding.paper_ref,
                binding.claim_ref,
                binding.claim_paper_evidence_ref,
            )
            for binding in self.citation_bindings
        ]
        _validate_unique(citation_keys, label="citation_bindings")
        if self.content_class is PropositionContentClass.SCIENTIFIC_CLAIM:
            if not self.claim_refs or not self.citation_bindings:
                raise ValueError("ScientificClaim requires claims and citations")
            if self.corpus_fact_refs or self.process_fact_refs:
                raise ValueError("ScientificClaim cannot mix provenance classes")
            if {item.claim_ref for item in self.citation_bindings} != set(
                self.claim_refs
            ):
                raise ValueError("ScientificClaim citations must cover all claims")
        elif self.content_class is PropositionContentClass.CORPUS_FACT:
            if (
                self.claim_refs
                or self.citation_bindings
                or not self.corpus_fact_refs
                or self.process_fact_refs
            ):
                raise ValueError("CorpusFact must contain only corpus-fact provenance")
        elif self.content_class is PropositionContentClass.REVIEW_PROCESS_STATEMENT:
            if (
                self.claim_refs
                or self.citation_bindings
                or self.corpus_fact_refs
                or not self.process_fact_refs
            ):
                raise ValueError(
                    "ReviewProcessStatement must contain only process provenance"
                )
        elif any(
            (
                self.claim_refs,
                self.citation_bindings,
                self.corpus_fact_refs,
                self.process_fact_refs,
            )
        ):
            raise ValueError("Rhetorical proposal cannot contain provenance")
        return self


class PropositionProposalBundle(DTOModel):
    propositions: Annotated[
        list[PropositionProposal],
        Field(min_length=1, max_length=MAX_DRAFT_TASK_ITEMS),
    ]

    @model_validator(mode="after")
    def _drafts_are_bounded_and_unique(self) -> "PropositionProposalBundle":
        _validate_unique(
            [item.local_ref for item in self.propositions],
            label="proposition local_refs",
        )
        if len(self.model_dump_json().encode("utf-8")) > MAX_DRAFT_PROPOSAL_UTF8_BYTES:
            raise ValueError(
                "proposition proposal bundle exceeds its UTF-8 byte budget"
            )
        return self


class SemanticAuditProposal(DTOModel):
    target_ref: DraftLocalRef
    class_verdict: AuditClassVerdict
    provenance_verdict: AuditProvenanceVerdict
    reason: Annotated[str, Field(max_length=MAX_DRAFT_TEXT_CHARS)]
    referenced_claim_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]
    referenced_corpus_fact_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]
    referenced_process_fact_refs: Annotated[
        list[DraftObjectRef], Field(max_length=MAX_DRAFT_REFS_PER_ITEM)
    ]

    @model_validator(mode="after")
    def _draft_target_and_refs_are_bounded(self) -> "SemanticAuditProposal":
        if _has_canonical_numeric_id(self.target_ref, "PR"):
            raise ValueError(
                "semantic audit target must be a proposition draft local ref"
            )
        _validate_draft_text(self.reason, label="semantic audit reason")
        _validate_unique(self.referenced_claim_refs, label="referenced_claim_refs")
        _validate_unique(
            self.referenced_corpus_fact_refs,
            label="referenced_corpus_fact_refs",
        )
        _validate_unique(
            self.referenced_process_fact_refs,
            label="referenced_process_fact_refs",
        )
        return self


class RenderedSentenceProposal(DTOModel):
    local_ref: DraftLocalRef
    text: Annotated[str, Field(min_length=1, max_length=MAX_DRAFT_TEXT_CHARS)]
    source_proposition_refs: Annotated[
        list[DraftObjectRef],
        Field(min_length=1, max_length=MAX_DRAFT_REFS_PER_ITEM),
    ]

    @model_validator(mode="after")
    def _draft_is_bounded_and_owned(self) -> "RenderedSentenceProposal":
        if _has_canonical_numeric_id(self.local_ref, "RS"):
            raise ValueError("sentence local_ref must not be a canonical RS ID")
        _validate_draft_text(self.text, label="rendered sentence text")
        if any(marker in self.text for marker in ("\r", "\n", "\x00")):
            raise ValueError("rendered sentence text contains a forbidden line marker")
        _validate_unique(
            self.source_proposition_refs, label="source_proposition_refs"
        )
        return self


class RenderedSentenceProposalBundle(DTOModel):
    sentences: Annotated[
        list[RenderedSentenceProposal],
        Field(min_length=1, max_length=MAX_DRAFT_TASK_ITEMS),
    ]

    @model_validator(mode="after")
    def _drafts_are_bounded_and_unique(self) -> "RenderedSentenceProposalBundle":
        _validate_unique(
            [item.local_ref for item in self.sentences],
            label="sentence local_refs",
        )
        if len(self.model_dump_json().encode("utf-8")) > MAX_DRAFT_PROPOSAL_UTF8_BYTES:
            raise ValueError(
                "rendered sentence proposal bundle exceeds its UTF-8 byte budget"
            )
        return self


class RenderedSentenceAuditProposal(DTOModel):
    sentence_ref: DraftLocalRef
    verdict: AuditProvenanceVerdict
    reason: Annotated[str, Field(max_length=MAX_DRAFT_TEXT_CHARS)]

    @model_validator(mode="after")
    def _draft_target_is_bounded(self) -> "RenderedSentenceAuditProposal":
        if _has_canonical_numeric_id(self.sentence_ref, "RS"):
            raise ValueError("sentence audit target must be a sentence draft local ref")
        _validate_draft_text(self.reason, label="rendered sentence audit reason")
        return self
