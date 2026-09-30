from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
PAIRS = ((V1_PA, V2_PA), (V1_STEP, V2_STEP))


def _submit(client: TestClient) -> dict[str, object]:
    interaction = client.post(
        "/api/v1/questions", json={"question": demo.CANONICAL_QUESTION}
    )
    assert interaction.status_code == 200
    response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": interaction.json()["interaction_id"],
            "actor": "Synthetic Audit Submitter",
            "actor_role": "CLINICIAN",
            "message": "Synthetic audit rationale.",
            "target_version_id": V1,
            "proposed_version_id": V2,
            "replacement_items": [
                {
                    "predecessor_assertion_id": predecessor,
                    "successor_assertion_id": successor,
                }
                for predecessor, successor in PAIRS
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _audit_payload(
    settings: Settings, feedback_id: str, event_type: str
) -> tuple[str, str | None, str | None, dict[str, object]]:
    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute(
            """SELECT occurred_at, interaction_id, feedback_id, payload_json
               FROM audit_event WHERE feedback_id = ? AND event_type = ?""",
            (feedback_id, event_type),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], json.loads(row[3])


def test_submission_audit_is_self_contained_and_immutable(
    client: TestClient, settings: Settings
) -> None:
    feedback = _submit(client)
    feedback_id = str(feedback["feedback_id"])
    occurred_at, interaction_id, audit_feedback_id, submitted = _audit_payload(
        settings, feedback_id, "FEEDBACK_SUBMITTED"
    )

    assert interaction_id == feedback["interaction_id"]
    assert audit_feedback_id == feedback_id
    assert submitted["action"] == "SUBMIT"
    assert str(submitted["proposal_id"]).startswith("PROP-")
    assert submitted["feedback_id"] == feedback_id
    assert submitted["actor"] == "Synthetic Audit Submitter"
    assert submitted["actor_role"] == "CLINICIAN"
    assert submitted["rationale"] == "Synthetic audit rationale."
    assert submitted["message"] == "Synthetic audit rationale."
    assert submitted["status"] == "PENDING"
    assert submitted["target_document_version_id"] == V1
    assert submitted["proposed_document_version_id"] == V2
    assert submitted["timestamp"] == occurred_at == demo.SUBMITTED_TIME

    items = submitted["replacement_items"]
    assert isinstance(items, list)
    assert [
        (item["predecessor_assertion_id"], item["successor_assertion_id"])
        for item in items
    ] == list(PAIRS)
    immutable_keys = {
        "predecessor_assertion_id",
        "successor_assertion_id",
        "predicate",
        "decision_dimension",
        "normalized_scope",
        "predecessor_value",
        "successor_value",
        "predecessor_effective_from",
        "predecessor_effective_to",
        "successor_effective_from",
        "successor_effective_to",
        "predecessor_document_version_id",
        "successor_document_version_id",
        "predecessor_evidence_ids",
        "successor_evidence_ids",
        "predecessor_state",
        "successor_state",
    }
    assert all(set(item) == immutable_keys for item in items)
    assert all(item["predecessor_state"] == "APPLIED" for item in items)
    assert all(item["successor_state"] == "CANDIDATE" for item in items)
    assert all(item["predecessor_evidence_ids"] for item in items)
    assert all(item["successor_evidence_ids"] for item in items)
    assert "relevant_excerpt" not in json.dumps(submitted)

    with database.managed_connection(settings.database_path) as connection:
        audit_identity_before = connection.execute(
            """SELECT event_id, occurred_at, interaction_id, feedback_id
               FROM audit_event
               WHERE feedback_id = ? AND event_type = 'FEEDBACK_SUBMITTED'""",
            (feedback_id,),
        ).fetchone()
        connection.execute(
            """UPDATE correction_proposal
               SET status = 'REJECTED', decided_at = ?,
                   decision_actor = 'Synthetic Mutated Reviewer',
                   decision_role = 'KNOWLEDGE_REVIEWER',
                   decision_rationale = 'Synthetic post-submission mutation.'
               WHERE feedback_id = ?""",
            (demo.APPROVED_TIME, feedback_id),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'SUPERSEDED', value_json = '{"mutated":true}',
                   normalized_scope_json = '{"mutated":true}',
                   effective_from = '2040-01-01T00:00:00Z', effective_to = NULL
               WHERE assertion_id IN (?, ?)""",
            (V1_PA, V1_STEP),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'APPLIED', value_json = '{"mutated":true}',
                   normalized_scope_json = '{"mutated":true}',
                   effective_from = '2040-01-01T00:00:00Z', effective_to = NULL
               WHERE assertion_id IN (?, ?)""",
            (V2_PA, V2_STEP),
        )
        connection.execute(
            """INSERT INTO assertion_evidence(assertion_id, evidence_id)
               VALUES (?, 'EV-SYN-POL-V1-STEP-001')""",
            (V1_PA,),
        )

    assert _audit_payload(settings, feedback_id, "FEEDBACK_SUBMITTED")[3] == submitted
    with database.managed_connection(settings.database_path) as connection:
        audit_identity_after = connection.execute(
            """SELECT event_id, occurred_at, interaction_id, feedback_id
               FROM audit_event
               WHERE feedback_id = ? AND event_type = 'FEEDBACK_SUBMITTED'""",
            (feedback_id,),
        ).fetchone()
    assert audit_identity_before is not None
    assert audit_identity_before[0] > 0
    assert audit_identity_after == audit_identity_before


def test_approval_and_rejection_audit_payload_shapes_remain_unchanged(
    client: TestClient, settings: Settings
) -> None:
    approved_feedback = _submit(client)
    approved_id = str(approved_feedback["feedback_id"])
    approved = client.post(
        f"/api/v1/feedback/{approved_id}/approve",
        json={"reviewer": "Synthetic Audit Reviewer", "rationale": "Approved."},
    )
    assert approved.status_code == 200
    approved_payload = _audit_payload(
        settings, approved_id, "REVIEW_APPROVED"
    )[3]

    expected_decision_keys = {
        "action",
        "proposal_id",
        "feedback_id",
        "actor",
        "actor_role",
        "decision",
        "rationale",
        "target_document_version_id",
        "proposed_document_version_id",
        "replacement_items",
        "before",
        "after",
        "lineage_ids",
        "review_id",
        "supersession_id",
        "update_id",
        "failure_reason",
        "timestamp",
    }
    assert set(approved_payload) == expected_decision_keys
    assert approved_payload["action"] == "APPLY"
    assert approved_payload["decision"] == "APPLIED"
    assert approved_payload["replacement_items"] == [
        {
            "predecessor_assertion_id": predecessor,
            "successor_assertion_id": successor,
        }
        for predecessor, successor in PAIRS
    ]

    database.reset_demo_database(settings)
    rejected_feedback = _submit(client)
    rejected_id = str(rejected_feedback["feedback_id"])
    rejected = client.post(
        f"/api/v1/feedback/{rejected_id}/reject",
        json={"reviewer": "Synthetic Audit Reviewer", "rationale": "Rejected."},
    )
    assert rejected.status_code == 200
    rejected_payload = _audit_payload(
        settings, rejected_id, "REVIEW_REJECTED"
    )[3]

    assert set(rejected_payload) == expected_decision_keys
    assert rejected_payload["action"] == "REJECT"
    assert rejected_payload["decision"] == "REJECTED"
    assert rejected_payload["replacement_items"] == approved_payload[
        "replacement_items"
    ]


def test_stale_and_future_effective_audit_payload_shapes_remain_unchanged(
    client: TestClient, settings: Settings
) -> None:
    first = _submit(client)
    second = _submit(client)
    assert client.post(
        f"/api/v1/feedback/{first['feedback_id']}/approve", json={}
    ).status_code == 200
    stale = client.post(
        f"/api/v1/feedback/{second['feedback_id']}/approve", json={}
    )
    assert stale.status_code == 409
    stale_payload = _audit_payload(
        settings, str(second["feedback_id"]), "GOVERNANCE_APPROVAL_BLOCKED"
    )[3]
    assert stale_payload["action"] == "APPLY_ATTEMPT"
    assert stale_payload["failure_reason"] == "STALE_TARGET"
    assert stale_payload["before"] == stale_payload["after"]

    database.reset_demo_database(settings)
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET effective_from = '2030-01-01T00:00:00Z'
               WHERE document_version_id = ?""",
            (V2,),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET effective_from = '2030-01-01T00:00:00Z'
               WHERE assertion_id IN (?, ?)""",
            (V2_PA, V2_STEP),
        )
    future_feedback = _submit(client)
    future = client.post(
        f"/api/v1/feedback/{future_feedback['feedback_id']}/approve", json={}
    )
    assert future.status_code == 409
    future_payload = _audit_payload(
        settings,
        str(future_feedback["feedback_id"]),
        "GOVERNANCE_APPROVAL_BLOCKED",
    )[3]
    assert future_payload["action"] == "APPLY_ATTEMPT"
    assert future_payload["failure_reason"] == "FUTURE_EFFECTIVE"
    assert future_payload["before"] == future_payload["after"]
