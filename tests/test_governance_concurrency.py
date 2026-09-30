from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    GovernanceService,
    KnowledgeRepository,
    ProposalStatus,
    ReplacementItem,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
CREATED_AT = "2026-06-16T15:00:00Z"


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _replacement(settings: Settings) -> ReplacementItem:
    repository = KnowledgeRepository(settings.database_path)
    predecessor = repository.get_assertion(V1_PA)
    successor = repository.get_assertion(V2_PA)
    predecessor_provenance = repository.get_assertion_provenance(V1_PA)
    successor_provenance = repository.get_assertion_provenance(V2_PA)
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


def _shared_feedback(settings: Settings) -> str:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    feedback_id = "FDB-SYN-CONCURRENT-SUBMISSION"
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """INSERT INTO feedback VALUES (?, ?, ?, 'CARE_COORDINATOR', ?,
                      ?, ?, 'PENDING', ?, NULL)""",
            (
                feedback_id,
                interaction["interaction_id"],
                "Synthetic Concurrent Submitter",
                "Synthetic concurrent proposal test.",
                V1,
                V2,
                CREATED_AT,
            ),
        )
    return feedback_id


def _proposal(
    settings: Settings, feedback_id: str, proposal_id: str
) -> CorrectionProposal:
    return CorrectionProposal(
        proposal_id=proposal_id,
        feedback_id=feedback_id,
        target_document_version_id=V1,
        proposed_document_version_id=V2,
        submitter=Actor("Synthetic Concurrent Submitter", "CARE_COORDINATOR"),
        rationale="Synthetic concurrent proposal test.",
        status=ProposalStatus.PENDING,
        created_at=CREATED_AT,
        decided_at=None,
        replacement_items=(_replacement(settings),),
    )


def test_schema_v6_service_check_allows_two_concurrent_proposals_for_one_feedback(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterize the missing structural feedback/proposal uniqueness invariant."""

    feedback_id = _shared_feedback(initialized_settings)
    proposals = (
        _proposal(initialized_settings, feedback_id, "PROP-SYN-CONCURRENT-A"),
        _proposal(initialized_settings, feedback_id, "PROP-SYN-CONCURRENT-B"),
    )
    barrier = threading.Barrier(2)
    original = GovernanceService._validate_lineage_policy

    def synchronize_after_validation(
        service: GovernanceService, proposal: CorrectionProposal
    ) -> None:
        original(service, proposal)
        barrier.wait(timeout=10)

    monkeypatch.setattr(
        GovernanceService, "_validate_lineage_policy", synchronize_after_validation
    )

    def submit(proposal: CorrectionProposal) -> str:
        with database.managed_connection(
            initialized_settings.database_path
        ) as connection:
            GovernanceService(
                initialized_settings.database_path, connection
            ).submit_correction(proposal)
        return proposal.proposal_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, proposals))

    assert set(outcomes) == {proposal.proposal_id for proposal in proposals}
    with database.managed_connection(initialized_settings.database_path) as connection:
        rows = connection.execute(
            """SELECT proposal_id FROM correction_proposal
               WHERE feedback_id = ? ORDER BY proposal_id""",
            (feedback_id,),
        ).fetchall()
    assert rows == [
        ("PROP-SYN-CONCURRENT-A",),
        ("PROP-SYN-CONCURRENT-B",),
    ]


def test_submission_audit_payloads_depend_on_mutable_related_rows_for_core_facts(
    initialized_settings: Settings,
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Audit Submitter",
        "Synthetic audit completeness characterization.",
        actor_role="CLINICIAN",
        replacement_pairs=((V1_PA, V2_PA),),
    )

    with database.managed_connection(initialized_settings.database_path) as connection:
        rows = connection.execute(
            """SELECT event_type, occurred_at, payload_json
               FROM audit_event WHERE feedback_id = ? ORDER BY event_id""",
            (feedback["feedback_id"],),
        ).fetchall()
        proposal_id = connection.execute(
            "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone()[0]

    assert [(event_type, json.loads(payload)) for event_type, _, payload in rows] == [
        (
            "FEEDBACK_SUBMITTED",
            {
                "message": "Synthetic audit completeness characterization.",
                "status": "SUBMITTED",
            },
        ),
        (
            "FEEDBACK_PENDING",
            {"proposed_version_id": V2, "status": "PENDING"},
        ),
    ]
    assert {occurred_at for _, occurred_at, _ in rows} == {demo.SUBMITTED_TIME}
    immutable_payload_keys = {
        key
        for _, _, payload in rows
        for key in json.loads(payload)
    }
    assert {
        "proposal_id",
        "feedback_id",
        "actor",
        "actor_role",
        "action",
        "rationale",
        "target_document_version_id",
        "replacement_items",
        "before",
        "after",
        "lineage_ids",
        "review_id",
        "update_id",
        "failure_reason",
        "timestamp",
    }.isdisjoint(immutable_payload_keys)
    assert proposal_id.startswith("PROP-")


def test_two_concurrent_approvals_do_not_guarantee_explicit_stale_target(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterize the validation/write race without changing production locking."""

    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    first = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Submitter A",
        "Synthetic competing proposal A.",
        replacement_pairs=((V1_PA, V2_PA),),
    )
    second = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Submitter B",
        "Synthetic competing proposal B.",
        replacement_pairs=((V1_PA, V2_PA),),
    )
    barrier = threading.Barrier(2)
    original = GovernanceService.validate_transition

    def synchronize_after_validation(
        service: GovernanceService,
        proposal_id: str,
        decision: GovernanceDecision,
    ) -> str | None:
        result = original(service, proposal_id, decision)
        if result is None:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        GovernanceService, "validate_transition", synchronize_after_validation
    )

    def approve(feedback_id: str) -> tuple[str, str]:
        try:
            result = demo.approve_feedback(
                initialized_settings,
                feedback_id,
                "Synthetic Concurrent Reviewer",
                "Synthetic concurrency review.",
            )
            return "success", str(result["status"])
        except Exception as error:  # characterization records the exact loser class
            return type(error).__name__, str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                approve,
                (str(first["feedback_id"]), str(second["feedback_id"])),
            )
        )

    assert outcomes.count(("success", "APPLIED")) == 1
    loser = next(outcome for outcome in outcomes if outcome[0] != "success")
    assert loser == (
        "KnowledgeStateTransitionError",
        "Invalid knowledge assertion state transition: SUPERSEDED -> SUPERSEDED",
    )
    assert loser != ("ValueError", "STALE_TARGET")
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM assertion_lineage"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM assertion_supersession"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_update"
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT COUNT(*) FROM source_document_version
               WHERE document_id = 'DOC-SYN-POL-VEL' AND is_current = 1"""
        ).fetchone() == (1,)


def test_live_approval_rejects_an_unsupported_lineage_merge(
    initialized_settings: Settings,
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Merge Submitter",
        "Synthetic unsupported merge characterization.",
        replacement_pairs=((V1_PA, V2_PA),),
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
            (
                "LIN-SYN-PREEXISTING-MERGE",
                "AST-SYN-POL-V1-STEP",
                V2_PA,
                feedback["feedback_id"],
                CREATED_AT,
            ),
        )

    with pytest.raises(ValueError, match="MERGE_NOT_SUPPORTED"):
        demo.approve_feedback(
            initialized_settings,
            str(feedback["feedback_id"]),
            "Synthetic Merge Reviewer",
            "Synthetic merge refusal.",
        )

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("PENDING",)
        assert connection.execute(
            "SELECT is_current FROM source_document_version WHERE document_version_id = ?",
            (V1,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM review WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_update WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (0,)
