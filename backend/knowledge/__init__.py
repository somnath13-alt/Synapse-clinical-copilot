"""Read-only typed access to persisted knowledge."""

from backend.knowledge.models import (
    AssertionLineageEdge,
    AssertionProvenance,
    KnowledgeAssertion,
    KnowledgeAssertionState,
    KnowledgeQuery,
)
from backend.knowledge.repository import KnowledgeDataError, KnowledgeRepository
from backend.knowledge.service import KnowledgeService

__all__ = [
    "AssertionLineageEdge",
    "AssertionProvenance",
    "KnowledgeAssertion",
    "KnowledgeAssertionState",
    "KnowledgeDataError",
    "KnowledgeQuery",
    "KnowledgeRepository",
    "KnowledgeService",
]
