from __future__ import annotations

from dataclasses import fields, replace

import pytest

from backend.knowledge import KnowledgeAssertionState
from backend.reasoning import (
    AssertionOrigin,
    ComparisonOutcome,
    ComparisonResult,
    DecisionScope,
    DecisionType,
    GovernedBaselineAssertion,
    ReasoningInput,
    SourceObservation,
    compare_reasoning_input,
)
from backend.retrieval import (
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
    RetrievalTraceItem,
    SourceType,
)


AS_OF = "2026-06-15T14:00:00Z"
REQUEST = RetrievalRequest(
    interaction_id="INT-M6-COMPARISON",
    intent="MEDICATION_PRIOR_AUTHORIZATION",
    case_id="SYN-CASE-001",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    as_of=AS_OF,
)


def _scope(
    *,
    payer_id: str | None = "SYN-PAYER-NHH",
    plan_id: str | None = "SYN-PLAN-HLP",
    medication_id: str | None = "SYN-MED-VEL",
    indication_id: str | None = "SYN-COND-LDS",
) -> DecisionScope:
    return DecisionScope(
        payer_id=payer_id,
        plan_id=plan_id,
        medication_id=medication_id,
        indication_id=indication_id,
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        as_of=AS_OF,
    )


def _source(
    *,
    observation_id: str = "OBS-PA-1",
    evidence_id: str = "EV-SOURCE-PA-1",
    value: str | bool | int | tuple[str, ...] = True,
    decision_type: DecisionType = DecisionType.COVERAGE_AUTHORIZATION,
    scope: DecisionScope | None = None,
    source_type: SourceType = SourceType.PAYER_POLICY,
    source_id: str = "SRC-SYN-PAYER",
    document_version_id: str = "DV-SOURCE-1",
) -> SourceObservation:
    return SourceObservation(
        observation_id=observation_id,
        decision_type=decision_type,
        value=value,
        normalized_scope=scope or _scope(),
        source_type=source_type,
        evidence_ids=(evidence_id,),
        source_id=source_id,
        document_version_id=document_version_id,
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        temporal_context=REQUEST,
    )


def _governed(
    *,
    assertion_id: str = "KA-PA-1",
    evidence_id: str = "EV-KNOWLEDGE-PA-1",
    value: str | bool | int | tuple[str, ...] = True,
    decision_type: DecisionType = DecisionType.COVERAGE_AUTHORIZATION,
    scope: DecisionScope | None = None,
    source_type: SourceType = SourceType.PAYER_POLICY,
    source_id: str = "SRC-SYN-PAYER",
    document_version_id: str = "DV-KNOWLEDGE-1",
    recorded_at: str = "2026-02-01T00:00:00Z",
) -> GovernedBaselineAssertion:
    return GovernedBaselineAssertion(
        assertion_id=assertion_id,
        state=KnowledgeAssertionState.APPLIED,
        decision_type=decision_type,
        value=value,
        normalized_scope=scope or _scope(),
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        recorded_at=recorded_at,
        evidence_ids=(evidence_id,),
        source_id=source_id,
        source_type=source_type,
        document_id=f"DOC-{document_version_id}",
        document_version_id=document_version_id,
        document_version="synthetic-version",
        document_effective_from="2026-01-01T00:00:00Z",
        document_effective_to=None,
        temporal_context=REQUEST,
    )


