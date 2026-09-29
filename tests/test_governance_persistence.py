from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    GovernancePersistenceError,
    GovernanceProposalRepository,
    KnowledgeRepository,
    ProposalStatus,
    ReplacementItem,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
CREATED_AT = "2026-06-16T15:00:00Z"
DECIDED_AT = "2026-07-02T13:00:00Z"


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
    assert predecessor.normalized_scope == successor.normalized_scope
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


def _proposal(
    settings: Settings,
    *,
    proposal_id: str = "PROP-SYN-M73-001",
    feedback_id: str | None = None,
    status: ProposalStatus = ProposalStatus.PENDING,
    decided_at: str | None = None,
    replacement_items: tuple[ReplacementItem, ...] | None = None,
) -> CorrectionProposal:
    return CorrectionProposal(
        proposal_id=proposal_id,
        feedback_id=feedback_id,
        target_document_version_id=V1,
        proposed_document_version_id=V2,
        submitter=Actor("Synthetic Care Coordinator", "CARE_COORDINATOR"),
        rationale="Synthetic V2 corrects the outdated policy assertions.",
        status=status,
        created_at=CREATED_AT,
        decided_at=decided_at,
        replacement_items=replacement_items
        or (
            _item(settings, V1_PA, V2_PA),
            _item(settings, V1_STEP, V2_STEP),
        ),
    )


def _decision(
    proposal_id: str,
    status: ProposalStatus = ProposalStatus.APPLIED,
) -> GovernanceDecision:
    return GovernanceDecision(
        proposal_id=proposal_id,
        decision=status,
        actor=Actor("Synthetic Knowledge Reviewer", "KNOWLEDGE_REVIEWER"),
        rationale="Reviewed the synthetic proposal evidence and provenance.",
        timestamp=DECIDED_AT,
    )


