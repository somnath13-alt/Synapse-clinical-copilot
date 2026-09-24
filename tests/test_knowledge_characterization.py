from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
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
        V1,
        "REQUIRES_AUTHORIZATION",
        "SYN-MED-VEL",
        "true",
        "AUTHORIZATION_REQUIREMENT",
        '{"condition_id":"SYN-COND-LDS","medication_id":"SYN-MED-VEL",'
        '"payer_id":"SYN-PAYER-NHH","plan_id":"SYN-PLAN-HLP"}',
        "2025-12-15T12:05:00Z",
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
        "APPLIED",
    ),
    "AST-SYN-POL-V1-STEP": (
        V1,
        V1,
        "REQUIRES_PREREQUISITE",
        None,
        '{"combination_rule":"ALL","minimum_days_each":30,'
        '"minimum_distinct_failures":2,'
        '"therapy_ids":["SYN-MED-NOR","SYN-MED-BRV"]}',
        "PREREQUISITE_REQUIREMENT",
        '{"condition_id":"SYN-COND-LDS","medication_id":"SYN-MED-VEL",'
        '"payer_id":"SYN-PAYER-NHH","plan_id":"SYN-PLAN-HLP"}',
        "2025-12-15T12:05:00Z",
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
        "APPLIED",
    ),
    "AST-SYN-POL-V2-PA": (
        V2,
        V2,
        "REQUIRES_AUTHORIZATION",
        "SYN-MED-VEL",
        "true",
        "AUTHORIZATION_REQUIREMENT",
        '{"condition_id":"SYN-COND-LDS","medication_id":"SYN-MED-VEL",'
        '"payer_id":"SYN-PAYER-NHH","plan_id":"SYN-PLAN-HLP"}',
        "2026-06-10T12:05:00Z",
        "2026-07-01T00:00:00Z",
        None,
        "CANDIDATE",
    ),
    "AST-SYN-POL-V2-STEP": (
        V2,
        V2,
        "REQUIRES_PREREQUISITE",
        None,
        '{"combination_rule":"ANY","minimum_days_each":30,'
        '"minimum_distinct_failures":1,'
        '"therapy_ids":["SYN-MED-NOR","SYN-MED-BRV"]}',
        "PREREQUISITE_REQUIREMENT",
        '{"condition_id":"SYN-COND-LDS","medication_id":"SYN-MED-VEL",'
        '"payer_id":"SYN-PAYER-NHH","plan_id":"SYN-PLAN-HLP"}',
        "2026-06-10T12:05:00Z",
        "2026-07-01T00:00:00Z",
        None,
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


def _fixture_assertions(settings: Settings) -> list[tuple[Any, ...]]:
    fixture_root = demo._fixture_root(settings)
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    expected: list[tuple[Any, ...]] = []
    for item in manifest["fixture_files"]:
        fixture = json.loads((fixture_root / item["path"]).read_text(encoding="utf-8"))
        for version in fixture["versions"]:
            for assertion in version.get("assertions", []):
                expected.append(
                    (
                        assertion["assertion_id"],
                        version["document_version_id"],
                        assertion["subject_id"],
                        assertion["predicate"],
                        assertion.get("object_id"),
                        demo._json(assertion.get("value")),
                        assertion["decision_dimension"],
                        demo._json(assertion["normalized_scope"])
                        if assertion.get("normalized_scope") is not None
                        else None,
                        assertion.get("recorded_at", version["recorded_at"]),
                        assertion.get("effective_from", version["effective_from"]),
                        assertion.get("effective_to", version.get("effective_to")),
                        "APPLIED" if version.get("current_at_seed") else "CANDIDATE",
                    )
                )
    return sorted(expected)


def _write_single_fixture_project(
    settings: Settings,
    tmp_path: Path,
    fixture: dict[str, Any],
) -> Settings:
    fixture_root = tmp_path / "data" / "fixtures"
    fixture_root.mkdir(parents=True)
    (fixture_root / "manifest.json").write_text(
        json.dumps({"fixture_files": [{"path": "fixture.json"}]}),
        encoding="utf-8",
    )
    (fixture_root / "fixture.json").write_text(
        json.dumps(fixture),
        encoding="utf-8",
    )
    return replace(
        settings,
        project_root=tmp_path,
        data_directory=tmp_path / "local-data",
        database_path=tmp_path / "local-data" / "synapse.sqlite3",
    )


def _minimal_version(
    version_id: str,
    evidence_id: str,
    *,
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_version_id": version_id,
        "source_id": "SRC-SYN-TEST",
        "source_type": "EHR",
        "source_title": "SYNTHETIC — Seed validation fixture",
        "version": "1",
        "timestamp": "2026-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T00:05:00Z",
        "effective_from": "2026-01-01T00:00:00Z",
        "effective_to": None,
        "relevant_excerpt": "Synthetic seed validation fixture.",
        "structured_data": {},
        "checksum": f"checksum-{version_id}",
        "governance_state": "APPLIED",
        "current_at_seed": True,
        "evidence_items": [
            {
                "evidence_id": evidence_id,
                "document_version_id": version_id,
                "source_id": "SRC-SYN-TEST",
                "source_type": "EHR",
                "source_title": "SYNTHETIC — Seed validation fixture",
                "version": "1",
                "timestamp": "2026-01-01T00:00:00Z",
                "section": "test/section",
                "relevant_excerpt": "Synthetic seed validation fixture.",
                "structured_data": {},
            }
        ],
        "assertions": assertions,
    }


def test_seeded_payer_assertion_projection_preserves_first_class_fields(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(knowledge_assertion)")
        }
        rows = connection.execute(
            """SELECT assertion_id, document_version_id, subject_id, predicate,
                      object_id, value_json, decision_dimension, normalized_scope_json,
                      recorded_at, effective_from, effective_to, state
               FROM knowledge_assertion
               WHERE document_version_id IN (?, ?)
               ORDER BY assertion_id""",
            (V1, V2),
        ).fetchall()

    assert columns == {
        "assertion_id",
        "document_version_id",
        "subject_id",
        "predicate",
        "object_id",
        "value_json",
        "decision_dimension",
        "normalized_scope_json",
        "recorded_at",
        "effective_from",
        "effective_to",
        "state",
    }
    assert "evidence_ids_json" not in columns
    assert {row[0]: row[1:] for row in rows} == EXPECTED_PAYER_ASSERTIONS


def test_seeded_assertion_evidence_and_document_provenance_resolve_in_application(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        assertion_rows = connection.execute(
            """SELECT ka.assertion_id, ka.document_version_id, ae.evidence_id,
                      dv.document_id, d.source_id, d.source_type, d.source_title
               FROM knowledge_assertion AS ka
               JOIN assertion_evidence AS ae ON ae.assertion_id = ka.assertion_id
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
        evidence_id,
        document_id,
        source_id,
        source_type,
        source_title,
    ) in assertion_rows:
        assert document_id.startswith("DOC-SYN-")
        assert source_id.startswith("SRC-SYN-")
        assert source_type
        assert source_title.startswith("SYNTHETIC —")
        assert evidence_id in evidence_versions, assertion_id
        assert evidence_versions[evidence_id] == document_version_id

    payer_mappings = {
        row[0]: row[2]
        for row in assertion_rows
        if row[1] in {V1, V2}
    }
    assert payer_mappings == {
        "AST-SYN-POL-V1-PA": "EV-SYN-POL-V1-PA-001",
        "AST-SYN-POL-V1-STEP": "EV-SYN-POL-V1-STEP-001",
        "AST-SYN-POL-V2-PA": "EV-SYN-POL-V2-PA-001",
        "AST-SYN-POL-V2-STEP": "EV-SYN-POL-V2-STEP-001",
    }


def test_all_fixture_assertion_fields_and_temporal_fallbacks_round_trip(
    initialized_settings: Settings,
) -> None:
    expected = _fixture_assertions(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        actual = connection.execute(
            """SELECT assertion_id, document_version_id, subject_id, predicate,
                      object_id, value_json, decision_dimension, normalized_scope_json,
                      recorded_at, effective_from, effective_to, state
               FROM knowledge_assertion ORDER BY assertion_id"""
        ).fetchall()

    assert len(expected) == 18
    assert actual == expected
    assert all(row[2] for row in actual)
    assert any(row[7] is None for row in actual)
    assert any(row[7] is not None for row in actual)


def test_explicit_assertion_temporal_values_override_document_defaults(
    settings: Settings,
    tmp_path: Path,
) -> None:
    assertion = {
        "assertion_id": "AST-SYN-OVERRIDE",
        "subject_id": "SYN-SUBJECT",
        "predicate": "HAS_TEST_VALUE",
        "value": "synthetic",
        "decision_dimension": "PATIENT_CONTEXT",
        "recorded_at": "2026-02-01T00:01:00Z",
        "effective_from": "2026-02-02T00:00:00Z",
        "effective_to": "2026-02-03T00:00:00Z",
        "evidence_ids": ["EV-SYN-OVERRIDE"],
    }
    fixture = {
        "document_id": "DOC-SYN-OVERRIDE",
        "versions": [
            _minimal_version(
                "DV-SYN-OVERRIDE",
                "EV-SYN-OVERRIDE",
                assertions=[assertion],
            )
        ],
    }
    override_settings = _write_single_fixture_project(settings, tmp_path, fixture)
    demo.initialize_demo(override_settings)

    with database.managed_connection(override_settings.database_path) as connection:
        row = connection.execute(
            """SELECT normalized_scope_json, recorded_at, effective_from, effective_to
               FROM knowledge_assertion WHERE assertion_id = 'AST-SYN-OVERRIDE'"""
        ).fetchone()

    assert row == (
        None,
        "2026-02-01T00:01:00Z",
        "2026-02-02T00:00:00Z",
        "2026-02-03T00:00:00Z",
    )


def test_baseline_has_exactly_18_fk_backed_assertion_evidence_rows(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM assertion_evidence"
        ).fetchone() == (18,)
        assert connection.execute(
            """SELECT COUNT(*)
               FROM assertion_evidence AS ae
               JOIN knowledge_assertion AS ka ON ka.assertion_id = ae.assertion_id
               JOIN evidence_item AS ei ON ei.evidence_id = ae.evidence_id
               WHERE ka.document_version_id <> ei.document_version_id"""
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_assertion_evidence_constraints_reject_duplicate_and_unknown_links(
    initialized_settings: Settings,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_evidence VALUES (?, ?)",
                ("AST-SYN-POL-V1-PA", "EV-SYN-POL-V1-PA-001"),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_evidence VALUES (?, ?)",
                ("AST-SYN-UNKNOWN", "EV-SYN-POL-V1-PA-001"),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_evidence VALUES (?, ?)",
                ("AST-SYN-POL-V1-PA", "EV-SYN-UNKNOWN"),
            )


def test_seed_rejects_assertion_evidence_from_another_document_version(
    settings: Settings,
    tmp_path: Path,
) -> None:
    mismatched_assertion = {
        "assertion_id": "AST-SYN-MISMATCH",
        "subject_id": "SYN-SUBJECT",
        "predicate": "HAS_TEST_VALUE",
        "decision_dimension": "PATIENT_CONTEXT",
        "evidence_ids": ["EV-SYN-V2"],
    }
    fixture = {
        "document_id": "DOC-SYN-MISMATCH",
        "versions": [
            _minimal_version("DV-SYN-V2", "EV-SYN-V2", assertions=[]),
            _minimal_version(
                "DV-SYN-V1",
                "EV-SYN-V1",
                assertions=[mismatched_assertion],
            ),
        ],
    }
    mismatch_settings = _write_single_fixture_project(settings, tmp_path, fixture)

    with pytest.raises(
        database.DatabaseValidationError,
        match="must belong to the same document version",
    ):
        demo.initialize_demo(mismatch_settings)


def test_assertion_state_constraint_accepts_only_approved_states(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        for state in ("CANDIDATE", "APPLIED", "SUPERSEDED"):
            connection.execute(
                "UPDATE knowledge_assertion SET state = ? WHERE assertion_id = ?",
                (state, "AST-SYN-POL-V1-PA"),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE knowledge_assertion SET state = 'REJECTED' "
                "WHERE assertion_id = 'AST-SYN-POL-V1-PA'"
            )


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
        assertion_lineage_count = connection.execute(
            "SELECT COUNT(*) FROM assertion_lineage"
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
    assert assertion_lineage_count == (0,)
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
        assertion_lineage = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id,
                      feedback_id, created_at
               FROM assertion_lineage WHERE feedback_id = ?
               ORDER BY predecessor_assertion_id""",
            (feedback["feedback_id"],),
        ).fetchall()
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
    assert assertion_lineage == [
        (
            "AST-SYN-POL-V1-PA",
            "AST-SYN-POL-V2-PA",
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        ),
        (
            "AST-SYN-POL-V1-STEP",
            "AST-SYN-POL-V2-STEP",
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        ),
    ]
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


def test_approval_delegates_knowledge_mutations_through_service(
    initialized_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    transitions: list[tuple[str, str]] = []
    lineage: list[tuple[str, str, str, str]] = []
    original_transition = demo.KnowledgeService.apply_assertion_state_transition
    original_lineage = demo.KnowledgeService.create_assertion_lineage

    def record_transition(service: Any, assertion_id: str, new_state: Any) -> None:
        transitions.append((assertion_id, new_state.value))
        original_transition(service, assertion_id, new_state)

    def record_lineage(
        service: Any,
        predecessor_assertion_id: str,
        successor_assertion_id: str,
        feedback_id: str,
        created_at: str,
    ) -> None:
        lineage.append(
            (
                predecessor_assertion_id,
                successor_assertion_id,
                feedback_id,
                created_at,
            )
        )
        original_lineage(
            service,
            predecessor_assertion_id,
            successor_assertion_id,
            feedback_id,
            created_at,
        )

    monkeypatch.setattr(
        demo.KnowledgeService,
        "apply_assertion_state_transition",
        record_transition,
    )
    monkeypatch.setattr(
        demo.KnowledgeService,
        "create_assertion_lineage",
        record_lineage,
    )

    _approve(initialized_settings, feedback["feedback_id"])

    assert transitions == [
        ("AST-SYN-POL-V1-PA", "SUPERSEDED"),
        ("AST-SYN-POL-V1-STEP", "SUPERSEDED"),
        ("AST-SYN-POL-V2-PA", "APPLIED"),
        ("AST-SYN-POL-V2-STEP", "APPLIED"),
    ]
    assert lineage == [
        (
            "AST-SYN-POL-V1-PA",
            "AST-SYN-POL-V2-PA",
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        ),
        (
            "AST-SYN-POL-V1-STEP",
            "AST-SYN-POL-V2-STEP",
            feedback["feedback_id"],
            demo.APPROVED_TIME,
        ),
    ]


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


def test_assertion_lineage_endpoints_are_assertion_ids(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])
    _approve(initialized_settings, feedback["feedback_id"])

    with database.managed_connection(initialized_settings.database_path) as connection:
        endpoints = connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id
               FROM assertion_lineage ORDER BY predecessor_assertion_id"""
        ).fetchall()
        assertion_ids = {
            row[0]
            for row in connection.execute(
                "SELECT assertion_id FROM knowledge_assertion"
            )
        }
        version_ids = {
            row[0]
            for row in connection.execute(
                "SELECT document_version_id FROM source_document_version"
            )
        }

    assert endpoints == [
        ("AST-SYN-POL-V1-PA", "AST-SYN-POL-V2-PA"),
        ("AST-SYN-POL-V1-STEP", "AST-SYN-POL-V2-STEP"),
    ]
    assert {endpoint for pair in endpoints for endpoint in pair} <= assertion_ids
    assert {endpoint for pair in endpoints for endpoint in pair}.isdisjoint(version_ids)


def test_assertion_lineage_rejects_self_and_duplicate_edges(
    initialized_settings: Settings,
) -> None:
    interaction = _ask(initialized_settings)
    feedback = _submit(initialized_settings, interaction["interaction_id"])

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
                (
                    "LIN-SYN-SELF",
                    "AST-SYN-POL-V1-PA",
                    "AST-SYN-POL-V1-PA",
                    feedback["feedback_id"],
                    demo.APPROVED_TIME,
                ),
            )

    _approve(initialized_settings, feedback["feedback_id"])
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "INSERT INTO assertion_lineage VALUES (?, ?, ?, ?, ?)",
                (
                    "LIN-SYN-DUPLICATE",
                    "AST-SYN-POL-V1-PA",
                    "AST-SYN-POL-V2-PA",
                    feedback["feedback_id"],
                    demo.APPROVED_TIME,
                ),
            )


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
            for table in (
                "review",
                "assertion_supersession",
                "assertion_lineage",
                "knowledge_update",
            )
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
        "assertion_lineage": 0,
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
            for table in (
                "review",
                "assertion_supersession",
                "assertion_lineage",
                "knowledge_update",
            )
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
        "assertion_lineage": 2,
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
