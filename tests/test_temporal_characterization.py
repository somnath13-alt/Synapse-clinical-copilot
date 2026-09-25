"""Characterize v1.3 temporal behavior before selector implementation.

CURRENT and AS_OF are intentionally distinct terms here. HISTORICAL INTERACTION
SNAPSHOT display is supported; independent deterministic replay is not fully
self-contained and is not exercised by these tests.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import KnowledgeAssertionState, KnowledgeRepository
from backend.reasoning import ConfidenceLabel, DecisionType, EscalationTrigger, reason
from backend.reasoning.reconciliation import assertion_applies, normalize_evidence
from backend.retrieval import (
    PayerPolicyAdapter,
    RetrievalRequest,
    RetrievalService,
    SourceType,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _request(*, as_of: str = JUNE_15) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-SYN-TEMPORAL-CHARACTERIZATION",
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        as_of=as_of,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
    )


def _payer_result(bundle: Any) -> Any:
    return next(
        result
        for result in bundle.retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )


def _submit_v2(settings: Settings, interaction_id: str) -> dict[str, Any]:
    return demo.submit_feedback(
        settings,
        interaction_id,
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )


def _approve_v2(settings: Settings, feedback_id: str) -> None:
    demo.approve_feedback(
        settings,
        feedback_id,
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def test_current_query_uses_current_version_before_pending_and_after_approval(
    initialized_settings: Settings,
) -> None:
    """CURRENT has v1.3 current-version semantics when no mode is provided."""

    initial = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    assert initial["policy_version_id"] == V1

    feedback = _submit_v2(initialized_settings, initial["interaction_id"])
    pending = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request()
    )
    repository = KnowledgeRepository(initialized_settings.database_path)

    assert feedback["status"] == "PENDING"
    assert pending.document_version_ids == (V1,)
    assert repository.get_assertion(V1_PA).state is KnowledgeAssertionState.APPLIED  # type: ignore[union-attr]
    assert repository.get_assertion(V2_PA).state is KnowledgeAssertionState.CANDIDATE  # type: ignore[union-attr]

    _approve_v2(initialized_settings, feedback["feedback_id"])
    current = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)

    assert current["policy_version_id"] == V2
    assert repository.get_assertion(V1_PA).state is KnowledgeAssertionState.SUPERSEDED  # type: ignore[union-attr]
    assert repository.get_assertion(V2_PA).state is KnowledgeAssertionState.APPLIED  # type: ignore[union-attr]


def test_as_of_is_ignored_and_reasoning_rejects_future_current_v2_known_limitation(
    initialized_settings: Settings,
) -> None:
    """KNOWN LIMITATION: AS_OF does not control v1.3 retrieval selection."""

    request = _request(as_of=JUNE_15)
    service = RetrievalService(initialized_settings.database_path)
    before = _payer_result(service.retrieve(request))
    with database.managed_connection(initialized_settings.database_path) as connection:
        before_versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()

    assert before_versions == [(V1, 1, "APPLIED"), (V2, 0, "CANDIDATE_NOT_CURRENT")]
    assert before.document_version_ids == (V1,)

    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = _submit_v2(initialized_settings, interaction["interaction_id"])
    _approve_v2(initialized_settings, feedback["feedback_id"])

    after_bundle = service.retrieve(request)
    after = _payer_result(after_bundle)
    reasoning = reason(after_bundle, request)
    payer_assertions = tuple(
        assertion
        for assertion in normalize_evidence(after_bundle, request)
        if assertion.source_type is SourceType.PAYER_POLICY
        and assertion.decision_type is DecisionType.COVERAGE_AUTHORIZATION
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        after_versions = connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version
               WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()

    assert request.as_of == JUNE_15
    assert after_versions == [(V1, 0, "APPLIED"), (V2, 1, "APPLIED")]
    assert after.document_version_ids == (V2,)
    assert payer_assertions
    assert not any(assertion_applies(assertion) for assertion in payer_assertions)
    assert reasoning.confidence.label is ConfidenceLabel.LOW
    assert (
        reasoning.confidence.factors.freshness
        == "authoritative payer applicability is not established"
    )
    assert reasoning.escalation.requires_escalation
    assert EscalationTrigger.INSUFFICIENT_EVIDENCE in reasoning.escalation.triggers

    # Future expectation only: AS_OF June 15 should eventually select V1 in
    # retrieval. This characterization deliberately does not assert that result.


def test_historical_interaction_snapshot_display_does_not_retrieve_or_replay(
    initialized_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    snapshot_keys = (
        "answer",
        "claims",
        "citations",
        "confidence",
        "confidence_rationale",
        "reconciliation",
        "escalation",
        "policy_version_id",
    )
    original_snapshot = {key: original[key] for key in snapshot_keys}
    feedback = _submit_v2(initialized_settings, original["interaction_id"])
    _approve_v2(initialized_settings, feedback["feedback_id"])

    class UnexpectedRetrievalService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("historical display must not run CURRENT or AS_OF retrieval")

    monkeypatch.setattr(demo, "RetrievalService", UnexpectedRetrievalService)
    historical = demo.get_interaction(initialized_settings, original["interaction_id"])

    assert historical is not None
    assert {key: historical[key] for key in snapshot_keys} == original_snapshot
    assert historical["policy_version_id"] == V1
    assert {
        citation["document_version_id"]
        for citation in historical["citations"]
        if citation["source_type"] == SourceType.PAYER_POLICY.value
    } == {V1}


def test_effective_intervals_are_exposed_and_reasoning_uses_closed_boundaries(
    initialized_settings: Settings,
) -> None:
    repository = KnowledgeRepository(initialized_settings.database_path)
    v1_assertion = repository.get_assertion(V1_PA)
    v2_assertion = repository.get_assertion(V2_PA)
    v1_document = repository.get_assertion_provenance(V1_PA)
    v2_document = repository.get_assertion_provenance(V2_PA)

    assert v1_assertion is not None and v2_assertion is not None
    assert v1_document is not None and v2_document is not None
    assert (v1_assertion.effective_from, v1_assertion.effective_to) == (
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
    )
    assert (v2_assertion.effective_from, v2_assertion.effective_to) == (
        "2026-07-01T00:00:00Z",
        None,
    )
    assert (
        v1_document.document_effective_from,
        v1_document.document_effective_to,
    ) == ("2026-01-01T00:00:00Z", "2026-06-30T23:59:59Z")
    assert (
        v2_document.document_effective_from,
        v2_document.document_effective_to,
    ) == ("2026-07-01T00:00:00Z", None)

    service = RetrievalService(initialized_settings.database_path)
    v1_bundle = service.retrieve(_request(as_of=JUNE_15))
    v1_payer_assertions = tuple(
        assertion
        for assertion in normalize_evidence(v1_bundle, _request(as_of=JUNE_15))
        if assertion.source_type is SourceType.PAYER_POLICY
    )
    assert v1_payer_assertions
    assert all(assertion_applies(assertion) for assertion in v1_payer_assertions)

    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    feedback = _submit_v2(initialized_settings, interaction["interaction_id"])
    _approve_v2(initialized_settings, feedback["feedback_id"])
    july_request = replace(_request(), as_of=JULY_3)
    v2_bundle = service.retrieve(july_request)
    v2_payer_assertions = tuple(
        assertion
        for assertion in normalize_evidence(v2_bundle, july_request)
        if assertion.source_type is SourceType.PAYER_POLICY
    )

    assert _payer_result(v2_bundle).document_version_ids == (V2,)
    assert v2_payer_assertions
    assert all(assertion_applies(assertion) for assertion in v2_payer_assertions)
    assert not any(
        assertion_applies(assertion)
        for assertion in normalize_evidence(v2_bundle, _request(as_of=JUNE_15))
        if assertion.source_type is SourceType.PAYER_POLICY
    )

    # Characterization only: these seeded document and assertion intervals are
    # observed data, not a new general interval-containment invariant.
