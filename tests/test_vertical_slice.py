from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from backend import database


QUESTION = "Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?"
BASELINE_POLICY_VERSION = "DV-SYN-POL-VEL-V1"
UPDATED_POLICY_VERSION = "DV-SYN-POL-VEL-V2"
MINIMUM_PROVENANCE_FIELDS = {
    "source_id",
    "source_type",
    "source_title",
    "version",
    "timestamp",
    "relevant_excerpt",
}
BASELINE_CLAIM_EVIDENCE = {
    "CLM-A-CASE": {"EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"},
    "CLM-A-PA": {"EV-SYN-POL-V1-PA-001"},
    "CLM-A-V1-CRITERIA": {"EV-SYN-POL-V1-STEP-001"},
    "CLM-A-HISTORY": {
        "EV-SYN-EHR-THERAPY-001",
        "EV-SYN-NOTE-HISTORY-001",
    },
    "CLM-A-GUIDE": {"EV-SYN-GUIDE-SUPPORT-001"},
    "CLM-A-FORM": {"EV-SYN-FORM-STATUS-001"},
    "CLM-A-RECON": {
        "EV-SYN-GUIDE-SCOPE-001",
        "EV-SYN-POL-V1-PA-001",
    },
}
UPDATED_CLAIM_EVIDENCE = {
    "CLM-E-V2-PA": {"EV-SYN-POL-V2-PA-001"},
    "CLM-E-V2-CRITERIA": {"EV-SYN-POL-V2-STEP-001"},
    "CLM-E-READY": {
        "EV-SYN-EHR-THERAPY-001",
        "EV-SYN-POL-V2-STEP-001",
    },
    "CLM-A-GUIDE": {"EV-SYN-GUIDE-SUPPORT-001"},
    "CLM-A-FORM": {"EV-SYN-FORM-STATUS-001"},
}


def ask(client: TestClient, source_mode: str = "BASELINE") -> dict[str, Any]:
    response = client.post(
        "/api/v1/questions",
        json={"question": QUESTION, "source_mode": source_mode},
    )
    assert response.status_code == 200
    return response.json()


def claim_evidence(payload: dict[str, Any]) -> dict[str, set[str]]:
    return {
        claim["claim_id"]: set(claim["evidence_ids"])
        for claim in payload["claims"]
    }


def citation_pairs(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (citation["claim_id"], citation["evidence_id"])
        for citation in payload["citations"]
    }


def approve_v2(client: TestClient, interaction_id: str) -> dict[str, Any]:
    feedback_response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": interaction_id,
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )
    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["status"] == "PENDING"

    approval = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={},
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "APPLIED"
    return feedback


def test_canonical_question_returns_characterized_v1_answer(
    client: TestClient,
) -> None:
    payload = ask(client)

    assert payload["status"] == "ANSWERED"
    assert payload["intent"] == "PRIOR_AUTHORIZATION"
    assert payload["policy_version_id"] == BASELINE_POLICY_VERSION
    assert payload["confidence"] == "HIGH"
    assert payload["escalation"] is None
    assert claim_evidence(payload) == BASELINE_CLAIM_EVIDENCE

    evidence_ids = {citation["evidence_id"] for citation in payload["citations"]}
    assert {
        "EV-SYN-EHR-CONTEXT-001",
        "EV-SYN-POL-V1-PA-001",
        "EV-SYN-POL-V1-STEP-001",
        "EV-SYN-GUIDE-SUPPORT-001",
        "EV-SYN-FORM-STATUS-001",
    } <= evidence_ids
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= evidence_ids
    assert not any(evidence_id.startswith("EV-SYN-POL-V2-") for evidence_id in evidence_ids)


def test_retrieval_trace_preserves_current_order_labels_and_statuses(
    client: TestClient,
) -> None:
    payload = ask(client)

    assert payload["selected_sources"] == [
        "EHR",
        "GUIDELINE",
        "PAYER_POLICY",
        "FORMULARY",
        "SPECIALIST_NOTE",
    ]
    assert payload["orchestration_trace"] == [
        {"sequence": 1, "source_type": "EHR", "label": "EHR", "status": "RETRIEVED"},
        {
            "sequence": 2,
            "source_type": "GUIDELINE",
            "label": "Guideline",
            "status": "RETRIEVED",
        },
        {
            "sequence": 3,
            "source_type": "PAYER_POLICY",
            "label": "Payer",
            "status": "RETRIEVED",
        },
        {
            "sequence": 4,
            "source_type": "FORMULARY",
            "label": "Formulary",
            "status": "RETRIEVED",
        },
        {
            "sequence": 5,
            "source_type": "SPECIALIST_NOTE",
            "label": "Specialist Notes",
            "status": "RETRIEVED",
        },
    ]


