from __future__ import annotations

import inspect
import json

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.knowledge import KnowledgeAssertionState, KnowledgeService


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"


def _ask(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/questions", json={"question": demo.CANONICAL_QUESTION}
    )
    assert response.status_code == 200
    return response.json()


def _proposal_payload(
    interaction_id: str,
    pairs: tuple[tuple[str, str], ...],
    *,
    target: str = V1,
    proposed: str = V2,
) -> dict[str, object]:
    return {
        "interaction_id": interaction_id,
        "actor": "Synthetic Generalized Submitter",
        "actor_role": "CLINICIAN",
        "message": "Synthetic governed correction with explicit replacements.",
        "target_version_id": target,
        "proposed_version_id": proposed,
        "replacement_items": [
            {
                "predecessor_assertion_id": predecessor,
                "successor_assertion_id": successor,
            }
            for predecessor, successor in pairs
        ],
    }


def _submit(
    client: TestClient,
    interaction_id: str,
    pairs: tuple[tuple[str, str], ...] = ((V1_PA, V2_PA), (V1_STEP, V2_STEP)),
    *,
    target: str = V1,
    proposed: str = V2,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/feedback",
        json=_proposal_payload(
            interaction_id, pairs, target=target, proposed=proposed
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()


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


def _seed_candidate(
    settings: Settings,
    *,
    version_id: str,
    assertion_id: str,
    evidence_id: str,
    effective_from: str,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """INSERT INTO source_document_version
               (document_version_id, document_id, version, timestamp, recorded_at,
                effective_from, effective_to, relevant_excerpt, structured_data,
                checksum, governance_state, is_current, test_only)
               SELECT ?, document_id, ?, timestamp, recorded_at, ?, effective_to,
                      relevant_excerpt, structured_data, ?, 'CANDIDATE_NOT_CURRENT', 0, 0
               FROM source_document_version WHERE document_version_id = ?""",
            (version_id, version_id, effective_from, f"CHK-{version_id}", V2),
        )
        connection.execute(
            """INSERT INTO evidence_item
               (evidence_id, document_version_id, source_id, source_type,
                source_title, version, timestamp, section, relevant_excerpt,
                structured_data)
               SELECT ?, ?, source_id, source_type, source_title, ?, timestamp,
                      section, relevant_excerpt, structured_data
               FROM evidence_item WHERE evidence_id = 'EV-SYN-POL-V2-PA-001'""",
            (evidence_id, version_id, version_id),
        )
        connection.execute(
            """INSERT INTO knowledge_assertion
               (assertion_id, document_version_id, subject_id, predicate, object_id,
                value_json, decision_dimension, normalized_scope_json, recorded_at,
                effective_from, effective_to, state)
               SELECT ?, ?, ?, predicate, object_id, value_json, decision_dimension,
                      normalized_scope_json, recorded_at, ?, effective_to, 'CANDIDATE'
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (assertion_id, version_id, version_id, effective_from, V2_PA),
        )
        connection.execute(
            "INSERT INTO assertion_evidence VALUES (?, ?)",
            (assertion_id, evidence_id),
        )


def test_public_submission_persists_canonical_generalized_mapping_and_keeps_response(
    client: TestClient, settings: Settings
) -> None:
    interaction = _ask(client)
    response = _submit(client, interaction["interaction_id"], ((V1_PA, V2_PA),))

    assert response == {
        "feedback_id": response["feedback_id"],
        "interaction_id": interaction["interaction_id"],
        "status": "PENDING",
        "target_version_id": V1,
        "proposed_version_id": V2,
        "message": "Synthetic governed correction with explicit replacements.",
    }
    with database.managed_connection(settings.database_path) as connection:
        proposal = connection.execute(
            """SELECT proposal_id, feedback_id, submitter_actor, submitter_role,
                      rationale, status, target_document_version_id,
                      proposed_document_version_id
               FROM correction_proposal"""
        ).fetchone()
        items = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id
               FROM correction_proposal_replacement_item ORDER BY ordinal"""
        ).fetchall()
    assert proposal[0].startswith("PROP-")
    assert proposal[0] != f"PROP-{str(response['feedback_id'])[4:]}"
    assert proposal[1:] == (
        response["feedback_id"],
        "Synthetic Generalized Submitter",
        "CLINICIAN",
        "Synthetic governed correction with explicit replacements.",
        "PENDING",
        V1,
        V2,
    )
    assert items == [(V1_PA, V2_PA)]


def test_legacy_submission_is_compatible_and_live_path_has_no_fixed_governance_ids(
    client: TestClient,
) -> None:
    interaction = _ask(client)
    response = client.post(
        "/api/v1/feedback",
        json={"interaction_id": interaction["interaction_id"]},
    )

    assert response.status_code == 200
    assert response.json().keys() == {
        "feedback_id",
        "interaction_id",
        "status",
        "target_version_id",
        "proposed_version_id",
        "message",
    }
    source = inspect.getsource(demo.submit_feedback)
    for fixed_id in (V1, V2, V1_PA, V1_STEP, V2_PA, V2_STEP):
        assert fixed_id not in source


@pytest.mark.parametrize(
    "pairs",
    [
        ((V1_PA, V2_PA),),
        ((V1_PA, V2_PA), (V1_STEP, V2_STEP)),
    ],
    ids=["single", "multi"],
)
def test_public_approval_routes_persisted_single_or_multi_set_through_governance(
    client: TestClient, settings: Settings, pairs: tuple[tuple[str, str], ...]
) -> None:
    interaction = _ask(client)
    feedback = _submit(client, interaction["interaction_id"], pairs)

    response = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    )

    assert response.status_code == 200
    assert response.json() == {
        "feedback_id": feedback["feedback_id"],
        "status": "APPLIED",
        "review_decision": "APPROVED",
        "current_policy_version_id": V2,
        "supersedes": V1,
    }
    with database.managed_connection(settings.database_path) as connection:
        lineage = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id
               FROM assertion_lineage ORDER BY predecessor_assertion_id"""
        ).fetchall()
        proposal_status = connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone()
    assert lineage == sorted(pairs)
    assert proposal_status == ("APPLIED",)


def test_public_rejection_is_terminal_non_authoritative_and_duplicate_is_rejected(
    client: TestClient, settings: Settings
) -> None:
    interaction = _ask(client)
    feedback = _submit(client, interaction["interaction_id"])
    before = _authority(settings)

    rejected = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/reject",
        json={"rationale": "Synthetic rejection with retained provenance."},
    )
    duplicate = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/reject", json={}
    )

    assert rejected.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Only pending feedback can be rejected"}
    assert _authority(settings) == before
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("REJECTED",)
        assert connection.execute(
            "SELECT COUNT(*) FROM review WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT COUNT(*) FROM audit_event
               WHERE feedback_id = ? AND event_type = 'REVIEW_REJECTED'""",
            (feedback["feedback_id"],),
        ).fetchone() == (1,)


