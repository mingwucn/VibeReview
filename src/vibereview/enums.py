"""Canonical Phase-0 enumerations."""

from enum import StrEnum


class Origin(StrEnum):
    DEEP_RESEARCH = "deep_research"
    CORPUS_CHALLENGER = "corpus_challenger"
    GENERATED = "generated"
    HUMAN = "human"


class IndependenceStatus(StrEnum):
    INDEPENDENT = "independent"
    SHARED_DATASET = "shared_dataset"
    EXTENDED_PUBLICATION = "extended_publication"
    UNKNOWN = "unknown"


class RetrievalIntent(StrEnum):
    SUPPORT = "support"
    CONTRADICTION = "contradiction"
    BOUNDARY = "boundary"
    ALTERNATIVE = "alternative"
    METHOD_CHALLENGE = "method_challenge"
    NULL_RESULT = "null_result"


class RetrievalDispositionStatus(StrEnum):
    ASSESSED = "assessed"
    DUPLICATE = "duplicate"
    REDUNDANT = "redundant"
    EXCLUDED_BY_BUDGET = "excluded_by_budget"
    INVALID_LOCATOR = "invalid_locator"


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"
    CONTEXTUAL = "contextual"
    UNCLEAR = "unclear"


class EvidenceDirectness(StrEnum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    UNCLEAR = "unclear"
    NOT_ASSESSABLE = "not_assessable"


class MethodologicalRelevance(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    UNCLEAR = "unclear"
    NOT_ASSESSABLE = "not_assessable"


class EvidenceStrength(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNKNOWN = "unknown"
    NOT_ASSESSABLE = "not_assessable"


class Assessability(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NOT_ASSESSABLE = "not_assessable"


class AggregateRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"
    CONTEXTUAL = "contextual"
    MIXED = "mixed"
    UNCLEAR = "unclear"


class WithinPaperConsistency(StrEnum):
    CONSISTENT = "consistent"
    MIXED = "mixed"
    UNCLEAR = "unclear"


class AggregateStrength(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNKNOWN = "unknown"


class EvidenceSufficiency(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNCLEAR = "unclear"


class ClaimDecision(StrEnum):
    RETAIN = "RETAIN"
    WEAKEN = "WEAKEN"
    NARROW = "NARROW"
    REFORMULATE = "REFORMULATE"
    REJECT = "REJECT"


class RejectionBasis(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTED = "contradicted"
    OUT_OF_SCOPE = "out_of_scope"
    UNRESOLVABLE = "unresolvable"


class FinalClaimStatus(StrEnum):
    VALID = "VALID"
    REVISE_AGAIN = "REVISE_AGAIN"
    REJECT = "REJECT"
    UNCLEAR = "UNCLEAR"


class ValidationCheckResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNCLEAR = "unclear"
    NOT_APPLICABLE = "not_applicable"


class CorpusFactDerivationType(StrEnum):
    REGISTRY_ARITHMETIC = "registry_arithmetic"
    DETERMINISTIC_METADATA_QUERY = "deterministic_metadata_query"
    SEMANTIC_CLASSIFICATION = "semantic_classification"


class ReviewProcessSourceType(StrEnum):
    PROJECT_CONFIG = "project_config"
    RUN_MANIFEST = "run_manifest"
    PIPELINE_LOG = "pipeline_log"


class PropositionContentClass(StrEnum):
    SCIENTIFIC_CLAIM = "ScientificClaim"
    CORPUS_FACT = "CorpusFact"
    REVIEW_PROCESS_STATEMENT = "ReviewProcessStatement"
    RHETORICAL = "Rhetorical"


class AuditClassVerdict(StrEnum):
    CORRECT = "CORRECT"
    MISCLASSIFIED = "MISCLASSIFIED"
    UNCLEAR = "UNCLEAR"


class AuditProvenanceVerdict(StrEnum):
    ENTAILED = "ENTAILED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    OVERSTATED = "OVERSTATED"
    UNSUPPORTED = "UNSUPPORTED"
    UNCLEAR = "UNCLEAR"


class AuditDisposition(StrEnum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    REPAIR_BLOCKING = "REPAIR_BLOCKING"
    HUMAN_REVIEW = "HUMAN_REVIEW"

