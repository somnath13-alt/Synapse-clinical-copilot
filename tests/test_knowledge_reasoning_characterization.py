"""M6.1 characterization of the retrieval/knowledge/reasoning seam.

These tests intentionally describe CURRENT BEHAVIOR.  Comments prefixed with
``FUTURE M6 EXPECTATION`` are release direction, not assertions that the
corresponding production contract or finding already exists.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    KnowledgeAssertionState,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.reasoning import (
    ConfidenceLabel,
    DecisionAssertion,
    DecisionType,
    EscalationTrigger,
    FindingType,
    normalize_evidence,
    reason,
)
from backend.retrieval import (
    EvidenceBundle,
    PayerPolicyAdapter,
    RetrievalRequest,
    RetrievalService,
    RetrievalStatus,
    SourceType,
    TemporalMode,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"

PAYER_SCOPE = KnowledgeQuery(
    predicate="REQUIRES_AUTHORIZATION",
    decision_dimension="AUTHORIZATION_REQUIREMENT",
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
)


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _request(
    *,
    interaction_id: str = "INT-M6-CHARACTERIZATION",
    source_mode: str = "BASELINE",
    temporal_mode: TemporalMode = TemporalMode.CURRENT,
    as_of: str = JUNE_15,
) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id=interaction_id,
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        as_of=as_of,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        source_mode=source_mode,
        temporal_mode=temporal_mode,
    )


def _payer_result(bundle: EvidenceBundle):
    return next(
        result
        for result in bundle.retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )


def _payer_authorizations(
    bundle: EvidenceBundle, request: RetrievalRequest
) -> tuple[DecisionAssertion, ...]:
    return tuple(
        assertion
        for assertion in normalize_evidence(bundle, request)
        if assertion.decision_type is DecisionType.COVERAGE_AUTHORIZATION
        and assertion.source_type is SourceType.PAYER_POLICY
    )


def _knowledge(settings: Settings) -> KnowledgeService:
    return KnowledgeService(KnowledgeRepository(settings.database_path))


def _payer_governed(assertions):
    return tuple(
        assertion
        for assertion in assertions
        if assertion.document_version_id in {V1, V2}
    )


def _approve_v2(settings: Settings) -> None:
    initial = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        settings,
        initial["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def _set_retrieved_payer_conclusion(
    settings: Settings,
    *,
    required: bool,
    recorded_at: str | None = None,
) -> None:
    """Change only the isolated test database, never fixtures or production code."""

    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute(
            "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
            ("EV-SYN-POL-V1-PA-001",),
        ).fetchone()
        payload = json.loads(row[0])
        payload["prior_authorization_required"] = required
        connection.execute(
            "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
            (json.dumps(payload, sort_keys=True), "EV-SYN-POL-V1-PA-001"),
        )
        if recorded_at is not None:
            connection.execute(
                "UPDATE source_document_version SET recorded_at = ? "
                "WHERE document_version_id = ?",
                (recorded_at, V1),
            )


def _submit_pending_v2(settings: Settings) -> dict[str, Any]:
    initial = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    return demo.submit_feedback(
        settings,
        initial["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )


def test_current_agreement_is_independent_but_not_explicit_corroboration(
    initialized_settings: Settings,
) -> None:
    request = _request()
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    source = _payer_authorizations(bundle, request)
    governed = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    result = reason(bundle, request)

    assert len(source) == 1 and source[0].value is True
    assert len(governed) == 1 and governed[0].value is True
    assert source[0].evidence_ids == ("EV-SYN-POL-V1-PA-001",)
    assert governed[0].assertion_id == V1_PA
    assert result.confidence.label is ConfidenceLabel.HIGH
    assert {finding.finding_type for finding in result.findings} == {
        FindingType.COMPATIBLE_CONSTRAINT
    }
    assert "CORROBORATED" not in FindingType.__members__

    # CURRENT BEHAVIOR: reason() receives retrieved evidence, not governed.
    assert tuple(parameter.name for parameter in fields(DecisionAssertion)) == (
        "decision_type",
        "value",
        "scope",
        "source_type",
        "evidence_ids",
        "authority_scope",
    )
    # FUTURE M6 EXPECTATION: same-scope agreement becomes explicit
    # CORROBORATED while preserving both provenance chains.


def test_current_source_knowledge_disagreement_has_no_first_class_finding(
    initialized_settings: Settings,
) -> None:
    _set_retrieved_payer_conclusion(initialized_settings, required=False)
    request = _request(interaction_id="INT-M6-DISAGREEMENT")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    source = _payer_authorizations(bundle, request)
    governed = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    result = reason(bundle, request)

    assert len(source) == 1 and source[0].value is False
    assert len(governed) == 1 and governed[0].value is True
    # CURRENT BEHAVIOR: the baseline formulary still says PA=true, so the
    # retrieved payer/formulary pair produces today's source/source conflict.
    assert [finding.finding_type for finding in result.findings] == [
        FindingType.SAME_DIMENSION_DISAGREEMENT
    ]
    assert result.confidence.label is ConfidenceLabel.LOW
    assert result.escalation.requires_escalation is True
    assert (
        EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT
        in result.escalation.triggers
    )
    assert "SOURCE_KNOWLEDGE_DISAGREEMENT" not in FindingType.__members__
    # FUTURE M6 EXPECTATION: preserve SourceObservation and
    # GovernedBaselineAssertion and report explicit disagreement with no winner.


def test_newer_source_disagreement_does_not_update_stale_governed_knowledge(
    initialized_settings: Settings,
) -> None:
    newer_recorded_at = "2026-06-20T12:05:00Z"
    _set_retrieved_payer_conclusion(
        initialized_settings,
        required=False,
        recorded_at=newer_recorded_at,
    )
    request = _request(interaction_id="INT-M6-STALE-KNOWLEDGE")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    source_item = bundle.evidence_by_id["EV-SYN-POL-V1-PA-001"]
    repository = KnowledgeRepository(initialized_settings.database_path)
    baseline = repository.get_assertion(V1_PA)
    provenance = repository.get_assertion_provenance(V1_PA)
    result = reason(bundle, request)

    assert baseline is not None and provenance is not None
    assert source_item.structured_data["prior_authorization_required"] is False
    assert baseline.value is True
    # Freshness is characterized with persisted recorded metadata, not labels.
    assert source_item.document_recorded_at == newer_recorded_at
    assert source_item.document_recorded_at > baseline.recorded_at
    assert source_item.evidence_id == "EV-SYN-POL-V1-PA-001"
    assert provenance.assertion.assertion_id == V1_PA
    assert result.confidence.label is ConfidenceLabel.LOW
    assert "STALE_KNOWLEDGE_DISAGREEMENT" not in FindingType.__members__
    unchanged = repository.get_assertion(V1_PA)
    assert unchanged is not None
    assert unchanged.value is True
    assert unchanged.state is KnowledgeAssertionState.APPLIED
    # FUTURE M6 EXPECTATION: the newer source remains an observation; it does
    # not become a knowledge update and both provenance chains remain visible.


def test_source_present_without_matching_governed_assertion_remains_usable(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "DELETE FROM assertion_evidence WHERE assertion_id = ?", (V1_PA,)
        )
        connection.execute("DELETE FROM knowledge_assertion WHERE assertion_id = ?", (V1_PA,))

    request = _request(interaction_id="INT-M6-SOURCE-ONLY")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    source = _payer_authorizations(bundle, request)
    governed = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    result = reason(bundle, request)

    assert len(source) == 1 and source[0].value is True
    assert governed == ()
    assert result.confidence.label is ConfidenceLabel.HIGH
    assert result.escalation.requires_escalation is False
    assert "SOURCE_ONLY" not in FindingType.__members__
    # FUTURE M6 EXPECTATION: classify SOURCE_ONLY without fabricating governance.


def test_source_unavailable_does_not_use_present_knowledge_as_fallback(
    initialized_settings: Settings,
) -> None:
    request = _request(
        interaction_id="INT-M6-KNOWLEDGE-PRESENT",
        source_mode="PAYER_POLICY_UNAVAILABLE",
    )
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    governed = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    result = reason(bundle, request)

    assert _payer_result(bundle).status is RetrievalStatus.SOURCE_UNAVAILABLE
    assert len(governed) == 1 and governed[0].assertion_id == V1_PA
    assert _payer_authorizations(bundle, request) == ()
    assert result.confidence.label is ConfidenceLabel.LOW
    assert result.escalation.requires_escalation is True
    assert EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE in result.escalation.triggers
    assert "KNOWLEDGE_ONLY" not in FindingType.__members__
    # FUTURE M6 EXPECTATION: knowledge may be displayed only as an explicitly
    # unverified governed baseline; LOW and escalation remain mandatory.


def test_source_unavailable_and_knowledge_missing_is_insufficient(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "DELETE FROM assertion_evidence WHERE assertion_id = ?", (V1_PA,)
        )
        connection.execute("DELETE FROM knowledge_assertion WHERE assertion_id = ?", (V1_PA,))

    request = _request(
        interaction_id="INT-M6-BOTH-MISSING",
        source_mode="PAYER_POLICY_UNAVAILABLE",
    )
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    result = reason(bundle, request)

    assert _payer_result(bundle).status is RetrievalStatus.SOURCE_UNAVAILABLE
    assert _payer_authorizations(bundle, request) == ()
    assert _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    ) == ()
    assert result.confidence.label is ConfidenceLabel.LOW
    assert result.escalation.requires_escalation is True
    assert EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE in result.escalation.triggers
    assert "MISSING_SOURCE_CHANNEL" not in FindingType.__members__
    # FUTURE M6 EXPECTATION: comparison may describe the missing source and
    # absent knowledge channels without inventing either conclusion.


def test_candidate_v2_is_not_authority_and_v1_behavior_remains_current(
    initialized_settings: Settings,
) -> None:
    feedback = _submit_pending_v2(initialized_settings)
    repository = KnowledgeRepository(initialized_settings.database_path)
    v2 = repository.get_assertion(V2_PA)
    current = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    request = _request(interaction_id="INT-M6-CANDIDATE")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    result = reason(bundle, request)

    assert feedback["status"] == "PENDING"
    assert v2 is not None and v2.state is KnowledgeAssertionState.CANDIDATE
    assert tuple(assertion.assertion_id for assertion in current) == (V1_PA,)
    assert _payer_result(bundle).document_version_ids == (V1,)
    assert _payer_authorizations(bundle, request)[0].value is True
    assert result.confidence.label is ConfidenceLabel.HIGH
    # FUTURE M6 EXPECTATION: V2 is GOVERNANCE_PENDING, never authority.


def test_after_approval_current_selects_only_v2_across_existing_channels(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    repository = KnowledgeRepository(initialized_settings.database_path)
    request = _request(interaction_id="INT-M6-CURRENT-V2", as_of=JULY_3)
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    current = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    result = reason(bundle, request)

    v1 = repository.get_assertion(V1_PA)
    assert v1 is not None and v1.state is KnowledgeAssertionState.SUPERSEDED
    assert tuple(assertion.assertion_id for assertion in current) == (V2_PA,)
    assert all(
        assertion.state is KnowledgeAssertionState.APPLIED for assertion in current
    )
    assert _payer_result(bundle).document_version_ids == (V2,)
    assert {item.document_version_id for item in bundle.evidence_by_id.values() if item.source_type is SourceType.PAYER_POLICY} == {V2}
    assert result.confidence.label is ConfidenceLabel.HIGH


def test_after_approval_as_of_june_keeps_superseded_v1_historically_eligible(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    request = _request(
        interaction_id="INT-M6-AS-OF-V1",
        temporal_mode=TemporalMode.AS_OF,
        as_of=JUNE_15,
    )
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    applicable = _payer_governed(
        _knowledge(initialized_settings).get_applicable_assertions(
            replace(PAYER_SCOPE, as_of=JUNE_15)
        )
    )
    reasoning_bundle = demo._as_of_reasoning_bundle(
        initialized_settings, request, bundle
    )
    result = reason(reasoning_bundle, request)

    assert _payer_result(bundle).document_version_ids == (V1,)
    assert tuple(assertion.assertion_id for assertion in applicable) == (V1_PA,)
    assert applicable[0].state is KnowledgeAssertionState.SUPERSEDED
    assert _payer_authorizations(reasoning_bundle, request)[0].value is True
    assert result.confidence.label is ConfidenceLabel.HIGH
    current = _payer_governed(
        _knowledge(initialized_settings).get_current_applied_assertions(PAYER_SCOPE)
    )
    assert tuple(assertion.assertion_id for assertion in current) == (V2_PA,)


def test_cross_dimension_guideline_support_and_payer_pa_are_compatible(
    initialized_settings: Settings,
) -> None:
    request = _request(interaction_id="INT-M6-CROSS-DIMENSION")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    result = reason(bundle, request)
    compatible = next(
        finding
        for finding in result.findings
        if finding.finding_type is FindingType.COMPATIBLE_CONSTRAINT
    )

    assert {side.decision_type for side in compatible.sides} == {
        DecisionType.CLINICAL_APPROPRIATENESS,
        DecisionType.COVERAGE_AUTHORIZATION,
    }
    assert {side.source_type for side in compatible.sides} == {
        SourceType.GUIDELINE,
        SourceType.PAYER_POLICY,
    }
    assert all(
        finding.finding_type is not FindingType.SAME_DIMENSION_DISAGREEMENT
        for finding in result.findings
    )


def test_retrieval_and_knowledge_provenance_are_distinct_and_reasoning_loses_identity(
    initialized_settings: Settings,
) -> None:
    request = _request(interaction_id="INT-M6-PROVENANCE")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    evidence = bundle.evidence_by_id["EV-SYN-POL-V1-PA-001"]
    provenance = KnowledgeRepository(
        initialized_settings.database_path
    ).get_assertion_provenance(V1_PA)
    transient = _payer_authorizations(bundle, request)[0]

    assert provenance is not None
    assert all(
        (
            evidence.source_id,
            evidence.source_title,
            evidence.version,
            evidence.timestamp,
            evidence.relevant_excerpt,
            evidence.document_version_id,
            evidence.document_recorded_at,
        )
    )
    assert evidence.source_type is SourceType.PAYER_POLICY
    assert provenance.assertion.assertion_id == V1_PA
    assert provenance.assertion.state is KnowledgeAssertionState.APPLIED
    assert provenance.document_version_id == evidence.document_version_id
    assert provenance.evidence_items[0].evidence_id == evidence.evidence_id
    assert not hasattr(transient, "assertion_id")
    assert not hasattr(transient, "knowledge_state")
    assert not hasattr(transient, "origin")
    assert not hasattr(transient, "lineage_id")


def test_schema_v4_snapshot_has_evidence_claims_and_time_but_no_knowledge_roles(
    initialized_settings: Settings,
) -> None:
    payload = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    interaction_id = payload["interaction_id"]
    with database.managed_connection(initialized_settings.database_path) as connection:
        temporal = connection.execute(
            "SELECT temporal_mode, requested_as_of FROM interaction "
            "WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM interaction_evidence WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM supported_claim WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        citation_count = connection.execute(
            "SELECT COUNT(*) FROM citation WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()[0]
        schema = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        snapshot_columns = {
            row[1]
            for table in ("interaction", "interaction_evidence", "supported_claim", "citation")
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    assert temporal == ("CURRENT", None)
    assert evidence_count > 0 and claim_count > 0 and citation_count > 0
    assert "interaction_knowledge_assertion" not in schema
    assert {
        "assertion_id",
        "knowledge_state",
        "knowledge_role",
        "assertion_origin",
    }.isdisjoint(snapshot_columns)
    # FUTURE M6 RELEASE GATE: knowledge must not materially change rendered
    # answers until execution-time assertion selection and roles are snapshotted.


def test_source_knowledge_disagreement_does_not_create_governance_records(
    initialized_settings: Settings,
) -> None:
    _set_retrieved_payer_conclusion(initialized_settings, required=False)
    tables = (
        "feedback",
        "review",
        "assertion_lineage",
        "assertion_supersession",
        "knowledge_update",
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        assertion_before = connection.execute(
            "SELECT state, value_json FROM knowledge_assertion WHERE assertion_id = ?",
            (V1_PA,),
        ).fetchone()

    request = _request(interaction_id="INT-M6-NO-AUTOMATIC-GOVERNANCE")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    result = reason(bundle, request)

    with database.managed_connection(initialized_settings.database_path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        assertion_after = connection.execute(
            "SELECT state, value_json FROM knowledge_assertion WHERE assertion_id = ?",
            (V1_PA,),
        ).fetchone()

    assert result.confidence.label is ConfidenceLabel.LOW
    assert before == after
    assert assertion_after == assertion_before == ("APPLIED", "true")
    assert KnowledgeRepository(initialized_settings.database_path).get_successors(V1_PA) == ()
    # FUTURE M6 EXPECTATION: comparison remains read-only; only the existing
    # human approval workflow may mutate governed knowledge.
