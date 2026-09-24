"""Immutable contracts for persisted knowledge reads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from backend.retrieval.models import EvidenceItem, SourceType


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def freeze_json(value: Any) -> JsonValue:
    """Return a recursively immutable representation of a decoded JSON value."""

    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("value must contain only JSON-compatible data")


class KnowledgeAssertionState(StrEnum):
    CANDIDATE = "CANDIDATE"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True, slots=True)
class KnowledgeAssertion:
    assertion_id: str
    document_version_id: str
    subject_id: str
    predicate: str
    object_id: str | None
    value: JsonValue
    decision_dimension: str
    normalized_scope: Mapping[str, JsonValue] | None
    recorded_at: str
    effective_from: str
    effective_to: str | None
    state: KnowledgeAssertionState
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, KnowledgeAssertionState):
            object.__setattr__(self, "state", KnowledgeAssertionState(self.state))
        object.__setattr__(self, "value", freeze_json(self.value))
        if self.normalized_scope is not None:
            frozen_scope = freeze_json(dict(self.normalized_scope))
            if not isinstance(frozen_scope, Mapping):
                raise TypeError("normalized_scope must be a mapping or None")
            object.__setattr__(self, "normalized_scope", frozen_scope)
        if isinstance(self.evidence_ids, str):
            raise TypeError("evidence_ids must be a sequence of strings")
        evidence_ids = tuple(self.evidence_ids)
        if not evidence_ids or any(
            not isinstance(evidence_id, str) or not evidence_id
            for evidence_id in evidence_ids
        ):
            raise ValueError("evidence_ids must contain non-empty strings")
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class AssertionProvenance:
    assertion: KnowledgeAssertion
    evidence_items: tuple[EvidenceItem, ...]
    document_id: str
    document_version_id: str
    document_version: str
    source_id: str
    source_type: SourceType
    source_title: str
    document_timestamp: str
    document_recorded_at: str
    document_effective_from: str
    document_effective_to: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_items", tuple(self.evidence_items))


@dataclass(frozen=True, slots=True)
class AssertionLineageEdge:
    lineage_id: str
    predecessor_assertion_id: str
    successor_assertion_id: str
    feedback_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    subject_id: str | None = None
    predicate: str | None = None
    decision_dimension: str | None = None
    state: KnowledgeAssertionState | None = None
    payer_id: str | None = None
    plan_id: str | None = None
    medication_id: str | None = None
    indication_id: str | None = None

    def __post_init__(self) -> None:
        if self.state is not None and not isinstance(self.state, KnowledgeAssertionState):
            object.__setattr__(self, "state", KnowledgeAssertionState(self.state))
        for field_name in (
            "subject_id",
            "predicate",
            "decision_dimension",
            "payer_id",
            "plan_id",
            "medication_id",
            "indication_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{field_name} must be a non-empty string or None")
