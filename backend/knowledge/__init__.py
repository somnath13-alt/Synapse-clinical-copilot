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
from backend.knowledge.governance_service import (
    APPLIED_BRANCH,
    DUPLICATE_LINEAGE,
    FEEDBACK_REQUIRED,
    FUTURE_EFFECTIVE,
    INVALID_REPLACEMENT_SET,
    INVALID_TRANSITION,
    LINEAGE_CYCLE,
    MERGE_NOT_SUPPORTED,
    STALE_TARGET,
    GovernancePolicyError,
    GovernanceService,
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
    "GovernancePolicyError",
    "GovernanceProposalRepository",
    "GovernanceService",
    "KnowledgeAssertion",
    "KnowledgeAssertionState",
    "KnowledgeDataError",
    "KnowledgeQuery",
    "KnowledgeRepository",
    "KnowledgeService",
    "ProposalStatus",
    "ReplacementItem",
    "TransitionResult",
    "APPLIED_BRANCH",
    "DUPLICATE_LINEAGE",
    "FEEDBACK_REQUIRED",
    "FUTURE_EFFECTIVE",
    "INVALID_REPLACEMENT_SET",
    "INVALID_TRANSITION",
    "LINEAGE_CYCLE",
    "MERGE_NOT_SUPPORTED",
    "STALE_TARGET",
]