def _bundle(
    sources: tuple[SourceObservation, ...] = (),
    *,
    failure_type: SourceType | None = None,
    failure_status: RetrievalStatus = RetrievalStatus.SOURCE_UNAVAILABLE,
    recorded_at: dict[str, str] | None = None,
) -> EvidenceBundle:
    recorded_at = recorded_at or {}
    by_type: dict[SourceType, list[EvidenceItem]] = {}
    for source in sources:
        evidence_id = source.evidence_ids[0]
        by_type.setdefault(source.source_type, []).append(
            EvidenceItem(
                evidence_id=evidence_id,
                source_id=source.source_id,
                source_type=source.source_type,
                source_title=f"Synthetic {source.source_type.value}",
                document_id=f"DOC-{source.document_version_id}",
                document_version_id=source.document_version_id,
                version="synthetic-version",
                timestamp="2026-01-01T00:00:00Z",
                section="Synthetic comparison evidence",
                relevant_excerpt="Synthetic evidence for deterministic comparison.",
                structured_data={},
                document_recorded_at=recorded_at.get(
                    evidence_id, "2026-01-01T00:00:00Z"
                ),
                document_effective_from="2026-01-01T00:00:00Z",
                document_effective_to=None,
            )
        )

    results: list[RetrievalResult] = []
    traces: list[RetrievalTraceItem] = []
    for source_type in sorted(by_type, key=lambda item: item.value):
        items = tuple(by_type[source_type])
        document_ids = tuple(dict.fromkeys(item.document_version_id for item in items))
        results.append(
            RetrievalResult(
                source_type=source_type,
                status=RetrievalStatus.RETRIEVED,
                evidence_items=items,
                document_version_ids=document_ids,
            )
        )
        traces.append(
            RetrievalTraceItem(source_type, f"Synthetic {source_type.value}", RetrievalStatus.RETRIEVED)
        )
    if failure_type is not None:
        results.append(
            RetrievalResult(
                source_type=failure_type,
                status=failure_status,
                failure_reason="Synthetic source channel is unavailable.",
            )
        )
        traces.append(
            RetrievalTraceItem(failure_type, f"Synthetic {failure_type.value}", failure_status)
        )
    return EvidenceBundle.from_results(tuple(results), tuple(traces))


def _input(
    sources: tuple[SourceObservation, ...] = (),
    assertions: tuple[GovernedBaselineAssertion, ...] = (),
    *,
    bundle: EvidenceBundle | None = None,
    pending: tuple[str, ...] = (),
) -> ReasoningInput:
    return ReasoningInput(
        temporal_context=REQUEST,
        evidence_bundle=bundle or _bundle(sources),
        source_observations=sources,
        governed_assertions=assertions,
        pending_assertion_ids=pending,
    )


def test_exact_source_knowledge_corroboration() -> None:
    result = compare_reasoning_input(_input((_source(),), (_governed(),)))

    assert len(result) == 1
    assert result[0].outcome is ComparisonOutcome.CORROBORATION
    assert result[0].origin is AssertionOrigin.CORROBORATED


def test_corroboration_preserves_both_evidence_chains() -> None:
    source = _source(evidence_id="EV-RETRIEVED")
    governed = _governed(evidence_id="EV-INDEPENDENTLY-PERSISTED")

    result = compare_reasoning_input(_input((source,), (governed,)))[0]

    assert result.source_observations == (source,)
    assert result.governed_assertions == (governed,)
    assert result.evidence_ids == ("EV-RETRIEVED", "EV-INDEPENDENTLY-PERSISTED")


def test_source_only_does_not_fabricate_knowledge() -> None:
    result = compare_reasoning_input(_input((_source(),)))[0]

    assert result.outcome is ComparisonOutcome.SOURCE_ONLY
    assert result.origin is AssertionOrigin.SOURCE
    assert result.governed_assertions == ()


def test_knowledge_only_is_not_current_source_verification() -> None:
    result = compare_reasoning_input(_input(assertions=(_governed(),)))[0]

    assert result.outcome is ComparisonOutcome.KNOWLEDGE_ONLY
    assert result.origin is AssertionOrigin.KNOWLEDGE
    assert result.source_observations == ()


def test_same_dimension_boolean_opposition_preserves_both_with_no_winner() -> None:
    source = _source(value=False)
    governed = _governed(value=True)

    result = compare_reasoning_input(_input((source,), (governed,)))[0]

    assert result.outcome is ComparisonOutcome.SAME_DIMENSION_CONFLICT
    assert result.origin is None
    assert result.source_observations == (source,)
    assert result.governed_assertions == (governed,)