def _persist(
    settings: Settings,
    proposal: CorrectionProposal,
    decision: GovernanceDecision | None = None,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        GovernanceProposalRepository(
            settings.database_path, connection=connection
        ).create_proposal(proposal, decision=decision)


def _authority_snapshot(settings: Settings) -> dict[str, object]:
    with database.managed_connection(settings.database_path) as connection:
        return {
            "versions": connection.execute(
                """SELECT document_version_id, governance_state, is_current
                   FROM source_document_version
                   WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
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
            "feedback": connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
            "reviews": connection.execute("SELECT COUNT(*) FROM review").fetchone()[0],
        }


def test_proposal_and_replacement_snapshots_round_trip_in_deterministic_order(
    initialized_settings: Settings,
) -> None:
    proposal = _proposal(
        initialized_settings,
        replacement_items=(
            _item(initialized_settings, V1_STEP, V2_STEP),
            _item(initialized_settings, V1_PA, V2_PA),
        ),
    )
    _persist(initialized_settings, proposal)

    stored = GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(proposal.proposal_id)
    assert stored == proposal
    assert [item.predecessor_assertion_id for item in stored.replacement_items] == [
        V1_PA,
        V1_STEP,
    ]
    with database.managed_connection(initialized_settings.database_path) as connection:
        rows = connection.execute(
            """SELECT ordinal, predecessor_assertion_id, successor_assertion_id,
                      predicate, decision_dimension, normalized_scope_json,
                      predecessor_value_json, successor_value_json,
                      predecessor_effective_from, predecessor_effective_to,
                      successor_effective_from, successor_effective_to
               FROM correction_proposal_replacement_item
               WHERE proposal_id = ? ORDER BY ordinal""",
            (proposal.proposal_id,),
        ).fetchall()
    assert [row[:3] for row in rows] == [
        (0, V1_PA, V2_PA),
        (1, V1_STEP, V2_STEP),
    ]
    assert json.loads(rows[0][5]) == dict(proposal.replacement_items[0].normalized_scope)
    assert json.loads(rows[0][6]) == proposal.replacement_items[0].predecessor_value
    assert json.loads(rows[0][7]) == proposal.replacement_items[0].successor_value


def test_proposal_creation_is_non_authoritative_and_creates_no_applied_history(
    initialized_settings: Settings,
) -> None:
    before = _authority_snapshot(initialized_settings)
    _persist(initialized_settings, _proposal(initialized_settings))

    assert _authority_snapshot(initialized_settings) == before


def test_snapshot_does_not_late_bind_changed_assertion_fields(
    initialized_settings: Settings,
) -> None:
    proposal = _proposal(initialized_settings)
    _persist(initialized_settings, proposal)

    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE knowledge_assertion
               SET predicate = 'MUTATED_AFTER_PROPOSAL', value_json = 'false',
                   normalized_scope_json = '{"mutated":true}',
                   effective_from = '2030-01-01T00:00:00Z'
               WHERE assertion_id = ?""",
            (V1_PA,),
        )

    stored = GovernanceProposalRepository(
        initialized_settings.database_path
    ).get_proposal(proposal.proposal_id)
    assert stored == proposal
    assert stored.replacement_items[0].predicate == "REQUIRES_AUTHORIZATION"


@pytest.mark.parametrize("status", [ProposalStatus.APPLIED, ProposalStatus.REJECTED])
def test_terminal_status_persists_complete_decision_facts(
    initialized_settings: Settings, status: ProposalStatus
) -> None:
    proposal = _proposal(
        initialized_settings,
        proposal_id=f"PROP-SYN-M73-{status.value}",
        status=status,
        decided_at=DECIDED_AT,
    )
    decision = _decision(proposal.proposal_id, status)
    _persist(initialized_settings, proposal, decision)

    repository = GovernanceProposalRepository(initialized_settings.database_path)
    assert repository.get_proposal(proposal.proposal_id) == proposal
    assert repository.get_decision(proposal.proposal_id) == decision


def test_pending_and_terminal_decision_consistency_is_enforced(
    initialized_settings: Settings,
) -> None:
    pending = _proposal(initialized_settings)
    with pytest.raises(GovernancePersistenceError, match="must not include"):
        _persist(initialized_settings, pending, _decision(pending.proposal_id))

    terminal = _proposal(
        initialized_settings,
        proposal_id="PROP-SYN-M73-TERMINAL",
        status=ProposalStatus.REJECTED,
        decided_at=DECIDED_AT,
    )
    with pytest.raises(GovernancePersistenceError, match="require decision facts"):
        _persist(initialized_settings, terminal)


def test_empty_proposal_is_rejected_by_the_deferred_database_constraint(
    initialized_settings: Settings,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                """INSERT INTO correction_proposal
                   (proposal_id, feedback_id, logical_document_id,
                    target_document_version_id, proposed_document_version_id,
                    submitter_actor, submitter_role, rationale, status, created_at,
                    decided_at, decision_actor, decision_role, decision_rationale,
                    first_replacement_ordinal)
                   VALUES ('PROP-SYN-EMPTY', NULL, 'DOC-SYN-POL-VEL', ?, ?,
                           'Synthetic Actor', 'CARE_COORDINATOR', 'Synthetic reason',
                           'PENDING', ?, NULL, NULL, NULL, NULL, 0)""",
                (V1, V2, CREATED_AT),
            )


def test_status_vocabulary_and_item_ownership_constraints_are_structural(
    initialized_settings: Settings,
) -> None:
    proposal = _proposal(initialized_settings)
    _persist(initialized_settings, proposal)
    with database.managed_connection(initialized_settings.database_path) as connection:
        proposal_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'correction_proposal'"
        ).fetchone()[0]
        replacement_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(correction_proposal_replacement_item)"
            )
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO correction_proposal_replacement_item
                   SELECT proposal_id, ordinal, predecessor_assertion_id,
                          successor_assertion_id, predicate, decision_dimension,
                          normalized_scope_json, predecessor_value_json,
                          successor_value_json, predecessor_effective_from,
                          predecessor_effective_to, successor_effective_from,
                          successor_effective_to, predecessor_document_version_id,
                          successor_document_version_id
                   FROM correction_proposal_replacement_item
                   WHERE proposal_id = ? AND ordinal = 0""",
                (proposal.proposal_id,),
            )
    assert "'PENDING', 'APPLIED', 'REJECTED'" in proposal_sql
    assert len(replacement_indexes) >= 5


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("predecessor_assertion_id", "AST-SYN-UNKNOWN", "Referenced assertion"),
        ("successor_assertion_id", "AST-SYN-UNKNOWN", "Referenced assertion"),
    ],
)
def test_unknown_assertion_references_are_rejected(
    initialized_settings: Settings, field: str, value: str, message: str
) -> None:
    item = replace(_item(initialized_settings, V1_PA, V2_PA), **{field: value})
    proposal = _proposal(initialized_settings, replacement_items=(item,))
    with pytest.raises(GovernancePersistenceError, match=message):
        _persist(initialized_settings, proposal)


@pytest.mark.parametrize(
    ("predecessor_id", "successor_id"),
    [
        ("AST-SYN-UNKNOWN", V2_STEP),
        (V1_STEP, "AST-SYN-UNKNOWN"),
    ],
)
def test_database_foreign_keys_reject_unknown_replacement_assertions(
    initialized_settings: Settings,
    predecessor_id: str,
    successor_id: str,
) -> None:
    proposal = _proposal(
        initialized_settings,
        replacement_items=(_item(initialized_settings, V1_PA, V2_PA),),
    )
    _persist(initialized_settings, proposal)

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                """INSERT INTO correction_proposal_replacement_item
                   SELECT proposal_id, 1, ?, ?, predicate, decision_dimension,
                          normalized_scope_json, predecessor_value_json,
                          successor_value_json, predecessor_effective_from,
                          predecessor_effective_to, successor_effective_from,
                          successor_effective_to, predecessor_document_version_id,
                          successor_document_version_id
                   FROM correction_proposal_replacement_item
                   WHERE proposal_id = ? AND ordinal = 0""",
                (predecessor_id, successor_id, proposal.proposal_id),
            )


@pytest.mark.parametrize("field", ["target_document_version_id", "proposed_document_version_id"])
def test_unknown_document_version_references_are_rejected(
    initialized_settings: Settings, field: str
) -> None:
    proposal = replace(_proposal(initialized_settings), **{field: "DV-SYN-UNKNOWN"})
    with pytest.raises(GovernancePersistenceError, match="document version does not exist"):
        _persist(initialized_settings, proposal)


def test_document_versions_must_share_one_logical_document(
    initialized_settings: Settings,
) -> None:
    proposal = replace(
        _proposal(initialized_settings),
        proposed_document_version_id="DV-SYN-GUIDE-LDS-2026-1",
    )
    with pytest.raises(GovernancePersistenceError, match="same logical document"):
        _persist(initialized_settings, proposal)


def test_snapshot_validation_is_atomic_on_failure(
    initialized_settings: Settings,
) -> None:
    item = replace(
        _item(initialized_settings, V1_PA, V2_PA),
        successor_value={"synthetic": "does not match"},
    )
    proposal = _proposal(initialized_settings, replacement_items=(item,))
    with pytest.raises(GovernancePersistenceError, match="Successor proposal-time facts"):
        _persist(initialized_settings, proposal)

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM correction_proposal"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM correction_proposal_replacement_item"
        ).fetchone() == (0,)


def test_feedback_api_does_not_implicitly_activate_proposal_persistence(
    initialized_settings: Settings,
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        initialized_settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "The synthetic payer policy has a newer reviewed version.",
    )

    assert feedback["status"] == "PENDING"
    assert feedback["target_version_id"] == V1
    assert feedback["proposed_version_id"] == V2
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM correction_proposal"
        ).fetchone() == (0,)


def test_reset_removes_all_proposal_rows_and_preserves_seeded_v1_v2(
    initialized_settings: Settings,
) -> None:
    _persist(initialized_settings, _proposal(initialized_settings))
    database.reset_demo_database(initialized_settings)

    with database.managed_connection(initialized_settings.database_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "correction_proposal",
                "correction_proposal_replacement_item",
                "correction_proposal_predecessor_evidence",
                "correction_proposal_successor_evidence",
            )
        }
        payer = connection.execute(
            """SELECT document_version_id, is_current FROM source_document_version
               WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert counts == {table: 0 for table in counts}
    assert payer == [(V1, 1), (V2, 0)]
    assert foreign_keys == []
    assert integrity == ("ok",)
