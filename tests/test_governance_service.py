from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    APPLIED_BRANCH,
    DUPLICATE_LINEAGE,
    FUTURE_EFFECTIVE,
    LINEAGE_CYCLE,
    STALE_TARGET,
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    GovernanceProposalRepository,
    GovernanceService,
    KnowledgeAssertionState,
    KnowledgeRepository,
    KnowledgeService,
    ProposalStatus,
    ReplacementItem,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
V2_PA_BRANCH = "AST-SYN-POL-V2-PA-BRANCH"
CREATED_AT = "2026-06-16T15:00:00Z"
DECIDED_AT = demo.APPROVED_TIME


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _item(settings: Settings, predecessor_id: str, successor_id: str) -> ReplacementItem:
    repository = KnowledgeRepository(settings.database_path)
    predecessor = repository.get_assertion(predecessor_id)
    successor = repository.get_assertion(successor_id)
    predecessor_provenance = repository.get_assertion_provenance(predecessor_id)
    successor_provenance = repository.get_assertion_provenance(successor_id)
    assert predecessor is not None and successor is not None
    assert predecessor_provenance is not None and successor_provenance is not None
    return ReplacementItem(
        predecessor_assertion_id=predecessor.assertion_id,
        successor_assertion_id=successor.assertion_id,
        predicate=predecessor.predicate,
        decision_dimension=predecessor.decision_dimension,
        normalized_scope=predecessor.normalized_scope,
        predecessor_value=predecessor.value,
        successor_value=successor.value,
        predecessor_effective_from=predecessor.effective_from,
        predecessor_effective_to=predecessor.effective_to,
        successor_effective_from=successor.effective_from,
        successor_effective_to=successor.effective_to,
        predecessor_document_id=predecessor_provenance.document_id,
        predecessor_document_version_id=predecessor.document_version_id,
        predecessor_evidence_ids=predecessor.evidence_ids,
        successor_document_id=successor_provenance.document_id,
        successor_document_version_id=successor.document_version_id,
        successor_evidence_ids=successor.evidence_ids,
    )


def _feedback(settings: Settings) -> str:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    feedback_id = f"FDB-M74-{interaction['interaction_id'][-12:]}"
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """INSERT INTO feedback VALUES (?, ?, ?, 'CARE_COORDINATOR', ?,
                      ?, ?, 'PENDING', ?, NULL)""",
            (
                feedback_id,
                interaction["interaction_id"],
                "Synthetic Care Coordinator",
                "Synthetic policy correction.",
                V1,
                V2,
                CREATED_AT,
            ),
        )
    return feedback_id


def _proposal(
    settings: Settings,
    feedback_id: str | None,
    *,
    proposal_id: str = "PROP-SYN-M74-001",
) -> CorrectionProposal:
    return CorrectionProposal(
        proposal_id=proposal_id,
        feedback_id=feedback_id,
        target_document_version_id=V1,
        proposed_document_version_id=V2,
        submitter=Actor("Synthetic Care Coordinator", "CARE_COORDINATOR"),
        rationale="Synthetic V2 corrects the outdated payer assertions.",
        status=ProposalStatus.PENDING,
        created_at=CREATED_AT,
        decided_at=None,
        replacement_items=(
            _item(settings, V1_PA, V2_PA),
            _item(settings, V1_STEP, V2_STEP),
        ),
    )


def _decision(
    proposal_id: str,
    status: ProposalStatus = ProposalStatus.APPLIED,
    *,
    timestamp: str = DECIDED_AT,
) -> GovernanceDecision:
    return GovernanceDecision(
        proposal_id=proposal_id,
        decision=status,
        actor=Actor("Synthetic Knowledge Reviewer", "KNOWLEDGE_REVIEWER"),
        rationale="Reviewed the synthetic evidence and provenance.",
        timestamp=timestamp,
    )


def _submit(settings: Settings, proposal: CorrectionProposal) -> None:
    with database.managed_connection(settings.database_path) as connection:
        result = GovernanceService(
            settings.database_path, connection
        ).submit_correction(proposal)
    assert result.success is True
    assert result.status is ProposalStatus.PENDING


def _authority(settings: Settings) -> dict[str, object]:
    with database.managed_connection(settings.database_path) as connection:
        return {
            "versions": connection.execute(
                """SELECT document_version_id, is_current, governance_state
                   FROM source_document_version WHERE document_version_id IN (?, ?)
                   ORDER BY document_version_id""",
                (V1, V2),
            ).fetchall(),
            "assertions": connection.execute(
                """SELECT assertion_id, state FROM knowledge_assertion
                   WHERE assertion_id IN (?, ?, ?, ?) ORDER BY assertion_id""",
                (V1_PA, V1_STEP, V2_PA, V2_STEP),
            ).fetchall(),
            "lineage": connection.execute(
                "SELECT COUNT(*) FROM assertion_lineage"
            ).fetchone()[0],
            "updates": connection.execute(
                "SELECT COUNT(*) FROM knowledge_update"
            ).fetchone()[0],
        }


