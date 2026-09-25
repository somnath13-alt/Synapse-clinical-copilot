"""Cross-layer characterization for governed temporal selection.

These tests keep CURRENT, AS_OF, and historical interaction snapshots distinct
while exercising retrieval, persisted knowledge, and the existing reasoning
pipeline against the same synthetic database state.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import KnowledgeQuery, KnowledgeRepository, KnowledgeService
from backend.reasoning import (
    ConfidenceLabel,
    EscalationTrigger,
    FindingType,
    ResolutionState,
    Severity,
    normalize_evidence,
    reason,
)
from backend.reasoning.reconciliation import assertion_applies
from backend.retrieval import (
    EvidenceBundle,
    RetrievalRequest,
    RetrievalResult,
    RetrievalService,
    RetrievalStatus,
    SourceType,
    TemporalMode,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
PAYER_VERSION_IDS = frozenset({V1, V2})
V1_ASSERTIONS = ("AST-SYN-POL-V1-PA", "AST-SYN-POL-V1-STEP")
V2_ASSERTIONS = ("AST-SYN-POL-V2-PA", "AST-SYN-POL-V2-STEP")
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
PAYER_SCOPE = KnowledgeQuery(
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
    temporal_mode: TemporalMode,
    as_of: str | None = None,
) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-SYN-TEMPORAL-INTEGRATION",
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        temporal_mode=temporal_mode,
        as_of=as_of,
    )


def _payer_result(bundle: EvidenceBundle) -> RetrievalResult:
    return next(
        result
        for result in bundle.retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )


def _knowledge(settings: Settings) -> KnowledgeService:
    return KnowledgeService(KnowledgeRepository(settings.database_path))


def _payer_assertions_current(settings: Settings) -> tuple[Any, ...]:
    return tuple(
        assertion
        for assertion in _knowledge(settings).get_current_applied_assertions(PAYER_SCOPE)
        if assertion.document_version_id in PAYER_VERSION_IDS
    )


def _payer_assertions_as_of(settings: Settings, as_of: str) -> tuple[Any, ...]:
    return tuple(
        assertion
        for assertion in _knowledge(settings).get_applicable_assertions(
            replace(PAYER_SCOPE, as_of=as_of)
        )
        if assertion.document_version_id in PAYER_VERSION_IDS
    )


def _submit_v2(settings: Settings, interaction_id: str | None = None) -> dict[str, Any]:
    if interaction_id is None:
        interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
        interaction_id = interaction["interaction_id"]
    return demo.submit_feedback(
        settings,
        interaction_id,
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )


def _approve_v2(settings: Settings, interaction_id: str | None = None) -> None:
    feedback = _submit_v2(settings, interaction_id)
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def _version_ids(assertions: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.document_version_id for item in assertions))


def _assertion_ids(assertions: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(item.assertion_id for item in assertions)


def _assert_cross_layer_provenance(
    settings: Settings,
    payer_result: RetrievalResult,
    assertions: tuple[Any, ...],
) -> None:
    service = _knowledge(settings)
    retrieved_by_id = {item.evidence_id: item for item in payer_result.evidence_items}
    asserted_evidence_ids = {
        evidence_id
        for assertion in assertions
        for evidence_id in assertion.evidence_ids
    }

    assert asserted_evidence_ids == set(retrieved_by_id)
    for assertion in assertions:
        provenance = service.get_provenance(assertion.assertion_id)
        assert provenance is not None
        assert provenance.document_version_id == assertion.document_version_id
        assert provenance.document_version_id in payer_result.document_version_ids
        for evidence in provenance.evidence_items:
            retrieved = retrieved_by_id[evidence.evidence_id]
            assert evidence.document_version_id == retrieved.document_version_id
            assert evidence.document_id == retrieved.document_id
            assert evidence.source_id == retrieved.source_id
            assert evidence.source_type is retrieved.source_type
            assert evidence.source_title == retrieved.source_title


def test_current_retrieval_and_knowledge_move_from_v1_to_v2_only_after_approval(
    initialized_settings: Settings,
) -> None:
    service = RetrievalService(initialized_settings.database_path)
    current_request = _request(temporal_mode=TemporalMode.CURRENT)

    before = _payer_result(service.retrieve(current_request))
    before_knowledge = _payer_assertions_current(initialized_settings)
    feedback = _submit_v2(initialized_settings)
    pending = _payer_result(service.retrieve(current_request))
    pending_knowledge = _payer_assertions_current(initialized_settings)

    assert before.document_version_ids == (V1,)
    assert _version_ids(before_knowledge) == (V1,)
    assert _assertion_ids(before_knowledge) == V1_ASSERTIONS
    assert feedback["status"] == "PENDING"
    assert pending.document_version_ids == (V1,)
    assert pending_knowledge == before_knowledge

    demo.approve_feedback(
        initialized_settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )
    after = _payer_result(service.retrieve(current_request))
    after_knowledge = _payer_assertions_current(initialized_settings)

    assert after.document_version_ids == (V2,)
    assert _version_ids(after_knowledge) == (V2,)
    assert _assertion_ids(after_knowledge) == V2_ASSERTIONS


@pytest.mark.parametrize(
    ("as_of", "expected_version", "expected_assertions"),
    [
        pytest.param(JUNE_15, V1, V1_ASSERTIONS, id="approved-june-v1"),
        pytest.param(JULY_3, V2, V2_ASSERTIONS, id="approved-july-v2"),
    ],
)
def test_approved_as_of_retrieval_knowledge_reasoning_and_provenance_align(
    initialized_settings: Settings,
    as_of: str,
    expected_version: str,
    expected_assertions: tuple[str, ...],
) -> None:
    _approve_v2(initialized_settings)
    request = _request(temporal_mode=TemporalMode.AS_OF, as_of=as_of)
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    payer = _payer_result(bundle)
    assertions = _payer_assertions_as_of(initialized_settings, as_of)
    payer_reasoning_assertions = tuple(
        assertion
        for assertion in normalize_evidence(bundle, request)
        if assertion.source_type is SourceType.PAYER_POLICY
    )
    reasoning = reason(bundle, request)

    assert payer.document_version_ids == (expected_version,)
    assert _version_ids(assertions) == (expected_version,)
    assert _assertion_ids(assertions) == expected_assertions
    assert payer_reasoning_assertions
    assert all(assertion_applies(assertion) for assertion in payer_reasoning_assertions)
    assert reasoning.confidence.label is not ConfidenceLabel.LOW
    assert not reasoning.escalation.requires_escalation
    _assert_cross_layer_provenance(initialized_settings, payer, assertions)


@pytest.mark.parametrize(
    ("as_of", "expected_status", "expected_version", "expected_assertions"),
    [
        pytest.param(
            JUNE_15,
            RetrievalStatus.RETRIEVED,
            (V1,),
            V1_ASSERTIONS,
            id="pending-june-v1",
        ),
        pytest.param(
            JULY_3,
            RetrievalStatus.NOT_FOUND,
            (),
            (),
            id="pending-july-not-found",
        ),
    ],
)
def test_pending_v2_is_excluded_from_as_of_retrieval_and_knowledge(
    initialized_settings: Settings,
    as_of: str,
    expected_status: RetrievalStatus,
    expected_version: tuple[str, ...],
    expected_assertions: tuple[str, ...],
) -> None:
    feedback = _submit_v2(initialized_settings)
    request = _request(temporal_mode=TemporalMode.AS_OF, as_of=as_of)
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    payer = _payer_result(bundle)
    assertions = _payer_assertions_as_of(initialized_settings, as_of)

    assert feedback["status"] == "PENDING"
    assert payer.status is expected_status
    assert payer.document_version_ids == expected_version
    assert _assertion_ids(assertions) == expected_assertions
    assert _version_ids(assertions) == expected_version

    if as_of == JULY_3:
        reasoning = reason(bundle, request)
        assert reasoning.confidence.label is ConfidenceLabel.LOW
        assert reasoning.escalation.requires_escalation
        assert (
            EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE
            in reasoning.escalation.triggers
        )


def test_historical_snapshot_keeps_v1_without_invoking_live_layers(
    initialized_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    _approve_v2(initialized_settings, original["interaction_id"])

    class UnexpectedService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("historical loading must not construct a live service")

    def unexpected_reasoning(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("historical loading must not rerun reasoning")

    monkeypatch.setattr(demo, "RetrievalService", UnexpectedService)
    monkeypatch.setattr(demo, "KnowledgeService", UnexpectedService)
    monkeypatch.setattr(demo, "reason", unexpected_reasoning)
    historical = demo.get_interaction(
        initialized_settings,
        original["interaction_id"],
    )

    assert historical is not None
    assert historical["policy_version_id"] == V1
    assert historical["answer"] == original["answer"]
    assert historical["claims"] == original["claims"]
    assert historical["confidence"] == original["confidence"]
    assert {
        citation["document_version_id"]
        for citation in historical["citations"]
        if citation["source_type"] == SourceType.PAYER_POLICY.value
    } == {V1}


def test_overlap_returns_every_version_and_assertion_and_reasoning_keeps_conflict(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
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
            "UPDATE knowledge_assertion SET value_json = 'false' WHERE assertion_id = ?",
            ("AST-SYN-POL-V2-PA",),
        )
        connection.execute(
            "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
            (json.dumps(opposing_evidence, sort_keys=True), "EV-SYN-POL-V2-PA-001"),
        )

    request = _request(temporal_mode=TemporalMode.AS_OF, as_of=JUNE_15)
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    payer = _payer_result(bundle)
    assertions = _payer_assertions_as_of(initialized_settings, JUNE_15)
    reasoning = reason(bundle, request)
    conflicts = tuple(
        finding
        for finding in reasoning.findings
        if finding.finding_type is FindingType.SAME_DIMENSION_DISAGREEMENT
    )

    assert payer.document_version_ids == (V1, V2)
    assert _version_ids(assertions) == (V1, V2)
    assert _assertion_ids(assertions) == (*V1_ASSERTIONS, *V2_ASSERTIONS)
    _assert_cross_layer_provenance(initialized_settings, payer, assertions)
    assert conflicts
    assert all(finding.severity is Severity.HIGH for finding in conflicts)
    assert all(
        finding.resolution_state is ResolutionState.UNRESOLVED
        for finding in conflicts
    )
    assert reasoning.confidence.label is ConfidenceLabel.LOW
    assert reasoning.escalation.requires_escalation
    assert (
        EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT
        in reasoning.escalation.triggers
    )


def test_same_approved_state_separates_current_v2_from_as_of_june_v1(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    service = RetrievalService(initialized_settings.database_path)

    current = _payer_result(
        service.retrieve(_request(temporal_mode=TemporalMode.CURRENT))
    )
    june = _payer_result(
        service.retrieve(
            _request(temporal_mode=TemporalMode.AS_OF, as_of=JUNE_15)
        )
    )
    current_knowledge = _payer_assertions_current(initialized_settings)
    june_knowledge = _payer_assertions_as_of(initialized_settings, JUNE_15)

    assert current.document_version_ids == (V2,)
    assert _version_ids(current_knowledge) == (V2,)
    assert june.document_version_ids == (V1,)
    assert _version_ids(june_knowledge) == (V1,)
