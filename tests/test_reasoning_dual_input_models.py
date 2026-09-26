from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace

import pytest

from backend.knowledge import KnowledgeAssertionState
from backend.reasoning import (
    AssertionOrigin,
    ComparisonOutcome,
    ComparisonResult,
    DecisionAssertion,
    DecisionScope,
    DecisionType,
    GovernedBaselineAssertion,
    ReasoningInput,
    SourceObservation,
)
from backend.retrieval import (
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
    RetrievalTraceItem,
    SourceType,
    TemporalMode,
)


AS_OF = "2026-06-15T14:00:00Z"
EVIDENCE_ID = "EV-SYN-POL-V1-PA-001"


def temporal_context() -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-M6-DUAL-INPUT",
        intent="MEDICATION_PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        temporal_mode=TemporalMode.AS_OF,
        as_of=AS_OF,
    )


def normalized_scope() -> DecisionScope:
    return DecisionScope(
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        effective_from="2026-01-01T00:00:00Z",
        effective_to="2026-06-30T23:59:59Z",
        as_of=AS_OF,
    )


def evidence_bundle() -> EvidenceBundle:
    item = EvidenceItem(
        evidence_id=EVIDENCE_ID,
        source_id="SRC-SYN-PAYER",
        source_type=SourceType.PAYER_POLICY,
        source_title="Synthetic payer policy",
        document_id="DOC-SYN-POL",
        document_version_id="DV-SYN-POL-V1",
        version="1.0",
        timestamp="2026-01-01T00:00:00Z",
        section="Prior authorization",
        relevant_excerpt="Synthetic policy requires prior authorization.",
        structured_data={"prior_authorization_required": True},
        document_recorded_at="2026-01-01T00:00:00Z",
        document_effective_from="2026-01-01T00:00:00Z",
        document_effective_to="2026-06-30T23:59:59Z",
    )
    result = RetrievalResult(
        source_type=SourceType.PAYER_POLICY,
        status=RetrievalStatus.RETRIEVED,
        evidence_items=(item,),
        document_version_ids=(item.document_version_id,),
    )
    trace = RetrievalTraceItem(
        source_type=SourceType.PAYER_POLICY,
        display_label="Synthetic payer policy",
        status=RetrievalStatus.RETRIEVED,
    )
    return EvidenceBundle.from_results((result,), (trace,))


def source_observation() -> SourceObservation:
    return SourceObservation(
        observation_id="OBS-SYN-PA-V1",
        decision_type=DecisionType.COVERAGE_AUTHORIZATION,
        value=True,
        normalized_scope=normalized_scope(),
        source_type=SourceType.PAYER_POLICY,
        evidence_ids=[EVIDENCE_ID],  # type: ignore[arg-type]
        source_id="SRC-SYN-PAYER",
        document_version_id="DV-SYN-POL-V1",
        effective_from="2026-01-01T00:00:00Z",
        effective_to="2026-06-30T23:59:59Z",
        temporal_context=temporal_context(),
    )


def governed_assertion() -> GovernedBaselineAssertion:
    return GovernedBaselineAssertion(
        assertion_id="KA-SYN-POL-V1-PA",
        state=KnowledgeAssertionState.SUPERSEDED,
        decision_type=DecisionType.COVERAGE_AUTHORIZATION,
        value=True,
        normalized_scope=normalized_scope(),
        effective_from="2026-01-01T00:00:00Z",
        effective_to="2026-06-30T23:59:59Z",
        recorded_at="2026-01-01T00:05:00Z",
        evidence_ids=[EVIDENCE_ID],  # type: ignore[arg-type]
        source_id="SRC-SYN-PAYER",
        source_type=SourceType.PAYER_POLICY,
        document_id="DOC-SYN-POL",
        document_version_id="DV-SYN-POL-V1",
        document_version="1.0",
        document_effective_from="2026-01-01T00:00:00Z",
        document_effective_to="2026-06-30T23:59:59Z",
        temporal_context=temporal_context(),
        lineage_ids=["LIN-SYN-V1-V2"],  # type: ignore[arg-type]
        correction_ids=["FB-SYN-POL-V2"],  # type: ignore[arg-type]
    )


def reasoning_input() -> ReasoningInput:
    return ReasoningInput(
        temporal_context=temporal_context(),
        evidence_bundle=evidence_bundle(),
        source_observations=[source_observation()],  # type: ignore[arg-type]
        governed_assertions=[governed_assertion()],  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "instance",
    [
        source_observation(),
        governed_assertion(),
        reasoning_input(),
        ComparisonResult(
            ComparisonOutcome.CORROBORATION,
            (source_observation(),),
            (governed_assertion(),),
        ),
    ],
)
def test_dual_input_contracts_are_immutable(instance: object) -> None:
    with pytest.raises((FrozenInstanceError, AttributeError)):
        setattr(instance, fields(instance)[0].name, None)


