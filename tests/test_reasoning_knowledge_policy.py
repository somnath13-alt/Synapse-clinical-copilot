from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from backend.knowledge import KnowledgeAssertionState
from backend.reasoning import (
    KNOWLEDGE_AWARE_POLICY_VERSION_ID,
    POLICY_VERSION_ID,
    ComparisonOutcome,
    ComparisonResult,
    ConfidenceLabel,
    DecisionScope,
    DecisionType,
    EscalationTrigger,
    GovernedBaselineAssertion,
    SourceObservation,
    assess_knowledge_aware_confidence,
    decide_knowledge_aware_escalation,
    reason,
    reconcile,
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
START = "2026-01-01T00:00:00Z"
REQUEST = RetrievalRequest(
    interaction_id="INT-M6-KNOWLEDGE-POLICY",
    intent="MEDICATION_PRIOR_AUTHORIZATION",
    case_id="SYN-CASE-001",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    as_of=AS_OF,
)


def _evidence(
    evidence_id: str,
    source_type: SourceType,
    structured_data: dict[str, Any],
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id=f"SRC-SYN-{source_type.value}",
        source_type=source_type,
        source_title=f"SYNTHETIC — {source_type.value}",
        document_id=f"DOC-{evidence_id}",
        document_version_id=f"DV-{evidence_id}",
        version="synthetic-v1",
        timestamp=START,
        section="synthetic/test",
        relevant_excerpt="SYNTHETIC — deterministic policy evidence.",
        structured_data=structured_data,
        document_recorded_at=START,
        document_effective_from=START,
        document_effective_to=None,
    )


def _result(
    source_type: SourceType,
    items: tuple[EvidenceItem, ...] = (),
    status: RetrievalStatus = RetrievalStatus.RETRIEVED,
) -> RetrievalResult:
    if status is not RetrievalStatus.RETRIEVED:
        return RetrievalResult(
            source_type,
            status,
            failure_reason="Synthetic source unavailable.",
        )
    return RetrievalResult(
        source_type,
        status,
        items,
        tuple(item.document_version_id for item in items),
    )


def _bundle(
    *,
    guideline_available: bool = True,
    payer_status: RetrievalStatus = RetrievalStatus.RETRIEVED,
) -> EvidenceBundle:
    ehr_case = _evidence(
        "EV-EHR-CASE",
        SourceType.EHR,
        {
            "case_id": REQUEST.case_id,
            "requested_medication_id": REQUEST.medication_id,
            "condition_id": REQUEST.indication_id,
        },
    )
    ehr_plan = _evidence(
        "EV-EHR-PLAN",
        SourceType.EHR,
        {"payer_id": REQUEST.payer_id, "plan_id": REQUEST.plan_id},
    )
    guideline = _evidence(
        "EV-GUIDELINE",
        SourceType.GUIDELINE,
        {
            "condition_id": REQUEST.indication_id,
            "medication_id": REQUEST.medication_id,
            "recommendation": "SUPPORTED_OPTION",
            "decision_dimension": "CLINICAL_APPROPRIATENESS",
        },
    )
    payer = _evidence(
        "EV-PAYER",
        SourceType.PAYER_POLICY,
        {
            "payer_id": REQUEST.payer_id,
            "plan_id": REQUEST.plan_id,
            "condition_id": REQUEST.indication_id,
            "medication_id": REQUEST.medication_id,
            "prior_authorization_required": True,
            "decision_dimension": "AUTHORIZATION_REQUIREMENT",
        },
    )
    formulary = replace(
        payer,
        evidence_id="EV-FORMULARY",
        source_id="SRC-SYN-FORMULARY",
        source_type=SourceType.FORMULARY,
        source_title="SYNTHETIC — FORMULARY",
        document_id="DOC-EV-FORMULARY",
        document_version_id="DV-EV-FORMULARY",
    )
    results = (
        _result(SourceType.EHR, (ehr_case, ehr_plan)),
        (
            _result(SourceType.GUIDELINE, (guideline,))
            if guideline_available
            else _result(SourceType.GUIDELINE, status=RetrievalStatus.NOT_FOUND)
        ),
        (
            _result(SourceType.PAYER_POLICY, (payer,))
            if payer_status is RetrievalStatus.RETRIEVED
            else _result(SourceType.PAYER_POLICY, status=payer_status)
        ),
        _result(SourceType.FORMULARY, (formulary,)),
    )
    trace = tuple(
        RetrievalTraceItem(item.source_type, item.source_type.value, item.status)
        for item in results
    )
    return EvidenceBundle.from_results(results, trace)


def _scope(*, payer: bool = True) -> DecisionScope:
    return DecisionScope(
        payer_id=REQUEST.payer_id if payer else None,
        plan_id=REQUEST.plan_id if payer else None,
        medication_id=REQUEST.medication_id,
        indication_id=REQUEST.indication_id,
        effective_from=START,
        effective_to=None,
        as_of=AS_OF,
    )


def _source(
    *,
    value: str | bool = True,
    source_type: SourceType = SourceType.PAYER_POLICY,
    decision_type: DecisionType = DecisionType.COVERAGE_AUTHORIZATION,
    payer_scope: bool = True,
) -> SourceObservation:
    return SourceObservation(
        observation_id=f"OBS-{source_type.value}-{value}",
        decision_type=decision_type,
        value=value,
        normalized_scope=_scope(payer=payer_scope),
        source_type=source_type,
        evidence_ids=("EV-PAYER",),
        source_id=f"SRC-SYN-{source_type.value}",
        document_version_id="DV-SOURCE",
        effective_from=START,
        effective_to=None,
        temporal_context=REQUEST,
    )


def _governed(
    *,
    value: str | bool = True,
    source_type: SourceType = SourceType.PAYER_POLICY,
    decision_type: DecisionType = DecisionType.COVERAGE_AUTHORIZATION,
    payer_scope: bool = True,
) -> GovernedBaselineAssertion:
    return GovernedBaselineAssertion(
        assertion_id=f"KA-{source_type.value}-{value}",
        state=KnowledgeAssertionState.APPLIED,
        decision_type=decision_type,
        value=value,
        normalized_scope=_scope(payer=payer_scope),
        effective_from=START,
        effective_to=None,
        recorded_at=START,
        evidence_ids=("EV-KNOWLEDGE",),
        source_id=f"SRC-SYN-{source_type.value}",
        source_type=source_type,
        document_id="DOC-KNOWLEDGE",
        document_version_id="DV-KNOWLEDGE",
        document_version="synthetic-v1",
        document_effective_from=START,
        document_effective_to=None,
        temporal_context=REQUEST,
    )


def _assessment(
    comparisons: tuple[ComparisonResult, ...],
    *,
    bundle: EvidenceBundle | None = None,
):
    value = bundle or _bundle()
    return assess_knowledge_aware_confidence(
        value,
        reconcile(value, REQUEST),
        comparisons,
        REQUEST,
    )


def _escalation(
    comparisons: tuple[ComparisonResult, ...],
    *,
    bundle: EvidenceBundle | None = None,
):
    value = bundle or _bundle()
    findings = reconcile(value, REQUEST)
    confidence = assess_knowledge_aware_confidence(
        value, findings, comparisons, REQUEST
    )
    return decide_knowledge_aware_escalation(
        value, confidence, findings, comparisons, REQUEST
    )


def test_policy_identifiers_are_stable_and_distinct() -> None:
    assert KNOWLEDGE_AWARE_POLICY_VERSION_ID == "CONF-PA-SYN-V2"
    assert POLICY_VERSION_ID == "CONF-PA-SYN-V1"


def test_corroboration_preserves_high_and_both_provenance_chains() -> None:
    comparison = ComparisonResult(
        ComparisonOutcome.CORROBORATION, (_source(),), (_governed(),)
    )

    confidence = _assessment((comparison,))

    assert confidence.label is ConfidenceLabel.HIGH
    assert confidence.policy_version_id == KNOWLEDGE_AWARE_POLICY_VERSION_ID
    assert comparison.evidence_ids == ("EV-PAYER", "EV-KNOWLEDGE")
    assert "both provenance chains" in confidence.rationale


def test_corroboration_cannot_manufacture_high_from_incomplete_source_evidence() -> None:
    comparison = ComparisonResult(
        ComparisonOutcome.CORROBORATION, (_source(),), (_governed(),)
    )

    confidence = _assessment((comparison,), bundle=_bundle(guideline_available=False))

    assert confidence.label is ConfidenceLabel.MEDIUM


def test_source_only_does_not_lower_confidence_for_absent_knowledge() -> None:
    confidence = _assessment(
        (ComparisonResult(ComparisonOutcome.SOURCE_ONLY, (_source(),)),)
    )

    assert confidence.label is ConfidenceLabel.HIGH
    assert "no comparable governed baseline" in confidence.rationale


def test_critical_payer_knowledge_only_is_low_and_escalates() -> None:
    comparison = ComparisonResult(
        ComparisonOutcome.KNOWLEDGE_ONLY,
        governed_assertions=(_governed(),),
    )

    confidence = _assessment((comparison,))
    escalation = _escalation((comparison,))

    assert confidence.label is ConfidenceLabel.LOW
    assert "not verified" in confidence.rationale
    assert escalation.requires_escalation
    assert EscalationTrigger.INSUFFICIENT_EVIDENCE in escalation.triggers


def test_noncritical_knowledge_only_uses_source_policy_deterministically() -> None:
    governed = _governed(
        value="SUPPORTED_OPTION",
        source_type=SourceType.GUIDELINE,
        decision_type=DecisionType.CLINICAL_APPROPRIATENESS,
        payer_scope=False,
    )
    comparison = ComparisonResult(
        ComparisonOutcome.KNOWLEDGE_ONLY,
        governed_assertions=(governed,),
    )

    assert _assessment((comparison,)).label is ConfidenceLabel.HIGH
    assert not _escalation((comparison,)).requires_escalation


def test_missing_critical_payer_source_with_baseline_is_low_and_escalates() -> None:
    comparison = ComparisonResult(
        ComparisonOutcome.MISSING_SOURCE_CHANNEL,
        governed_assertions=(_governed(),),
    )

    unavailable = _bundle(payer_status=RetrievalStatus.SOURCE_UNAVAILABLE)
    confidence = _assessment((comparison,), bundle=unavailable)
    escalation = _escalation((comparison,), bundle=unavailable)

    assert confidence.label is ConfidenceLabel.LOW
    assert "does not verify the current source" in confidence.rationale
    assert EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE in escalation.triggers


@pytest.mark.parametrize(
    "outcome",
    [
        ComparisonOutcome.SAME_DIMENSION_CONFLICT,
        ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT,
    ],
)
def test_payer_disagreement_is_low_and_requires_conflict_escalation(
    outcome: ComparisonOutcome,
) -> None:
    comparison = ComparisonResult(outcome, (_source(value=False),), (_governed(),))

    confidence = _assessment((comparison,))
    escalation = _escalation((comparison,))

    assert confidence.label is ConfidenceLabel.LOW
    assert escalation.requires_escalation
    assert (
        EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT
        in escalation.triggers
    )
    assert "winner" in (escalation.requested_action or "")


def test_compatible_cross_dimension_does_not_create_conflict_escalation() -> None:
    clinical = _source(
        value="SUPPORTED_OPTION",
        source_type=SourceType.GUIDELINE,
        decision_type=DecisionType.CLINICAL_APPROPRIATENESS,
        payer_scope=False,
    )
    comparison = ComparisonResult(
        ComparisonOutcome.COMPATIBLE_CROSS_DIMENSION_CONSTRAINT,
        (clinical,),
        (_governed(),),
    )

    confidence = _assessment((comparison,))
    escalation = _escalation((comparison,))

    assert confidence.label is ConfidenceLabel.HIGH
    assert not escalation.requires_escalation
    assert "not a conflict" in confidence.rationale


def test_policy_has_no_winner_or_governance_mutation_contract() -> None:
    source = _source(value=False)
    governed = _governed()
    comparison = ComparisonResult(
        ComparisonOutcome.SAME_DIMENSION_CONFLICT, (source,), (governed,)
    )
    before = (source, governed, comparison)

    _assessment((comparison,))
    _escalation((comparison,))

    assert (source, governed, comparison) == before
    assert "winner" not in {field.name for field in fields(comparison)}


def test_pending_and_malformed_knowledge_are_non_authoritative_and_unscoped() -> None:
    pending = ComparisonResult(
        ComparisonOutcome.GOVERNANCE_PENDING,
        pending_assertion_ids=("KA-PENDING",),
    )
    malformed = ComparisonResult(ComparisonOutcome.MALFORMED_KNOWLEDGE_CHANNEL)

    confidence = _assessment((pending, malformed))

    assert confidence.label is ConfidenceLabel.HIGH
    assert "pending knowledge is non-authoritative" in confidence.rationale
    assert "does not identify whether that channel is required" in confidence.rationale


def test_policy_outputs_only_public_labels_and_no_numeric_probability() -> None:
    assessment = _assessment(
        (ComparisonResult(ComparisonOutcome.SOURCE_ONLY, (_source(),)),)
    )
    serialized = repr(assessment).lower()

    assert {label.value for label in ConfidenceLabel} == {"HIGH", "MEDIUM", "LOW"}
    assert "probability" not in serialized
    assert "score" not in serialized


def test_policy_is_deterministic_and_does_not_mutate_inputs() -> None:
    comparisons = (
        ComparisonResult(ComparisonOutcome.SOURCE_ONLY, (_source(),)),
        ComparisonResult(
            ComparisonOutcome.CORROBORATION, (_source(),), (_governed(),)
        ),
    )
    before = repr(comparisons)

    first = _assessment(comparisons)
    second = _assessment(tuple(reversed(comparisons)))

    assert first == second
    assert repr(comparisons) == before


def test_policy_is_service_database_and_filesystem_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"external access attempted: {args!r} {kwargs!r}")

    monkeypatch.setattr(sqlite3, "connect", fail)
    monkeypatch.setattr(Path, "open", fail)
    comparison = ComparisonResult(
        ComparisonOutcome.CORROBORATION, (_source(),), (_governed(),)
    )

    assert _assessment((comparison,)) == _assessment((comparison,))


def test_existing_v1_policy_and_reason_path_are_unchanged() -> None:
    result = reason(_bundle(), REQUEST)

    assert result.confidence.label is ConfidenceLabel.HIGH
    assert result.confidence.policy_version_id == "CONF-PA-SYN-V1"
    assert POLICY_VERSION_ID == "CONF-PA-SYN-V1"
