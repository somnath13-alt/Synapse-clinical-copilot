"""Deterministic planning and sequential retrieval orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from backend.retrieval.adapters import (
    EHRAdapter,
    FormularyAdapter,
    GuidelineAdapter,
    PayerPolicyAdapter,
    SpecialistNotesAdapter,
)
from backend.retrieval.models import (
    EvidenceBundle,
    RetrievalPlan,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTraceItem,
    SourceType,
    TemporalMode,
)


SOURCE_ORDER = (
    SourceType.EHR,
    SourceType.GUIDELINE,
    SourceType.PAYER_POLICY,
    SourceType.FORMULARY,
    SourceType.SPECIALIST_NOTE,
)
SOURCE_LABELS = {
    SourceType.EHR: "EHR",
    SourceType.GUIDELINE: "Guideline",
    SourceType.PAYER_POLICY: "Payer",
    SourceType.FORMULARY: "Formulary",
    SourceType.SPECIALIST_NOTE: "Specialist Notes",
}
SUPPORTED_INTENTS = frozenset(
    {"PRIOR_AUTHORIZATION", "CLINICAL_GUIDANCE", "SPECIALIST_HISTORY"}
)


class RetrievalAdapter(Protocol):
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...

    def retrieve_current(self, request: RetrievalRequest) -> RetrievalResult: ...

    def retrieve_as_of(self, request: RetrievalRequest) -> RetrievalResult: ...


def build_plan(intent: str) -> RetrievalPlan:
    sources = SOURCE_ORDER if intent in SUPPORTED_INTENTS else ()
    return RetrievalPlan(intent=intent, ordered_sources=sources)


class RetrievalService:
    def __init__(
        self,
        database_path: Path,
        adapters: Mapping[SourceType, RetrievalAdapter] | None = None,
    ) -> None:
        self._adapters: Mapping[SourceType, RetrievalAdapter] = (
            adapters
            if adapters is not None
            else {
                SourceType.EHR: EHRAdapter(database_path),
                SourceType.GUIDELINE: GuidelineAdapter(database_path),
                SourceType.PAYER_POLICY: PayerPolicyAdapter(database_path),
                SourceType.FORMULARY: FormularyAdapter(database_path),
                SourceType.SPECIALIST_NOTE: SpecialistNotesAdapter(database_path),
            }
        )

    def retrieve(self, request: RetrievalRequest) -> EvidenceBundle:
        plan = build_plan(request.intent)
        collected_results: list[RetrievalResult] = []
        for source_type in plan.ordered_sources:
            adapter = self._adapters[source_type]
            if request.temporal_mode is TemporalMode.CURRENT:
                result = adapter.retrieve_current(request)
            else:
                result = adapter.retrieve_as_of(request)
            if result.source_type is not source_type:
                raise ValueError(
                    f"Adapter for {source_type.value} returned {result.source_type.value}"
                )
            collected_results.append(result)
        results = tuple(collected_results)
        trace = tuple(
            RetrievalTraceItem(
                source_type=result.source_type,
                display_label=SOURCE_LABELS[result.source_type],
                status=result.status,
            )
            for result in results
        )
        return EvidenceBundle.from_results(results, trace)
