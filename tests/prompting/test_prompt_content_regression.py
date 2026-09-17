"""Scientific prompt regression tests (goal.md §51) against committed prompt bytes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vibereview.prompting import PROMPT_ROOT
from vibereview.prompting.registry import load_registry, resolve_focus_module, resolve_prompt

CORE_CLASSES = ("project", "deep_research", "claims", "synthesis", "manuscript")

PROTOCOL_SLOTS = {
    "project_brief": "P001@1.0.0",
    "structure": "P010@1.0.0",
    "outline": "P020@1.0.0",
    "outline_challenge": "P030@1.0.0",
    "outline_revision": "P040@1.0.0",
    "scoping_research": "DR100@1.0.0",
    "section_research": "DR110@1.0.0",
    "adversarial_research": "DR120@1.0.0",
    "recent_check": "DR130@1.0.0",
    "claim_generation": "C200@1.0.0",
    "claim_normalization": "C210@1.0.0",
    "claim_challenge": "C220@1.0.0",
    "claim_synthesis": "S300@1.0.0",
    "cross_section_synthesis": "S310@1.0.0",
    "manuscript_outline": "M400@1.0.0",
    "section_contract": "M410@1.0.0",
    "propositions": "M420@1.0.0",
    "rendering": "M430@1.0.0",
    "proposition_audit": "M440@1.0.0",
    "sentence_audit": "M450@1.0.0",
}

FOCUS_MODULES = (
    "manufacturing_ai",
    "edm",
    "ecm",
    "laser_precision",
    "other_nonconventional",
    "sensing",
    "ai_validation",
    "autonomy",
    "physics_informed_ai",
    "atomic_manufacturing",
)


def _prompt_text(prompt_id: str) -> str:
    registry = load_registry(PROMPT_ROOT / "registry.yaml")
    resolved = resolve_prompt(registry, PROMPT_ROOT, f"{prompt_id}@1.0.0")
    return resolved.bytes.decode("utf-8")


def _focus_text(name: str) -> str:
    registry = load_registry(PROMPT_ROOT / "registry.yaml")
    resolved = resolve_focus_module(registry, PROMPT_ROOT, f"{name}@1.0.0")
    return resolved.bytes.decode("utf-8")


def test_registry_covers_full_initial_release() -> None:
    registry = load_registry(PROMPT_ROOT / "registry.yaml")
    assert set(registry.prompts) == set(PROTOCOL_SLOTS.values())
    assert set(registry.focus_modules) == {f"{name}@1.0.0" for name in FOCUS_MODULES}
    assert set(registry.protocols) == {"vibereview-review-protocol-1.0"}
    bundle = registry.protocols["vibereview-review-protocol-1.0"]
    assert dict(bundle.prompts) == PROTOCOL_SLOTS
    for entry in registry.prompts.values():
        assert entry.status == "RELEASED"
        assert entry.canonical_evidence is False


def test_every_core_prompt_has_exactly_one_output_contract() -> None:
    for prompt_id in sorted({ref.split("@")[0] for ref in PROTOCOL_SLOTS.values()}):
        text = _prompt_text(prompt_id)
        assert text.count("## Output Contract") == 1, prompt_id


def test_focus_modules_have_no_output_contract() -> None:
    for name in FOCUS_MODULES:
        text = _focus_text(name)
        assert "## Output Contract" not in text, name


def test_dr110_requires_adversarial_evidence_seeking() -> None:
    text = _prompt_text("DR110").lower()
    for phrase in (
        "primary studies",
        "contradiction",
        "null findings",
        "boundary conditions",
        "alternative",
        "methodological weaknesses",
        "generalization",
        "challenges the outline",
    ):
        assert phrase in text, phrase


def test_c200_requires_atomic_scoped_restrained_claims_with_hints() -> None:
    text = _prompt_text("C200").lower()
    for phrase in ("atomic", "scope", "causal", "retrieval hints"):
        assert phrase in text, phrase


def test_s300_prohibits_vote_counting_and_omission() -> None:
    text = _prompt_text("S300").lower()
    assert "vote" in text
    assert "prohibit" in text
    assert "contradictory" in text
    assert "omit" in text


def test_m430_prohibits_new_scientific_propositions() -> None:
    text = _prompt_text("M430").lower()
    assert "new scientific propositions" in text
    for allowed in ("syntax", "flow", "readability", "terminology", "transitions"):
        assert allowed in text, allowed


def test_m450_requires_entailment_assessment() -> None:
    text = _prompt_text("M450").lower()
    assert "entailment" in text
    assert "entailed" in text


def test_p030_challenge_classifications_are_exact() -> None:
    text = _prompt_text("P030")
    for verdict in ("KEEP", "RENAME", "MERGE", "SPLIT", "MOVE", "ADD", "REMOVE", "UNRESOLVED"):
        assert verdict in text


def test_c210_relationship_vocabulary_is_exact() -> None:
    text = _prompt_text("C210")
    for relation in (
        "DUPLICATE_OF",
        "NARROWS",
        "BROADENS",
        "OVERLAPS",
        "CONTRADICTS",
        "RELATED_MECHANISM",
    ):
        assert relation in text


def test_s300_outcome_vocabulary_matches_contract() -> None:
    text = _prompt_text("S300")
    for outcome in ("RETAIN", "NARROW", "REVISE", "REJECT", "UNCLEAR"):
        assert outcome in text


def test_atomic_manufacturing_focus_policy() -> None:
    text = _focus_text("atomic_manufacturing").lower()
    for phrase in (
        "demonstrated capability",
        "plausible methodological transfer",
        "speculative extrapolation",
        "physics-informed learning",
        "active learning",
        "uncertainty-aware control",
        "inverse design",
        "autonomous metrology",
        "bayesian experimental design",
    ):
        assert phrase in text, phrase
    assert "direct process continuity" in text


def test_dr100_is_discovery_only() -> None:
    text = _prompt_text("DR100").lower()
    assert "not canonical evidence" in text or "discovery-only" in text


def test_deep_research_prompts_record_manual_boundary() -> None:
    for prompt_id in ("DR100", "DR110", "DR120", "DR130"):
        text = _prompt_text(prompt_id).lower()
        assert "operator" in text
