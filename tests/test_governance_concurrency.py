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
from backend.knowledge.models import KnowledgeAssertionState
from backend.knowledge.service import KnowledgeService


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_STEP = "AST-SYN-POL-V2-STEP"
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


def test_schema_v7_rejects_one_of_two_concurrent_proposals_for_one_feedback(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database invariant closes the synchronized service check/write race."""

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

    def submit(proposal: CorrectionProposal) -> tuple[str, str]:
        try:
            with database.managed_connection(
                initialized_settings.database_path
            ) as connection:
                GovernanceService(
                    initialized_settings.database_path, connection
                ).submit_correction(proposal)
            return "success", proposal.proposal_id
        except Exception as error:
            return type(error).__name__, str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, proposals))

    assert sum(outcome[0] == "success" for outcome in outcomes) == 1
    loser = next(outcome for outcome in outcomes if outcome[0] != "success")
    assert loser == (
        "IntegrityError",
        "UNIQUE constraint failed: correction_proposal.feedback_id",
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        rows = connection.execute(
            """SELECT proposal_id FROM correction_proposal
               WHERE feedback_id = ? ORDER BY proposal_id""",
            (feedback_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] in {proposal.proposal_id for proposal in proposals}


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


def test_two_concurrent_approvals_return_deterministic_stale_target(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guarded head claim turns the former SUPERSEDED race into STALE_TARGET."""

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
    with database.managed_connection(initialized_settings.database_path) as connection:
        proposals_before = connection.execute(
            """SELECT proposal_id, feedback_id, target_document_version_id,
                      proposed_document_version_id, status, decided_at,
                      decision_actor, decision_role, decision_rationale
               FROM correction_proposal ORDER BY proposal_id"""
        ).fetchall()
        items_before = connection.execute(
            """SELECT * FROM correction_proposal_replacement_item
               ORDER BY proposal_id, ordinal"""
        ).fetchall()
        feedback_before = {
            row[0]: row
            for row in connection.execute(
                "SELECT * FROM feedback ORDER BY feedback_id"
            ).fetchall()
        }
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
    assert loser == ("ValueError", "STALE_TARGET")
    winner_feedback_id = str(
        first["feedback_id"] if outcomes[0][0] == "success" else second["feedback_id"]
    )
    loser_feedback_id = str(
        second["feedback_id"] if outcomes[0][0] == "success" else first["feedback_id"]
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        proposal_states = connection.execute(
            """SELECT feedback_id, status FROM correction_proposal
               ORDER BY feedback_id"""
        ).fetchall()
        feedback_states = connection.execute(
            "SELECT feedback_id, status FROM feedback ORDER BY feedback_id"
        ).fetchall()
        feedback_after = {
            row[0]: row
            for row in connection.execute(
                "SELECT * FROM feedback ORDER BY feedback_id"
            ).fetchall()
        }
        proposals_after = connection.execute(
            """SELECT proposal_id, feedback_id, target_document_version_id,
                      proposed_document_version_id, status, decided_at,
                      decision_actor, decision_role, decision_rationale
               FROM correction_proposal ORDER BY proposal_id"""
        ).fetchall()
        items_after = connection.execute(
            """SELECT * FROM correction_proposal_replacement_item
               ORDER BY proposal_id, ordinal"""
        ).fetchall()
        loser_audit = connection.execute(
            """SELECT event_type, occurred_at, payload_json FROM audit_event
               WHERE feedback_id = ? ORDER BY event_id""",
            (loser_feedback_id,),
        ).fetchall()
        loser_counts = tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE feedback_id = ?",
                (loser_feedback_id,),
            ).fetchone()[0]
            for table in (
                "assertion_lineage",
                "assertion_supersession",
                "knowledge_update",
                "review",
            )
        )
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
        assert connection.execute(
            """SELECT document_version_id FROM source_document_version
               WHERE document_id = 'DOC-SYN-POL-VEL' AND is_current = 1"""
        ).fetchone() == (V2,)

    assert sorted(proposal_states) == sorted(
        [(winner_feedback_id, "APPLIED"), (loser_feedback_id, "PENDING")]
    )
    assert sorted(feedback_states) == sorted(
        [(winner_feedback_id, "APPLIED"), (loser_feedback_id, "PENDING")]
    )
    before_by_feedback = {row[1]: row for row in proposals_before}
    after_by_feedback = {row[1]: row for row in proposals_after}
    assert after_by_feedback[loser_feedback_id] == before_by_feedback[loser_feedback_id]
    assert feedback_after[loser_feedback_id] == feedback_before[loser_feedback_id]
    assert items_after == items_before
    assert loser_counts == (0, 0, 0, 0)
    blocked = [
        (occurred_at, json.loads(payload))
        for event_type, occurred_at, payload in loser_audit
        if event_type == "GOVERNANCE_APPROVAL_BLOCKED"
    ]
    assert len(blocked) == 1
    occurred_at, blocked_facts = blocked[0]
    assert occurred_at == demo.APPROVED_TIME
    assert blocked_facts["proposal_id"] == after_by_feedback[loser_feedback_id][0]
    assert blocked_facts["failure_reason"] == "STALE_TARGET"
    assert blocked_facts["feedback_id"] == loser_feedback_id
    assert blocked_facts["actor"] == "Synthetic Concurrent Reviewer"
    assert blocked_facts["actor_role"] == "KNOWLEDGE_REVIEWER"
    assert blocked_facts["decision"] == "APPLIED"
    assert blocked_facts["action"] == "APPLY_ATTEMPT"
    assert blocked_facts["target_document_version_id"] == V1
    assert blocked_facts["proposed_document_version_id"] == V2
    assert blocked_facts["replacement_items"] == [
        {
            "predecessor_assertion_id": V1_PA,
            "successor_assertion_id": V2_PA,
        }
    ]
    assert blocked_facts["before"] == blocked_facts["after"]
    assert not any(
        event_type in {"REVIEW_APPROVED", "KNOWLEDGE_UPDATE_APPLIED"}
        for event_type, _, _ in loser_audit
    )


