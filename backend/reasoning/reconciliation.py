"""Pure normalization and reconciliation for supported synthetic evidence."""

from __future__ import annotations

from datetime import datetime
from itertools import combinations
from typing import Iterable

from backend.reasoning.models import (
    DecisionAssertion,
    DecisionScope,
    DecisionType,
    FindingType,
    ReasoningFinding,
    ResolutionState,
    Severity,
)
from backend.retrieval.models import (
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
    RetrievalResult,
    SourceType,
)


SUPPORTED_CLINICAL_RECOMMENDATION = "SUPPORTED_OPTION"
AUTHORIZATION_DIMENSION = "AUTHORIZATION_REQUIREMENT"
PREREQUISITE_DIMENSION = "PREREQUISITE_REQUIREMENT"


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _scope(evidence: EvidenceItem, as_of: str) -> DecisionScope:
    data = evidence.structured_data
    return DecisionScope(
        payer_id=_nonempty_string(data.get("payer_id")),
        plan_id=_nonempty_string(data.get("plan_id")),
        medication_id=_nonempty_string(data.get("medication_id")),
        indication_id=_nonempty_string(data.get("condition_id")),
        effective_from=_nonempty_string(evidence.document_effective_from),
        effective_to=_nonempty_string(evidence.document_effective_to),
        as_of=as_of,
    )


def _normalize_guideline(
    evidence: EvidenceItem,
    context: RetrievalRequest,
) -> DecisionAssertion | None:
    data = evidence.structured_data
    if (
        evidence.source_type is not SourceType.GUIDELINE
        or data.get("decision_dimension") != DecisionType.CLINICAL_APPROPRIATENESS.value
        or data.get("recommendation") != SUPPORTED_CLINICAL_RECOMMENDATION
        or not _nonempty_string(data.get("medication_id"))
        or not _nonempty_string(data.get("condition_id"))
    ):
        return None
    return DecisionAssertion(
        decision_type=DecisionType.CLINICAL_APPROPRIATENESS,
        value=SUPPORTED_CLINICAL_RECOMMENDATION,
        scope=_scope(evidence, context.as_of),
        source_type=evidence.source_type,
        evidence_ids=(evidence.evidence_id,),
        authority_scope="clinical appropriateness",
    )


def _normalize_authorization(
    evidence: EvidenceItem,
    context: RetrievalRequest,
) -> DecisionAssertion | None:
    data = evidence.structured_data
    value = data.get("prior_authorization_required")
    if (
        evidence.source_type not in {SourceType.PAYER_POLICY, SourceType.FORMULARY}
        or data.get("decision_dimension") != AUTHORIZATION_DIMENSION
        or not isinstance(value, bool)
        or not all(
            _nonempty_string(data.get(field))
            for field in ("payer_id", "plan_id", "medication_id", "condition_id")
        )
    ):
        return None
    return DecisionAssertion(
        decision_type=DecisionType.COVERAGE_AUTHORIZATION,
        value=value,
        scope=_scope(evidence, context.as_of),
        source_type=evidence.source_type,
        evidence_ids=(evidence.evidence_id,),
        authority_scope="coverage and authorization",
    )


def _normalize_prerequisite(
    evidence: EvidenceItem,
    context: RetrievalRequest,
) -> DecisionAssertion | None:
    data = evidence.structured_data
    therapies = data.get("required_preferred_therapies")
    combination_rule = data.get("combination_rule")
    minimum_failures = data.get("minimum_distinct_failures")
    minimum_days = data.get("minimum_days_each")
    if (
        evidence.source_type is not SourceType.PAYER_POLICY
        or data.get("decision_dimension") != PREREQUISITE_DIMENSION
        or not isinstance(therapies, tuple)
        or not therapies
        or not all(isinstance(therapy, str) and therapy for therapy in therapies)
        or combination_rule not in {"ALL", "ANY"}
        or not isinstance(minimum_failures, int)
        or isinstance(minimum_failures, bool)
        or minimum_failures < 1
        or not isinstance(minimum_days, int)
        or isinstance(minimum_days, bool)
        or minimum_days < 1
        or not all(
            _nonempty_string(data.get(field))
            for field in ("payer_id", "plan_id", "medication_id", "condition_id")
        )
    ):
        return None
    value = (
        f"COMBINATION:{combination_rule}",
        f"MINIMUM_FAILURES:{minimum_failures}",
        f"MINIMUM_DAYS:{minimum_days}",
        *(f"THERAPY:{therapy}" for therapy in therapies),
    )
    return DecisionAssertion(
        decision_type=DecisionType.PREREQUISITE_REQUIREMENT,
        value=value,
        scope=_scope(evidence, context.as_of),
        source_type=evidence.source_type,
        evidence_ids=(evidence.evidence_id,),
        authority_scope="coverage prerequisites",
    )


def _evidence_items(
    evidence: EvidenceBundle | RetrievalResult,
) -> Iterable[EvidenceItem]:
    if isinstance(evidence, RetrievalResult):
        return evidence.evidence_items
    return (
        item
        for result in evidence.retrieval_results
        for item in result.evidence_items
    )


