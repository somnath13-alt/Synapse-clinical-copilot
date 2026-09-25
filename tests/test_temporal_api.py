"""Public question API coverage for optional governed as-of selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.retrieval import RetrievalService, TemporalMode


JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"


def _ask(client: TestClient, *, as_of: str | None = None) -> dict[str, Any]:
    request = {"question": demo.CANONICAL_QUESTION}
    if as_of is not None:
        request["as_of"] = as_of
    response = client.post("/api/v1/questions", json=request)
    assert response.status_code == 200
    return response.json()


def _submit_v2(client: TestClient, interaction_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": interaction_id,
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )
    assert response.status_code == 200
    return response.json()


def _approve_v2(client: TestClient, interaction_id: str) -> None:
    feedback = _submit_v2(client, interaction_id)
    response = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={},
    )
    assert response.status_code == 200


def _payer_citation_versions(payload: dict[str, Any]) -> set[str]:
    return {
        citation["document_version_id"]
        for citation in payload["citations"]
        if citation["source_type"] == "PAYER_POLICY"
    }


def test_omitted_as_of_uses_current_mode(
    client: TestClient,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []
    service = RetrievalService(settings.database_path)

    class ObservedRetrievalService:
        def __init__(self, database_path: Path) -> None:
            assert database_path == settings.database_path

        def retrieve(self, request: Any) -> Any:
            observed.append(request)
            return service.retrieve(request)

    monkeypatch.setattr(demo, "RetrievalService", ObservedRetrievalService)

    payload = _ask(client)

    assert payload["policy_version_id"] == V1
    assert observed[0].temporal_mode is TemporalMode.CURRENT


def test_valid_as_of_uses_as_of_mode_and_normalized_utc(
    client: TestClient,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []
    service = RetrievalService(settings.database_path)

    class ObservedRetrievalService:
        def __init__(self, database_path: Path) -> None:
            assert database_path == settings.database_path

        def retrieve(self, request: Any) -> Any:
            observed.append(request)
            return service.retrieve(request)

    monkeypatch.setattr(demo, "RetrievalService", ObservedRetrievalService)

    payload = _ask(client, as_of="2026-06-15T14:00:00.000000+00:00")

    assert payload["policy_version_id"] == V1
    assert observed[0].temporal_mode is TemporalMode.AS_OF
    assert observed[0].as_of == JUNE_15


@pytest.mark.parametrize(
    "as_of",
    [
        pytest.param("not-a-time", id="malformed"),
        pytest.param("2026-06-15T14:00:00", id="naive"),
        pytest.param("2026-06-15T10:00:00-04:00", id="non-utc"),
    ],
)
def test_invalid_as_of_returns_validation_error(
    client: TestClient,
    as_of: str,
) -> None:
    response = client.post(
        "/api/v1/questions",
        json={"question": demo.CANONICAL_QUESTION, "as_of": as_of},
    )

    assert response.status_code == 422
    assert "as_of must" in str(response.json()["detail"])


def test_unsupported_intent_with_as_of_skips_retrieval_and_reasoning(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedRetrievalService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unsupported intent constructed retrieval")

    def unexpected_reasoning(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsupported intent invoked reasoning")

    monkeypatch.setattr(demo, "RetrievalService", UnexpectedRetrievalService)
    monkeypatch.setattr(demo, "reason", unexpected_reasoning)
    response = client.post(
        "/api/v1/questions",
        json={
            "question": "Can this demo schedule a home delivery?",
            "as_of": JUNE_15,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "UNSUPPORTED_SCOPE"


def test_public_response_shape_is_unchanged_with_as_of(client: TestClient) -> None:
    current = _ask(client)
    temporal = _ask(client, as_of=JUNE_15)

    assert set(temporal) == set(current)
    assert "as_of" not in temporal
    assert "temporal_mode" not in temporal


def test_approved_v2_current_and_as_of_answers_follow_selected_versions(
    client: TestClient,
) -> None:
    original = _ask(client)
    _approve_v2(client, original["interaction_id"])

    current = _ask(client)
    june = _ask(client, as_of=JUNE_15)
    july = _ask(client, as_of=JULY_3)

    assert current["policy_version_id"] == V2
    assert june["policy_version_id"] == V1
    assert july["policy_version_id"] == V2
    assert "Payer Policy V1" in june["answer"]
    assert "Payer Policy V2" in july["answer"]
    assert _payer_citation_versions(june) == {V1}
    assert _payer_citation_versions(july) == {V2}


def test_pending_v2_as_of_june_uses_v1_and_july_escalates_without_payer_claim(
    client: TestClient,
) -> None:
    initial = _ask(client)
    feedback = _submit_v2(client, initial["interaction_id"])
    assert feedback["status"] == "PENDING"

    june = _ask(client, as_of=JUNE_15)
    july = _ask(client, as_of=JULY_3)

    assert june["policy_version_id"] == V1
    assert _payer_citation_versions(june) == {V1}
    assert july["policy_version_id"] is None
    assert july["confidence"] == "LOW"
    assert july["escalation"]["required"] is True
    assert _payer_citation_versions(july) == set()
    assert all(
        not evidence_id.startswith("EV-SYN-POL-")
        for claim in july["claims"]
        for evidence_id in claim["evidence_ids"]
    )


def test_historical_interaction_remains_v1_after_v2_approval(
    client: TestClient,
) -> None:
    original = _ask(client)
    _approve_v2(client, original["interaction_id"])

    response = client.get(f"/api/v1/interactions/{original['interaction_id']}")

    assert response.status_code == 200
    historical = response.json()
    assert historical["policy_version_id"] == V1
    assert historical["answer"] == original["answer"]
    assert _payer_citation_versions(historical) == {V1}


def test_overlapping_opposing_payer_versions_preserve_both_through_public_answer(
    client: TestClient,
    settings: Any,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute(
            "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
            ("EV-SYN-POL-V2-PA-001",),
        ).fetchone()
        assert row is not None
        opposing_evidence = json.loads(row[0])
        opposing_evidence["prior_authorization_required"] = False
        connection.execute(
            """UPDATE source_document_version
               SET governance_state = 'APPLIED', effective_from = ?
               WHERE document_version_id = ?""",
            ("2026-06-01T00:00:00Z", V2),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'APPLIED', effective_from = ?
               WHERE document_version_id = ?""",
            ("2026-06-01T00:00:00Z", V2),
        )
        connection.execute(
            "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
            (
                json.dumps(opposing_evidence, sort_keys=True),
                "EV-SYN-POL-V2-PA-001",
            ),
        )

    payload = _ask(client, as_of=JUNE_15)

    assert payload["policy_version_id"] is None
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert _payer_citation_versions(payload) == {V1, V2}
    assert {
        finding["type"] for finding in payload["reconciliation"]
    } == {"SAME_DIMENSION_DISAGREEMENT"}


