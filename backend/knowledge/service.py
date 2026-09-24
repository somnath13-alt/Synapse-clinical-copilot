"""Intent-revealing read-only facade over the knowledge repository."""

from __future__ import annotations

from backend.knowledge.models import (
    AssertionLineageEdge,
    AssertionProvenance,
    KnowledgeAssertion,
    KnowledgeQuery,
)
from backend.knowledge.repository import KnowledgeRepository


class KnowledgeService:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repository = repository

    def get_assertion(self, assertion_id: str) -> KnowledgeAssertion | None:
        return self._repository.get_assertion(assertion_id)

    def query_assertions(self, query: KnowledgeQuery) -> tuple[KnowledgeAssertion, ...]:
        return self._repository.find_assertions(query)

    def get_current_applied_assertions(
        self, query: KnowledgeQuery = KnowledgeQuery()
    ) -> tuple[KnowledgeAssertion, ...]:
        return self._repository.current_applied_assertions(query)

    def get_provenance(self, assertion_id: str) -> AssertionProvenance | None:
        return self._repository.get_assertion_provenance(assertion_id)

    def get_predecessors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._repository.get_predecessors(assertion_id)

    def get_successors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._repository.get_successors(assertion_id)