def normalize_evidence(
    evidence: EvidenceBundle | RetrievalResult,
    context: RetrievalRequest,
) -> tuple[DecisionAssertion, ...]:
    """Create assertions only for explicitly supported structured evidence shapes."""

    assertions: list[DecisionAssertion] = []
    for item in _evidence_items(evidence):
        assertion = (
            _normalize_guideline(item, context)
            or _normalize_authorization(item, context)
            or _normalize_prerequisite(item, context)
        )
        if assertion is not None:
            assertions.append(assertion)
    return tuple(assertions)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def assertion_applies(assertion: DecisionAssertion) -> bool:
    """Return whether an assertion's explicit interval contains its as-of time."""

    as_of = _parse_time(assertion.scope.as_of)
    effective_from = _parse_time(assertion.scope.effective_from)
    effective_to = _parse_time(assertion.scope.effective_to)
    if as_of is None or effective_from is None:
        return False
    try:
        return effective_from <= as_of and (
            effective_to is None or as_of <= effective_to
        )
    except TypeError:
        return False


def scope_matches_context(
    assertion: DecisionAssertion,
    context: RetrievalRequest,
    *,
    require_coverage_scope: bool,
) -> bool:
    """Match only explicit assertion scope; absent values never act as wildcards."""

    scope = assertion.scope
    if not all(
        (
            scope.medication_id,
            scope.indication_id,
            context.medication_id,
            context.indication_id,
        )
    ):
        return False
    if (
        scope.medication_id != context.medication_id
        or scope.indication_id != context.indication_id
    ):
        return False
    if not require_coverage_scope:
        return True
    if not all((scope.payer_id, scope.plan_id, context.payer_id, context.plan_id)):
        return False
    return scope.payer_id == context.payer_id and scope.plan_id == context.plan_id


def _same_conflict_scope(left: DecisionAssertion, right: DecisionAssertion) -> bool:
    left_scope = left.scope
    right_scope = right.scope
    required_pairs = (
        (left_scope.payer_id, right_scope.payer_id),
        (left_scope.plan_id, right_scope.plan_id),
        (left_scope.medication_id, right_scope.medication_id),
        (left_scope.indication_id, right_scope.indication_id),
    )
    return all(left_value and left_value == right_value for left_value, right_value in required_pairs)


def _opposing_comparable_values(left: object, right: object) -> bool:
    return isinstance(left, bool) and isinstance(right, bool) and left is not right


def reconcile_assertions(
    assertions: Iterable[DecisionAssertion],
    context: RetrievalRequest,
) -> tuple[ReasoningFinding, ...]:
    """Classify supported compatible constraints and genuine scoped conflicts."""

    normalized = tuple(assertions)
    findings: list[ReasoningFinding] = []

    clinical_assertions = tuple(
        assertion
        for assertion in normalized
        if assertion.decision_type is DecisionType.CLINICAL_APPROPRIATENESS
        and assertion.value == SUPPORTED_CLINICAL_RECOMMENDATION
        and assertion.source_type is SourceType.GUIDELINE
        and assertion_applies(assertion)
        and scope_matches_context(assertion, context, require_coverage_scope=False)
    )
    payer_authorizations = tuple(
        assertion
        for assertion in normalized
        if assertion.decision_type is DecisionType.COVERAGE_AUTHORIZATION
        and assertion.value is True
        and assertion.source_type is SourceType.PAYER_POLICY
        and assertion_applies(assertion)
        and scope_matches_context(assertion, context, require_coverage_scope=True)
    )
    for clinical in clinical_assertions:
        for payer in payer_authorizations:
            evidence_ids = tuple(dict.fromkeys(clinical.evidence_ids + payer.evidence_ids))
            findings.append(
                ReasoningFinding(
                    finding_type=FindingType.COMPATIBLE_CONSTRAINT,
                    severity=Severity.INFORMATIONAL,
                    resolution_state=ResolutionState.NOT_APPLICABLE,
                    sides=(clinical, payer),
                    evidence_ids=evidence_ids,
                    explanation_code="CLINICAL_SUPPORT_WITH_AUTHORIZATION_CONSTRAINT",
                    explanation=(
                        "The guideline supports the medication clinically while the payer "
                        "requires prior authorization; these are compatible decision dimensions."
                    ),
                )
            )

    for left, right in combinations(normalized, 2):
        if (
            left.decision_type is not right.decision_type
            or not _same_conflict_scope(left, right)
            or not assertion_applies(left)
            or not assertion_applies(right)
            or not _opposing_comparable_values(left.value, right.value)
            or not left.evidence_ids
            or not right.evidence_ids
        ):
            continue
        evidence_ids = tuple(dict.fromkeys(left.evidence_ids + right.evidence_ids))
        findings.append(
            ReasoningFinding(
                finding_type=FindingType.SAME_DIMENSION_DISAGREEMENT,
                severity=Severity.HIGH,
                resolution_state=ResolutionState.UNRESOLVED,
                sides=(left, right),
                evidence_ids=evidence_ids,
                explanation_code="OPPOSING_AUTHORIZATION_VALUES_SAME_SCOPE",
                explanation=(
                    "Current evidence makes opposing authorization conclusions for the "
                    "same payer, plan, medication, indication, and as-of time."
                ),
            )
        )

    return tuple(findings)


def reconcile(
    bundle: EvidenceBundle,
    context: RetrievalRequest,
) -> tuple[ReasoningFinding, ...]:
    """Normalize and reconcile one in-memory retrieval bundle."""

    return reconcile_assertions(normalize_evidence(bundle, context), context)
