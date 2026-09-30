from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    KnowledgeAssertionState,
    KnowledgeDataError,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.knowledge.repository import KnowledgeStateTransitionError


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
PAYER_DOCUMENT = "DOC-SYN-POL-VEL"
ACTOR = "Synthetic Care Coordinator"
MESSAGE = "Payer policy V1 is outdated; use V2."
REVIEWER = "Synthetic Knowledge Reviewer"
RATIONALE = "Verified the pre-seeded V2 provenance and effective date."
PAYER_QUERY = KnowledgeQuery(
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
)


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _ask(settings: Settings, *, as_of: str | None = None) -> dict[str, Any]:
    return demo.ask_question(settings, demo.CANONICAL_QUESTION, as_of=as_of)


def _submit(settings: Settings, interaction_id: str) -> dict[str, Any]:
    return demo.submit_feedback(settings, interaction_id, ACTOR, MESSAGE)


def _approve(settings: Settings, feedback_id: str) -> dict[str, Any]:
    return demo.approve_feedback(settings, feedback_id, REVIEWER, RATIONALE)


def _mark_feedback_applied(
    connection: sqlite3.Connection, feedback_id: str
) -> None:
    connection.execute(
        "UPDATE feedback SET status = 'APPLIED', applied_at = ? WHERE feedback_id = ?",
        (demo.APPROVED_TIME, feedback_id),
    )


def _state_snapshot(
    connection: sqlite3.Connection, feedback_id: str
) -> dict[str, Any]:
    target_version_id, proposed_version_id = connection.execute(
        "SELECT target_version_id, proposed_version_id FROM feedback WHERE feedback_id = ?",
        (feedback_id,),
    ).fetchone()
    return {
        "feedback": connection.execute(
            "SELECT status, applied_at FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone(),
        "versions": connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (target_version_id, proposed_version_id),
        ).fetchall(),
        "assertions": connection.execute(
            """SELECT assertion_id, document_version_id, state
               FROM knowledge_assertion
               WHERE document_version_id IN (?, ?)
               ORDER BY assertion_id""",
            (target_version_id, proposed_version_id),
        ).fetchall(),
        "counts": {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()[0]
            for table in (
                "review",
                "assertion_supersession",
                "assertion_lineage",
                "knowledge_update",
            )
        },
    }