def test_material_claims_map_to_existing_citations_with_minimum_provenance(
    client: TestClient,
) -> None:
    payload = ask(client)

    expected_pairs = {
        (claim_id, evidence_id)
        for claim_id, evidence_ids in BASELINE_CLAIM_EVIDENCE.items()
        for evidence_id in evidence_ids
    }
    assert all(claim["evidence_ids"] for claim in payload["claims"])
    assert citation_pairs(payload) == expected_pairs

    cited_evidence_ids = {citation["evidence_id"] for citation in payload["citations"]}
    for claim in payload["claims"]:
        assert set(claim["evidence_ids"]) <= cited_evidence_ids
        assert {
            citation["evidence_id"]
            for citation in payload["citations"]
            if citation["claim_id"] == claim["claim_id"]
        } == set(claim["evidence_ids"])

    for citation in payload["citations"]:
        assert MINIMUM_PROVENANCE_FIELDS <= citation.keys()
        assert all(citation[field] for field in MINIMUM_PROVENANCE_FIELDS)


def test_guideline_payer_reconciliation_is_cross_dimensional_without_winner(
    client: TestClient,
) -> None:
    payload = ask(client)
    reconciliation = payload["reconciliation"]

    assert len(reconciliation) == 1
    assert reconciliation[0]["type"] == "COMPATIBLE_CONSTRAINT"
    assert reconciliation[0]["severity"] == "INFORMATIONAL"
    assert reconciliation[0]["resolution_state"] == "NOT_APPLICABLE"
    assert reconciliation[0]["type"] != "SAME_DIMENSION_DISAGREEMENT"
    assert "winner" not in reconciliation[0]

    reconciliation_citations = {
        citation["source_type"]
        for citation in payload["citations"]
        if citation["claim_id"] == "CLM-A-RECON"
    }
    assert reconciliation_citations == {"GUIDELINE", "PAYER_POLICY"}


def test_missing_payer_uses_current_unavailable_behavior_without_payer_conclusion(
    client: TestClient,
) -> None:
    payload = ask(client, "PAYER_POLICY_UNAVAILABLE")
    payer_trace = next(
        item
        for item in payload["orchestration_trace"]
        if item["source_type"] == "PAYER_POLICY"
    )

    assert payer_trace == {
        "sequence": 3,
        "source_type": "PAYER_POLICY",
        "label": "Payer",
        "status": "SOURCE_UNAVAILABLE",
    }
    assert payload["policy_version_id"] is None
    assert claim_evidence(payload) == {
        "CLM-C-CASE": {"EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"},
        "CLM-C-GUIDE": {
            "EV-SYN-GUIDE-SUPPORT-001",
            "EV-SYN-GUIDE-SCOPE-001",
        },
        "CLM-C-FORM": {"EV-SYN-FORM-STATUS-001"},
    }
    assert not any(
        citation["source_type"] == "PAYER_POLICY"
        for citation in payload["citations"]
    )
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert len(payload["reconciliation"]) == 1
    assert payload["reconciliation"][0]["type"] == "SOURCE_UNAVAILABLE"
    assert payload["reconciliation"][0]["severity"] == "HIGH"
    assert payload["reconciliation"][0]["resolution_state"] == "UNRESOLVED"
    assert "cannot determine whether prior authorization is required" in payload["answer"]


def test_true_conflict_represents_both_sides_and_does_not_choose_winner(
    client: TestClient,
) -> None:
    payload = ask(client, "TEST_ONLY_FORMULARY_SUBSTITUTION")
    conflict = payload["reconciliation"][0]

    assert claim_evidence(payload) == {
        "CLM-F-POLICY": {"EV-SYN-POL-V1-PA-001"},
        "CLM-F-FORM": {"EV-SYN-FORM-CONFLICT-001"},
        "CLM-F-CONFLICT": {
            "EV-SYN-POL-V1-PA-001",
            "EV-SYN-FORM-CONFLICT-001",
        },
    }
    assert conflict["type"] == "SAME_DIMENSION_DISAGREEMENT"
    assert conflict["severity"] == "HIGH"
    assert conflict["resolution_state"] == "UNRESOLVED"
    assert "winner" not in conflict
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True


