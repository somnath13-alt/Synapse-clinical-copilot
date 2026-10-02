"""Immutable schema-v7 historical facts, bounded by M8.1 characterization.

None preserves a recorded null/optional fact; an empty tuple means a known empty
collection. UnavailableFact names detail v7 never copied (not an empty result).
The future reader must validate completeness and persisted relationships, decide
malformed-row failures, and supply these contracts without mutable fallbacks.
These types validate structure only; they neither select nor recompute anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from types import MappingProxyType


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _optional_text(value: object, name: str) -> None:
    if value is not None:
        _text(value, name)


def _utc(value: str, name: str) -> None:
    _text(value, name)
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be a UTC timestamp") from error
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC timestamp")
    # Validate without rewriting the stored timestamp or checking applicability.


def _freeze(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise TypeError("value must contain only finite JSON-compatible data")


def _mapping(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a JSON mapping")
    return _freeze(value)  # type: ignore[return-value]


def _tuple(values: object, item_type: type, name: str) -> tuple:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{name} must be an ordered sequence")
    result = tuple(values)
    if any(not isinstance(item, item_type) for item in result):
        raise TypeError(f"{name} contains invalid item types")
    return result


def _ids(values: object, name: str) -> tuple[str, ...]:
    result = _tuple(values, str, name)
    for item in result:
        _text(item, name)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _ordered(values: object, item_type: type, name: str, identity: str) -> tuple:
    result = _tuple(values, item_type, name)
    if tuple(item.ordinal for item in result) != tuple(range(len(result))):
        raise ValueError(f"{name} must have contiguous zero-based ordinals in order")
    if len({getattr(item, identity) for item in result}) != len(result):
        raise ValueError(f"{name} must have unique identities")
    return result


class UnavailableFact(StrEnum):
    """Known v7 omissions, not facts reconstructed from current rows."""

    UNCOPIED_EVIDENCE_PAYLOADS = "UNCOPIED_EVIDENCE_PAYLOADS"
    KNOWLEDGE_PREDICATE = "KNOWLEDGE_PREDICATE"
    COMPARISON_INTERNALS = "COMPARISON_INTERNALS"
    ESCALATION_TRIGGER_IDS = "ESCALATION_TRIGGER_IDS"


@dataclass(frozen=True, slots=True)
class InteractionIdentity:
    interaction_id: str
    question: str
    intent: str
    source_mode: str
    selected_sources: tuple[str, ...]
    orchestration_trace: tuple[Mapping[str, JsonValue], ...]
    created_at: str

    def __post_init__(self) -> None:
        for name in ("interaction_id", "question", "intent", "source_mode"):
            _text(getattr(self, name), name)
        _utc(self.created_at, "created_at")
        object.__setattr__(self, "selected_sources", _ids(self.selected_sources, "selected_sources"))
        trace = _tuple(self.orchestration_trace, Mapping, "orchestration_trace")
        object.__setattr__(self, "orchestration_trace", tuple(_mapping(item) for item in trace))


@dataclass(frozen=True, slots=True)
class TemporalContext:
    temporal_mode: str
    requested_as_of: str | None
    # interaction.as_of is legacy synthetic execution time, NOT requested time.
    legacy_as_of: str | None

    def __post_init__(self) -> None:
        if self.temporal_mode not in ("CURRENT", "AS_OF"):
            raise ValueError("temporal_mode must be CURRENT or AS_OF")
        if (self.temporal_mode == "AS_OF") != (self.requested_as_of is not None):
            raise ValueError("temporal_mode and requested_as_of must be paired")
        for name in ("requested_as_of", "legacy_as_of"):
            if getattr(self, name) is not None:
                _utc(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class EvidenceMembership:
    """Identity/order only, for retrieval or knowledge; never copied provenance."""

    evidence_id: str
    ordinal: int

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("ordinal must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class KnowledgeParticipation:
    interaction_knowledge_id: str
    assertion_id: str
    ordinal: int
    origin: str
    state_at_execution: str
    decision_type: str
    value: JsonValue
    normalized_scope: Mapping[str, JsonValue] | None
    effective_from: str
    effective_to: str | None
    recorded_at: str
    source_id: str
    source_type: str
    document_id: str
    document_version_id: str
    document_version: str
    document_effective_from: str
    document_effective_to: str | None
    lineage_ids: tuple[str, ...]
    correction_ids: tuple[str, ...]
    evidence: tuple[EvidenceMembership, ...]

    def __post_init__(self) -> None:
        for name in ("interaction_knowledge_id", "assertion_id", "decision_type", "source_id",
                     "source_type", "document_id", "document_version_id", "document_version"):
            _text(getattr(self, name), name)
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("ordinal must be a nonnegative integer")
        if self.origin not in ("SOURCE", "KNOWLEDGE", "CORROBORATED"):
            raise ValueError("invalid execution origin")
        if self.state_at_execution not in ("APPLIED", "SUPERSEDED"):
            raise ValueError("invalid execution state")
        for name in ("effective_from", "recorded_at", "document_effective_from"):
            _utc(getattr(self, name), name)
        for name in ("effective_to", "document_effective_to"):
            if getattr(self, name) is not None:
                _utc(getattr(self, name), name)
        object.__setattr__(self, "value", _freeze(self.value))
        if self.normalized_scope is not None:
            object.__setattr__(self, "normalized_scope", _mapping(self.normalized_scope))
        for name in ("lineage_ids", "correction_ids"):
            object.__setattr__(self, name, _ids(getattr(self, name), name))
        object.__setattr__(self, "evidence", _ordered(self.evidence, EvidenceMembership, "evidence", "evidence_id"))

    @property
    def decision_dimension(self) -> str:
        """Alias of persisted decision_type; v7 has no independent predicate."""
        return self.decision_type


@dataclass(frozen=True, slots=True)
class ClaimSnapshot:
    supported_claim_id: str
    claim_key: str
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("supported_claim_id", "claim_key", "text"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids, "evidence_ids"))


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    citation_id: str
    supported_claim_id: str
    evidence_id: str
    source_id: str
    document_version_id: str
    source_title: str
    source_type: str
    version: str
    timestamp: str
    section: str
    relevant_excerpt: str

    def __post_init__(self) -> None:
        for name in self.__slots__:
            _text(getattr(self, name), name)
        _utc(self.timestamp, "timestamp")


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    """Only the four persisted public projection fields, not ComparisonResult."""

    type: str
    severity: str
    resolution_state: str
    explanation: str

    def __post_init__(self) -> None:
        for name in self.__slots__:
            _text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ConfidenceSnapshot:
    label: str | None
    rationale: str | None
    confidence_policy_id: str | None

    def __post_init__(self) -> None:
        if self.label is not None and self.label not in ("HIGH", "MEDIUM", "LOW"):
            raise ValueError("confidence label must be HIGH, MEDIUM, LOW or None")
        _optional_text(self.rationale, "rationale")
        _optional_text(self.confidence_policy_id, "confidence_policy_id")


@dataclass(frozen=True, slots=True)
class EscalationSnapshot:
    required: bool
    reviewer: str | None
    reason: str | None
    trigger_ids: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if type(self.required) is not bool:
            raise TypeError("required must be a boolean")
        _optional_text(self.reviewer, "reviewer")
        _optional_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class AnswerSnapshot:
    answer: str
    confidence: ConfidenceSnapshot
    reconciliation: tuple[ReconciliationSnapshot, ...]
    escalation: EscalationSnapshot | None
    policy_version_id: str | None

    def __post_init__(self) -> None:
        _text(self.answer, "answer")
        _optional_text(self.policy_version_id, "policy_version_id")
        if not isinstance(self.confidence, ConfidenceSnapshot):
            raise TypeError("confidence must be a ConfidenceSnapshot")
        if self.escalation is not None and not isinstance(self.escalation, EscalationSnapshot):
            raise TypeError("escalation must be an EscalationSnapshot or None")
        object.__setattr__(self, "reconciliation", _tuple(self.reconciliation, ReconciliationSnapshot, "reconciliation"))


@dataclass(frozen=True, slots=True)
class GovernanceLink:
    """Persisted path justifying inclusion, supplied/verified by the reader.

    INTERACTION uses the event's direct interaction ID. FEEDBACK adds the
    feedback->interaction path. PROPOSAL adds proposal->feedback->interaction.
    No document, assertion, free-text or timestamp-based relationship exists.
    """

    kind: str
    interaction_id: str
    feedback_id: str | None = None
    proposal_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.interaction_id, "interaction_id")
        _optional_text(self.feedback_id, "feedback_id")
        _optional_text(self.proposal_id, "proposal_id")
        shape = (self.feedback_id is not None, self.proposal_id is not None)
        expected = {"INTERACTION": (False, False), "FEEDBACK": (True, False), "PROPOSAL": (True, True)}
        if self.kind not in expected or shape != expected[self.kind]:
            raise ValueError("governance link must be an explicit interaction/feedback/proposal path")


@dataclass(frozen=True, slots=True)
class AuditEventSnapshot:
    """Prototype history, not tamper-evident, compliance-grade or certified.

    Payload actor/role metadata is not authenticated identity. Row links remain
    distinct from the path establishing relatedness; proposal_id lives in JSON.
    """

    event_id: int
    event_type: str
    occurred_at: str
    interaction_id: str | None
    feedback_id: str | None
    payload: Mapping[str, JsonValue]
    relationship: GovernanceLink

    def __post_init__(self) -> None:
        if type(self.event_id) is not int or self.event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        _text(self.event_type, "event_type")
        _utc(self.occurred_at, "occurred_at")
        _optional_text(self.interaction_id, "interaction_id")
        _optional_text(self.feedback_id, "feedback_id")
        object.__setattr__(self, "payload", _mapping(self.payload))
        link = self.relationship
        if not isinstance(link, GovernanceLink):
            raise TypeError("relationship must be a GovernanceLink")
        if self.interaction_id is not None and self.interaction_id != link.interaction_id:
            raise ValueError("conflicting interaction linkage")
        if link.kind == "INTERACTION" and self.interaction_id != link.interaction_id:
            raise ValueError("direct interaction linkage required")
        if link.kind == "FEEDBACK" and self.feedback_id != link.feedback_id:
            raise ValueError("explicit feedback linkage required")
        if link.kind == "PROPOSAL":
            if self.payload.get("proposal_id") != link.proposal_id:
                raise ValueError("structured proposal linkage required")
            if self.feedback_id is not None and self.feedback_id != link.feedback_id:
                raise ValueError("conflicting feedback linkage")
        for name in ("interaction_id", "feedback_id", "proposal_id"):
            recorded = self.payload.get(name)
            expected = getattr(link, name)
            row_value = getattr(self, name, None)
            if recorded is not None:
                _text(recorded, name)
                if (expected is not None and recorded != expected) or (row_value is not None and recorded != row_value):
                    raise ValueError(f"conflicting payload {name}")


@dataclass(frozen=True, slots=True)
class GovernanceTimeline:
    """Related history, including later events; never execution-time inputs."""

    events: tuple[AuditEventSnapshot, ...]

    def __post_init__(self) -> None:
        events = _tuple(self.events, AuditEventSnapshot, "events")
        ids = tuple(event.event_id for event in events)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("events must be unique and in event_id append order")
        object.__setattr__(self, "events", events)


@dataclass(frozen=True, slots=True)
class InteractionExplanation:
    identity: InteractionIdentity
    temporal: TemporalContext
    answer: AnswerSnapshot
    retrieved_evidence: tuple[EvidenceMembership, ...]
    knowledge: tuple[KnowledgeParticipation, ...]
    claims: tuple[ClaimSnapshot, ...]
    citations: tuple[CitationSnapshot, ...]
    related_governance: GovernanceTimeline
    unavailable_facts: tuple[UnavailableFact, ...] = field(default=tuple(UnavailableFact), init=False)

    def __post_init__(self) -> None:
        for name, kind in (("identity", InteractionIdentity), ("temporal", TemporalContext),
                           ("answer", AnswerSnapshot), ("related_governance", GovernanceTimeline)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        object.__setattr__(self, "retrieved_evidence", _ordered(self.retrieved_evidence, EvidenceMembership, "retrieved_evidence", "evidence_id"))
        object.__setattr__(self, "knowledge", _ordered(self.knowledge, KnowledgeParticipation, "knowledge", "interaction_knowledge_id"))
        object.__setattr__(self, "claims", _tuple(self.claims, ClaimSnapshot, "claims"))
        object.__setattr__(self, "citations", _tuple(self.citations, CitationSnapshot, "citations"))
        if any(event.relationship.interaction_id != self.identity.interaction_id for event in self.related_governance.events):
            raise ValueError("timeline belongs to a different interaction")
