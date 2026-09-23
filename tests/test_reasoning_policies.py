from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from backend.reasoning import (
    ConfidenceLabel,
    DecisionType,
    EscalationTrigger,
    FindingType,
    ResolutionState,
    Severity,
    decide_escalation,
    normalize_evidence,
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
END = "2026-12-31T23:59:59Z"


def context(**changes: str) -> RetrievalRequest:
    values = {
        "interaction_id": "INT-POLICY-TEST",
        "intent": "PRIOR_AUTHORIZATION",
        "case_id": "SYN-CASE-001",
        "as_of": AS_OF,
        "medication_id": "SYN-MED-VEL",
        "indication_id": "SYN-COND-LDS",
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
    }
    values.update(changes)
    return RetrievalRequest(**values)


def evidence(
    evidence_id: str,
    source_type: SourceType,
    structured_data: dict[str, Any],
    *,
    effective_from: str = START,
    effective_to: str | None = END,
    source_title: str | None = None,
    relevant_excerpt: str = "SYNTHETIC — supported structured evidence.",
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id=f"SRC-{source_type.value}",
        source_type=source_type,
        source_title=source_title if source_title is not None else f"SYNTHETIC — {source_type.value}",
        document_id=f"DOC-{evidence_id}",
        document_version_id=f"DV-{evidence_id}",
        version="1",
        timestamp="2026-01-01T00:00:00Z",
        section="structured/test",
        relevant_excerpt=relevant_excerpt,
        structured_data=structured_data,
        document_recorded_at="2026-01-01T00:00:00Z",
        document_effective_from=effective_from,
        document_effective_to=effective_to,
    )


def ehr_items() -> tuple[EvidenceItem, ...]:
    return (
        evidence(
            "EHR-CONTEXT",
            SourceType.EHR,
            {
                "case_id": "SYN-CASE-001",
                "requested_medication_id": "SYN-MED-VEL",
                "condition_id": "SYN-COND-LDS",
            },
        ),
        evidence(
            "EHR-PLAN",
            SourceType.EHR,
            {"payer_id": "SYN-PAYER-NHH", "plan_id": "SYN-PLAN-HLP"},
        ),
    )


def guideline_item(evidence_id: str = "GUIDE-SUPPORT") -> EvidenceItem:
    return evidence(
        evidence_id,
        SourceType.GUIDELINE,
        {
            "condition_id": "SYN-COND-LDS",
            "medication_id": "SYN-MED-VEL",
            "minimum_preferred_failures": 1,
            "recommendation": "SUPPORTED_OPTION",
            "decision_dimension": "CLINICAL_APPROPRIATENESS",
        },
    )


def authorization_item(
    evidence_id: str,
    source_type: SourceType,
    required: bool,
    **overrides: Any,
) -> EvidenceItem:
    structured_data = {
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
        "condition_id": "SYN-COND-LDS",
        "medication_id": "SYN-MED-VEL",
        "prior_authorization_required": required,
        "decision_dimension": "AUTHORIZATION_REQUIREMENT",
    }
    effective_from = overrides.pop("effective_from", START)
    effective_to = overrides.pop("effective_to", END)
    structured_data.update(overrides)
    return evidence(
        evidence_id,
        source_type,
        structured_data,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def result(
    source_type: SourceType,
    items: tuple[EvidenceItem, ...] = (),
    status: RetrievalStatus = RetrievalStatus.RETRIEVED,
) -> RetrievalResult:
    if status is not RetrievalStatus.RETRIEVED:
        return RetrievalResult(source_type, status, failure_reason=f"{source_type.value} failed")
    return RetrievalResult(
        source_type,
        status,
        items,
        tuple(dict.fromkeys(item.document_version_id for item in items)),
    )


def bundle(*results: RetrievalResult) -> EvidenceBundle:
    trace = tuple(
        RetrievalTraceItem(item.source_type, item.source_type.value, item.status)
        for item in results
    )
    return EvidenceBundle.from_results(tuple(results), trace)


def baseline_bundle(
    *,
    guideline_status: RetrievalStatus = RetrievalStatus.RETRIEVED,
    payer_status: RetrievalStatus = RetrievalStatus.RETRIEVED,
    formulary_status: RetrievalStatus = RetrievalStatus.RETRIEVED,
    include_note: bool = True,
) -> EvidenceBundle:
    results = [
        result(SourceType.EHR, ehr_items()),
        result(
            SourceType.GUIDELINE,
            (guideline_item(),) if guideline_status is RetrievalStatus.RETRIEVED else (),
            guideline_status,
        ),
        result(
            SourceType.PAYER_POLICY,
            (
                authorization_item(
                    "PAYER-PA", SourceType.PAYER_POLICY, True
                ),
            )
            if payer_status is RetrievalStatus.RETRIEVED
            else (),
            payer_status,
        ),
        result(
            SourceType.FORMULARY,
            (
                authorization_item(
                    "FORMULARY-PA", SourceType.FORMULARY, True
                ),
            )
            if formulary_status is RetrievalStatus.RETRIEVED
            else (),
            formulary_status,
        ),
    ]
    if include_note:
        results.append(
            result(
                SourceType.SPECIALIST_NOTE,
                (
                    evidence(
                        "NOTE-CONTEXT",
                        SourceType.SPECIALIST_NOTE,
                        {"decision_dimension": "PATIENT_CONTEXT"},
                    ),
                ),
            )
        )
    return bundle(*results)


def conflict_bundle(
    *,
    formulary_overrides: dict[str, Any] | None = None,
) -> EvidenceBundle:
    overrides = formulary_overrides or {}
    base = baseline_bundle()
    results = tuple(
        result(
            SourceType.FORMULARY,
            (
                authorization_item(
                    "UNRELATED-CONFLICT-ID",
                    SourceType.FORMULARY,
                    False,
                    **overrides,
                ),
            ),
        )
        if item.source_type is SourceType.FORMULARY
        else item
        for item in base.retrieval_results
    )
    return bundle(*results)


def approved_v2_bundle() -> EvidenceBundle:
    value = baseline_bundle()
    results = tuple(
        result(
            SourceType.PAYER_POLICY,
            (
                authorization_item(
                    "PAYER-V2-PA",
                    SourceType.PAYER_POLICY,
                    True,
                    effective_from="2026-07-01T00:00:00Z",
                    effective_to=None,
                ),
            ),
        )
        if item.source_type is SourceType.PAYER_POLICY
        else item
        for item in value.retrieval_results
    )
    return bundle(*results)


def conflict_findings(value: EvidenceBundle) -> list[Any]:
    return [
        item
        for item in reconcile(value, context())
        if item.finding_type is FindingType.SAME_DIMENSION_DISAGREEMENT
    ]


def test_clinical_support_and_payer_pa_are_a_compatible_constraint() -> None:
    findings = reconcile(baseline_bundle(), context())
    compatible = [
        finding
        for finding in findings
        if finding.finding_type is FindingType.COMPATIBLE_CONSTRAINT
    ]

    assert len(compatible) == 1
    assert compatible[0].severity is Severity.INFORMATIONAL
    assert compatible[0].resolution_state is ResolutionState.NOT_APPLICABLE
    assert {side.decision_type for side in compatible[0].sides} == {
        DecisionType.CLINICAL_APPROPRIATENESS,
        DecisionType.COVERAGE_AUTHORIZATION,
    }
    assert compatible[0].evidence_ids == ("GUIDE-SUPPORT", "PAYER-PA")
    assert not conflict_findings(baseline_bundle())


def test_matching_scope_opposing_values_create_high_unresolved_conflict() -> None:
    findings = conflict_findings(conflict_bundle())

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert findings[0].resolution_state is ResolutionState.UNRESOLVED
    assert {side.value for side in findings[0].sides} == {True, False}
    assert findings[0].evidence_ids == ("PAYER-PA", "UNRELATED-CONFLICT-ID")
    assert "winner" not in {field.name for field in fields(findings[0])}


@pytest.mark.parametrize(
    "overrides",
    [
        {"payer_id": "SYN-PAYER-OTHER"},
        {"plan_id": "SYN-PLAN-OTHER"},
        {"medication_id": "SYN-MED-OTHER"},
        {"condition_id": "SYN-COND-OTHER"},
        {"payer_id": None},
        {
            "effective_from": "2027-01-01T00:00:00Z",
            "effective_to": "2027-12-31T23:59:59Z",
        },
    ],
    ids=[
        "payer-mismatch",
        "plan-mismatch",
        "medication-mismatch",
        "indication-mismatch",
        "missing-scope",
        "non-overlapping-effective-interval",
    ],
)
def test_conflict_requires_complete_matching_applicable_scope(
    overrides: dict[str, Any],
) -> None:
    assert not conflict_findings(conflict_bundle(formulary_overrides=overrides))


def test_conflict_requires_opposing_comparable_values() -> None:
    assert not conflict_findings(baseline_bundle())


def test_conflict_detection_does_not_special_case_evidence_ids() -> None:
    value = conflict_bundle()
    findings = conflict_findings(value)

    assert findings[0].evidence_ids == ("PAYER-PA", "UNRELATED-CONFLICT-ID")


def test_supported_prerequisite_shape_is_normalized_without_prose_inference() -> None:
    item = evidence(
        "PAYER-STEP",
        SourceType.PAYER_POLICY,
        {
            "payer_id": "SYN-PAYER-NHH",
            "plan_id": "SYN-PLAN-HLP",
            "condition_id": "SYN-COND-LDS",
            "medication_id": "SYN-MED-VEL",
            "required_preferred_therapies": ["SYN-MED-NOR", "SYN-MED-BRV"],
            "minimum_distinct_failures": 2,
            "combination_rule": "ALL",
            "minimum_days_each": 30,
            "decision_dimension": "PREREQUISITE_REQUIREMENT",
        },
    )

    assertions = normalize_evidence(result(SourceType.PAYER_POLICY, (item,)), context())

    assert len(assertions) == 1
    assert assertions[0].decision_type is DecisionType.PREREQUISITE_REQUIREMENT
    assert assertions[0].evidence_ids == ("PAYER-STEP",)


@pytest.mark.parametrize(
    "item",
    [
        evidence(
            "PROSE-ONLY",
            SourceType.SPECIALIST_NOTE,
            {},
            relevant_excerpt="This prose strongly claims prior authorization is required.",
        ),
        evidence(
            "UNKNOWN-SHAPE",
            SourceType.FORMULARY,
            {"decision_dimension": "AUTHORIZATION_REQUIREMENT", "answer": True},
        ),
    ],
    ids=["prose-only", "unsupported-structured-shape"],
)
def test_no_evidence_or_unsupported_shape_creates_no_healthcare_assertion(
    item: EvidenceItem,
) -> None:
    assert normalize_evidence(result(item.source_type, (item,)), context()) == ()
    empty = bundle(
        result(SourceType.PAYER_POLICY, status=RetrievalStatus.NOT_FOUND)
    )
    assert normalize_evidence(empty, context()) == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (baseline_bundle(), ConfidenceLabel.HIGH),
        (approved_v2_bundle(), ConfidenceLabel.HIGH),
        (baseline_bundle(include_note=False), ConfidenceLabel.HIGH),
        (
            baseline_bundle(guideline_status=RetrievalStatus.NOT_FOUND),
            ConfidenceLabel.MEDIUM,
        ),
        (
            baseline_bundle(formulary_status=RetrievalStatus.NOT_FOUND),
            ConfidenceLabel.MEDIUM,
        ),
        (
            baseline_bundle(
                guideline_status=RetrievalStatus.NOT_FOUND,
                formulary_status=RetrievalStatus.NOT_FOUND,
            ),
            ConfidenceLabel.MEDIUM,
        ),
        (
            baseline_bundle(payer_status=RetrievalStatus.SOURCE_UNAVAILABLE),
            ConfidenceLabel.LOW,
        ),
        (
            baseline_bundle(payer_status=RetrievalStatus.NOT_FOUND),
            ConfidenceLabel.LOW,
        ),
        (
            baseline_bundle(payer_status=RetrievalStatus.MALFORMED),
            ConfidenceLabel.LOW,
        ),
        (conflict_bundle(), ConfidenceLabel.LOW),
    ],
    ids=[
        "baseline",
        "approved-v2",
        "optional-note-absent",
        "guideline-missing",
        "formulary-missing",
        "important-gaps-only",
        "payer-unavailable",
        "payer-not-found",
        "payer-malformed",
        "unresolved-high-conflict",
    ],
)
def test_confidence_precedence(value: EvidenceBundle, expected: ConfidenceLabel) -> None:
    policy_context = context()
    if any("V2" in item.evidence_id for item in value.evidence_by_id.values()):
        policy_context = context(as_of="2026-07-03T14:00:00Z")
    assert reason(value, policy_context).confidence.label is expected