def test_dual_input_sequences_are_tuple_normalized() -> None:
    source = source_observation()
    governed = governed_assertion()
    model = reasoning_input()

    assert source.evidence_ids == (EVIDENCE_ID,)
    assert governed.evidence_ids == (EVIDENCE_ID,)
    assert governed.lineage_ids == ("LIN-SYN-V1-V2",)
    assert governed.correction_ids == ("FB-SYN-POL-V2",)
    assert model.source_observations == (source,)
    assert model.governed_assertions == (governed,)
    assert model.pending_assertion_ids == ()


def test_origin_terms_are_exact_and_channel_contracts_do_not_imply_precedence() -> None:
    assert {origin.value for origin in AssertionOrigin} == {
        "SOURCE",
        "KNOWLEDGE",
        "CORROBORATED",
    }
    assert source_observation().origin is AssertionOrigin.SOURCE
    assert governed_assertion().origin is AssertionOrigin.KNOWLEDGE


def test_governed_assertion_retains_state_and_complete_lightweight_provenance() -> None:
    assertion = governed_assertion()

    assert assertion.state is KnowledgeAssertionState.SUPERSEDED
    assert assertion.assertion_id == "KA-SYN-POL-V1-PA"
    assert assertion.value is True
    assert assertion.normalized_scope == normalized_scope()
    assert assertion.evidence_ids == (EVIDENCE_ID,)
    assert assertion.source_id == "SRC-SYN-PAYER"
    assert assertion.document_id == "DOC-SYN-POL"
    assert assertion.document_version_id == "DV-SYN-POL-V1"
    assert assertion.document_version == "1.0"
    assert assertion.recorded_at == "2026-01-01T00:05:00Z"
    assert assertion.effective_from == "2026-01-01T00:00:00Z"
    assert assertion.document_effective_from == "2026-01-01T00:00:00Z"
    assert assertion.lineage_ids == ("LIN-SYN-V1-V2",)
    assert assertion.correction_ids == ("FB-SYN-POL-V2",)


def test_temporal_context_is_retained_without_selecting_authority() -> None:
    model = reasoning_input()

    assert model.temporal_context == temporal_context()
    assert model.temporal_mode is TemporalMode.AS_OF
    assert model.as_of == AS_OF
    assert model.source_observations[0].temporal_mode is TemporalMode.AS_OF
    assert model.governed_assertions[0].as_of == AS_OF


def test_source_and_knowledge_evidence_ids_are_retained() -> None:
    model = reasoning_input()

    assert model.source_observations[0].evidence_ids == (EVIDENCE_ID,)
    assert model.governed_assertions[0].evidence_ids == (EVIDENCE_ID,)


def test_new_contracts_have_no_numeric_probability_or_confidence() -> None:
    names = {
        item.name
        for contract in (
            SourceObservation,
            GovernedBaselineAssertion,
            ReasoningInput,
            ComparisonResult,
        )
        for item in fields(contract)
    }

    assert "probability" not in names
    assert "confidence" not in names
    assert "score" not in names


