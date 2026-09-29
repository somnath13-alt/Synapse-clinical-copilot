"""M6.3c1 characterization of the future live dual-input orchestration seam.

The builder in this module is deliberately test-only.  Production continues to
send an ``EvidenceBundle`` to source-only reasoning until execution-time
knowledge participation can be preserved by the interaction snapshot.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    AssertionProvenance,
    KnowledgeAssertion,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.reasoning import (
    KNOWLEDGE_AWARE_POLICY_VERSION_ID,
    POLICY_VERSION_ID,
    ComparisonOutcome,
    ConfidenceLabel,
    DecisionScope,
    DecisionType,
    EscalationTrigger,
    GovernedBaselineAssertion,
    ReasoningInput,
    SourceObservation,
    assess_knowledge_aware_confidence,
    compare_reasoning_input,
    decide_knowledge_aware_escalation,
    normalize_evidence,
    reason,
)
from backend.retrieval import (
    EvidenceBundle,
    RetrievalRequest,
    RetrievalService,
    SourceType,
    TemporalMode,
)


JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
QUESTION = demo.CANONICAL_QUESTION

PAYER_AUTHORIZATION_QUERY = KnowledgeQuery(
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
    interaction_id: str,
    *,
    source_mode: str = "BASELINE",
    temporal_mode: TemporalMode = TemporalMode.CURRENT,
    as_of: str = JUNE_15,
) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id=interaction_id,
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        source_mode=source_mode,
        temporal_mode=temporal_mode,
        as_of=as_of,
    )


def _source_observations(
    bundle: EvidenceBundle, request: RetrievalRequest
) -> tuple[SourceObservation, ...]:
    """Project observations for the payer-governed assertion family only.

    A formulary authorization statement is a distinct source channel.  Feeding
    it into the payer-policy comparison family would let it mask an unavailable
    payer channel as corroboration.
    """

    observations: list[SourceObservation] = []
    for assertion in normalize_evidence(bundle, request):
        if (
            assertion.decision_type is not DecisionType.COVERAGE_AUTHORIZATION
            or assertion.source_type is not SourceType.PAYER_POLICY
        ):
            continue
        evidence = bundle.evidence_by_id[assertion.evidence_ids[0]]
        observations.append(
            SourceObservation(
                observation_id=(
                    f"OBS-{evidence.evidence_id}-{assertion.decision_type.value}"
                ),
                decision_type=assertion.decision_type,
                value=assertion.value,
                normalized_scope=assertion.scope,
                source_type=assertion.source_type,
                evidence_ids=assertion.evidence_ids,
                source_id=evidence.source_id,
                document_version_id=evidence.document_version_id,
                effective_from=evidence.document_effective_from,
                effective_to=evidence.document_effective_to,
                temporal_context=request,
            )
        )
    return tuple(observations)


def _decision_type(assertion: KnowledgeAssertion) -> DecisionType:
    if assertion.decision_dimension == "AUTHORIZATION_REQUIREMENT":
        return DecisionType.COVERAGE_AUTHORIZATION
    raise AssertionError(
        f"Test seam does not map decision dimension {assertion.decision_dimension!r}"
    )


def _governed_view(
    service: KnowledgeService,
    assertion: KnowledgeAssertion,
    provenance: AssertionProvenance,
    request: RetrievalRequest,
) -> GovernedBaselineAssertion:
    scope = assertion.normalized_scope
    assert scope is not None
    lineage = (
        *service.get_predecessors(assertion.assertion_id),
        *service.get_successors(assertion.assertion_id),
    )
    return GovernedBaselineAssertion(
        assertion_id=assertion.assertion_id,
        state=assertion.state,
        decision_type=_decision_type(assertion),
        value=assertion.value,
        normalized_scope=DecisionScope(
            payer_id=scope.get("payer_id"),
            plan_id=scope.get("plan_id"),
            medication_id=scope.get("medication_id"),
            indication_id=scope.get("condition_id"),
            effective_from=assertion.effective_from,
            effective_to=assertion.effective_to,
            as_of=request.as_of,
        ),
        effective_from=assertion.effective_from,
        effective_to=assertion.effective_to,
        recorded_at=assertion.recorded_at,
        evidence_ids=assertion.evidence_ids,
        source_id=provenance.source_id,
        source_type=provenance.source_type,
        document_id=provenance.document_id,
        document_version_id=provenance.document_version_id,
        document_version=provenance.document_version,
        document_effective_from=provenance.document_effective_from,
        document_effective_to=provenance.document_effective_to,
        temporal_context=request,
        lineage_ids=tuple(dict.fromkeys(edge.lineage_id for edge in lineage)),
        correction_ids=tuple(dict.fromkeys(edge.feedback_id for edge in lineage)),
    )


def _future_reasoning_input(
    settings: Settings,
    request: RetrievalRequest,
    bundle: EvidenceBundle,
    *,
    source_observations: tuple[SourceObservation, ...] | None = None,
    governed_assertions: tuple[GovernedBaselineAssertion, ...] | None = None,
) -> ReasoningInput:
    """Construct the future peer inputs without modifying the live path."""

    service = KnowledgeService(KnowledgeRepository(settings.database_path))
    query = replace(PAYER_AUTHORIZATION_QUERY, as_of=request.as_of)
    selected = (
        service.get_applicable_assertions(query)
        if request.temporal_mode is TemporalMode.AS_OF
        else service.get_current_applied_assertions(
            replace(PAYER_AUTHORIZATION_QUERY, as_of=None)
        )
    )
    governed: list[GovernedBaselineAssertion] = []
    for assertion in selected:
        provenance = service.get_provenance(assertion.assertion_id)
        assert provenance is not None
        if provenance.source_type is not SourceType.PAYER_POLICY:
            continue
        governed.append(_governed_view(service, assertion, provenance, request))
    return ReasoningInput(
        temporal_context=request,
        evidence_bundle=bundle,
        source_observations=(
            _source_observations(bundle, request)
            if source_observations is None
            else source_observations
        ),
        governed_assertions=(
            tuple(governed)
            if governed_assertions is None
            else governed_assertions
        ),
    )


def _comparison_for_payer(
    comparisons: tuple[Any, ...], outcome: ComparisonOutcome
):
    return next(
        item
        for item in comparisons
        if item.outcome is outcome
        and any(
            value.source_type is SourceType.PAYER_POLICY
            for value in (*item.source_observations, *item.governed_assertions)
        )
    )


def _knowledge_policy(reasoning_input: ReasoningInput):
    comparisons = compare_reasoning_input(reasoning_input)
    source_result = reason(
        reasoning_input.evidence_bundle, reasoning_input.temporal_context
    )
    confidence = assess_knowledge_aware_confidence(
        reasoning_input.evidence_bundle,
        source_result.findings,
        comparisons,
        reasoning_input.temporal_context,
    )
    escalation = decide_knowledge_aware_escalation(
        reasoning_input.evidence_bundle,
        confidence,
        source_result.findings,
        comparisons,
        reasoning_input.temporal_context,
    )
    return source_result, comparisons, confidence, escalation


def _approve_v2(settings: Settings) -> None:
    answer = demo.ask_question(settings, QUESTION)
    feedback = demo.submit_feedback(
        settings,
        answer["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def _set_source_authorization(settings: Settings, required: bool) -> None:
    """Alter only the isolated test database so source policy is internally coherent."""

    ids = ("EV-SYN-POL-V1-PA-001", "EV-SYN-FORM-STATUS-001")
    with database.managed_connection(settings.database_path) as connection:
        for evidence_id in ids:
            row = connection.execute(
                "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["prior_authorization_required"] = required
            connection.execute(
                "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
                (json.dumps(payload, sort_keys=True), evidence_id),
            )


def _governance_counts(settings: Settings) -> dict[str, int]:
    tables = (
        "feedback",
        "review",
        "assertion_lineage",
        "assertion_supersession",
        "knowledge_update",
    )
    with database.managed_connection(settings.database_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


def test_live_current_path_invokes_dual_input_policy_without_changing_answer_shape(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = demo.compare_reasoning_input

    def tracked(reasoning_input: ReasoningInput):
        nonlocal calls
        calls += 1
        return original(reasoning_input)

    monkeypatch.setattr(demo, "compare_reasoning_input", tracked)
    response = client.post("/api/v1/questions", json={"question": QUESTION})

    assert response.status_code == 200
    payload = response.json()
    assert payload["confidence"] == "HIGH"
    assert payload["policy_version_id"] == V1
    assert calls == 1
    assert {citation["document_version_id"] for citation in payload["citations"] if citation["source_type"] == "PAYER_POLICY"} == {V1}
    assert KNOWLEDGE_AWARE_POLICY_VERSION_ID == "CONF-PA-SYN-V2"
    assert POLICY_VERSION_ID == "CONF-PA-SYN-V1"


def test_live_inputs_can_be_composed_at_the_post_selection_boundary(
    initialized_settings: Settings,
) -> None:
    request = _request("INT-M63C1-LIVE")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    live_source_result = reason(bundle, request)
    dual_input = _future_reasoning_input(initialized_settings, request, bundle)

    assert bundle.evidence_by_id
    assert dual_input.evidence_bundle is bundle
    assert any(
        item.source_type is SourceType.PAYER_POLICY
        and item.document_version_id == V1
        for item in dual_input.source_observations
    )
    assert any(item.assertion_id == V1_PA for item in dual_input.governed_assertions)
    assert live_source_result.confidence.policy_version_id == POLICY_VERSION_ID
    assert live_source_result.confidence.label is ConfidenceLabel.HIGH


def test_canonical_agreement_corroborates_without_confidence_inflation(
    initialized_settings: Settings,
) -> None:
    request = _request("INT-M63C1-AGREEMENT")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    dual_input = _future_reasoning_input(initialized_settings, request, bundle)
    source_result, comparisons, confidence, escalation = _knowledge_policy(dual_input)
    corroboration = _comparison_for_payer(
        comparisons, ComparisonOutcome.CORROBORATION
    )

    assert source_result.confidence.label is ConfidenceLabel.HIGH
    assert confidence.label is source_result.confidence.label
    assert confidence.policy_version_id == KNOWLEDGE_AWARE_POLICY_VERSION_ID
    assert escalation.requires_escalation is False
    assert {item.document_version_id for item in corroboration.source_observations if item.source_type is SourceType.PAYER_POLICY} == {V1}
    assert {item.assertion_id for item in corroboration.governed_assertions if item.source_type is SourceType.PAYER_POLICY} == {V1_PA}


def test_disagreement_is_low_escalated_read_only_and_preserves_both_chains(
    initialized_settings: Settings,
) -> None:
    _set_source_authorization(initialized_settings, required=False)
    before = _governance_counts(initialized_settings)
    request = _request("INT-M63C1-CONFLICT")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    dual_input = _future_reasoning_input(initialized_settings, request, bundle)
    source_result, comparisons, confidence, escalation = _knowledge_policy(dual_input)
    conflict = _comparison_for_payer(
        comparisons, ComparisonOutcome.SAME_DIMENSION_CONFLICT
    )

    assert source_result.confidence.label is ConfidenceLabel.HIGH
    assert confidence.label is ConfidenceLabel.LOW
    assert escalation.requires_escalation is True
    assert EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT in escalation.triggers
    assert {item.value for item in conflict.source_observations} == {False}
    assert {item.value for item in conflict.governed_assertions} == {True}
    assert conflict.origin is None
    assert conflict.evidence_ids
    assert before == _governance_counts(initialized_settings)


def test_unavailable_source_keeps_governed_baseline_unverified_and_escalates(
    initialized_settings: Settings,
) -> None:
    request = _request(
        "INT-M63C1-UNAVAILABLE", source_mode="PAYER_POLICY_UNAVAILABLE"
    )
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    dual_input = _future_reasoning_input(initialized_settings, request, bundle)
    _, comparisons, confidence, escalation = _knowledge_policy(dual_input)
    missing = _comparison_for_payer(
        comparisons, ComparisonOutcome.MISSING_SOURCE_CHANNEL
    )

    assert not missing.source_observations
    assert {item.assertion_id for item in missing.governed_assertions} == {V1_PA}
    assert confidence.label is ConfidenceLabel.LOW
    assert escalation.requires_escalation is True
    assert EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE in escalation.triggers
    assert "does not verify the current source" in confidence.rationale


def test_source_only_does_not_lower_source_confidence(
    initialized_settings: Settings,
) -> None:
    request = _request("INT-M63C1-SOURCE-ONLY")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    dual_input = _future_reasoning_input(
        initialized_settings, request, bundle, governed_assertions=()
    )
    source_result, comparisons, confidence, escalation = _knowledge_policy(dual_input)

    _comparison_for_payer(comparisons, ComparisonOutcome.SOURCE_ONLY)
    assert confidence.label is source_result.confidence.label is ConfidenceLabel.HIGH
    assert escalation.requires_escalation is False


def test_knowledge_only_is_not_retrieval_fallback_and_escalates(
    initialized_settings: Settings,
) -> None:
    request = _request("INT-M63C1-KNOWLEDGE-ONLY")
    bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
    selected = _future_reasoning_input(initialized_settings, request, bundle)
    payer_governed = tuple(
        item
        for item in selected.governed_assertions
        if item.source_type is SourceType.PAYER_POLICY
    )
    dual_input = _future_reasoning_input(
        initialized_settings,
        request,
        bundle,
        source_observations=(),
        governed_assertions=payer_governed,
    )
    _, comparisons, confidence, escalation = _knowledge_policy(dual_input)
    knowledge_only = _comparison_for_payer(
        comparisons, ComparisonOutcome.KNOWLEDGE_ONLY
    )

    assert not knowledge_only.source_observations
    assert {item.assertion_id for item in knowledge_only.governed_assertions} == {V1_PA}
    assert confidence.label is ConfidenceLabel.LOW
    assert escalation.requires_escalation is True
    assert EscalationTrigger.INSUFFICIENT_EVIDENCE in escalation.triggers
    assert "unverified governed baseline" in confidence.rationale


def test_as_of_dual_input_selection_aligns_v1_then_v2_after_approval(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    selected: list[tuple[set[str], set[str]]] = []
    for label, instant, version, assertion_id in (
        ("JUNE", JUNE_15, V1, V1_PA),
        ("JULY", JULY_3, V2, V2_PA),
    ):
        request = _request(
            f"INT-M63C1-AS-OF-{label}",
            temporal_mode=TemporalMode.AS_OF,
            as_of=instant,
        )
        bundle = RetrievalService(initialized_settings.database_path).retrieve(request)
        dual_input = _future_reasoning_input(initialized_settings, request, bundle)
        comparisons = compare_reasoning_input(dual_input)
        corroboration = _comparison_for_payer(
            comparisons, ComparisonOutcome.CORROBORATION
        )
        source_versions = {
            item.document_version_id
            for item in corroboration.source_observations
            if item.source_type is SourceType.PAYER_POLICY
        }
        assertion_ids = {
            item.assertion_id
            for item in corroboration.governed_assertions
            if item.source_type is SourceType.PAYER_POLICY
        }
        assert source_versions == {version}
        assert assertion_ids == {assertion_id}
        selected.append((source_versions, assertion_ids))

    assert selected == [({V1}, {V1_PA}), ({V2}, {V2_PA})]


def test_schema_v6_preserves_snapshotted_live_material_knowledge_use(
    initialized_settings: Settings,
) -> None:
    payload = demo.ask_question(initialized_settings, QUESTION)
    with database.managed_connection(initialized_settings.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        snapshot_columns = {
            row[1]
            for table in (
                "interaction",
                "interaction_evidence",
                "supported_claim",
                "citation",
            )
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        confidence_policy_id = connection.execute(
            "SELECT confidence_policy_id FROM interaction WHERE interaction_id = ?",
            (payload["interaction_id"],),
        ).fetchone()[0]

    assert "interaction_knowledge_assertion" not in tables
    assert {
        "assertion_id",
        "knowledge_state",
        "knowledge_role",
        "assertion_origin",
        "assertion_value",
        "assertion_scope",
        "assertion_effective_from",
        "assertion_effective_to",
    }.isdisjoint(snapshot_columns)
    assert {"interaction_knowledge", "interaction_knowledge_evidence"} <= tables
    assert confidence_policy_id == KNOWLEDGE_AWARE_POLICY_VERSION_ID