def test_unrelated_scope_does_not_conflict() -> None:
    other_plan = replace(_scope(), plan_id="SYN-PLAN-OTHER")
    results = compare_reasoning_input(
        _input((_source(value=False),), (_governed(value=True, scope=other_plan),))
    )

    assert {result.outcome for result in results} == {
        ComparisonOutcome.SOURCE_ONLY,
        ComparisonOutcome.KNOWLEDGE_ONLY,
    }


def test_missing_scope_is_not_filled_from_peer() -> None:
    incomplete = replace(_scope(), plan_id=None)
    results = compare_reasoning_input(
        _input((_source(scope=incomplete),), (_governed(),))
    )

    assert {result.outcome for result in results} == {
        ComparisonOutcome.SOURCE_ONLY,
        ComparisonOutcome.KNOWLEDGE_ONLY,
    }


def test_supported_clinical_and_payer_dimensions_are_compatible() -> None:
    clinical = _source(
        decision_type=DecisionType.CLINICAL_APPROPRIATENESS,
        value="SUPPORTED_OPTION",
        scope=_scope(payer_id=None, plan_id=None),
        source_type=SourceType.GUIDELINE,
        source_id="SRC-SYN-GUIDELINE",
    )
    payer = _governed(value=True)

    result = compare_reasoning_input(_input((clinical,), (payer,)))[0]

    assert result.outcome is ComparisonOutcome.COMPATIBLE_CROSS_DIMENSION_CONSTRAINT
    assert result.origin is None


def test_multiple_independent_comparison_groups() -> None:
    other_scope = replace(_scope(), medication_id="SYN-MED-OTHER")
    sources = (
        _source(observation_id="OBS-B", evidence_id="EV-B", scope=other_scope, value=False),
        _source(observation_id="OBS-A", evidence_id="EV-A", value=True),
    )
    assertions = (
        _governed(assertion_id="KA-B", scope=other_scope, value=True),
        _governed(assertion_id="KA-A", value=True),
    )

    results = compare_reasoning_input(_input(sources, assertions))

    assert [result.outcome for result in results] == [
        ComparisonOutcome.SAME_DIMENSION_CONFLICT,
        ComparisonOutcome.CORROBORATION,
    ]


def test_output_order_is_deterministic_for_reordered_inputs() -> None:
    sources = (
        _source(observation_id="OBS-B", evidence_id="EV-B", scope=replace(_scope(), medication_id="SYN-MED-Z")),
        _source(observation_id="OBS-A", evidence_id="EV-A"),
    )
    assertions = (
        _governed(assertion_id="KA-B", scope=replace(_scope(), medication_id="SYN-MED-Z")),
        _governed(assertion_id="KA-A"),
    )
    bundle = _bundle(sources)

    forward = compare_reasoning_input(_input(sources, assertions, bundle=bundle))
    reverse = compare_reasoning_input(
        _input(tuple(reversed(sources)), tuple(reversed(assertions)), bundle=bundle)
    )

    assert forward == reverse


def test_provenance_distinct_equivalent_inputs_form_one_result() -> None:
    sources = (
        _source(observation_id="OBS-2", evidence_id="EV-2", document_version_id="DV-2"),
        _source(observation_id="OBS-1", evidence_id="EV-1", document_version_id="DV-1"),
    )
    assertions = (
        _governed(assertion_id="KA-2", evidence_id="KEV-2", document_version_id="KDV-2"),
        _governed(assertion_id="KA-1", evidence_id="KEV-1", document_version_id="KDV-1"),
    )

    results = compare_reasoning_input(_input(sources, assertions))

    assert len(results) == 1
    assert tuple(item.observation_id for item in results[0].source_observations) == (
        "OBS-1",
        "OBS-2",
    )
    assert tuple(item.assertion_id for item in results[0].governed_assertions) == (
        "KA-1",
        "KA-2",
    )
    assert set(results[0].evidence_ids) == {"EV-1", "EV-2", "KEV-1", "KEV-2"}