def test_narrower_assertion_interval_controls_live_as_of_reasoning(
    client: TestClient,
    settings: Any,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET effective_from = '2026-01-01T00:00:00Z',
                   effective_to = '2026-12-31T23:59:59Z'
               WHERE document_version_id = ?""",
            (V1,),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET effective_from = '2026-04-01T00:00:00Z',
                   effective_to = '2026-06-30T23:59:59Z'
               WHERE document_version_id = ?""",
            (V1,),
        )

    before = _ask(client, as_of="2026-03-15T14:00:00Z")
    inside = _ask(client, as_of=JUNE_15)
    after = _ask(client, as_of="2026-07-15T14:00:00Z")

    assert inside["confidence"] == "HIGH"
    assert _payer_citation_versions(inside) == {V1}
    for payload in (before, after):
        assert payload["confidence"] == "LOW"
        assert payload["escalation"]["required"] is True
        assert _payer_citation_versions(payload) == set()
        assert all(
            not evidence_id.startswith("EV-SYN-POL-")
            for claim in payload["claims"]
            for evidence_id in claim["evidence_ids"]
        )
        with database.managed_connection(settings.database_path) as connection:
            raw_payer_evidence = connection.execute(
                """SELECT COUNT(*) FROM interaction_evidence AS ie
                   JOIN evidence_item AS e ON e.evidence_id = ie.evidence_id
                   WHERE ie.interaction_id = ? AND e.source_type = 'PAYER_POLICY'""",
                (payload["interaction_id"],),
            ).fetchone()[0]
        assert raw_payer_evidence == 2


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE knowledge_assertion SET effective_from = 'not-a-time' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            id="malformed-assertion-effective-from",
        ),
        pytest.param(
            "UPDATE knowledge_assertion SET effective_to = 'not-a-time' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            id="malformed-assertion-effective-to",
        ),
        pytest.param(
            "UPDATE knowledge_assertion SET effective_from = '2026-01-01T00:00:00' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            id="naive-assertion-timestamp",
        ),
        pytest.param(
            "UPDATE knowledge_assertion "
            "SET effective_from = '2026-06-16T00:00:00Z', "
            "effective_to = '2026-06-15T00:00:00Z' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            id="inverted-assertion-interval",
        ),
        pytest.param(
            "UPDATE knowledge_assertion SET effective_from = '2025-12-01T00:00:00Z' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            id="assertion-outside-document-interval",
        ),
    ],
)
def test_invalid_assertion_applicability_is_publicly_safe_and_evidence_free(
    client: TestClient,
    settings: Any,
    statement: str,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(statement)

    payload = _ask(client, as_of=JUNE_15)

    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert payload["claims"] == []
    assert payload["citations"] == []
    assert "could not be verified" in payload["answer"].lower()


def test_agreeing_overlap_returns_409_without_persisting_interaction(
    client: TestClient,
    settings: Any,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET governance_state = 'APPLIED', effective_from = ?
               WHERE document_version_id = ?""",
            ("2026-06-01T00:00:00Z", V2),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'APPLIED', effective_from = ?
               WHERE document_version_id = ?""",
            ("2026-06-01T00:00:00Z", V2),
        )
        before = connection.execute("SELECT COUNT(*) FROM interaction").fetchone()[0]

    first = client.post(
        "/api/v1/questions",
        json={"question": demo.CANONICAL_QUESTION, "as_of": JUNE_15},
    )
    second = client.post(
        "/api/v1/questions",
        json={"question": demo.CANONICAL_QUESTION, "as_of": JUNE_15},
    )

    assert first.status_code == second.status_code == 409
    assert first.json() == second.json() == {
        "detail": "Multiple applicable payer-policy versions cannot be safely represented."
    }
    with database.managed_connection(settings.database_path) as connection:
        after = connection.execute("SELECT COUNT(*) FROM interaction").fetchone()[0]
    assert after == before


def test_retroactive_approved_correction_changes_new_as_of_not_old_snapshot(
    client: TestClient,
    settings: Any,
) -> None:
    original = _ask(client, as_of=JUNE_15)
    feedback = _submit_v2(client, original["interaction_id"])
    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute(
            "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
            ("EV-SYN-POL-V2-PA-001",),
        ).fetchone()
        assert row is not None
        opposing_evidence = json.loads(row[0])
        opposing_evidence["prior_authorization_required"] = False
        connection.execute(
            "UPDATE source_document_version SET effective_from = ? WHERE document_version_id = ?",
            ("2026-06-01T00:00:00Z", V2),
        )
        connection.execute(
            "UPDATE knowledge_assertion SET effective_from = ? WHERE document_version_id = ?",
            ("2026-06-01T00:00:00Z", V2),
        )
        connection.execute(
            "UPDATE knowledge_assertion SET value_json = 'false' WHERE assertion_id = ?",
            ("AST-SYN-POL-V2-PA",),
        )
        connection.execute(
            "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
            (json.dumps(opposing_evidence, sort_keys=True), "EV-SYN-POL-V2-PA-001"),
        )

    approval = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={},
    )
    assert approval.status_code == 200
    newly_executed = _ask(client, as_of=JUNE_15)
    historical = client.get(
        f"/api/v1/interactions/{original['interaction_id']}"
    ).json()

    assert newly_executed["confidence"] == "LOW"
    assert newly_executed["escalation"]["required"] is True
    assert _payer_citation_versions(newly_executed) == {V1, V2}
    assert historical["answer"] == original["answer"]
    assert historical["claims"] == original["claims"]
    assert historical["citations"] == original["citations"]
    assert historical["confidence"] == original["confidence"]
    assert _payer_citation_versions(historical) == {V1}
