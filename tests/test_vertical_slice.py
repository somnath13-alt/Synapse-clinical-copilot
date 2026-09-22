from __future__ import annotations

from fastapi.testclient import TestClient

from backend import database


QUESTION = "Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?"


def ask(client: TestClient, source_mode: str = "BASELINE") -> dict:
    response = client.post("/api/v1/questions", json={"question": QUESTION, "source_mode": source_mode})
    assert response.status_code == 200
    return response.json()


def test_canonical_question_returns_sourced_v1_answer(client: TestClient) -> None:
    payload = ask(client)
    assert payload["policy_version_id"] == "DV-SYN-POL-VEL-V1"
    assert payload["confidence"] == "HIGH"
    assert "requires prior authorization" in payload["answer"]
    assert payload["claims"] and payload["citations"]


def test_orchestration_selects_expected_sources_in_order(client: TestClient) -> None:
    payload = ask(client)
    assert payload["selected_sources"] == ["EHR", "GUIDELINE", "PAYER_POLICY", "FORMULARY", "SPECIALIST_NOTE"]
    assert [item["sequence"] for item in payload["orchestration_trace"]] == [1, 2, 3, 4, 5]


def test_citations_contain_minimum_provenance(client: TestClient) -> None:
    for citation in ask(client)["citations"]:
        assert all(citation[field] for field in ("source_id", "source_type", "source_title", "version", "timestamp", "relevant_excerpt"))


def test_guideline_payer_reconciliation_is_cross_dimensional(client: TestClient) -> None:
    payload = ask(client)
    assert payload["reconciliation"][0]["type"] == "COMPATIBLE_CONSTRAINT"
    assert "without logical contradiction" in payload["reconciliation"][0]["explanation"]


def test_missing_payer_is_low_confidence_and_escalated(client: TestClient) -> None:
    payload = ask(client, "PAYER_POLICY_UNAVAILABLE")
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert not any(c["source_type"] == "PAYER_POLICY" for c in payload["citations"])
    assert "cannot determine" in payload["answer"]


def test_true_conflict_is_low_and_does_not_choose_winner(client: TestClient) -> None:
    payload = ask(client, "TEST_ONLY_FORMULARY_SUBSTITUTION")
    assert payload["confidence"] == "LOW"
    assert payload["reconciliation"][0]["type"] == "SAME_DIMENSION_DISAGREEMENT"
    assert payload["escalation"]["required"] is True


def test_correction_pending_then_approval_activates_v2_and_preserves_v1(client: TestClient) -> None:
    initial = ask(client)
    feedback = client.post("/api/v1/feedback", json={"interaction_id": initial["interaction_id"], "message": "Payer policy V1 is outdated; use V2."})
    assert feedback.status_code == 200
    assert feedback.json()["status"] == "PENDING"
    assert ask(client)["policy_version_id"] == "DV-SYN-POL-VEL-V1"

    approval = client.post(f"/api/v1/feedback/{feedback.json()['feedback_id']}/approve", json={})
    assert approval.status_code == 200
    assert approval.json()["status"] == "APPLIED"
    updated = ask(client)
    assert updated["policy_version_id"] == "DV-SYN-POL-VEL-V2"
    assert "satisfies" in updated["answer"]

    historical = client.get(f"/api/v1/interactions/{initial['interaction_id']}").json()
    assert historical["policy_version_id"] == "DV-SYN-POL-VEL-V1"
    assert any(c["document_version_id"] == "DV-SYN-POL-VEL-V1" for c in historical["citations"])


def test_approval_records_lineage_and_audit(client: TestClient, settings) -> None:
    initial = ask(client)
    feedback = client.post("/api/v1/feedback", json={"interaction_id": initial["interaction_id"]}).json()
    client.post(f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={})
    audit = client.get(f"/api/v1/audit/{initial['interaction_id']}").json()
    types = [event["event_type"] for event in audit["events"]]
    assert "FEEDBACK_SUBMITTED" in types
    assert "REVIEW_APPROVED" in types
    assert "KNOWLEDGE_UPDATE_APPLIED" in types
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("SELECT prior_version_id, successor_version_id FROM assertion_supersession").fetchone() == ("DV-SYN-POL-VEL-V1", "DV-SYN-POL-VEL-V2")


def test_demo_reset_returns_to_v1(client: TestClient) -> None:
    initial = ask(client)
    feedback = client.post("/api/v1/feedback", json={"interaction_id": initial["interaction_id"]}).json()
    client.post(f"/api/v1/feedback/{feedback['feedback_id']}/approve", json={})
    assert ask(client)["policy_version_id"].endswith("V2")
    reset = client.post("/api/demo/reset", json={"confirmation": "RESET_SYNTHETIC_DEMO"})
    assert reset.status_code == 200
    assert ask(client)["policy_version_id"].endswith("V1")
