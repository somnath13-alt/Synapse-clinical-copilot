from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.retrieval import PayerPolicyAdapter, RetrievalRequest


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
PAYER_DOCUMENT = "DOC-SYN-POL-VEL"
FEEDBACK_MESSAGE = "Payer policy V1 is outdated; use V2."
REVIEWER = "Synthetic Knowledge Reviewer"
RATIONALE = "Verified the pre-seeded V2 provenance and effective date."

EXPECTED_PAYER_ASSERTIONS = {
    "AST-SYN-POL-V1-PA": (
        V1,
        "REQUIRES_AUTHORIZATION",
        "SYN-MED-VEL",
        "true",
        "AUTHORIZATION_REQUIREMENT",
        '["EV-SYN-POL-V1-PA-001"]',
        "APPLIED",
    ),
    "AST-SYN-POL-V1-STEP": (
        V1,
        "REQUIRES_PREREQUISITE",
        None,
        '{"combination_rule":"ALL","minimum_days_each":30,'
        '"minimum_distinct_failures":2,'
        '"therapy_ids":["SYN-MED-NOR","SYN-MED-BRV"]}',
        "PREREQUISITE_REQUIREMENT",
        '["EV-SYN-POL-V1-STEP-001"]',
        "APPLIED",
    ),
    "AST-SYN-POL-V2-PA": (
        V2,
        "REQUIRES_AUTHORIZATION",
        "SYN-MED-VEL",
        "true",
        "AUTHORIZATION_REQUIREMENT",
        '["EV-SYN-POL-V2-PA-001"]',
        "CANDIDATE",
    ),
    "AST-SYN-POL-V2-STEP": (
        V2,
        "REQUIRES_PREREQUISITE",
        None,
        '{"combination_rule":"ANY","minimum_days_each":30,'
        '"minimum_distinct_failures":1,'
        '"therapy_ids":["SYN-MED-NOR","SYN-MED-BRV"]}',
        "PREREQUISITE_REQUIREMENT",
        '["EV-SYN-POL-V2-STEP-001"]',
        "CANDIDATE",
    ),
}


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _ask(settings: Settings) -> dict[str, Any]:
    return demo.ask_question(settings, demo.CANONICAL_QUESTION)


def _submit(settings: Settings, interaction_id: str) -> dict[str, Any]:
    return demo.submit_feedback(
        settings,
        interaction_id,
        "Synthetic Care Coordinator",
        FEEDBACK_MESSAGE,
    )


def _approve(settings: Settings, feedback_id: str) -> dict[str, Any]:
    return demo.approve_feedback(settings, feedback_id, REVIEWER, RATIONALE)


def _payer_request() -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-SYN-KNOWLEDGE-CHARACTERIZATION",
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        as_of=demo.BASELINE_TIME,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        source_mode="BASELINE",
    )


def _snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_version_id": payload["policy_version_id"],
        "claims": payload["claims"],
        "citations": payload["citations"],
        "confidence": payload["confidence"],
        "confidence_rationale": payload["confidence_rationale"],
        "reconciliation": payload["reconciliation"],
        "escalation": payload["escalation"],
    }


def test_seeded_payer_assertion_projection_is_exact_and_omits_future_columns(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(knowledge_assertion)")
        }
        rows = connection.execute(
            """SELECT assertion_id, document_version_id, predicate, object_id,
                      value_json, decision_dimension, evidence_ids_json, state
               FROM knowledge_assertion
               WHERE document_version_id IN (?, ?)
               ORDER BY assertion_id""",
            (V1, V2),
        ).fetchall()

    assert columns == {
        "assertion_id",
        "document_version_id",
        "predicate",
        "object_id",
        "value_json",
        "decision_dimension",
        "evidence_ids_json",
        "state",
    }
    assert "subject_id" not in columns
    assert "normalized_scope_json" not in columns
    assert {row[0]: row[1:] for row in rows} == EXPECTED_PAYER_ASSERTIONS