def test_item_n_guard_failure_rolls_back_and_audits_stale_target(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Multi-item Submitter",
        "Synthetic multi-item guarded failure.",
        replacement_pairs=((V1_PA, V2_PA), (V1_STEP, V2_STEP)),
    )
    original = KnowledgeService.apply_assertion_state_transition
    calls = 0

    def stale_on_second_item(
        service: KnowledgeService,
        assertion_id: str,
        new_state: KnowledgeAssertionState,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            service._repository._write_connection().execute(
                "UPDATE knowledge_assertion SET state = 'SUPERSEDED' WHERE assertion_id = ?",
                (assertion_id,),
            )
        original(service, assertion_id, new_state)

    monkeypatch.setattr(
        KnowledgeService, "apply_assertion_state_transition", stale_on_second_item
    )

    with pytest.raises(ValueError, match="^STALE_TARGET$"):
        demo.approve_feedback(
            initialized_settings,
            str(feedback["feedback_id"]),
            "Synthetic Multi-item Reviewer",
            "Synthetic guarded rollback review.",
        )

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            """SELECT assertion_id, state FROM knowledge_assertion
               WHERE assertion_id IN (?, ?, ?, ?) ORDER BY assertion_id""",
            (V1_PA, V2_PA, V1_STEP, V2_STEP),
        ).fetchall() == [
            (V1_PA, "APPLIED"),
            (V1_STEP, "APPLIED"),
            (V2_PA, "CANDIDATE"),
            (V2_STEP, "CANDIDATE"),
        ]
        assert connection.execute(
            """SELECT document_version_id, is_current FROM source_document_version
               WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall() == [(V1, 1), (V2, 0)]
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("PENDING",)
        for table in (
            "assertion_lineage",
            "assertion_supersession",
            "knowledge_update",
            "review",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE feedback_id = ?",
                (feedback["feedback_id"],),
            ).fetchone() == (0,)
        blocked = connection.execute(
            """SELECT payload_json FROM audit_event
               WHERE feedback_id = ? AND event_type = 'GOVERNANCE_APPROVAL_BLOCKED'""",
            (feedback["feedback_id"],),
        ).fetchall()

    assert len(blocked) == 1
    assert json.loads(blocked[0][0])["failure_reason"] == "STALE_TARGET"


def test_sequential_competing_approval_still_returns_stale_target(
    initialized_settings: Settings,
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    first = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Sequential Submitter A",
        "Synthetic sequential proposal A.",
        replacement_pairs=((V1_PA, V2_PA),),
    )
    second = demo.submit_feedback(
        initialized_settings,
        str(interaction["interaction_id"]),
        "Synthetic Sequential Submitter B",
        "Synthetic sequential proposal B.",
        replacement_pairs=((V1_PA, V2_PA),),
    )

    assert demo.approve_feedback(
        initialized_settings,
        str(first["feedback_id"]),
        "Synthetic Sequential Reviewer",
        "Synthetic first review.",
    )["status"] == "APPLIED"
    with pytest.raises(ValueError, match="^STALE_TARGET$"):
        demo.approve_feedback(
            initialized_settings,
            str(second["feedback_id"]),
            "Synthetic Sequential Reviewer",
            "Synthetic stale review.",
        )

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            """SELECT status FROM correction_proposal WHERE feedback_id = ?""",
            (second["feedback_id"],),
        ).fetchone() == ("PENDING",)
        assert connection.execute(
            """SELECT COUNT(*) FROM audit_event
               WHERE feedback_id = ? AND event_type = 'GOVERNANCE_APPROVAL_BLOCKED'
                     AND json_extract(payload_json, '$.failure_reason') = 'STALE_TARGET'""",
            (second["feedback_id"],),
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