def test_competing_pending_proposals_coexist_and_later_approval_is_stale(
    client: TestClient, settings: Settings
) -> None:
    interaction = _ask(client)
    first = _submit(client, interaction["interaction_id"])
    second = _submit(client, interaction["interaction_id"])

    assert client.post(
        f"/api/v1/feedback/{first['feedback_id']}/approve", json={}
    ).status_code == 200
    stale = client.post(
        f"/api/v1/feedback/{second['feedback_id']}/approve", json={}
    )

    assert stale.status_code == 409
    assert stale.json() == {"detail": "STALE_TARGET"}
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (second["feedback_id"],),
        ).fetchone() == ("PENDING",)
        payload = json.loads(
            connection.execute(
                """SELECT payload_json FROM audit_event
                   WHERE feedback_id = ? AND event_type = 'GOVERNANCE_APPROVAL_BLOCKED'""",
                (second["feedback_id"],),
            ).fetchone()[0]
        )
    assert payload["failure_reason"] == "STALE_TARGET"
    assert payload["before"] == payload["after"]


def test_future_effective_public_attempt_stays_pending_and_current_is_unchanged(
    client: TestClient, settings: Settings
) -> None:
    version_id = "DV-SYN-POL-FUTURE"
    assertion_id = "AST-SYN-POL-FUTURE-PA"
    _seed_candidate(
        settings,
        version_id=version_id,
        assertion_id=assertion_id,
        evidence_id="EV-SYN-POL-FUTURE-PA",
        effective_from="2030-01-01T00:00:00Z",
    )
    interaction = _ask(client)
    feedback = _submit(
        client,
        interaction["interaction_id"],
        ((V1_PA, assertion_id),),
        proposed=version_id,
    )
    before = _authority(settings)

    response = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "FUTURE_EFFECTIVE"}
    assert _authority(settings) == before
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("PENDING",)


@pytest.mark.parametrize(
    ("edge", "reason"),
    [((V1_PA, V2_STEP), "APPLIED_BRANCH"), ((V2_PA, V1_PA), "LINEAGE_CYCLE")],
)
def test_live_approval_prevents_branch_and_cycle(
    client: TestClient,
    settings: Settings,
    edge: tuple[str, str],
    reason: str,
) -> None:
    interaction = _ask(client)
    feedback = _submit(client, interaction["interaction_id"], ((V1_PA, V2_PA),))
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
            (
                f"LIN-SYN-{reason}",
                edge[0],
                edge[1],
                feedback["feedback_id"],
                demo.SUBMITTED_TIME,
            ),
        )
    before = _authority(settings)

    response = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": reason}
    assert _authority(settings) == before


