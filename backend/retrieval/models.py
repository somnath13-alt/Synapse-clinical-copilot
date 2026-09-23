"""Internal retrieval contracts for persisted synthetic evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class SourceType(StrEnum):
    EHR = "EHR"
    GUIDELINE = "GUIDELINE"
    PAYER_POLICY = "PAYER_POLICY"
    FORMULARY = "FORMULARY"
    SPECIALIST_NOTE = "SPECIALIST_NOTE"


class RetrievalStatus(StrEnum):
    RETRIEVED = "RETRIEVED"
    NOT_FOUND = "NOT_FOUND"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    MALFORMED = "MALFORMED"


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    interaction_id: str
    intent: str
    case_id: str
    as_of: str
    medication_id: str
    indication_id: str
    payer_id: str
    plan_id: str
    source_mode: str = "BASELINE"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source_id: str
    source_type: SourceType
    source_title: str
    document_id: str
    document_version_id: str
    version: str
    timestamp: str
    section: str
    relevant_excerpt: str
    structured_data: Mapping[str, Any]
    document_recorded_at: str
    document_effective_from: str
    document_effective_to: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_data", _freeze_json(dict(self.structured_data)))


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    source_type: SourceType
    status: RetrievalStatus
    evidence_items: tuple[EvidenceItem, ...] = ()
    document_version_ids: tuple[str, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_items", tuple(self.evidence_items))
        object.__setattr__(self, "document_version_ids", tuple(self.document_version_ids))
        if self.status is RetrievalStatus.RETRIEVED:
            if not self.evidence_items or not self.document_version_ids:
                raise ValueError("Retrieved results require evidence and document versions")
            if self.failure_reason is not None:
                raise ValueError("Retrieved results cannot include a failure reason")
            return
        if self.evidence_items or self.document_version_ids:
            raise ValueError("Failure results cannot include evidence or document versions")
        if not self.failure_reason:
            raise ValueError("Failure results require a safe failure reason")


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    intent: str
    ordered_sources: tuple[SourceType, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ordered_sources", tuple(self.ordered_sources))


@dataclass(frozen=True, slots=True)
class RetrievalTraceItem:
    source_type: SourceType
    display_label: str
    status: RetrievalStatus


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    retrieval_results: tuple[RetrievalResult, ...]
    evidence_by_id: Mapping[str, EvidenceItem]
    source_statuses: Mapping[SourceType, RetrievalStatus]
    retrieval_trace: tuple[RetrievalTraceItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieval_results", tuple(self.retrieval_results))
        object.__setattr__(self, "retrieval_trace", tuple(self.retrieval_trace))
        evidence_by_id: dict[str, EvidenceItem] = {}
        source_statuses: dict[SourceType, RetrievalStatus] = {}
        for result in self.retrieval_results:
            source_statuses[result.source_type] = result.status
            for evidence in result.evidence_items:
                if evidence.evidence_id in evidence_by_id:
                    raise ValueError(f"Duplicate evidence ID: {evidence.evidence_id}")
                evidence_by_id[evidence.evidence_id] = evidence

        if dict(self.evidence_by_id) != evidence_by_id:
            raise ValueError("Evidence index does not match retrieval results")
        if dict(self.source_statuses) != source_statuses:
            raise ValueError("Source statuses do not match retrieval results")
        if tuple(item.source_type for item in self.retrieval_trace) != tuple(
            result.source_type for result in self.retrieval_results
        ):
            raise ValueError("Retrieval trace order does not match retrieval results")
        if tuple(item.status for item in self.retrieval_trace) != tuple(
            result.status for result in self.retrieval_results
        ):
            raise ValueError("Retrieval trace statuses do not match retrieval results")

        object.__setattr__(self, "evidence_by_id", MappingProxyType(evidence_by_id))
        object.__setattr__(self, "source_statuses", MappingProxyType(source_statuses))

    @classmethod
    def from_results(
        cls,
        results: tuple[RetrievalResult, ...],
        trace: tuple[RetrievalTraceItem, ...],
    ) -> EvidenceBundle:
        evidence_by_id: dict[str, EvidenceItem] = {}
        source_statuses: dict[SourceType, RetrievalStatus] = {}
        for result in results:
            source_statuses[result.source_type] = result.status
            for evidence in result.evidence_items:
                if evidence.evidence_id in evidence_by_id:
                    raise ValueError(f"Duplicate evidence ID: {evidence.evidence_id}")
                evidence_by_id[evidence.evidence_id] = evidence
        return cls(results, evidence_by_id, source_statuses, trace)
