"""Persistence coverage for self-contained temporal interaction snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.reasoning import POLICY_VERSION_ID
from backend.retrieval import EvidenceBundle, RetrievalService


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
JUNE_15 = "2026-06-15T15:45:00Z"
JULY_3 = "2026-07-03T16:45:00Z"


def _ask(client: TestClient, *, as_of: str | None = None) -> dict[str, Any]:
    request = {"question": demo.CANONICAL_QUESTION}
    if as_of is not None:
        request["as_of"] = as_of
    response = client.post("/api/v1/questions", json=request)
    assert response.status_code == 200
    return response.json()


def _submit_v2(client: TestClient, interaction_id: str) -> str:
    response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": interaction_id,
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )
    assert response.status_code == 200
    return response.json()["feedback_id"]


def _approve_v2(client: TestClient, feedback_id: str) -> None:
    response = client.post(f"/api/v1/feedback/{feedback_id}/approve", json={})
    assert response.status_code == 200


def _snapshot_rows(
    settings: Settings, interaction_id: str
) -> tuple[tuple[Any, ...], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with database.managed_connection(settings.database_path) as connection:
        interaction = connection.execute(
            """SELECT temporal_mode, requested_as_of, confidence_policy_id,
                      policy_version_id
               FROM interaction WHERE interaction_id = ?""",
            (interaction_id,),
        ).fetchone()
        evidence = connection.execute(
            """SELECT evidence_id, ordinal FROM interaction_evidence
               WHERE interaction_id = ? ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()
        citations = connection.execute(
            """SELECT evidence_id, source_id, document_version_id
               FROM citation WHERE interaction_id = ? ORDER BY rowid""",
            (interaction_id,),
        ).fetchall()
    assert interaction is not None
    return interaction, evidence, citations


def _payer_versions(payload: dict[str, Any]) -> set[str]:
    return {
        citation["document_version_id"]
        for citation in payload["citations"]
        if citation["source_type"] == "PAYER_POLICY"
    }