def test_multi_replacement_failure_rolls_back_earlier_item_mutations(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _ask(client)
    feedback = _submit(client, interaction["interaction_id"])
    before = _authority(settings)
    original = KnowledgeService.apply_assertion_state_transition
    calls = 0

    def fail_on_second(
        service: KnowledgeService,
        assertion_id: str,
        new_state: KnowledgeAssertionState,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic item-N failure")
        original(service, assertion_id, new_state)

    monkeypatch.setattr(
        KnowledgeService, "apply_assertion_state_transition", fail_on_second
    )
    with pytest.raises(RuntimeError, match="item-N"):
        demo.approve_feedback(
            settings,
            str(feedback["feedback_id"]),
            "Synthetic Reviewer",
            "Synthetic rollback check.",
        )

    assert _authority(settings) == before
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM correction_proposal WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == ("PENDING",)


def test_approval_audit_preserves_both_identities_mapping_states_and_lineage(
    client: TestClient, settings: Settings
) -> None:
    interaction = _ask(client)
    feedback = _submit(client, interaction["interaction_id"])
    assert client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={"reviewer": "Synthetic Auditor", "rationale": "Evidence reviewed."},
    ).status_code == 200

    with database.managed_connection(settings.database_path) as connection:
        payload = json.loads(
            connection.execute(
                """SELECT payload_json FROM audit_event
                   WHERE feedback_id = ? AND event_type = 'REVIEW_APPROVED'""",
                (feedback["feedback_id"],),
            ).fetchone()[0]
        )
    assert payload["proposal_id"].startswith("PROP-")
    assert payload["feedback_id"] == feedback["feedback_id"]
    assert payload["actor"] == "Synthetic Auditor"
    assert payload["actor_role"] == "KNOWLEDGE_REVIEWER"
    assert payload["decision"] == "APPLIED"
    assert payload["rationale"] == "Evidence reviewed."
    assert payload["target_document_version_id"] == V1
    assert payload["proposed_document_version_id"] == V2
    assert len(payload["replacement_items"]) == 2
    assert payload["before"] != payload["after"]
    assert len(payload["lineage_ids"]) == 2
    assert payload["failure_reason"] is None
    assert payload["timestamp"] == demo.APPROVED_TIME


def test_later_governance_does_not_mutate_historical_interaction_snapshots(
    client: TestClient, settings: Settings
) -> None:
    interaction = _ask(client)
    interaction_id = str(interaction["interaction_id"])
    with database.managed_connection(settings.database_path) as connection:
        before_interaction = connection.execute(
            "SELECT * FROM interaction WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        before_knowledge = connection.execute(
            """SELECT * FROM interaction_knowledge WHERE interaction_id = ?
               ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()
    feedback = _submit(client, interaction_id)
    assert client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    ).status_code == 200

    with database.managed_connection(settings.database_path) as connection:
        after_interaction = connection.execute(
            "SELECT * FROM interaction WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        after_knowledge = connection.execute(
            """SELECT * FROM interaction_knowledge WHERE interaction_id = ?
               ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()
    assert after_interaction == before_interaction
    assert after_knowledge == before_knowledge


def test_supported_pa_answer_remains_compatible_after_generalized_approval(
    client: TestClient,
) -> None:
    initial = _ask(client)
    feedback = _submit(client, initial["interaction_id"])
    assert client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    ).status_code == 200

    updated = _ask(client)

    assert updated["policy_version_id"] == V2
    assert "Payer Policy V2 requires prior authorization" in updated["answer"]


def test_unsupported_governed_projection_does_not_fabricate_a_generalized_answer(
    client: TestClient, settings: Settings
) -> None:
    version_id = "DV-SYN-POL-UNSUPPORTED"
    successor_id = "AST-SYN-POL-UNSUPPORTED-V2"
    predecessor_id = "AST-SYN-POL-UNSUPPORTED-V1"
    _seed_candidate(
        settings,
        version_id=version_id,
        assertion_id=successor_id,
        evidence_id="EV-SYN-POL-UNSUPPORTED",
        effective_from="2026-07-01T00:00:00Z",
    )
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """UPDATE knowledge_assertion
               SET predicate = 'UNSUPPORTED_REASONING_PREDICATE',
                   decision_dimension = 'UNSUPPORTED_REASONING_DIMENSION',
                   normalized_scope_json = '{"synthetic":"unsupported"}'
               WHERE assertion_id = ?""",
            (successor_id,),
        )
        connection.execute(
            """INSERT INTO knowledge_assertion
               (assertion_id, document_version_id, subject_id, predicate, object_id,
                value_json, decision_dimension, normalized_scope_json, recorded_at,
                effective_from, effective_to, state)
               SELECT ?, ?, ?, predicate, object_id, value_json, decision_dimension,
                      normalized_scope_json, recorded_at, effective_from, effective_to,
                      'APPLIED'
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (predecessor_id, V1, V1, successor_id),
        )
        connection.execute(
            "INSERT INTO assertion_evidence VALUES (?, 'EV-SYN-POL-V1-PA-001')",
            (predecessor_id,),
        )
    interaction = _ask(client)
    feedback = _submit(
        client,
        interaction["interaction_id"],
        ((predecessor_id, successor_id),),
        proposed=version_id,
    )
    assert client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={}
    ).status_code == 200

    response = client.post(
        "/api/v1/questions", json={"question": demo.CANONICAL_QUESTION}
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "The current payer-policy projection is not supported by the bounded "
            "answer renderer."
        )
    }