def test_incomplete_provenance_is_low() -> None:
    value = baseline_bundle()
    payer = next(
        item
        for item in value.retrieval_results
        if item.source_type is SourceType.PAYER_POLICY
    )
    incomplete = replace(payer.evidence_items[0], source_title="")
    results = tuple(
        result(SourceType.PAYER_POLICY, (incomplete,))
        if item.source_type is SourceType.PAYER_POLICY
        else item
        for item in value.retrieval_results
    )

    assert reason(bundle(*results), context()).confidence.label is ConfidenceLabel.LOW


def test_multiple_gaps_keep_low_precedence() -> None:
    value = baseline_bundle(
        payer_status=RetrievalStatus.NOT_FOUND,
        guideline_status=RetrievalStatus.NOT_FOUND,
        formulary_status=RetrievalStatus.NOT_FOUND,
    )

    assert reason(value, context(case_id="")).confidence.label is ConfidenceLabel.LOW


def test_strong_explanation_cannot_upgrade_missing_payer_confidence() -> None:
    value = baseline_bundle(payer_status=RetrievalStatus.NOT_FOUND)
    guideline = next(
        item
        for item in value.retrieval_results
        if item.source_type is SourceType.GUIDELINE
    )
    emphatic = replace(
        guideline.evidence_items[0],
        relevant_excerpt="This narrative says it is absolutely certain.",
    )
    results = tuple(
        result(SourceType.GUIDELINE, (emphatic,))
        if item.source_type is SourceType.GUIDELINE
        else item
        for item in value.retrieval_results
    )

    assert reason(bundle(*results), context()).confidence.label is ConfidenceLabel.LOW