def test_submission_persists_snapshot_without_changing_authority(
    initialized_settings: Settings,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    before = _authority(initialized_settings)

    _submit(initialized_settings, proposal)

    stored = GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(proposal.proposal_id)
    assert stored == proposal
    assert _authority(initialized_settings) == before


def test_approval_applies_complete_set_and_preserves_complete_audit_facts(
    initialized_settings: Settings,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    _submit(initialized_settings, proposal)
    decision = _decision(proposal.proposal_id)

    with database.managed_connection(initialized_settings.database_path) as connection:
        result = GovernanceService(
            initialized_settings.database_path, connection
        ).approve_correction(proposal.proposal_id, decision)

    assert result.success is True
    assert result.status is ProposalStatus.APPLIED
    assert _authority(initialized_settings) == {
        "versions": [(V1, 0, "APPLIED"), (V2, 1, "APPLIED")],
        "assertions": [
            (V1_PA, "SUPERSEDED"),
            (V1_STEP, "SUPERSEDED"),
            (V2_PA, "APPLIED"),
            (V2_STEP, "APPLIED"),
        ],
        "lineage": 2,
        "updates": 1,
    }
    repository = GovernanceProposalRepository(initialized_settings.database_path)
    assert repository.get_decision(proposal.proposal_id) == decision
    with database.managed_connection(initialized_settings.database_path) as connection:
        rows = connection.execute(
            """SELECT event_type, payload_json FROM audit_event
               WHERE feedback_id = ? AND event_type IN
                     ('REVIEW_APPROVED', 'KNOWLEDGE_UPDATE_APPLIED')
               ORDER BY event_id""",
            (feedback_id,),
        ).fetchall()
    assert [row[0] for row in rows] == [
        "REVIEW_APPROVED",
        "KNOWLEDGE_UPDATE_APPLIED",
    ]
    payload = json.loads(rows[0][1])
    assert payload["actor"] == decision.actor.actor_id
    assert payload["actor_role"] == decision.actor.role
    assert payload["rationale"] == decision.rationale
    assert payload["target_document_version_id"] == V1
    assert payload["proposed_document_version_id"] == V2
    assert len(payload["lineage_ids"]) == 2
    assert payload["before"]["assertions"] != payload["after"]["assertions"]


def test_rejection_is_terminal_and_does_not_change_authority(
    initialized_settings: Settings,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    _submit(initialized_settings, proposal)
    before = _authority(initialized_settings)
    decision = _decision(proposal.proposal_id, ProposalStatus.REJECTED)

    with database.managed_connection(initialized_settings.database_path) as connection:
        result = GovernanceService(
            initialized_settings.database_path, connection
        ).reject_correction(proposal.proposal_id, decision)

    assert result.success is True
    assert result.status is ProposalStatus.REJECTED
    assert _authority(initialized_settings) == before
    repository = GovernanceProposalRepository(initialized_settings.database_path)
    assert repository.get_decision(proposal.proposal_id) == decision
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT status, applied_at FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone() == ("REJECTED", None)
        assert connection.execute(
            "SELECT decision FROM review WHERE feedback_id = ?", (feedback_id,)
        ).fetchone() == ("REJECTED",)
        payload = json.loads(
            connection.execute(
                """SELECT payload_json FROM audit_event
                   WHERE feedback_id = ? AND event_type = 'REVIEW_REJECTED'""",
                (feedback_id,),
            ).fetchone()[0]
        )
    assert payload["decision"] == "REJECTED"
    assert payload["actor"] == decision.actor.actor_id
    assert payload["rationale"] == decision.rationale
    assert payload["before"] == payload["after"]
    assert payload["lineage_ids"] == []


def test_future_effective_approval_stays_pending_and_is_audited(
    initialized_settings: Settings,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    _submit(initialized_settings, proposal)
    before = _authority(initialized_settings)
    decision = _decision(
        proposal.proposal_id, timestamp="2026-06-30T00:00:00Z"
    )

    with database.managed_connection(initialized_settings.database_path) as connection:
        result = GovernanceService(
            initialized_settings.database_path, connection
        ).approve_correction(proposal.proposal_id, decision)

    assert result.success is False
    assert result.status is ProposalStatus.PENDING
    assert result.failure_reason == FUTURE_EFFECTIVE
    assert _authority(initialized_settings) == before
    stored = GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(proposal.proposal_id)
    assert stored is not None and stored.status is ProposalStatus.PENDING
    with database.managed_connection(initialized_settings.database_path) as connection:
        payload = json.loads(
            connection.execute(
                """SELECT payload_json FROM audit_event
                   WHERE feedback_id = ? AND event_type = 'GOVERNANCE_APPROVAL_BLOCKED'""",
                (feedback_id,),
            ).fetchone()[0]
        )
    assert payload["failure_reason"] == FUTURE_EFFECTIVE
    assert payload["before"] == payload["after"]


def test_competing_proposals_coexist_and_later_approval_is_stale(
    initialized_settings: Settings,
) -> None:
    first_feedback = _feedback(initialized_settings)
    second_feedback = _feedback(initialized_settings)
    first = _proposal(
        initialized_settings, first_feedback, proposal_id="PROP-SYN-M74-FIRST"
    )
    second = _proposal(
        initialized_settings, second_feedback, proposal_id="PROP-SYN-M74-SECOND"
    )
    _submit(initialized_settings, first)
    _submit(initialized_settings, second)

    with database.managed_connection(initialized_settings.database_path) as connection:
        service = GovernanceService(initialized_settings.database_path, connection)
        assert service.approve_correction(
            first.proposal_id, _decision(first.proposal_id)
        ).success
    with database.managed_connection(initialized_settings.database_path) as connection:
        result = GovernanceService(
            initialized_settings.database_path, connection
        ).approve_correction(second.proposal_id, _decision(second.proposal_id))

    assert result.success is False
    assert result.failure_reason == STALE_TARGET
    stored = GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(second.proposal_id)
    assert stored is not None and stored.status is ProposalStatus.PENDING
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM feedback WHERE feedback_id = ?", (second_feedback,)
        ).fetchone() == ("PENDING",)
        assert connection.execute(
            "SELECT COUNT(*) FROM review WHERE feedback_id = ?", (second_feedback,)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("edge", "reason"),
    [
        ((V1_PA, V2_PA), DUPLICATE_LINEAGE),
        ((V1_PA, V2_PA_BRANCH), APPLIED_BRANCH),
        ((V2_PA, V1_PA), LINEAGE_CYCLE),
    ],
)
def test_approval_prevents_duplicate_branch_and_bounded_cycle(
    initialized_settings: Settings,
    edge: tuple[str, str],
    reason: str,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    _submit(initialized_settings, proposal)
    with database.managed_connection(initialized_settings.database_path) as connection:
        if edge[1] == V2_PA_BRANCH:
            connection.execute(
                """INSERT INTO knowledge_assertion
                   SELECT ?, document_version_id, subject_id, predicate, object_id,
                          value_json, decision_dimension, normalized_scope_json,
                          recorded_at, effective_from, effective_to, 'APPLIED'
                   FROM knowledge_assertion WHERE assertion_id = ?""",
                (V2_PA_BRANCH, V2_PA),
            )
        connection.execute(
            "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
            ("LIN-SYN-PREEXISTING", edge[0], edge[1], feedback_id, CREATED_AT),
        )
    before = _authority(initialized_settings)

    with database.managed_connection(initialized_settings.database_path) as connection:
        result = GovernanceService(
            initialized_settings.database_path, connection
        ).approve_correction(proposal.proposal_id, _decision(proposal.proposal_id))

    assert result.success is False
    assert result.failure_reason == reason
    assert _authority(initialized_settings) == before


def test_service_uses_caller_transaction_and_does_not_commit_submission(
    initialized_settings: Settings,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    connection = sqlite3.connect(initialized_settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        GovernanceService(
            initialized_settings.database_path, connection
        ).submit_correction(proposal)
        assert connection.execute(
            "SELECT COUNT(*) FROM correction_proposal WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone() == (1,)
        connection.rollback()
    finally:
        connection.close()

    assert GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(proposal.proposal_id) is None


def test_approval_delegates_assertion_transitions_and_lineage(
    initialized_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_id = _feedback(initialized_settings)
    proposal = _proposal(initialized_settings, feedback_id)
    _submit(initialized_settings, proposal)
    transitions: list[tuple[str, KnowledgeAssertionState]] = []
    lineage: list[tuple[str, str]] = []
    original_transition = KnowledgeService.apply_assertion_state_transition
    original_lineage = KnowledgeService.create_assertion_lineage

    def capture_transition(
        service: KnowledgeService,
        assertion_id: str,
        new_state: KnowledgeAssertionState,
    ) -> None:
        transitions.append((assertion_id, new_state))
        original_transition(service, assertion_id, new_state)

    def capture_lineage(
        service: KnowledgeService,
        predecessor_id: str,
        successor_id: str,
        linked_feedback_id: str,
        created_at: str,
    ) -> None:
        lineage.append((predecessor_id, successor_id))
        original_lineage(
            service,
            predecessor_id,
            successor_id,
            linked_feedback_id,
            created_at,
        )

    monkeypatch.setattr(
        KnowledgeService, "apply_assertion_state_transition", capture_transition
    )
    monkeypatch.setattr(KnowledgeService, "create_assertion_lineage", capture_lineage)
    with database.managed_connection(initialized_settings.database_path) as connection:
        GovernanceService(
            initialized_settings.database_path, connection
        ).approve_correction(proposal.proposal_id, _decision(proposal.proposal_id))

    assert transitions == [
        (V1_PA, KnowledgeAssertionState.SUPERSEDED),
        (V1_STEP, KnowledgeAssertionState.SUPERSEDED),
        (V2_PA, KnowledgeAssertionState.APPLIED),
        (V2_STEP, KnowledgeAssertionState.APPLIED),
    ]
    assert lineage == [(V1_PA, V2_PA), (V1_STEP, V2_STEP)]