def test_unsupported_question_does_not_retrieve_or_fabricate_clinical_output(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/questions",
        json={"question": "Can this demo schedule a home delivery?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "UNSUPPORTED"
    assert payload["status"] == "UNSUPPORTED_SCOPE"
    assert payload["selected_sources"] == []
    assert payload["orchestration_trace"] == []
    assert payload["claims"] == []
    assert payload["citations"] == []
    assert payload["confidence"] is None
    assert payload["confidence_rationale"] is None
    assert payload["reconciliation"] == []
    assert payload["escalation"] is None
    assert payload["policy_version_id"] is None


def test_pending_feedback_leaves_v1_policy_and_evidence_current(
    client: TestClient,
) -> None:
    initial = ask(client)
    feedback_response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": initial["interaction_id"],
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )

    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["status"] == "PENDING"
    assert feedback["target_version_id"] == BASELINE_POLICY_VERSION
    assert feedback["proposed_version_id"] == UPDATED_POLICY_VERSION

    pending_result = ask(client)
    pending_evidence_ids = {
        citation["evidence_id"] for citation in pending_result["citations"]
    }
    assert pending_result["policy_version_id"] == BASELINE_POLICY_VERSION
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= (
        pending_evidence_ids
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V2-")
        for evidence_id in pending_evidence_ids
    )


def test_approval_activates_v2_claims_and_evidence(client: TestClient) -> None:
    initial = ask(client)
    approve_v2(client, initial["interaction_id"])

    updated = ask(client)
    assert updated["policy_version_id"] == UPDATED_POLICY_VERSION
    assert claim_evidence(updated) == UPDATED_CLAIM_EVIDENCE
    assert updated["confidence"] == "HIGH"
    assert updated["escalation"] is None

    payer_claim_ids = {"CLM-E-V2-PA", "CLM-E-V2-CRITERIA", "CLM-E-READY"}
    payer_claim_evidence = {
        evidence_id
        for claim_id, evidence_ids in claim_evidence(updated).items()
        if claim_id in payer_claim_ids
        for evidence_id in evidence_ids
    }
    assert {"EV-SYN-POL-V2-PA-001", "EV-SYN-POL-V2-STEP-001"} <= (
        payer_claim_evidence
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V1-")
        for evidence_id in payer_claim_evidence
    )


def test_approval_preserves_original_v1_interaction_snapshot(
    client: TestClient,
) -> None:
    initial = ask(client)
    original_snapshot = {
        "policy_version_id": initial["policy_version_id"],
        "claims": initial["claims"],
        "citations": [
            {
                "claim_id": citation["claim_id"],
                "evidence_id": citation["evidence_id"],
                "document_version_id": citation["document_version_id"],
                "version": citation["version"],
            }
            for citation in initial["citations"]
        ],
        "confidence": initial["confidence"],
        "confidence_rationale": initial["confidence_rationale"],
        "reconciliation": initial["reconciliation"],
        "escalation": initial["escalation"],
    }
    approve_v2(client, initial["interaction_id"])

    response = client.get(f"/api/v1/interactions/{initial['interaction_id']}")
    assert response.status_code == 200
    historical = response.json()
    historical_snapshot = {
        "policy_version_id": historical["policy_version_id"],
        "claims": historical["claims"],
        "citations": [
            {
                "claim_id": citation["claim_id"],
                "evidence_id": citation["evidence_id"],
                "document_version_id": citation["document_version_id"],
                "version": citation["version"],
            }
            for citation in historical["citations"]
        ],
        "confidence": historical["confidence"],
        "confidence_rationale": historical["confidence_rationale"],
        "reconciliation": historical["reconciliation"],
        "escalation": historical["escalation"],
    }
    assert historical_snapshot == original_snapshot
    assert historical["policy_version_id"] == BASELINE_POLICY_VERSION
    assert all(
        citation["document_version_id"] != UPDATED_POLICY_VERSION
        for citation in historical["citations"]
        if citation["source_type"] == "PAYER_POLICY"
    )


def test_approval_records_lineage_and_audit(client: TestClient, settings) -> None:
    initial = ask(client)
    feedback = approve_v2(client, initial["interaction_id"])

    audit = client.get(f"/api/v1/audit/{initial['interaction_id']}").json()
    types = [event["event_type"] for event in audit["events"]]
    assert "FEEDBACK_SUBMITTED" in types
    assert "REVIEW_APPROVED" in types
    assert "KNOWLEDGE_UPDATE_APPLIED" in types
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT prior_version_id, successor_version_id FROM assertion_supersession "
            "WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (BASELINE_POLICY_VERSION, UPDATED_POLICY_VERSION)


def test_demo_reset_restores_v1_behavior_and_evidence(client: TestClient) -> None:
    initial = ask(client)
    approve_v2(client, initial["interaction_id"])
    assert ask(client)["policy_version_id"] == UPDATED_POLICY_VERSION

    reset = client.post(
        "/api/demo/reset",
        json={"confirmation": "RESET_SYNTHETIC_DEMO"},
    )
    assert reset.status_code == 200

    restored = ask(client)
    restored_evidence_ids = {
        citation["evidence_id"] for citation in restored["citations"]
    }
    assert restored["policy_version_id"] == BASELINE_POLICY_VERSION
    assert claim_evidence(restored) == BASELINE_CLAIM_EVIDENCE
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= (
        restored_evidence_ids
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V2-")
        for evidence_id in restored_evidence_ids
    )
    assert restored["confidence"] == "HIGH"
    assert restored["escalation"] is None