def test_current_snapshot_persists_temporal_and_policy_identity(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = _ask(client)
    interaction, evidence, citations = _snapshot_rows(
        settings, payload["interaction_id"]
    )

    assert interaction == ("CURRENT", None, POLICY_VERSION_ID, V1)
    assert POLICY_VERSION_ID == "CONF-PA-SYN-V1"
    assert evidence
    assert citations


def test_as_of_snapshot_persists_canonical_requested_instant(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = _ask(client, as_of="2026-06-15T15:45:00.000000+00:00")
    interaction, _, _ = _snapshot_rows(settings, payload["interaction_id"])

    assert interaction == ("AS_OF", JUNE_15, POLICY_VERSION_ID, V1)


def test_database_rejects_malformed_temporal_pairing(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = _ask(client)

    for statement in (
        "UPDATE interaction SET temporal_mode = 'AS_OF' WHERE interaction_id = ?",
        "UPDATE interaction SET requested_as_of = '2026-06-15T00:00:00Z' WHERE interaction_id = ?",
        "UPDATE interaction SET temporal_mode = 'INVALID' WHERE interaction_id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with database.managed_connection(settings.database_path) as connection:
                connection.execute(statement, (payload["interaction_id"],))


def test_complete_bundle_order_and_claim_citation_subsets_are_persisted(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, EvidenceBundle] = {}
    service = RetrievalService(settings.database_path)

    class ObservedRetrievalService:
        def __init__(self, database_path: Path) -> None:
            assert database_path == settings.database_path

        def retrieve(self, request: Any) -> EvidenceBundle:
            bundle = service.retrieve(request)
            observed["bundle"] = bundle
            return bundle

    monkeypatch.setattr(demo, "RetrievalService", ObservedRetrievalService)
    payload = _ask(client)
    retrieved_ids = tuple(observed["bundle"].evidence_by_id)

    _, stored_evidence, _ = _snapshot_rows(settings, payload["interaction_id"])
    stored_ids = tuple(row[0] for row in stored_evidence)
    claim_ids = {
        evidence_id
        for claim in payload["claims"]
        for evidence_id in claim["evidence_ids"]
    }
    citation_ids = {item["evidence_id"] for item in payload["citations"]}

    assert stored_ids == retrieved_ids
    assert tuple(row[1] for row in stored_evidence) == tuple(range(len(retrieved_ids)))
    assert claim_ids <= set(stored_ids)
    assert citation_ids <= set(stored_ids)
    assert claim_ids < set(stored_ids)
    assert {"EV-SYN-FORM-PREF-001", "EV-SYN-NOTE-REQUEST-001"} <= (
        set(stored_ids) - claim_ids
    )


def test_interaction_evidence_uniqueness_and_foreign_keys(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = _ask(client)
    _, evidence, _ = _snapshot_rows(settings, payload["interaction_id"])
    evidence_id, ordinal = evidence[0]

    invalid_rows = (
        (payload["interaction_id"], evidence_id, ordinal + 100),
        (payload["interaction_id"], evidence[1][0], ordinal),
        (payload["interaction_id"], "EV-DOES-NOT-EXIST", ordinal + 101),
        ("INT-DOES-NOT-EXIST", evidence_id, ordinal + 102),
    )
    for row in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError):
            with database.managed_connection(settings.database_path) as connection:
                connection.execute(
                    """INSERT INTO interaction_evidence
                       (interaction_id, evidence_id, ordinal) VALUES (?, ?, ?)""",
                    row,
                )


def test_citations_persist_selected_source_and_document_version(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = _ask(client)
    _, _, stored = _snapshot_rows(settings, payload["interaction_id"])
    expected = [
        (
            citation["evidence_id"],
            citation["source_id"],
            citation["document_version_id"],
        )
        for citation in payload["citations"]
    ]

    assert stored == expected


def test_historical_citations_do_not_late_bind_provenance(
    client: TestClient,
    settings: Settings,
) -> None:
    original = _ask(client)
    payer = next(
        item for item in original["citations"] if item["source_type"] == "PAYER_POLICY"
    )

    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            """UPDATE evidence_item SET source_id = ?, document_version_id = ?
               WHERE evidence_id = ?""",
            ("SRC-SYN-LATE-BOUND", V2, payer["evidence_id"]),
        )

    historical = client.get(
        f"/api/v1/interactions/{original['interaction_id']}"
    ).json()
    unchanged = next(
        item
        for item in historical["citations"]
        if item["claim_id"] == payer["claim_id"]
        and item["evidence_id"] == payer["evidence_id"]
    )

    assert unchanged["source_id"] == payer["source_id"]
    assert unchanged["document_version_id"] == payer["document_version_id"]


def test_approval_does_not_mutate_existing_snapshot(
    client: TestClient,
    settings: Settings,
) -> None:
    current_v1 = _ask(client)
    june_v1 = _ask(client, as_of=JUNE_15)
    feedback_id = _submit_v2(client, current_v1["interaction_id"])
    july_pending = _ask(client, as_of=JULY_3)
    before = {
        payload["interaction_id"]: _snapshot_rows(settings, payload["interaction_id"])
        for payload in (current_v1, june_v1, july_pending)
    }

    _approve_v2(client, feedback_id)

    after = {
        interaction_id: _snapshot_rows(settings, interaction_id)
        for interaction_id in before
    }
    historical_v1 = client.get(
        f"/api/v1/interactions/{current_v1['interaction_id']}"
    ).json()
    historical_june = client.get(
        f"/api/v1/interactions/{june_v1['interaction_id']}"
    ).json()
    historical_pending = client.get(
        f"/api/v1/interactions/{july_pending['interaction_id']}"
    ).json()

    assert after == before
    immutable_public_fields = (
        "question",
        "intent",
        "selected_sources",
        "orchestration_trace",
        "answer",
        "claims",
        "citations",
        "confidence",
        "confidence_rationale",
        "reconciliation",
        "escalation",
        "policy_version_id",
    )
    for historical, original in (
        (historical_v1, current_v1),
        (historical_june, june_v1),
        (historical_pending, july_pending),
    ):
        assert all(historical[field] == original[field] for field in immutable_public_fields)
    assert _payer_versions(historical_v1) == {V1}
    assert _payer_versions(historical_june) == {V1}
    assert _payer_versions(historical_pending) == set()


def test_approved_current_and_as_of_snapshots_keep_selected_versions(
    client: TestClient,
    settings: Settings,
) -> None:
    initial = _ask(client)
    _approve_v2(client, _submit_v2(client, initial["interaction_id"]))
    current_v2 = _ask(client)
    june_v1 = _ask(client, as_of=JUNE_15)
    july_v2 = _ask(client, as_of=JULY_3)

    assert _snapshot_rows(settings, current_v2["interaction_id"])[0][3] == V2
    assert _snapshot_rows(settings, june_v1["interaction_id"])[0][3] == V1
    assert _snapshot_rows(settings, july_v2["interaction_id"])[0][3] == V2
    assert _payer_versions(june_v1) == {V1}
    assert _payer_versions(july_v2) == {V2}


def test_citation_database_constraint_requires_retrieved_evidence_membership(
    client: TestClient,
    settings: Settings,
) -> None:
    first = _ask(client)
    with database.managed_connection(settings.database_path) as connection:
        claim_id = connection.execute(
            """SELECT supported_claim_id FROM supported_claim
               WHERE interaction_id = ? ORDER BY rowid LIMIT 1""",
            (first["interaction_id"],),
        ).fetchone()[0]
        foreign_evidence = "EV-SYN-POL-V2-PA-001"
        evidence = connection.execute(
            """SELECT source_id, document_version_id, source_title, source_type,
                      version, timestamp, section, relevant_excerpt
               FROM evidence_item WHERE evidence_id = ?""",
            (foreign_evidence,),
        ).fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(settings.database_path) as connection:
            connection.execute(
                """INSERT INTO citation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "CIT-INVALID-SUBSET",
                    first["interaction_id"],
                    claim_id,
                    foreign_evidence,
                    *evidence,
                ),
            )


def test_question_transaction_rolls_back_interaction_and_snapshot_rows(
    client: TestClient,
    settings: Settings,
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("interaction", "interaction_evidence", "supported_claim", "citation")
        )
        connection.execute(
            """CREATE TRIGGER fail_supported_claim
               BEFORE INSERT ON supported_claim
               BEGIN SELECT RAISE(ABORT, 'forced snapshot failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced snapshot failure"):
        _ask(client)

    with database.managed_connection(settings.database_path) as connection:
        after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("interaction", "interaction_evidence", "supported_claim", "citation")
        )
    assert after == before
