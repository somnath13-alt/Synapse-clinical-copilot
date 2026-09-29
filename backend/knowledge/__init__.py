"""Read-only typed access to persisted knowledge."""

from backend.knowledge.governance_models import (
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    ProposalStatus,
    ReplacementItem,
    TransitionResult,
)
from backend.knowledge.governance_repository import (
    GovernancePersistenceError,
    GovernanceProposalRepository,
)
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
    "Actor",
    "AssertionLineageEdge",
    "AssertionProvenance",
    "CorrectionProposal",
    "GovernanceDecision",
    "GovernancePersistenceError",
    "GovernanceProposalRepository",
    "KnowledgeAssertion",
    "KnowledgeAssertionState",
    "KnowledgeDataError",
    "KnowledgeQuery",
    "KnowledgeRepository",
    "KnowledgeService",
    "ProposalStatus",
    "ReplacementItem",
    "TransitionResult",
]