def test_seeded_assertion_evidence_and_document_provenance_resolve_in_application(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        assertion_rows = connection.execute(
            """SELECT ka.assertion_id, ka.document_version_id, ka.evidence_ids_json,
                      dv.document_id, d.source_id, d.source_type, d.source_title
               FROM knowledge_assertion AS ka
               JOIN source_document_version AS dv
                 ON dv.document_version_id = ka.document_version_id
               JOIN source_document AS d ON d.document_id = dv.document_id
               ORDER BY ka.assertion_id"""
        ).fetchall()
        evidence_rows = connection.execute(
            "SELECT evidence_id, document_version_id FROM evidence_item"
        ).fetchall()

    evidence_versions = dict(evidence_rows)
    assert assertion_rows
    for (
        assertion_id,
        document_version_id,
        evidence_ids_json,
        document_id,
        source_id,
        source_type,
        source_title,
    ) in assertion_rows:
        evidence_ids = json.loads(evidence_ids_json)
        assert evidence_ids
        assert document_id.startswith("DOC-SYN-")
        assert source_id.startswith("SRC-SYN-")
        assert source_type
        assert source_title.startswith("SYNTHETIC —")
        assert all(evidence_id in evidence_versions for evidence_id in evidence_ids), (
            assertion_id
        )
        assert all(
            evidence_versions[evidence_id] == document_version_id
            for evidence_id in evidence_ids
        )

    payer_mappings = {
        row[0]: tuple(json.loads(row[2]))
        for row in assertion_rows
        if row[1] in {V1, V2}
    }
    assert payer_mappings == {
        "AST-SYN-POL-V1-PA": ("EV-SYN-POL-V1-PA-001",),
        "AST-SYN-POL-V1-STEP": ("EV-SYN-POL-V1-STEP-001",),
        "AST-SYN-POL-V2-PA": ("EV-SYN-POL-V2-PA-001",),
        "AST-SYN-POL-V2-STEP": ("EV-SYN-POL-V2-STEP-001",),
    }


def test_reset_baseline_persists_current_and_governance_states(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_id = ? ORDER BY document_version_id""",
            (PAYER_DOCUMENT,),
        ).fetchall()
        assertion_states = connection.execute(
            """SELECT document_version_id, state, COUNT(*)
               FROM knowledge_assertion
               WHERE document_version_id IN (?, ?)
               GROUP BY document_version_id, state
               ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        current_count = connection.execute(
            """SELECT COUNT(*) FROM source_document_version
               WHERE document_id = ? AND is_current = 1""",
            (PAYER_DOCUMENT,),
        ).fetchone()

    assert versions == [
        (V1, 1, "APPLIED"),
        (V2, 0, "CANDIDATE_NOT_CURRENT"),
    ]
    assert assertion_states == [(V1, "APPLIED", 2), (V2, "CANDIDATE", 2)]
    assert current_count == (1,)


def test_pending_feedback_does_not_mutate_knowledge_or_retrieval(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        feedback_row = connection.execute(
            "SELECT status, applied_at FROM feedback WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone()
        versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        assertion_states = connection.execute(
            """SELECT document_version_id, state, COUNT(*)
               FROM knowledge_assertion WHERE document_version_id IN (?, ?)
               GROUP BY document_version_id, state ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        lineage_count = connection.execute(
            "SELECT COUNT(*) FROM assertion_supersession"
        ).fetchone()
        update_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_update"
        ).fetchone()

    payer = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _payer_request()
    )
    assert feedback["status"] == "PENDING"
    assert feedback_row == ("PENDING", None)
    assert versions == [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")]
    assert assertion_states == [(V1, "APPLIED", 2), (V2, "CANDIDATE", 2)]
    assert lineage_count == (0,)
    assert update_count == (0,)
    assert payer.document_version_ids == (V1,)


def test_approval_applies_exact_transition_and_coherent_governance_records(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    applied = _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        assertion_states = connection.execute(
            """SELECT document_version_id, state, COUNT(*)
               FROM knowledge_assertion WHERE document_version_id IN (?, ?)
               GROUP BY document_version_id, state ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        feedback_row = connection.execute(
            """SELECT interaction_id, target_version_id, proposed_version_id,
                      status, applied_at
               FROM feedback WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        review = connection.execute(
            """SELECT review_id, feedback_id, reviewer, reviewer_role, decision,
                      rationale, reviewed_at
               FROM review WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        lineage = connection.execute(
            """SELECT supersession_id, prior_version_id, successor_version_id,
                      feedback_id, created_at
               FROM assertion_supersession WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        update = connection.execute(
            """SELECT update_id, feedback_id, prior_version_id,
                      current_version_id, applied_at
               FROM knowledge_update WHERE feedback_id = ?""",
            (feedback["feedback_id"],),
        ).fetchone()
        audit = connection.execute(
            """SELECT event_type, occurred_at, payload_json, interaction_id, feedback_id
               FROM audit_event WHERE feedback_id = ? ORDER BY event_id""",
            (feedback["feedback_id"],),
        ).fetchall()

    assert applied == {
        "feedback_id": feedback["feedback_id"],
        "status": "APPLIED",
        "review_decision": "APPROVED",
        "current_policy_version_id": V2,
        "supersedes": V1,
    }
    assert versions == [(V1, 0, "APPLIED"), (V2, 1, "APPLIED")]
    assert assertion_states == [(V1, "SUPERSEDED", 2), (V2, "APPLIED", 2)]
    assert feedback_row == (
        interaction["interaction_id"],
        V1,
        V2,
        "APPLIED",
        demo.APPROVED_TIME,
    )
    assert review is not None
    assert review[0].startswith("REV-")
    assert review[1:] == (
        feedback["feedback_id"],
        REVIEWER,
        "KNOWLEDGE_REVIEWER",
        "APPROVED",
        RATIONALE,
        demo.APPROVED_TIME,
    )
    assert lineage is not None
    assert lineage[0].startswith("SUP-")
    assert lineage[1:] == (V1, V2, feedback["feedback_id"], demo.APPROVED_TIME)
    assert update is not None
    assert update[0].startswith("UPD-")
    assert update[1:] == (
        feedback["feedback_id"],
        V1,
        V2,
        demo.APPROVED_TIME,
    )
    assert [row[0] for row in audit] == [
        "FEEDBACK_SUBMITTED",
        "FEEDBACK_PENDING",
        "REVIEW_APPROVED",
        "KNOWLEDGE_UPDATE_APPLIED",
    ]
    assert all(row[3] == interaction["interaction_id"] for row in audit)
    assert all(row[4] == feedback["feedback_id"] for row in audit)
    assert audit[2][1:3] == (
        demo.APPROVED_TIME,
        '{"decision":"APPROVED","reviewer_role":"KNOWLEDGE_REVIEWER"}',
    )
    assert audit[3][1:3] == (
        demo.APPROVED_TIME,
        '{"current_version_id":"DV-SYN-POL-VEL-V2",'
        '"prior_version_id":"DV-SYN-POL-VEL-V1"}',
    )


def test_assertion_supersession_currently_records_document_version_lineage(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        lineage = connection.execute(
            """SELECT prior_version_id, successor_version_id, feedback_id
               FROM assertion_supersession"""
        ).fetchall()
        version_ids = {
            row[0]
            for row in connection.execute(
                "SELECT document_version_id FROM source_document_version"
            ).fetchall()
        }
        assertion_ids = {
            row[0]
            for row in connection.execute(
                "SELECT assertion_id FROM knowledge_assertion"
            ).fetchall()
        }

    assert lineage == [(V1, V2, feedback["feedback_id"])]
    assert {lineage[0][0], lineage[0][1]} <= version_ids
    assert {lineage[0][0], lineage[0][1]}.isdisjoint(assertion_ids)


def test_approval_preserves_historical_interaction_snapshot(
    initialized_settings: Settings,
) -> None:
    original = _ask(initialized_settings)
    original_snapshot = _snapshot(original)
    feedback = _submit(initialized_settings, original["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    historical = demo.get_interaction(
        initialized_settings, original["interaction_id"]
    )

    assert historical is not None
    assert _snapshot(historical) == original_snapshot
    assert historical["policy_version_id"] == V1


def test_approval_failure_rolls_back_every_partial_mutation(
    initialized_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    original_audit = demo._audit

    def fail_after_governance_mutations(
        connection: Any,
        event_type: str,
        occurred_at: str,
        payload: dict[str, Any],
        **references: Any,
    ) -> None:
        if event_type == "REVIEW_APPROVED":
            raise RuntimeError("controlled approval failure")
        original_audit(
            connection,
            event_type,
            occurred_at,
            payload,
            **references,
        )

    monkeypatch.setattr(demo, "_audit", fail_after_governance_mutations)

    with pytest.raises(RuntimeError, match="controlled approval failure"):
        _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        feedback_row = connection.execute(
            "SELECT status, applied_at FROM feedback WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone()
        versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?) ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        assertion_states = connection.execute(
            """SELECT document_version_id, state, COUNT(*)
               FROM knowledge_assertion WHERE document_version_id IN (?, ?)
               GROUP BY document_version_id, state ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()
        record_counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE feedback_id = ?",
                (feedback["feedback_id"],),
            ).fetchone()[0]
            for table in ("review", "assertion_supersession", "knowledge_update")
        }
        audit_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM audit_event WHERE feedback_id = ? ORDER BY event_id",
                (feedback["feedback_id"],),
            ).fetchall()
        ]

    assert feedback_row == ("PENDING", None)
    assert versions == [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")]
    assert assertion_states == [(V1, "APPLIED", 2), (V2, "CANDIDATE", 2)]
    assert record_counts == {
        "review": 0,
        "assertion_supersession": 0,
        "knowledge_update": 0,
    }
    assert audit_types == ["FEEDBACK_SUBMITTED", "FEEDBACK_PENDING"]


def test_duplicate_approval_returns_current_conflict_without_duplicate_records(
    client: TestClient,
    settings: Settings,
) -> None:
    interaction = _ask(settings)
    feedback = _submit(settings, interaction["interaction_id"])
    _approve(settings, feedback["feedback_id"])

    duplicate = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={},
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Only pending feedback can be approved"}
    with database.managed_connection(settings.database_path) as connection:
        current = connection.execute(
            """SELECT document_version_id FROM source_document_version
               WHERE document_id = ? AND is_current = 1""",
            (PAYER_DOCUMENT,),
        ).fetchall()
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE feedback_id = ?",
                (feedback["feedback_id"],),
            ).fetchone()[0]
            for table in ("review", "assertion_supersession", "knowledge_update")
        }
        approval_events = connection.execute(
            """SELECT event_type, COUNT(*) FROM audit_event
               WHERE feedback_id = ?
                 AND event_type IN ('REVIEW_APPROVED', 'KNOWLEDGE_UPDATE_APPLIED')
               GROUP BY event_type ORDER BY event_type""",
            (feedback["feedback_id"],),
        ).fetchall()

    assert current == [(V2,)]
    assert counts == {
        "review": 1,
        "assertion_supersession": 1,
        "knowledge_update": 1,
    }
    assert approval_events == [
        ("KNOWLEDGE_UPDATE_APPLIED", 1),
        ("REVIEW_APPROVED", 1),
    ]


def test_each_baseline_logical_document_has_one_current_version(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        cardinality = dict(
            connection.execute(
                """SELECT d.document_id, SUM(dv.is_current)
                   FROM source_document AS d
                   JOIN source_document_version AS dv
                     ON dv.document_id = d.document_id
                   WHERE EXISTS (
                       SELECT 1 FROM source_document_version AS baseline
                       WHERE baseline.document_id = d.document_id
                         AND baseline.test_only = 0
                   )
                   GROUP BY d.document_id ORDER BY d.document_id"""
            ).fetchall()
        )

    assert cardinality == {
        "DOC-SYN-EHR-CASE-001": 1,
        "DOC-SYN-FORM-HLP": 1,
        "DOC-SYN-GUIDE-LDS": 1,
        "DOC-SYN-NOTE-001": 1,
        PAYER_DOCUMENT: 1,
    }


def test_known_limitation_baseline_as_of_still_retrieves_current_v2(
    initialized_settings: Settings,
) -> None:
    """CHARACTERIZATION: v1.2 retrieval uses is_current, not request.as_of."""

    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    request = _payer_request()
    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(request)
    with database.managed_connection(initialized_settings.database_path) as connection:
        v2_effective_from = connection.execute(
            """SELECT effective_from FROM source_document_version
               WHERE document_version_id = ?""",
            (V2,),
        ).fetchone()

    assert request.as_of == demo.BASELINE_TIME
    assert v2_effective_from == ("2026-07-01T00:00:00Z",)
    assert request.as_of < v2_effective_from[0]
    assert result.document_version_ids == (V2,)
