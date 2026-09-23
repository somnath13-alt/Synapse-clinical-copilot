"""Immutable data contracts for evidence-backed reasoning results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from backend.retrieval.models import SourceType


DecisionValue: TypeAlias = str | bool | int | tuple[str, ...]


def _evidence_id_tuple(evidence_ids: object) -> tuple[str, ...]:
    if isinstance(evidence_ids, str):
        raise TypeError("evidence_ids must be a sequence of strings")
    try:
        normalized = tuple(evidence_ids)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError("evidence_ids must be a sequence of strings") from error
    if not all(isinstance(evidence_id, str) and evidence_id for evidence_id in normalized):
        raise ValueError("evidence_ids must contain only non-empty strings")
    return normalized


def _decision_value(value: object) -> DecisionValue:
    if isinstance(value, list):
        value = tuple(value)
    if isinstance(value, tuple):
        if not all(isinstance(item, str) for item in value):
            raise TypeError("tuple decision values must contain only strings")
        return value
    if isinstance(value, (str, bool, int)):
        return value
    raise TypeError("decision value must be a string, boolean, integer, or tuple of strings")


class DecisionType(StrEnum):
    CLINICAL_APPROPRIATENESS = "CLINICAL_APPROPRIATENESS"
    COVERAGE_AUTHORIZATION = "COVERAGE_AUTHORIZATION"
    PREREQUISITE_REQUIREMENT = "PREREQUISITE_REQUIREMENT"


@dataclass(frozen=True, slots=True)
class DecisionScope:
    payer_id: str | None
    plan_id: str | None
    medication_id: str | None
    indication_id: str | None
    effective_from: str | None
    effective_to: str | None
    as_of: str


@dataclass(frozen=True, slots=True)
class DecisionAssertion:
    decision_type: DecisionType
    value: DecisionValue
    scope: DecisionScope
    source_type: SourceType
    evidence_ids: tuple[str, ...]
    authority_scope: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _decision_value(self.value))
        evidence_ids = _evidence_id_tuple(self.evidence_ids)
        if not evidence_ids:
            raise ValueError("DecisionAssertion requires at least one evidence ID")
        object.__setattr__(self, "evidence_ids", evidence_ids)


class FindingType(StrEnum):
    COMPATIBLE_CONSTRAINT = "COMPATIBLE_CONSTRAINT"
    SAME_DIMENSION_DISAGREEMENT = "SAME_DIMENSION_DISAGREEMENT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Severity(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    HIGH = "HIGH"


class ResolutionState(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ReasoningFinding:
    finding_type: FindingType
    severity: Severity
    resolution_state: ResolutionState
    sides: tuple[DecisionAssertion, ...]
    evidence_ids: tuple[str, ...]
    explanation_code: str
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sides", tuple(self.sides))
        object.__setattr__(self, "evidence_ids", _evidence_id_tuple(self.evidence_ids))


class ConfidenceLabel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class ConfidenceFactors:
    source_availability: str
    relevance: str
    agreement_or_conflict: str
    freshness: str
    required_context_complete: str
    provenance_complete: str


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    label: ConfidenceLabel
    factors: ConfidenceFactors
    rationale: str
    policy_version_id: str


class EscalationTrigger(StrEnum):
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    CRITICAL_SOURCE_UNAVAILABLE = "CRITICAL_SOURCE_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNRESOLVED_HIGH_SEVERITY_CONFLICT = "UNRESOLVED_HIGH_SEVERITY_CONFLICT"


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    requires_escalation: bool
    triggers: tuple[EscalationTrigger, ...]
    reviewer: str | None
    requested_action: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "triggers", tuple(self.triggers))


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    findings: tuple[ReasoningFinding, ...]
    confidence: ConfidenceAssessment
    escalation: EscalationDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