def _feedback_audit(
    connection: sqlite3.Connection, feedback_id: str
) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (event_type, occurred_at, json.loads(payload_json))
        for event_type, occurred_at, payload_json in connection.execute(
            """SELECT event_type, occurred_at, payload_json
               FROM audit_event WHERE feedback_id = ? ORDER BY event_id""",
            (feedback_id,),
        ).fetchall()
    ]


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_current_submission_is_pending_and_non_authoritative(
    initialized_settings: Settings,
) -> None:
    """CURRENT BEHAVIOR: submission records intent but applies no governance writes.

    M7 EXPECTATION: a proposal will own explicit predecessor/successor items.
    """

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        row = connection.execute(
            """SELECT interaction_id, actor, actor_role, message,
                      target_version_id, proposed_version_id, status,
                      submitted_at, applied_at
               FROM feedback WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        snapshot = _state_snapshot(connection, feedback["feedback_id"])
        audit = _feedback_audit(connection, feedback["feedback_id"])

    assert feedback == {
        "feedback_id": feedback["feedback_id"],
        "interaction_id": interaction["interaction_id"],
        "status": "PENDING",
        "target_version_id": V1,
        "proposed_version_id": V2,
        "message": MESSAGE,
    }
    assert row == (
        interaction["interaction_id"],
        ACTOR,
        "CARE_COORDINATOR",
        MESSAGE,
        V1,
        V2,
        "PENDING",
        demo.SUBMITTED_TIME,
        None,
    )
    assert snapshot == {
        "feedback": ("PENDING", None),
        "versions": [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")],
        "assertions": [
            (V1_PA, V1, "APPLIED"),
            (V1_STEP, V1, "APPLIED"),
            (V2_PA, V2, "CANDIDATE"),
            (V2_STEP, V2, "CANDIDATE"),
        ],
        "counts": {
            "review": 0,
            "assertion_supersession": 0,
            "assertion_lineage": 0,
            "knowledge_update": 0,
        },
    }
    assert audit == [
        (
            "FEEDBACK_SUBMITTED",
            demo.SUBMITTED_TIME,
            {"message": MESSAGE, "status": "SUBMITTED"},
        ),
        (
            "FEEDBACK_PENDING",
            demo.SUBMITTED_TIME,
            {"proposed_version_id": V2, "status": "PENDING"},
        ),
    ]


def test_current_approval_applies_exact_fixed_v1_to_v2_transaction(
    initialized_settings: Settings,
) -> None:
    """CURRENT BEHAVIOR: the golden V1-to-V2 replacement is one transaction."""

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    result = _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        snapshot = _state_snapshot(connection, feedback["feedback_id"])
        review = connection.execute(
            """SELECT feedback_id, reviewer, reviewer_role, decision, rationale,
                      reviewed_at
               FROM review WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        supersession = connection.execute(
            """SELECT prior_version_id, successor_version_id, feedback_id, created_at
               FROM assertion_supersession WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchall()
        lineage = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id,
                      feedback_id, created_at
               FROM assertion_lineage WHERE feedback_id = ?
               ORDER BY predecessor_assertion_id""",
            (feedback["feedback_id"],),
        ).fetchall()
        update = connection.execute(
            """SELECT feedback_id, prior_version_id, current_version_id, applied_at
               FROM knowledge_update WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchall()
        audit = _feedback_audit(connection, feedback["feedback_id"])

    assert result == {
        "feedback_id": feedback["feedback_id"],
        "status": "APPLIED",
        "review_decision": "APPROVED",
        "current_policy_version_id": V2,
        "supersedes": V1,
    }
    assert snapshot["feedback"] == ("APPLIED", demo.APPROVED_TIME)
    # governance_state on V1 remains APPLIED; currentness and assertion state move.
    assert snapshot["versions"] == [(V1, 0, "APPLIED"), (V2, 1, "APPLIED")]
    assert snapshot["assertions"] == [
        (V1_PA, V1, "SUPERSEDED"),
        (V1_STEP, V1, "SUPERSEDED"),
        (V2_PA, V2, "APPLIED"),
        (V2_STEP, V2, "APPLIED"),
    ]
    assert snapshot["counts"] == {
        "review": 1,
        "assertion_supersession": 1,
        "assertion_lineage": 2,
        "knowledge_update": 1,
    }
    assert review == (
        feedback["feedback_id"],
        REVIEWER,
        "KNOWLEDGE_REVIEWER",
        "APPROVED",
        RATIONALE,
        demo.APPROVED_TIME,
    )
    assert supersession == [(V1, V2, feedback["feedback_id"], demo.APPROVED_TIME)]
    assert lineage == [
        (V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME),
        (V1_STEP, V2_STEP, feedback["feedback_id"], demo.APPROVED_TIME),
    ]
    assert update == [(feedback["feedback_id"], V1, V2, demo.APPROVED_TIME)]
    assert [event[0] for event in audit] == [
        "FEEDBACK_SUBMITTED",
        "FEEDBACK_PENDING",
        "REVIEW_APPROVED",
        "KNOWLEDGE_UPDATE_APPLIED",
    ]
    assert audit[0][2] == {"message": MESSAGE, "status": "SUBMITTED"}
    assert audit[1][2] == {"proposed_version_id": V2, "status": "PENDING"}
    approval = audit[2][2]
    assert approval["decision"] == "APPLIED"
    assert approval["actor"] == REVIEWER
    assert approval["actor_role"] == "KNOWLEDGE_REVIEWER"
    assert approval["rationale"] == RATIONALE
    assert approval["target_document_version_id"] == V1
    assert approval["proposed_document_version_id"] == V2
    assert len(approval["lineage_ids"]) == 2
    assert approval["before"] != approval["after"]


def test_current_approval_delegates_persisted_replacement_items(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7.4: approval consumes the persisted proposal replacement set."""

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    transitions: list[tuple[str, str]] = []
    edges: list[tuple[str, str]] = []
    original_transition = demo.KnowledgeService.apply_assertion_state_transition
    original_lineage = demo.KnowledgeService.create_assertion_lineage

    def capture_transition(service: Any, assertion_id: str, new_state: Any) -> None:
        transitions.append((assertion_id, new_state.value))
        original_transition(service, assertion_id, new_state)

    def capture_lineage(
        service: Any,
        predecessor_assertion_id: str,
        successor_assertion_id: str,
        feedback_id: str,
        created_at: str,
    ) -> None:
        edges.append((predecessor_assertion_id, successor_assertion_id))
        original_lineage(
            service,
            predecessor_assertion_id,
            successor_assertion_id,
            feedback_id,
            created_at,
        )

    monkeypatch.setattr(
        demo.KnowledgeService, "apply_assertion_state_transition", capture_transition
    )
    monkeypatch.setattr(
        demo.KnowledgeService, "create_assertion_lineage", capture_lineage
    )
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        feedback_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(feedback)")
        ]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        proposal_count = connection.execute(
            "SELECT COUNT(*) FROM correction_proposal"
        ).fetchone()[0]
        replacement_count = connection.execute(
            "SELECT COUNT(*) FROM correction_proposal_replacement_item"
        ).fetchone()[0]

    assert transitions == [
        (V1_PA, "SUPERSEDED"),
        (V1_STEP, "SUPERSEDED"),
        (V2_PA, "APPLIED"),
        (V2_STEP, "APPLIED"),
    ]
    assert edges == [(V1_PA, V2_PA), (V1_STEP, V2_STEP)]
    assert "target_version_id" in feedback_columns
    assert "proposed_version_id" in feedback_columns
    assert not any("assertion" in column for column in feedback_columns)
    assert "correction_proposal" in tables
    assert "correction_proposal_replacement_item" in tables
    assert proposal_count == 1
    assert replacement_count == 2


def test_rejected_is_schema_vocabulary_without_a_runtime_workflow(
    initialized_settings: Settings, client: TestClient
) -> None:
    """CURRENT BEHAVIOR: REJECTED is storable, but no transition owns its effects.

    M7 EXPECTATION: PENDING -> REJECTED will atomically persist review and audit only.
    """

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    paths = client.app.openapi()["paths"]
    assert not any("reject" in path for path in paths)
    assert not hasattr(demo, "reject_feedback")

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert "'REJECTED'" in _table_sql(connection, "feedback")
        before_audit = _feedback_audit(connection, feedback["feedback_id"])
        connection.execute(
            "UPDATE feedback SET status = 'REJECTED' WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        )

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM feedback WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("REJECTED",)
        assert connection.execute(
            "SELECT COUNT(*) FROM review WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (0,)
        assert _feedback_audit(connection, feedback["feedback_id"]) == before_audit


def test_duplicate_approval_fails_without_duplicate_mutation(
    initialized_settings: Settings, client: TestClient
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with pytest.raises(ValueError, match="^Only pending feedback can be approved$"):
        _approve(initialized_settings, feedback["feedback_id"])
    response = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Only pending feedback can be approved"}
    with database.managed_connection(initialized_settings.database_path) as connection:
        snapshot = _state_snapshot(connection, feedback["feedback_id"])
        approval_events = [
            event
            for event in _feedback_audit(connection, feedback["feedback_id"])
            if event[0] in {"REVIEW_APPROVED", "KNOWLEDGE_UPDATE_APPLIED"}
        ]
    assert snapshot["counts"] == {
        "review": 1,
        "assertion_supersession": 1,
        "assertion_lineage": 2,
        "knowledge_update": 1,
    }
    assert [event[0] for event in approval_events] == [
        "REVIEW_APPROVED",
        "KNOWLEDGE_UPDATE_APPLIED",
    ]


def test_competing_pending_feedback_later_approval_is_stale_target(
    initialized_settings: Settings, client: TestClient
) -> None:
    """M7.4: proposals coexist and the later approval reports STALE_TARGET."""

    interaction = _ask(initialized_settings)
    first = _submit(initialized_settings, interaction["interaction_id"])
    second = _submit(initialized_settings, interaction["interaction_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        pending = connection.execute(
            """SELECT feedback_id, target_version_id, proposed_version_id, status
               FROM feedback ORDER BY feedback_id"""
        ).fetchall()
        authority_before = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()

    assert {row for row in pending} == {
        (first["feedback_id"], V1, V2, "PENDING"),
        (second["feedback_id"], V1, V2, "PENDING"),
    }
    assert authority_before == [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")]

    _approve(initialized_settings, first["feedback_id"])
    with pytest.raises(ValueError, match="^STALE_TARGET$"):
        _approve(initialized_settings, second["feedback_id"])
    response = client.post(
        f"/api/v1/feedback/{second['feedback_id']}/approve", json={}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "STALE_TARGET"}

    with database.managed_connection(initialized_settings.database_path) as connection:
        second_snapshot = _state_snapshot(connection, second["feedback_id"])
    assert second_snapshot["feedback"] == ("PENDING", None)
    assert second_snapshot["counts"] == {
        "review": 0,
        "assertion_supersession": 0,
        "assertion_lineage": 0,
        "knowledge_update": 0,
    }


def test_future_effective_candidate_remains_pending_and_current_does_not_change(
    initialized_settings: Settings,
) -> None:
    """M7.4: early approval leaves the proposal pending and V1 current."""

    future_effective = "2026-08-01T00:00:00Z"
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE source_document_version SET effective_from = ? WHERE document_version_id = ?",
            (future_effective, V2),
        )
        connection.execute(
            "UPDATE knowledge_assertion SET effective_from = ? WHERE document_version_id = ?",
            (future_effective, V2),
        )

    assert future_effective > demo.APPROVED_TIME
    with pytest.raises(ValueError, match="^FUTURE_EFFECTIVE$"):
        _approve(initialized_settings, feedback["feedback_id"])
    service = KnowledgeService(KnowledgeRepository(initialized_settings.database_path))
    current = service.get_current_applied_assertions(PAYER_QUERY)
    june = service.get_applicable_assertions(
        replace(PAYER_QUERY, as_of="2026-06-15T14:00:00Z")
    )
    july = service.get_applicable_assertions(
        replace(PAYER_QUERY, as_of="2026-07-03T00:00:00Z")
    )
    august = service.get_applicable_assertions(
        replace(PAYER_QUERY, as_of="2026-08-02T00:00:00Z")
    )

    current_payer = [item for item in current if item.document_version_id in {V1, V2}]
    june_payer = [item for item in june if item.document_version_id in {V1, V2}]
    july_payer = [item for item in july if item.document_version_id in {V1, V2}]
    august_payer = [item for item in august if item.document_version_id in {V1, V2}]
    assert {item.document_version_id for item in current_payer} == {V1}
    assert {item.state for item in current_payer} == {KnowledgeAssertionState.APPLIED}
    assert {item.document_version_id for item in june_payer} == {V1}
    assert {item.state for item in june_payer} == {KnowledgeAssertionState.APPLIED}
    assert july_payer == []
    assert august_payer == []
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("PENDING",)


def test_lineage_contract_enforces_current_bounded_validations(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])

    with pytest.raises(KnowledgeDataError, match="requires APPLIED feedback"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with database.managed_connection(initialized_settings.database_path) as connection:
        _mark_feedback_applied(connection, feedback["feedback_id"])

    with pytest.raises(KnowledgeDataError, match="Unknown predecessor assertion"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                "AST-SYN-UNKNOWN", V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with pytest.raises(
        KnowledgeDataError,
        match="successor does not belong to the feedback proposed version",
    ):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V1_STEP, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with pytest.raises(KnowledgeDataError, match="incompatible predicate"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V2_STEP, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with pytest.raises(KnowledgeDataError, match="incompatible decision_dimension"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE knowledge_assertion SET decision_dimension = 'OTHER' WHERE assertion_id = ?",
                (V2_PA,),
            )
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with pytest.raises(KnowledgeDataError, match="incompatible normalized_scope"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE knowledge_assertion SET normalized_scope_json = ? WHERE assertion_id = ?",
                ('{"payer_id":"DIFFERENT"}', V2_PA),
            )
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
            )

    with pytest.raises(
        KnowledgeDataError, match="versions belong to different documents"
    ):
        with database.managed_connection(initialized_settings.database_path) as connection:
            other = connection.execute(
                """SELECT ka.assertion_id, ka.document_version_id
                   FROM knowledge_assertion AS ka
                   JOIN source_document_version AS dv
                     ON dv.document_version_id = ka.document_version_id
                   WHERE dv.document_id <> ? ORDER BY ka.assertion_id LIMIT 1""",
                (PAYER_DOCUMENT,),
            ).fetchone()
            assert other is not None
            connection.execute(
                "UPDATE feedback SET proposed_version_id = ? WHERE feedback_id = ?",
                (other[1], feedback["feedback_id"]),
            )
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, other[0], feedback["feedback_id"], demo.APPROVED_TIME
            )


def test_lineage_db_rejects_self_and_duplicate_and_scope_is_structural(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    with database.managed_connection(initialized_settings.database_path) as connection:
        _mark_feedback_applied(connection, feedback["feedback_id"])

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
                (
                    "LIN-SYN-SELF-M71",
                    V1_PA,
                    V1_PA,
                    feedback["feedback_id"],
                    demo.APPROVED_TIME,
                ),
            )

    with database.managed_connection(initialized_settings.database_path) as connection:
        raw_scope = connection.execute(
            "SELECT normalized_scope_json FROM knowledge_assertion WHERE assertion_id = ?",
            (V2_PA,),
        ).fetchone()[0]
        scope = json.loads(raw_scope)
        reversed_scope = {key: scope[key] for key in reversed(tuple(scope))}
        connection.execute(
            "UPDATE knowledge_assertion SET normalized_scope_json = ? WHERE assertion_id = ?",
            (json.dumps(reversed_scope), V2_PA),
        )
        repository = KnowledgeRepository(
            initialized_settings.database_path, connection=connection
        )
        repository.create_assertion_lineage(
            V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path, connection=connection
            ).create_assertion_lineage(
                V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
            )


def test_lineage_does_not_prevent_cycle_branch_merge_or_noncurrent_predecessor(
    initialized_settings: Settings,
) -> None:
    """CURRENT BEHAVIOR: bounded edge checks are not generalized graph policy.

    M7 EXPECTATION: prevent cycles, applied branches/merges, and stale heads.
    """

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    with database.managed_connection(initialized_settings.database_path) as connection:
        _mark_feedback_applied(connection, feedback["feedback_id"])
        connection.execute(
            """INSERT INTO knowledge_assertion
               SELECT 'AST-SYN-POL-V2-PA-BRANCH', document_version_id, subject_id,
                      predicate, object_id, value_json, decision_dimension,
                      normalized_scope_json, recorded_at, effective_from,
                      effective_to, 'APPLIED'
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (V2_PA,),
        )
        connection.execute(
            """INSERT INTO knowledge_assertion
               SELECT 'AST-SYN-POL-V1-PA-MERGE', document_version_id, subject_id,
                      predicate, object_id, value_json, decision_dimension,
                      normalized_scope_json, recorded_at, effective_from,
                      effective_to, 'APPLIED'
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (V1_PA,),
        )
        connection.execute(
            "UPDATE knowledge_assertion SET state = 'APPLIED' WHERE assertion_id = ?",
            (V2_PA,),
        )
        repository = KnowledgeRepository(
            initialized_settings.database_path, connection=connection
        )
        repository.create_assertion_lineage(
            V1_PA, V2_PA, feedback["feedback_id"], demo.APPROVED_TIME
        )
        repository.create_assertion_lineage(
            V1_PA,
            "AST-SYN-POL-V2-PA-BRANCH",
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        )
        repository.create_assertion_lineage(
            "AST-SYN-POL-V1-PA-MERGE",
            V2_PA,
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        )
        connection.execute(
            """UPDATE feedback SET target_version_id = ?, proposed_version_id = ?
               WHERE feedback_id = ?""",
            (V2, V1, feedback["feedback_id"]),
        )
        # V2 is noncurrent here, but the reverse edge is accepted and closes a cycle.
        repository.create_assertion_lineage(
            V2_PA, V1_PA, feedback["feedback_id"], demo.APPROVED_TIME
        )
        edges = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id
               FROM assertion_lineage WHERE feedback_id = ?
               ORDER BY predecessor_assertion_id, successor_assertion_id""",
            (feedback["feedback_id"],),
        ).fetchall()

    assert edges == [
        (V1_PA, V2_PA),
        (V1_PA, "AST-SYN-POL-V2-PA-BRANCH"),
        ("AST-SYN-POL-V1-PA-MERGE", V2_PA),
        (V2_PA, V1_PA),
    ]


def test_assertion_state_machine_allows_only_two_forward_transitions(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        service = KnowledgeService(
            KnowledgeRepository(initialized_settings.database_path, connection=connection)
        )
        service.apply_assertion_state_transition(
            V2_PA, KnowledgeAssertionState.APPLIED
        )
        service.apply_assertion_state_transition(
            V2_PA, KnowledgeAssertionState.SUPERSEDED
        )

    assert (
        KnowledgeRepository(initialized_settings.database_path).get_assertion(V2_PA).state
        is KnowledgeAssertionState.SUPERSEDED
    )
    for assertion_id, illegal_state, transition in (
        (V1_PA, KnowledgeAssertionState.CANDIDATE, "APPLIED -> CANDIDATE"),
        (V1_PA, KnowledgeAssertionState.APPLIED, "APPLIED -> APPLIED"),
        (V2_STEP, KnowledgeAssertionState.SUPERSEDED, "CANDIDATE -> SUPERSEDED"),
        (V2_STEP, KnowledgeAssertionState.CANDIDATE, "CANDIDATE -> CANDIDATE"),
    ):
        with pytest.raises(KnowledgeStateTransitionError, match=transition):
            with database.managed_connection(
                initialized_settings.database_path
            ) as connection:
                KnowledgeService(
                    KnowledgeRepository(
                        initialized_settings.database_path, connection=connection
                    )
                ).apply_assertion_state_transition(assertion_id, illegal_state)

    assert not hasattr(KnowledgeAssertionState, "REJECTED")
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE knowledge_assertion SET state = 'REJECTED' WHERE assertion_id = ?",
                (V1_PA,),
            )


def test_document_currentness_is_at_most_one_but_zero_and_any_governance_state_are_allowed(
    initialized_settings: Settings,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE source_document_version SET is_current = 1 WHERE document_version_id = ?",
                (V2,),
            )

    with database.managed_connection(initialized_settings.database_path) as connection:
        table_sql = _table_sql(connection, "source_document_version")
        indexes = connection.execute(
            "PRAGMA index_list(source_document_version)"
        ).fetchall()
        connection.execute(
            "UPDATE source_document_version SET is_current = 0 WHERE document_id = ?",
            (PAYER_DOCUMENT,),
        )
        connection.execute(
            "UPDATE source_document_version SET governance_state = 'UNCONSTRAINED' WHERE document_version_id = ?",
            (V2,),
        )

    with database.managed_connection(initialized_settings.database_path) as connection:
        versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version WHERE document_id = ?
               ORDER BY document_version_id""",
            (PAYER_DOCUMENT,),
        ).fetchall()

    assert "CHECK (governance_state" not in table_sql
    assert any(row[1] == "idx_source_document_version_one_current" for row in indexes)
    assert versions == [(V1, 0, "APPLIED"), (V2, 0, "UNCONSTRAINED")]


def test_review_contract_has_no_decision_uniqueness_authorization_or_separation_checks(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        review_sql = _table_sql(connection, "review")
        connection.execute(
            "INSERT INTO review VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "REV-SYN-M71-SECOND",
                feedback["feedback_id"],
                ACTOR,
                "UNVERIFIED_ROLE",
                "UNRECOGNIZED_DECISION",
                "No database policy prevents this second review.",
                "2026-07-02T14:00:00Z",
            ),
        )
        reviews = connection.execute(
            """SELECT reviewer, reviewer_role, decision, rationale, reviewed_at
               FROM review WHERE feedback_id = ? ORDER BY review_id""",
            (feedback["feedback_id"],),
        ).fetchall()

    assert "CHECK" not in review_sql.upper()
    assert "UNIQUE" not in review_sql.upper()
    assert len(reviews) == 2
    assert any(row[:3] == (ACTOR, "UNVERIFIED_ROLE", "UNRECOGNIZED_DECISION") for row in reviews)


def test_knowledge_update_has_feedback_fk_but_no_version_fks_or_uniqueness(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        foreign_keys = connection.execute("PRAGMA foreign_key_list(knowledge_update)").fetchall()
        connection.execute(
            "INSERT INTO knowledge_update VALUES (?, ?, ?, ?, ?)",
            (
                "UPD-SYN-M71-DUPLICATE",
                feedback["feedback_id"],
                "DV-NOT-REAL-PRIOR",
                "DV-NOT-REAL-CURRENT",
                demo.APPROVED_TIME,
            ),
        )
        updates = connection.execute(
            """SELECT feedback_id, prior_version_id, current_version_id, applied_at
               FROM knowledge_update WHERE feedback_id = ? ORDER BY update_id""",
            (feedback["feedback_id"],),
        ).fetchall()

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO knowledge_update VALUES (?, ?, ?, ?, ?)",
                (
                    "UPD-SYN-M71-UNKNOWN-FEEDBACK",
                    "FDB-SYN-UNKNOWN",
                    V1,
                    V2,
                    demo.APPROVED_TIME,
                ),
            )

    assert {row[3] for row in foreign_keys} == {"feedback_id"}
    assert len(updates) == 2
    assert any(row[1:3] == ("DV-NOT-REAL-PRIOR", "DV-NOT-REAL-CURRENT") for row in updates)


def test_audit_decision_payload_preserves_complete_governance_facts(
    initialized_settings: Settings,
) -> None:
    """M7.4 decision audit snapshots immutable before/after governance facts."""

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        audit = _feedback_audit(connection, feedback["feedback_id"])
        event_types = [event[0] for event in audit]
        approval_payload = audit[2][2]
        application_payload = audit[3][2]

    assert event_types == [
        "FEEDBACK_SUBMITTED",
        "FEEDBACK_PENDING",
        "REVIEW_APPROVED",
        "KNOWLEDGE_UPDATE_APPLIED",
    ]
    assert not any("ASSERTION" in event_type or "LINEAGE" in event_type for event_type in event_types)
    required_decision_facts = {
        "actor",
        "actor_role",
        "rationale",
        "target_document_version_id",
        "proposed_document_version_id",
        "replacement_items",
        "lineage_ids",
        "before",
        "after",
        "review_id",
        "update_id",
        "timestamp",
    }
    assert required_decision_facts <= approval_payload.keys()
    assert required_decision_facts <= application_payload.keys()
    assert approval_payload["before"] != approval_payload["after"]
    assert len(approval_payload["lineage_ids"]) == 2


def test_failure_after_governance_mutation_begins_rolls_back_every_effect(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    original_lineage = demo.KnowledgeService.create_assertion_lineage
    calls = 0

    def fail_on_second_lineage(
        service: Any,
        predecessor_assertion_id: str,
        successor_assertion_id: str,
        feedback_id: str,
        created_at: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("controlled failure after first lineage")
        original_lineage(
            service,
            predecessor_assertion_id,
            successor_assertion_id,
            feedback_id,
            created_at,
        )

    monkeypatch.setattr(
        demo.KnowledgeService, "create_assertion_lineage", fail_on_second_lineage
    )
    with pytest.raises(RuntimeError, match="controlled failure after first lineage"):
        _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        snapshot = _state_snapshot(connection, feedback["feedback_id"])
        audit = _feedback_audit(connection, feedback["feedback_id"])

    assert calls == 2
    assert snapshot == {
        "feedback": ("PENDING", None),
        "versions": [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")],
        "assertions": [
            (V1_PA, V1, "APPLIED"),
            (V1_STEP, V1, "APPLIED"),
            (V2_PA, V2, "CANDIDATE"),
            (V2_STEP, V2, "CANDIDATE"),
        ],
        "counts": {
            "review": 0,
            "assertion_supersession": 0,
            "assertion_lineage": 0,
            "knowledge_update": 0,
        },
    }
    assert [event[0] for event in audit] == ["FEEDBACK_SUBMITTED", "FEEDBACK_PENDING"]


def test_pending_and_approval_preserve_reasoning_authority_and_old_snapshots(
    initialized_settings: Settings,
) -> None:
    original = _ask(initialized_settings)
    interaction_id = original["interaction_id"]
    with database.managed_connection(initialized_settings.database_path) as connection:
        snapshot_before = connection.execute(
            """SELECT assertion_id, ordinal, origin, state_at_execution, value_json,
                      document_version_id, lineage_ids_json, correction_ids_json
               FROM interaction_knowledge WHERE interaction_id = ? ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()

    feedback = _submit(initialized_settings, interaction_id)
    service = KnowledgeService(KnowledgeRepository(initialized_settings.database_path))
    pending_current = service.get_current_applied_assertions(PAYER_QUERY)
    assert {
        item.document_version_id
        for item in pending_current
        if item.document_version_id in {V1, V2}
    } == {V1}

    _approve(initialized_settings, feedback["feedback_id"])
    approved_current = service.get_current_applied_assertions(PAYER_QUERY)
    assert {
        item.document_version_id
        for item in approved_current
        if item.document_version_id in {V1, V2}
    } == {V2}
    historical = demo.get_interaction(initialized_settings, interaction_id)
    with database.managed_connection(initialized_settings.database_path) as connection:
        snapshot_after = connection.execute(
            """SELECT assertion_id, ordinal, origin, state_at_execution, value_json,
                      document_version_id, lineage_ids_json, correction_ids_json
               FROM interaction_knowledge WHERE interaction_id = ? ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()

    assert snapshot_before
    assert snapshot_after == snapshot_before
    assert historical is not None
    assert historical["answer"] == original["answer"]
    assert historical["policy_version_id"] == original["policy_version_id"] == V1
    assert historical["claims"] == original["claims"]
    assert historical["citations"] == original["citations"]