@pytest.mark.parametrize(
    ("value", "expected_trigger"),
    [
        (
            baseline_bundle(payer_status=status),
            EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE,
        )
        for status in (
            RetrievalStatus.SOURCE_UNAVAILABLE,
            RetrievalStatus.NOT_FOUND,
            RetrievalStatus.MALFORMED,
        )
    ]
    + [
        (conflict_bundle(), EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT),
    ],
)
def test_escalation_uses_specific_low_confidence_triggers(
    value: EvidenceBundle,
    expected_trigger: EscalationTrigger,
) -> None:
    decision = reason(value, context()).escalation

    assert decision.requires_escalation
    assert expected_trigger in decision.triggers
    assert decision.reviewer == "Synthetic coverage-policy reviewer"
    assert decision.requested_action


def test_low_confidence_independently_requires_escalation() -> None:
    value = baseline_bundle()
    high_result = reason(value, context())
    forced_low = replace(high_result.confidence, label=ConfidenceLabel.LOW)

    decision = decide_escalation(value, forced_low, high_result.findings, context())

    assert decision.requires_escalation
    assert decision.triggers == (EscalationTrigger.LOW_CONFIDENCE,)


def test_insufficient_required_context_escalates() -> None:
    decision = reason(baseline_bundle(), context(plan_id="")).escalation

    assert decision.requires_escalation
    assert EscalationTrigger.INSUFFICIENT_EVIDENCE in decision.triggers


@pytest.mark.parametrize(
    "value",
    [
        baseline_bundle(guideline_status=RetrievalStatus.NOT_FOUND),
        baseline_bundle(formulary_status=RetrievalStatus.NOT_FOUND),
        baseline_bundle(include_note=False),
        baseline_bundle(),
    ],
    ids=[
        "medium-guideline-gap",
        "medium-formulary-gap",
        "optional-note-gap",
        "compatible-constraint",
    ],
)
def test_non_escalating_policy_states(value: EvidenceBundle) -> None:
    assert not reason(value, context()).escalation.requires_escalation


def test_policy_is_pure_and_does_not_query_database_or_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = baseline_bundle()

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"external access attempted: {args!r} {kwargs!r}")

    monkeypatch.setattr(sqlite3, "connect", fail)
    monkeypatch.setattr(Path, "open", fail)

    first = reason(value, context())
    second = reason(value, context())
    assert first == second
    assert first.findings[0].evidence_ids