@pytest.mark.parametrize(
    ("instance", "field_name"),
    [
        (source_observation(), "observation_id"),
        (source_observation(), "source_id"),
        (source_observation(), "document_version_id"),
        (governed_assertion(), "assertion_id"),
        (governed_assertion(), "source_id"),
        (governed_assertion(), "document_version_id"),
    ],
)
def test_missing_required_identifiers_are_rejected(
    instance: SourceObservation | GovernedBaselineAssertion,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(instance, **{field_name: ""})


def test_reasoning_input_rejects_unknown_source_evidence() -> None:
    source = replace(source_observation(), evidence_ids=("EV-NOT-RETRIEVED",))
    with pytest.raises(ValueError, match="absent from evidence_bundle"):
        ReasoningInput(temporal_context(), evidence_bundle(), (source,), ())


def test_candidate_is_pending_metadata_not_a_governed_baseline() -> None:
    with pytest.raises(ValueError, match="cannot represent a CANDIDATE"):
        replace(governed_assertion(), state=KnowledgeAssertionState.CANDIDATE)

    model = replace(
        reasoning_input(), pending_assertion_ids=["KA-SYN-POL-V2-PA"]
    )
    assert model.pending_assertion_ids == ("KA-SYN-POL-V2-PA",)


def test_channel_tuple_order_is_deterministic_without_cross_channel_precedence() -> None:
    first_source = source_observation()
    second_source = replace(first_source, observation_id="OBS-SYN-PA-V1-SECOND")
    first_governed = governed_assertion()
    second_governed = replace(
        first_governed, assertion_id="KA-SYN-POL-V1-PA-SECOND"
    )

    model = ReasoningInput(
        temporal_context(),
        evidence_bundle(),
        [second_source, first_source],  # type: ignore[arg-type]
        [second_governed, first_governed],  # type: ignore[arg-type]
    )

    assert tuple(item.observation_id for item in model.source_observations) == (
        "OBS-SYN-PA-V1-SECOND",
        "OBS-SYN-PA-V1",
    )
    assert tuple(item.assertion_id for item in model.governed_assertions) == (
        "KA-SYN-POL-V1-PA-SECOND",
        "KA-SYN-POL-V1-PA",
    )
    assert not hasattr(model, "selection_order")


def test_governed_evidence_need_not_be_in_current_retrieval_bundle() -> None:
    independently_persisted = replace(
        governed_assertion(), evidence_ids=("EV-SYN-POL-HISTORICAL",)
    )

    model = ReasoningInput(
        temporal_context(),
        evidence_bundle(),
        (source_observation(),),
        (independently_persisted,),
    )

    assert model.governed_assertions[0].evidence_ids == (
        "EV-SYN-POL-HISTORICAL",
    )


@pytest.mark.parametrize(
    ("outcome", "sources", "knowledge", "origin"),
    [
        (
            ComparisonOutcome.CORROBORATION,
            (source_observation(),),
            (governed_assertion(),),
            AssertionOrigin.CORROBORATED,
        ),
        (
            ComparisonOutcome.SOURCE_ONLY,
            (source_observation(),),
            (),
            AssertionOrigin.SOURCE,
        ),
        (
            ComparisonOutcome.KNOWLEDGE_ONLY,
            (),
            (governed_assertion(),),
            AssertionOrigin.KNOWLEDGE,
        ),
    ],
)
def test_comparison_result_derives_valid_origin_and_preserves_evidence(
    outcome: ComparisonOutcome,
    sources: tuple[SourceObservation, ...],
    knowledge: tuple[GovernedBaselineAssertion, ...],
    origin: AssertionOrigin,
) -> None:
    result = ComparisonResult(outcome, list(sources), list(knowledge))  # type: ignore[arg-type]

    assert result.origin is origin
    assert result.source_observations == sources
    assert result.governed_assertions == knowledge
    assert result.evidence_ids == (EVIDENCE_ID,)


def test_comparison_result_requires_channels_for_declared_outcome() -> None:
    with pytest.raises(ValueError, match="CORROBORATION"):
        ComparisonResult(ComparisonOutcome.CORROBORATION)
    with pytest.raises(ValueError, match="SOURCE_ONLY"):
        ComparisonResult(
            ComparisonOutcome.SOURCE_ONLY,
            (source_observation(),),
            (governed_assertion(),),
        )
    with pytest.raises(ValueError, match="both input channels"):
        ComparisonResult(
            ComparisonOutcome.SAME_DIMENSION_CONFLICT,
            (source_observation(),),
        )
    with pytest.raises(ValueError, match="cannot contain source"):
        ComparisonResult(
            ComparisonOutcome.MISSING_SOURCE_CHANNEL,
            (source_observation(),),
        )
    with pytest.raises(ValueError, match="pending assertion metadata"):
        ComparisonResult(
            ComparisonOutcome.GOVERNANCE_PENDING,
        )

    pending = ComparisonResult(
        ComparisonOutcome.GOVERNANCE_PENDING,
        pending_assertion_ids=["KA-SYN-POL-V2-PA"],  # type: ignore[arg-type]
    )
    assert pending.origin is None
    assert pending.pending_assertion_ids == ("KA-SYN-POL-V2-PA",)

    with pytest.raises(ValueError, match="require a GOVERNANCE_PENDING outcome"):
        ComparisonResult(
            ComparisonOutcome.SOURCE_ONLY,
            source_observations=(source_observation(),),
            pending_assertion_ids=("KA-SYN-POL-V2-PA",),
        )


def test_comparison_contract_declares_every_frozen_m6_outcome() -> None:
    assert {outcome.value for outcome in ComparisonOutcome} == {
        "CORROBORATION",
        "SOURCE_ONLY",
        "KNOWLEDGE_ONLY",
        "STALE_KNOWLEDGE_DISAGREEMENT",
        "SAME_DIMENSION_CONFLICT",
        "COMPATIBLE_CROSS_DIMENSION_CONSTRAINT",
        "MISSING_SOURCE_CHANNEL",
        "MALFORMED_KNOWLEDGE_CHANNEL",
        "GOVERNANCE_PENDING",
    }


def test_existing_reasoning_assertion_contract_is_unchanged() -> None:
    assert tuple(item.name for item in fields(DecisionAssertion)) == (
        "decision_type",
        "value",
        "scope",
        "source_type",
        "evidence_ids",
        "authority_scope",
    )
