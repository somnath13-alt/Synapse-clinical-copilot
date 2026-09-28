"""Focused M6.3c2 live knowledge-aware orchestration coverage."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings


QUESTION = demo.CANONICAL_QUESTION
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _ask(settings: Settings, **kwargs: Any) -> dict[str, Any]:
    return demo.ask_question(settings, QUESTION, **kwargs)


def _approve_v2(settings: Settings, interaction_id: str | None = None) -> None:
    initial = _ask(settings) if interaction_id is None else None
    feedback = demo.submit_feedback(
        settings,
        interaction_id or initial["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified synthetic provenance.",
    )


def _knowledge_rows(settings: Settings, interaction_id: str) -> list[sqlite3.Row]:
    with database.managed_connection(settings.database_path) as connection:
        return connection.execute(
            """SELECT assertion_id, ordinal, origin, state_at_execution, value_json,
                      normalized_scope_json, effective_from, effective_to, recorded_at,
                      source_id, document_version_id
               FROM interaction_knowledge WHERE interaction_id = ? ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()


def _set_source_authorization(
    settings: Settings, value: bool | None, *, recorded_at: str | None = None
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        evidence_ids = ["EV-SYN-POL-V1-PA-001"]
        if value is not None:
            evidence_ids.append("EV-SYN-FORM-STATUS-001")
        for evidence_id in evidence_ids:
            row = connection.execute(
                "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            payload = json.loads(row[0])
            if value is None:
                payload.pop("prior_authorization_required", None)
            else:
                payload["prior_authorization_required"] = value
            connection.execute(
                "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
                (json.dumps(payload, sort_keys=True), evidence_id),
            )
        if recorded_at is not None:
            connection.execute(
                "UPDATE source_document_version SET recorded_at = ? WHERE document_version_id = ?",
                (recorded_at, V1),
            )


def _governance_counts(settings: Settings) -> dict[str, int]:
    tables = ("feedback", "review", "assertion_lineage", "knowledge_update")
    with database.managed_connection(settings.database_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


def test_source_and_knowledge_corroboration_uses_v2_without_inflation(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    rows = _knowledge_rows(initialized_settings, payload["interaction_id"])
    with database.managed_connection(initialized_settings.database_path) as connection:
        policy = connection.execute(
            "SELECT confidence_policy_id FROM interaction WHERE interaction_id = ?",
            (payload["interaction_id"],),
        ).fetchone()[0]

    assert payload["confidence"] == "HIGH"
    assert payload["escalation"] is None
    assert policy == "CONF-PA-SYN-V2"
    assert [(row[0], row[2]) for row in rows] == [(V1_PA, "CORROBORATED")]


def test_source_only_keeps_source_result_without_fabricating_governance(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_assertion SET state = 'CANDIDATE' WHERE assertion_id = ?",
            (V1_PA,),
        )
    payload = _ask(initialized_settings)

    assert payload["confidence"] == "HIGH"
    assert "Payer Policy V1 requires prior authorization" in payload["answer"]
    assert _knowledge_rows(initialized_settings, payload["interaction_id"]) == []


def test_knowledge_only_is_unverified_low_and_escalated(
    initialized_settings: Settings,
) -> None:
    _set_source_authorization(initialized_settings, None)
    payload = _ask(initialized_settings)

    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert "governed payer baseline exists" in payload["answer"]
    assert "unverified" in payload["answer"]
    assert {claim["claim_id"] for claim in payload["claims"]} == {
        "CLM-U-CASE",
        "CLM-U-GUIDE",
        "CLM-U-FORM",
    }
    assert not any(
        citation["source_type"] == "PAYER_POLICY"
        for citation in payload["citations"]
    )
    assert _knowledge_rows(initialized_settings, payload["interaction_id"])[0][2] == "KNOWLEDGE"


def test_source_unavailable_surfaces_only_unverified_baseline_semantics(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings, source_mode="PAYER_POLICY_UNAVAILABLE")

    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert "governed payer baseline exists" in payload["answer"]
    assert "retrieval fallback" in payload["answer"]
    assert not any(c["source_type"] == "PAYER_POLICY" for c in payload["citations"])


def test_same_dimension_conflict_has_no_winner(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_assertion SET value_json = 'false' WHERE assertion_id = ?",
            (V1_PA,),
        )
    payload = _ask(initialized_settings)

    assert payload["confidence"] == "LOW"
    assert payload["policy_version_id"] is None
    assert payload["reconciliation"][0]["type"] == "SAME_DIMENSION_CONFLICT"
    assert "without selecting a winner" in payload["answer"]


def test_stale_knowledge_disagreement_is_low_and_preserves_both(
    initialized_settings: Settings,
) -> None:
    _set_source_authorization(
        initialized_settings, False, recorded_at="2026-06-20T12:00:00Z"
    )
    payload = _ask(initialized_settings)

    assert payload["confidence"] == "LOW"
    assert payload["reconciliation"][0]["type"] == "STALE_KNOWLEDGE_DISAGREEMENT"
    assert {claim["claim_id"] for claim in payload["claims"]} == {
        "CLM-K-SOURCE",
        "CLM-K-BASELINE",
        "CLM-K-CONFLICT",
    }


def test_cross_dimension_clinical_and_payer_constraints_remain_compatible(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)

    assert payload["reconciliation"] == [
        {
            "type": "COMPATIBLE_CONSTRAINT",
            "severity": "INFORMATIONAL",
            "resolution_state": "NOT_APPLICABLE",
            "explanation": "The guideline supports the medication clinically while the payer requires prior authorization; these are compatible decision dimensions.",
        }
    ]


def test_candidate_is_excluded_from_governed_input_and_snapshot(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    rows = _knowledge_rows(initialized_settings, payload["interaction_id"])

    assert {row[0] for row in rows} == {V1_PA}
    assert V2_PA not in {row[0] for row in rows}
    assert "pending knowledge is non-authoritative" in payload["confidence_rationale"]


def test_malformed_knowledge_preserves_usable_source_and_is_exposed(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_assertion SET normalized_scope_json = '{broken' WHERE assertion_id = ?",
            (V1_PA,),
        )
    payload = _ask(initialized_settings)

    assert payload["confidence"] == "HIGH"
    assert "Payer Policy V1 requires prior authorization" in payload["answer"]
    assert payload["reconciliation"][-1]["type"] == "MALFORMED_KNOWLEDGE_CHANNEL"
    assert _knowledge_rows(initialized_settings, payload["interaction_id"]) == []


def test_current_selects_and_snapshots_v1(initialized_settings: Settings) -> None:
    payload = _ask(initialized_settings)
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]

    assert payload["policy_version_id"] == V1
    assert (row[0], row[3], row[10]) == (V1_PA, "APPLIED", V1)


def test_as_of_june_selects_superseded_v1_after_approval(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    payload = _ask(initialized_settings, as_of=JUNE_15)
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]

    assert payload["policy_version_id"] == V1
    assert (row[0], row[3], row[10]) == (V1_PA, "SUPERSEDED", V1)


def test_as_of_july_selects_applied_v2_after_approval(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    payload = _ask(initialized_settings, as_of=JULY_3)
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]

    assert payload["policy_version_id"] == V2
    assert (row[0], row[3], row[10]) == (V2_PA, "APPLIED", V2)


def test_temporal_snapshot_persists_requested_instant_and_ordinal(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings, as_of=JUNE_15)
    with database.managed_connection(initialized_settings.database_path) as connection:
        interaction = connection.execute(
            "SELECT temporal_mode, requested_as_of FROM interaction WHERE interaction_id = ?",
            (payload["interaction_id"],),
        ).fetchone()
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]

    assert interaction == ("AS_OF", JUNE_15)
    assert row[1] == 0


def test_snapshot_contains_execution_time_value_scope_and_provenance(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]

    assert row[4] == "true"
    assert json.loads(row[5])["payer_id"] == "SYN-PAYER-NHH"
    assert row[6:11] == (
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
        "2025-12-15T12:05:00Z",
        "SRC-SYN-PAYER",
        V1,
    )


def test_approval_after_interaction_does_not_change_snapshot(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    before = [tuple(row) for row in _knowledge_rows(initialized_settings, payload["interaction_id"])]
    _approve_v2(initialized_settings, payload["interaction_id"])
    after = [tuple(row) for row in _knowledge_rows(initialized_settings, payload["interaction_id"])]

    assert after == before
    assert after[0][3] == "APPLIED"


def test_historical_get_does_not_rerun_live_services(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _ask(initialized_settings)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("historical GET reran a live service")

    monkeypatch.setattr(demo, "RetrievalService", forbidden)
    monkeypatch.setattr(demo, "KnowledgeService", forbidden)
    monkeypatch.setattr(demo, "compare_reasoning_input", forbidden)
    historical = demo.get_interaction(initialized_settings, payload["interaction_id"])

    assert historical is not None
    assert historical["answer"] == payload["answer"]
    assert historical["confidence"] == payload["confidence"]


def test_snapshot_failure_rolls_back_answer_transaction(
    initialized_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = demo._persist_interaction
    before = _governance_counts(initialized_settings)

    def invalid_snapshot(*args: Any, **kwargs: Any) -> None:
        participation = deepcopy(kwargs["knowledge_participation"])
        participation[0]["origin"] = "INVALID"
        kwargs["knowledge_participation"] = participation
        original(*args, **kwargs)

    monkeypatch.setattr(demo, "_persist_interaction", invalid_snapshot)
    with pytest.raises(sqlite3.IntegrityError):
        _ask(initialized_settings)

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM interaction").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM interaction_knowledge").fetchone()[0] == 0
    assert _governance_counts(initialized_settings) == before


def test_material_corroborated_claim_retains_both_provenance_chains(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    row = _knowledge_rows(initialized_settings, payload["interaction_id"])[0]
    payer_citations = [
        citation
        for citation in payload["citations"]
        if citation["source_type"] == "PAYER_POLICY"
    ]
    with database.managed_connection(initialized_settings.database_path) as connection:
        evidence = connection.execute(
            """SELECT ike.evidence_id FROM interaction_knowledge_evidence AS ike
               JOIN interaction_knowledge AS ik
                 ON ik.interaction_knowledge_id = ike.interaction_knowledge_id
               WHERE ik.interaction_id = ?""",
            (payload["interaction_id"],),
        ).fetchall()

    assert (row[0], row[2], row[9], row[10]) == (
        V1_PA,
        "CORROBORATED",
        "SRC-SYN-PAYER",
        V1,
    )
    assert {item["evidence_id"] for item in payer_citations} >= {"EV-SYN-POL-V1-PA-001"}
    assert evidence == [("EV-SYN-POL-V1-PA-001",)]


def test_no_fabricated_citations_outside_retrieved_snapshot(
    initialized_settings: Settings,
) -> None:
    payload = _ask(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        retrieved = {
            row[0]
            for row in connection.execute(
                "SELECT evidence_id FROM interaction_evidence WHERE interaction_id = ?",
                (payload["interaction_id"],),
            )
        }

    assert {item["evidence_id"] for item in payload["citations"]} <= retrieved
    assert all(claim["evidence_ids"] for claim in payload["claims"])


def test_disagreement_does_not_automatically_update_governance(
    initialized_settings: Settings,
) -> None:
    before = _governance_counts(initialized_settings)
    _set_source_authorization(initialized_settings, False)
    _ask(initialized_settings)
    after = _governance_counts(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        assertion = connection.execute(
            "SELECT state, value_json FROM knowledge_assertion WHERE assertion_id = ?",
            (V1_PA,),
        ).fetchone()

    assert after == before
    assert assertion == ("APPLIED", "true")
