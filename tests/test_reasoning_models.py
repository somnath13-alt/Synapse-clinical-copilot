from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, fields

import pytest

from backend.reasoning import (
    ConfidenceAssessment,
    ConfidenceFactors,
    ConfidenceLabel,
    DecisionAssertion,
    DecisionScope,
    DecisionType,
    EscalationDecision,
    EscalationTrigger,
    FindingType,
    ReasoningFinding,
    ReasoningResult,
    ResolutionState,
    Severity,
)
from backend.retrieval import SourceType


def scope() -> DecisionScope:
    return DecisionScope(
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        effective_from="2026-01-01T00:00:00Z",
        effective_to="2026-06-30T23:59:59Z",
        as_of="2026-06-15T14:00:00Z",
    )


def assertion() -> DecisionAssertion:
    return DecisionAssertion(
        decision_type=DecisionType.COVERAGE_AUTHORIZATION,
        value=True,
        scope=scope(),
        source_type=SourceType.PAYER_POLICY,
        evidence_ids=["EV-SYN-POL-V1-PA-001"],  # type: ignore[arg-type]
        authority_scope="coverage and authorization",
    )


def finding() -> ReasoningFinding:
    side = assertion()
    return ReasoningFinding(
        finding_type=FindingType.COMPATIBLE_CONSTRAINT,
        severity=Severity.INFORMATIONAL,
        resolution_state=ResolutionState.NOT_APPLICABLE,
        sides=[side],  # type: ignore[arg-type]
        evidence_ids=["EV-SYN-POL-V1-PA-001"],  # type: ignore[arg-type]
        explanation_code="DIFFERENT_AUTHORITY_SCOPES",
        explanation="Clinical appropriateness and authorization are separate dimensions.",
    )


def confidence() -> ConfidenceAssessment:
    return ConfidenceAssessment(
        label=ConfidenceLabel.HIGH,
        factors=ConfidenceFactors(
            source_availability="all required sources available",
            relevance="exact scope",
            agreement_or_conflict="compatible constraint",
            freshness="current as of question",
            required_context_complete="complete",
            provenance_complete="complete",
        ),
        rationale="Required evidence is available with complete provenance.",
        policy_version_id="CONF-PA-SYN-V1",
    )


def escalation() -> EscalationDecision:
    return EscalationDecision(
        requires_escalation=False,
        triggers=[],  # type: ignore[arg-type]
        reviewer=None,
        requested_action=None,
    )


@pytest.mark.parametrize(
    "instance",
    [
        scope(),
        assertion(),
        finding(),
        confidence().factors,
        confidence(),
        escalation(),
    ],
)
def test_contracts_are_immutable(instance: object) -> None:
    with pytest.raises((FrozenInstanceError, AttributeError)):
        setattr(instance, fields(instance)[0].name, None)


def test_sequence_fields_are_normalized_to_tuples() -> None:
    decision = assertion()
    reasoning_finding = finding()
    escalation_decision = EscalationDecision(
        requires_escalation=True,
        triggers=[EscalationTrigger.LOW_CONFIDENCE],  # type: ignore[arg-type]
        reviewer="Synthetic coverage-policy reviewer",
        requested_action="Review the evidence.",
    )
    result = ReasoningResult(
        findings=[reasoning_finding],  # type: ignore[arg-type]
        confidence=confidence(),
        escalation=escalation_decision,
    )

    assert decision.evidence_ids == ("EV-SYN-POL-V1-PA-001",)
    assert reasoning_finding.sides == (decision,)
    assert reasoning_finding.evidence_ids == ("EV-SYN-POL-V1-PA-001",)
    assert escalation_decision.triggers == (EscalationTrigger.LOW_CONFIDENCE,)
    assert result.findings == (reasoning_finding,)


def test_decision_assertion_requires_evidence_ids() -> None:
    with pytest.raises(ValueError, match="at least one evidence ID"):
        DecisionAssertion(
            decision_type=DecisionType.CLINICAL_APPROPRIATENESS,
            value="SUPPORTED",
            scope=scope(),
            source_type=SourceType.GUIDELINE,
            evidence_ids=(),
            authority_scope="clinical appropriateness",
        )


def test_reasoning_finding_retains_explicit_evidence_ids() -> None:
    evidence_ids = ("EV-SYN-GUIDE-SUPPORT-001", "EV-SYN-POL-V1-PA-001")
    reasoning_finding = ReasoningFinding(
        finding_type=FindingType.COMPATIBLE_CONSTRAINT,
        severity=Severity.INFORMATIONAL,
        resolution_state=ResolutionState.NOT_APPLICABLE,
        sides=(assertion(),),
        evidence_ids=evidence_ids,
        explanation_code="DIFFERENT_AUTHORITY_SCOPES",
        explanation="The evidence addresses different decision dimensions.",
    )

    assert reasoning_finding.evidence_ids == evidence_ids


def test_confidence_labels_are_categorical() -> None:
    assert {label.value for label in ConfidenceLabel} == {"HIGH", "MEDIUM", "LOW"}


def test_confidence_factors_have_no_probability_field() -> None:
    assert {field.name for field in fields(ConfidenceFactors)} == {
        "source_availability",
        "relevance",
        "agreement_or_conflict",
        "freshness",
        "required_context_complete",
        "provenance_complete",
    }


def test_escalation_triggers_are_explicit() -> None:
    assert {trigger.value for trigger in EscalationTrigger} == {
        "LOW_CONFIDENCE",
        "CRITICAL_SOURCE_UNAVAILABLE",
        "INSUFFICIENT_EVIDENCE",
        "UNRESOLVED_HIGH_SEVERITY_CONFLICT",
    }


def test_reasoning_result_is_immutable() -> None:
    result = ReasoningResult((finding(),), confidence(), escalation())

    with pytest.raises(FrozenInstanceError):
        result.confidence = confidence()  # type: ignore[misc]


def test_decision_assertion_uses_retrieval_source_type() -> None:
    decision = assertion()

    assert decision.source_type is SourceType.PAYER_POLICY


def test_contract_construction_performs_no_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"database access attempted: {args!r} {kwargs!r}")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)

    result = ReasoningResult((finding(),), confidence(), escalation())
    assert result.findings
