"""Intent-revealing facade over the knowledge repository."""

from __future__ import annotations

from backend.knowledge.models import (
    AssertionLineageEdge,
    AssertionProvenance,
    KnowledgeAssertion,
    KnowledgeAssertionState,
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

    def get_applicable_assertions(
        self, query: KnowledgeQuery
    ) -> tuple[KnowledgeAssertion, ...]:
        return self._repository.get_applicable_assertions(query)

    def get_provenance(self, assertion_id: str) -> AssertionProvenance | None:
        return self._repository.get_assertion_provenance(assertion_id)

    def get_predecessors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._repository.get_predecessors(assertion_id)

    def get_successors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._repository.get_successors(assertion_id)

    def apply_assertion_state_transition(
        self,
        assertion_id: str,
        new_state: KnowledgeAssertionState,
    ) -> None:
        self._repository.apply_assertion_state_transition(assertion_id, new_state)

    def create_assertion_lineage(
        self,
        predecessor_assertion_id: str,
        successor_assertion_id: str,
        feedback_id: str,
        created_at: str,
    ) -> None:
        self._repository.create_assertion_lineage(
            predecessor_assertion_id,
            successor_assertion_id,
            feedback_id,
            created_at,
        )
