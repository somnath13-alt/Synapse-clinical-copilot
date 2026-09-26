"""Immutable data contracts for evidence-backed reasoning results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from backend.knowledge.models import KnowledgeAssertionState
from backend.retrieval.models import (
    EvidenceBundle,
    RetrievalRequest,
    SourceType,
    TemporalMode,
)


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


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _identifier_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be a sequence of strings")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{field_name} must be a sequence of strings") from error
    if any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{field_name} must contain only non-empty strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


class DecisionType(StrEnum):
    CLINICAL_APPROPRIATENESS = "CLINICAL_APPROPRIATENESS"
    COVERAGE_AUTHORIZATION = "COVERAGE_AUTHORIZATION"
    PREREQUISITE_REQUIREMENT = "PREREQUISITE_REQUIREMENT"


class AssertionOrigin(StrEnum):
    """The independently preserved channel supporting a reasoning assertion."""

    SOURCE = "SOURCE"
    KNOWLEDGE = "KNOWLEDGE"
    CORROBORATED = "CORROBORATED"


class ComparisonOutcome(StrEnum):
    """Deterministic source/knowledge comparison classifications for M6.3."""

    CORROBORATION = "CORROBORATION"
    SOURCE_ONLY = "SOURCE_ONLY"
    KNOWLEDGE_ONLY = "KNOWLEDGE_ONLY"
    STALE_KNOWLEDGE_DISAGREEMENT = "STALE_KNOWLEDGE_DISAGREEMENT"
    SAME_DIMENSION_CONFLICT = "SAME_DIMENSION_CONFLICT"
    COMPATIBLE_CROSS_DIMENSION_CONSTRAINT = "COMPATIBLE_CROSS_DIMENSION_CONSTRAINT"
    MISSING_SOURCE_CHANNEL = "MISSING_SOURCE_CHANNEL"
    MALFORMED_KNOWLEDGE_CHANNEL = "MALFORMED_KNOWLEDGE_CHANNEL"
    GOVERNANCE_PENDING = "GOVERNANCE_PENDING"


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


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """A reasoning-facing observation derived only from retrieved evidence."""

    observation_id: str
    decision_type: DecisionType
    value: DecisionValue
    normalized_scope: DecisionScope
    source_type: SourceType
    evidence_ids: tuple[str, ...]
    source_id: str
    document_version_id: str
    effective_from: str | None
    effective_to: str | None
    temporal_context: RetrievalRequest
    origin: AssertionOrigin = field(default=AssertionOrigin.SOURCE, init=False)

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "source_id", "document_version_id"):
            _required_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.normalized_scope, DecisionScope):
            raise TypeError("normalized_scope must be a DecisionScope")
        if not isinstance(self.decision_type, DecisionType):
            object.__setattr__(self, "decision_type", DecisionType(self.decision_type))
        if not isinstance(self.source_type, SourceType):
            object.__setattr__(self, "source_type", SourceType(self.source_type))
        for field_name in ("effective_from", "effective_to"):
            value = getattr(self, field_name)
            if value is not None:
                _required_identifier(value, field_name)
        object.__setattr__(self, "value", _decision_value(self.value))
        evidence_ids = _evidence_id_tuple(self.evidence_ids)
        if not evidence_ids:
            raise ValueError("SourceObservation requires at least one evidence ID")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        if not isinstance(self.temporal_context, RetrievalRequest):
            raise TypeError("temporal_context must be a RetrievalRequest")

    @property
    def temporal_mode(self) -> TemporalMode:
        return self.temporal_context.temporal_mode

    @property
    def as_of(self) -> str | None:
        return self.temporal_context.as_of


@dataclass(frozen=True, slots=True)
class GovernedBaselineAssertion:
    """A lightweight reasoning view of a selected persisted assertion.

    This contract preserves execution-time provenance without replacing the
    persisted ``KnowledgeAssertion`` or copying full evidence payloads.
    """

    assertion_id: str
    state: KnowledgeAssertionState
    decision_type: DecisionType
    value: DecisionValue
    normalized_scope: DecisionScope
    effective_from: str
    effective_to: str | None
    recorded_at: str
    evidence_ids: tuple[str, ...]
    source_id: str
    source_type: SourceType
    document_id: str
    document_version_id: str
    document_version: str
    document_effective_from: str
    document_effective_to: str | None
    temporal_context: RetrievalRequest
    lineage_ids: tuple[str, ...] = ()
    correction_ids: tuple[str, ...] = ()
    origin: AssertionOrigin = field(default=AssertionOrigin.KNOWLEDGE, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "assertion_id",
            "effective_from",
            "recorded_at",
            "source_id",
            "document_id",
            "document_version_id",
            "document_version",
            "document_effective_from",
        ):
            _required_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.normalized_scope, DecisionScope):
            raise TypeError("normalized_scope must be a DecisionScope")
        if not isinstance(self.decision_type, DecisionType):
            object.__setattr__(self, "decision_type", DecisionType(self.decision_type))
        if not isinstance(self.source_type, SourceType):
            object.__setattr__(self, "source_type", SourceType(self.source_type))
        for field_name in ("effective_to", "document_effective_to"):
            value = getattr(self, field_name)
            if value is not None:
                _required_identifier(value, field_name)
        if not isinstance(self.state, KnowledgeAssertionState):
            object.__setattr__(self, "state", KnowledgeAssertionState(self.state))
        if self.state is KnowledgeAssertionState.CANDIDATE:
            raise ValueError(
                "GovernedBaselineAssertion cannot represent a CANDIDATE assertion"
            )
        object.__setattr__(self, "value", _decision_value(self.value))
        evidence_ids = _evidence_id_tuple(self.evidence_ids)
        if not evidence_ids:
            raise ValueError("GovernedBaselineAssertion requires at least one evidence ID")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(
            self, "lineage_ids", _identifier_tuple(self.lineage_ids, "lineage_ids")
        )
        object.__setattr__(
            self,
            "correction_ids",
            _identifier_tuple(self.correction_ids, "correction_ids"),
        )
        if not isinstance(self.temporal_context, RetrievalRequest):
            raise TypeError("temporal_context must be a RetrievalRequest")

    @property
    def temporal_mode(self) -> TemporalMode:
        return self.temporal_context.temporal_mode

    @property
    def as_of(self) -> str | None:
        return self.temporal_context.as_of


@dataclass(frozen=True, slots=True)
class ReasoningInput:
    """Orchestration-owned, already-selected dual-channel reasoning input."""

    temporal_context: RetrievalRequest
    evidence_bundle: EvidenceBundle
    source_observations: tuple[SourceObservation, ...] = ()
    governed_assertions: tuple[GovernedBaselineAssertion, ...] = ()
    pending_assertion_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.temporal_context, RetrievalRequest):
            raise TypeError("temporal_context must be a RetrievalRequest")
        if not isinstance(self.evidence_bundle, EvidenceBundle):
            raise TypeError("evidence_bundle must be an EvidenceBundle")
        source_observations = tuple(self.source_observations)
        governed_assertions = tuple(self.governed_assertions)
        if any(
            not isinstance(observation, SourceObservation)
            for observation in source_observations
        ):
            raise TypeError("source_observations must contain SourceObservation values")
        if any(
            not isinstance(assertion, GovernedBaselineAssertion)
            for assertion in governed_assertions
        ):
            raise TypeError(
                "governed_assertions must contain GovernedBaselineAssertion values"
            )
        object.__setattr__(self, "source_observations", source_observations)
        object.__setattr__(self, "governed_assertions", governed_assertions)
        object.__setattr__(
            self,
            "pending_assertion_ids",
            _identifier_tuple(self.pending_assertion_ids, "pending_assertion_ids"),
        )

        retrieved_ids = set(self.evidence_bundle.evidence_by_id)
        for observation in source_observations:
            if observation.temporal_context != self.temporal_context:
                raise ValueError("source observation temporal context does not match input")
            if not set(observation.evidence_ids).issubset(retrieved_ids):
                raise ValueError("source observation evidence is absent from evidence_bundle")
        for assertion in governed_assertions:
            if assertion.temporal_context != self.temporal_context:
                raise ValueError("governed assertion temporal context does not match input")

    @property
    def temporal_mode(self) -> TemporalMode:
        return self.temporal_context.temporal_mode

    @property
    def as_of(self) -> str | None:
        return self.temporal_context.as_of


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


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """A declared deterministic comparison outcome, without comparison policy."""

    outcome: ComparisonOutcome
    source_observations: tuple[SourceObservation, ...] = ()
    governed_assertions: tuple[GovernedBaselineAssertion, ...] = ()
    findings: tuple[ReasoningFinding, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    origin: AssertionOrigin | None = None
    pending_assertion_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ComparisonOutcome):
            object.__setattr__(self, "outcome", ComparisonOutcome(self.outcome))
        if self.origin is not None and not isinstance(self.origin, AssertionOrigin):
            object.__setattr__(self, "origin", AssertionOrigin(self.origin))
        source_observations = tuple(self.source_observations)
        governed_assertions = tuple(self.governed_assertions)
        findings = tuple(self.findings)
        if any(
            not isinstance(observation, SourceObservation)
            for observation in source_observations
        ):
            raise TypeError("source_observations must contain SourceObservation values")
        if any(
            not isinstance(assertion, GovernedBaselineAssertion)
            for assertion in governed_assertions
        ):
            raise TypeError(
                "governed_assertions must contain GovernedBaselineAssertion values"
            )
        if any(not isinstance(finding, ReasoningFinding) for finding in findings):
            raise TypeError("findings must contain ReasoningFinding values")
        object.__setattr__(self, "source_observations", source_observations)
        object.__setattr__(self, "governed_assertions", governed_assertions)
        object.__setattr__(self, "findings", findings)
        pending_assertion_ids = _identifier_tuple(
            self.pending_assertion_ids, "pending_assertion_ids"
        )
        object.__setattr__(self, "pending_assertion_ids", pending_assertion_ids)

        supported_ids = tuple(
            dict.fromkeys(
                evidence_id
                for item in (*source_observations, *governed_assertions)
                for evidence_id in item.evidence_ids
            )
        )
        evidence_ids = (
            supported_ids
            if not self.evidence_ids
            else _identifier_tuple(self.evidence_ids, "evidence_ids")
        )
        if supported_ids and set(evidence_ids) != set(supported_ids):
            raise ValueError("comparison evidence_ids must preserve both input channels")
        object.__setattr__(self, "evidence_ids", evidence_ids)

        expected_origin: AssertionOrigin | None
        if self.outcome is ComparisonOutcome.CORROBORATION:
            if not source_observations or not governed_assertions:
                raise ValueError("CORROBORATION requires source and knowledge inputs")
            expected_origin = AssertionOrigin.CORROBORATED
        elif self.outcome is ComparisonOutcome.SOURCE_ONLY:
            if not source_observations or governed_assertions:
                raise ValueError("SOURCE_ONLY requires only source observations")
            expected_origin = AssertionOrigin.SOURCE
        elif self.outcome is ComparisonOutcome.KNOWLEDGE_ONLY:
            if source_observations or not governed_assertions:
                raise ValueError("KNOWLEDGE_ONLY requires only governed assertions")
            expected_origin = AssertionOrigin.KNOWLEDGE
        elif self.outcome in {
            ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT,
            ComparisonOutcome.SAME_DIMENSION_CONFLICT,
            ComparisonOutcome.COMPATIBLE_CROSS_DIMENSION_CONSTRAINT,
        }:
            if not source_observations or not governed_assertions:
                raise ValueError(f"{self.outcome.value} requires both input channels")
            expected_origin = None
        elif self.outcome is ComparisonOutcome.MISSING_SOURCE_CHANNEL:
            if source_observations:
                raise ValueError("MISSING_SOURCE_CHANNEL cannot contain source observations")
            expected_origin = (
                AssertionOrigin.KNOWLEDGE if governed_assertions else None
            )
        elif self.outcome is ComparisonOutcome.MALFORMED_KNOWLEDGE_CHANNEL:
            if governed_assertions:
                raise ValueError(
                    "MALFORMED_KNOWLEDGE_CHANNEL cannot contain validated governed assertions"
                )
            expected_origin = AssertionOrigin.SOURCE if source_observations else None
        else:
            if not pending_assertion_ids:
                raise ValueError(
                    "GOVERNANCE_PENDING requires pending assertion metadata"
                )
            expected_origin = None

        if (
            self.outcome is not ComparisonOutcome.GOVERNANCE_PENDING
            and pending_assertion_ids
        ):
            raise ValueError(
                "pending_assertion_ids require a GOVERNANCE_PENDING outcome"
            )

        if self.origin is None:
            object.__setattr__(self, "origin", expected_origin)
        elif self.origin is not expected_origin:
            raise ValueError("origin is inconsistent with the declared comparison outcome")


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