def test_governed_evidence_may_be_outside_current_evidence_bundle() -> None:
    source = _source()
    governed = _governed(evidence_id="EV-HISTORICAL-KNOWLEDGE")

    result = compare_reasoning_input(_input((source,), (governed,)))[0]

    assert "EV-HISTORICAL-KNOWLEDGE" not in _bundle((source,)).evidence_by_id
    assert "EV-HISTORICAL-KNOWLEDGE" in result.evidence_ids


def test_pending_id_never_becomes_governed_authority() -> None:
    results = compare_reasoning_input(
        _input((_source(),), pending=("KA-CANDIDATE-1",))
    )

    assert [result.outcome for result in results] == [ComparisonOutcome.SOURCE_ONLY]
    assert all(result.governed_assertions == () for result in results)
    assert all(result.outcome is not ComparisonOutcome.GOVERNANCE_PENDING for result in results)


def test_comparison_does_not_mutate_inputs_or_governance_metadata() -> None:
    source = _source(value=False)
    governed = _governed(value=True)
    model = _input((source,), (governed,), pending=("KA-CANDIDATE-1",))
    before = (
        model.source_observations,
        model.governed_assertions,
        model.pending_assertion_ids,
        governed.state,
        governed.lineage_ids,
        governed.correction_ids,
    )

    compare_reasoning_input(model)

    assert before == (
        model.source_observations,
        model.governed_assertions,
        model.pending_assertion_ids,
        governed.state,
        governed.lineage_ids,
        governed.correction_ids,
    )


def test_comparison_does_not_calculate_confidence_or_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.reasoning.policies as policies

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("comparison called a live reasoning policy")

    monkeypatch.setattr(policies, "assess_confidence", unexpected)
    monkeypatch.setattr(policies, "decide_escalation", unexpected)

    result = compare_reasoning_input(_input((_source(),), (_governed(),)))

    assert result[0].findings == ()


def test_comparison_contract_has_no_probability_confidence_or_score() -> None:
    names = {item.name for item in fields(ComparisonResult)}

    assert {"probability", "confidence", "score"}.isdisjoint(names)


def test_stale_knowledge_requires_strictly_newer_persisted_recorded_metadata() -> None:
    source = _source(value=False)
    governed = _governed(value=True, recorded_at="2026-02-01T00:00:00Z")
    bundle = _bundle(
        (source,), recorded_at={source.evidence_ids[0]: "2026-03-01T00:00:00Z"}
    )

    result = compare_reasoning_input(_input((source,), (governed,), bundle=bundle))[0]

    assert result.outcome is ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT


@pytest.mark.parametrize(
    "source_recorded_at",
    ["2026-02-01T00:00:00Z", "not-a-timestamp"],
)
def test_unproved_freshness_remains_same_dimension_conflict(
    source_recorded_at: str,
) -> None:
    source = _source(value=False)
    governed = _governed(value=True, recorded_at="2026-02-01T00:00:00Z")
    bundle = _bundle(
        (source,), recorded_at={source.evidence_ids[0]: source_recorded_at}
    )

    result = compare_reasoning_input(_input((source,), (governed,), bundle=bundle))[0]

    assert result.outcome is ComparisonOutcome.SAME_DIMENSION_CONFLICT


def test_explicit_failed_retrieval_produces_missing_source_channel() -> None:
    governed = _governed()
    bundle = _bundle(failure_type=SourceType.PAYER_POLICY)

    result = compare_reasoning_input(_input(assertions=(governed,), bundle=bundle))[0]

    assert result.outcome is ComparisonOutcome.MISSING_SOURCE_CHANNEL
    assert result.origin is AssertionOrigin.KNOWLEDGE


def test_contract_gaps_do_not_invent_pending_or_malformed_knowledge_outcomes() -> None:
    model = _input(pending=("KA-CANDIDATE-UNSCOPED",))

    results = compare_reasoning_input(model)

    assert results == ()
    assert all(
        result.outcome
        not in {
            ComparisonOutcome.GOVERNANCE_PENDING,
            ComparisonOutcome.MALFORMED_KNOWLEDGE_CHANNEL,
        }
        for result in results
    )
