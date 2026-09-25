"""SQLite-backed internal retrieval boundary."""

from backend.retrieval.adapters import (
    EHRAdapter,
    FormularyAdapter,
    GuidelineAdapter,
    PayerPolicyAdapter,
    SpecialistNotesAdapter,
)
from backend.retrieval.models import (
    EvidenceBundle,
    EvidenceItem,
    RetrievalPlan,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
    RetrievalTraceItem,
    SourceType,
    TemporalMode,
)
from backend.retrieval.service import RetrievalService, build_plan

__all__ = [
    "EHRAdapter",
    "EvidenceBundle",
    "EvidenceItem",
    "FormularyAdapter",
    "GuidelineAdapter",
    "PayerPolicyAdapter",
    "RetrievalPlan",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalStatus",
    "RetrievalTraceItem",
    "SourceType",
    "SpecialistNotesAdapter",
    "TemporalMode",
    "build_plan",
]
