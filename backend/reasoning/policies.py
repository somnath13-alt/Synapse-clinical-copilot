"""Deterministic confidence and escalation policies for reasoning results."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from backend.reasoning.models import (
    ComparisonOutcome,
    ComparisonResult,
    ConfidenceAssessment,
    ConfidenceFactors,
    ConfidenceLabel,
    DecisionAssertion,
    DecisionType,
    EscalationDecision,
    EscalationTrigger,
    FindingType,
    ReasoningFinding,
    ReasoningResult,
    ResolutionState,
    Severity,
)
from backend.reasoning.reconciliation import (
    assertion_applies,
    normalize_evidence,
    reconcile,
    scope_matches_context,
)
from backend.retrieval.models import (
    EvidenceBundle,
    RetrievalRequest,
    RetrievalStatus,
    SourceType,
)


POLICY_VERSION_ID = "CONF-PA-SYN-V1"
KNOWLEDGE_AWARE_POLICY_VERSION_ID = "CONF-PA-SYN-V2"
ESCALATION_REVIEWER = "Synthetic coverage-policy reviewer"


class SourceCriticality(StrEnum):
    CRITICAL = "CRITICAL"
    IMPORTANT = "IMPORTANT"
    OPTIONAL = "OPTIONAL"


SOURCE_CRITICALITY: Mapping[SourceType, SourceCriticality] = MappingProxyType(
    {
        SourceType.EHR: SourceCriticality.CRITICAL,
        SourceType.PAYER_POLICY: SourceCriticality.CRITICAL,
        SourceType.GUIDELINE: SourceCriticality.IMPORTANT,
        SourceType.FORMULARY: SourceCriticality.IMPORTANT,
        SourceType.SPECIALIST_NOTE: SourceCriticality.OPTIONAL,
    }
)


def _status(bundle: EvidenceBundle, source_type: SourceType) -> RetrievalStatus | None:
    return bundle.source_statuses.get(source_type)


def _has_high_conflict(findings: tuple[ReasoningFinding, ...]) -> bool:
    return any(
        finding.finding_type is FindingType.SAME_DIMENSION_DISAGREEMENT
        and finding.severity is Severity.HIGH
        and finding.resolution_state is ResolutionState.UNRESOLVED
        for finding in findings
    )


def _provenance_complete(bundle: EvidenceBundle) -> bool:
    for evidence in bundle.evidence_by_id.values():
        if SOURCE_CRITICALITY[evidence.source_type] is SourceCriticality.OPTIONAL:
            continue
        if not all(
            (
                evidence.evidence_id,
                evidence.source_id,
                evidence.source_title,
                evidence.version,
                evidence.timestamp,
                evidence.relevant_excerpt,
            )
        ):
            return False
    return True


def _context_evidence_complete(
    bundle: EvidenceBundle,
    context: RetrievalRequest,
) -> bool:
    if not all(
        (
            context.case_id,
            context.medication_id,
            context.indication_id,
            context.payer_id,
            context.plan_id,
            context.as_of,
        )
    ):
        return False
    if _status(bundle, SourceType.EHR) is not RetrievalStatus.RETRIEVED:
        return False

    evidence = tuple(
        item
        for item in bundle.evidence_by_id.values()
        if item.source_type is SourceType.EHR
    )
    has_case = any(
        item.structured_data.get("case_id") == context.case_id
        and item.structured_data.get("requested_medication_id") == context.medication_id
        and item.structured_data.get("condition_id") == context.indication_id
        for item in evidence
    )
    has_plan = any(
        item.structured_data.get("payer_id") == context.payer_id
        and item.structured_data.get("plan_id") == context.plan_id
        for item in evidence
    )
    return has_case and has_plan


def _usable_assertions(
    assertions: tuple[DecisionAssertion, ...],
    context: RetrievalRequest,
    source_type: SourceType,
) -> tuple[DecisionAssertion, ...]:
    return tuple(
        assertion
        for assertion in assertions
        if assertion.source_type is source_type
        and assertion_applies(assertion)
        and scope_matches_context(
            assertion,
            context,
            require_coverage_scope=source_type
            in {SourceType.PAYER_POLICY, SourceType.FORMULARY},
        )
    )


def _definitive_payer_assertions(
    assertions: tuple[DecisionAssertion, ...],
    context: RetrievalRequest,
) -> tuple[DecisionAssertion, ...]:
    return tuple(
        assertion
        for assertion in _usable_assertions(
            assertions, context, SourceType.PAYER_POLICY
        )
        if assertion.decision_type is DecisionType.COVERAGE_AUTHORIZATION
        and isinstance(assertion.value, bool)
    )


def _important_gaps(
    bundle: EvidenceBundle,
    assertions: tuple[DecisionAssertion, ...],
    context: RetrievalRequest,
) -> tuple[SourceType, ...]:
    gaps: list[SourceType] = []
    for source_type in (SourceType.GUIDELINE, SourceType.FORMULARY):
        supported_for_context = any(
            assertion.source_type is source_type
            and scope_matches_context(
                assertion,
                context,
                require_coverage_scope=source_type is SourceType.FORMULARY,
            )
            for assertion in assertions
        )
        if (
            _status(bundle, source_type) is not RetrievalStatus.RETRIEVED
            or not supported_for_context
        ):
            gaps.append(source_type)
    return tuple(gaps)


def _source_names(source_types: tuple[SourceType, ...]) -> str:
    return ", ".join(source_type.value for source_type in source_types)


def assess_confidence(
    bundle: EvidenceBundle,
    findings: tuple[ReasoningFinding, ...],
    context: RetrievalRequest,
) -> ConfidenceAssessment:
    """Apply LOW-before-MEDIUM-before-HIGH categorical precedence."""

    assertions = normalize_evidence(bundle, context)
    context_complete = _context_evidence_complete(bundle, context)
    payer_status = _status(bundle, SourceType.PAYER_POLICY)
    payer_assertions = _definitive_payer_assertions(assertions, context)
    provenance_complete = _provenance_complete(bundle)
    high_conflict = _has_high_conflict(findings)
    important_gaps = _important_gaps(bundle, assertions, context)

    unavailable_sources = tuple(
        source_type
        for source_type in SOURCE_CRITICALITY
        if _status(bundle, source_type) is not RetrievalStatus.RETRIEVED
    )
    factors = ConfidenceFactors(
        source_availability=(
            "all critical and important sources available"
            if not unavailable_sources
            else f"unavailable or missing: {_source_names(unavailable_sources)}"
        ),
        relevance=(
            "exact medication, indication, payer, and plan scope"
            if context_complete and payer_assertions
            else "required exact-scope evidence is incomplete"
        ),
        agreement_or_conflict=(
            "unresolved high same-dimension conflict"
            if high_conflict
            else "no unresolved high same-dimension conflict"
        ),
        freshness=(
            "authoritative payer evidence applies at the as-of time"
            if payer_assertions
            else "authoritative payer applicability is not established"
        ),
        required_context_complete="complete" if context_complete else "incomplete",
        provenance_complete="complete" if provenance_complete else "incomplete",
    )

    low_reasons: list[str] = []
    if not context_complete:
        low_reasons.append("required patient, medication, indication, payer, or plan context is incomplete")
    if payer_status is not RetrievalStatus.RETRIEVED:
        low_reasons.append("critical payer-policy evidence is unavailable or unusable")
    elif not payer_assertions:
        low_reasons.append("evidence is insufficient for a definitive payer conclusion")
    if not provenance_complete:
        low_reasons.append("material provenance is incomplete")
    if high_conflict:
        low_reasons.append("an unresolved HIGH same-dimension conflict remains")

    if low_reasons:
        return ConfidenceAssessment(
            label=ConfidenceLabel.LOW,
            factors=factors,
            rationale="LOW because " + "; ".join(low_reasons) + ".",
            policy_version_id=POLICY_VERSION_ID,
        )
    if important_gaps:
        return ConfidenceAssessment(
            label=ConfidenceLabel.MEDIUM,
            factors=factors,
            rationale=(
                "MEDIUM because the definitive payer conclusion remains supported, but "
                f"important supporting evidence is missing or unusable: {_source_names(important_gaps)}."
            ),
            policy_version_id=POLICY_VERSION_ID,
        )
    return ConfidenceAssessment(
        label=ConfidenceLabel.HIGH,
        factors=factors,
        rationale=(
            "HIGH because required context and authoritative payer evidence are complete, "
            "important support is usable, provenance is complete, and no unresolved HIGH "
            "same-dimension conflict exists."
        ),
        policy_version_id=POLICY_VERSION_ID,
    )


def decide_escalation(
    bundle: EvidenceBundle,
    confidence: ConfidenceAssessment,
    findings: tuple[ReasoningFinding, ...],
    context: RetrievalRequest,
) -> EscalationDecision:
    """Evaluate escalation independently and retain the most specific triggers."""

    assertions = normalize_evidence(bundle, context)
    payer_status = _status(bundle, SourceType.PAYER_POLICY)
    critical_payer_unavailable = payer_status is not RetrievalStatus.RETRIEVED
    insufficient = (
        not _context_evidence_complete(bundle, context)
        or not _provenance_complete(bundle)
        or (
            payer_status is RetrievalStatus.RETRIEVED
            and not _definitive_payer_assertions(assertions, context)
        )
    )
    high_conflict = _has_high_conflict(findings)

    triggers: list[EscalationTrigger] = []
    actions: list[str] = []
    if critical_payer_unavailable:
        triggers.append(EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE)
        actions.append("Obtain and verify the applicable current payer policy")
    if insufficient:
        triggers.append(EscalationTrigger.INSUFFICIENT_EVIDENCE)
        actions.append("Obtain the missing required case or evidence context")
    if high_conflict:
        triggers.append(EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT)
        actions.append("Resolve which same-scope authorization source governs")
    if confidence.label is ConfidenceLabel.LOW and not triggers:
        triggers.append(EscalationTrigger.LOW_CONFIDENCE)
        actions.append("Review the evidence conditions producing LOW confidence")

    if not triggers:
        return EscalationDecision(False, (), None, None)
    return EscalationDecision(
        requires_escalation=True,
        triggers=tuple(triggers),
        reviewer=ESCALATION_REVIEWER,
        requested_action="; ".join(dict.fromkeys(actions)) + ".",
    )


def _has_critical_payer_input(result: ComparisonResult) -> bool:
    return any(
        item.source_type is SourceType.PAYER_POLICY
        and SOURCE_CRITICALITY[item.source_type] is SourceCriticality.CRITICAL
        for item in (*result.source_observations, *result.governed_assertions)
    )


def _comparison_outcomes(
    comparisons: tuple[ComparisonResult, ...],
) -> frozenset[ComparisonOutcome]:
    return frozenset(result.outcome for result in comparisons)


def _knowledge_policy_rationale(
    comparisons: tuple[ComparisonResult, ...],
) -> str:
    """Describe channel meaning in stable policy-precedence order."""

    outcomes = _comparison_outcomes(comparisons)
    descriptions: list[str] = []
    ordered = (
        (
            ComparisonOutcome.SAME_DIMENSION_CONFLICT,
            "source and governed knowledge disagree in the same decision dimension; "
            "neither is selected as a winner and both provenance chains are preserved",
        ),
        (
            ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT,
            "a newer source observation conflicts with the governed baseline; neither "
            "is selected as final truth and both provenance chains are preserved",
        ),
        (
            ComparisonOutcome.MISSING_SOURCE_CHANNEL,
            "the governed baseline is not verification of the missing current source channel",
        ),
        (
            ComparisonOutcome.KNOWLEDGE_ONLY,
            "governed baseline knowledge is available but is not verified against a "
            "comparable current source observation",
        ),
        (
            ComparisonOutcome.CORROBORATION,
            "the source conclusion is corroborated by an equivalent governed baseline "
            "and both provenance chains are preserved",
        ),
        (
            ComparisonOutcome.SOURCE_ONLY,
            "the conclusion is source-backed with no comparable governed baseline",
        ),
        (
            ComparisonOutcome.COMPATIBLE_CROSS_DIMENSION_CONSTRAINT,
            "clinical and coverage conclusions are compatible cross-dimension "
            "constraints, not a conflict",
        ),
        (
            ComparisonOutcome.GOVERNANCE_PENDING,
            "pending knowledge is non-authoritative and does not corroborate or "
            "override source evidence",
        ),
        (
            ComparisonOutcome.MALFORMED_KNOWLEDGE_CHANNEL,
            "malformed knowledge is non-authoritative; the current contract does not "
            "identify whether that channel is required, so source-policy safeguards "
            "remain controlling",
        ),
    )
    for outcome, description in ordered:
        if outcome in outcomes:
            descriptions.append(description)
    if not descriptions:
        return "no source/knowledge comparison changed the source-policy assessment"
    return "; ".join(descriptions)


def assess_knowledge_aware_confidence(
    bundle: EvidenceBundle,
    findings: tuple[ReasoningFinding, ...],
    comparisons: tuple[ComparisonResult, ...],
    context: RetrievalRequest,
) -> ConfidenceAssessment:
    """Interpret precomputed comparison results without changing v1 policy.

    The v1 source assessment remains the completeness and provenance baseline.
    Knowledge participation can explain that assessment or force a safety
    downgrade for an unverified critical payer baseline or disagreement, but it
    cannot upgrade incomplete source evidence.
    """

    comparisons = tuple(comparisons)
    source_assessment = assess_confidence(bundle, findings, context)
    outcomes = _comparison_outcomes(comparisons)
    critical_knowledge_only = any(
        result.outcome is ComparisonOutcome.KNOWLEDGE_ONLY
        and _has_critical_payer_input(result)
        for result in comparisons
    )
    critical_missing_source = any(
        result.outcome is ComparisonOutcome.MISSING_SOURCE_CHANNEL
        and _has_critical_payer_input(result)
        for result in comparisons
    )
    payer_stale_disagreement = any(
        result.outcome is ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT
        and _has_critical_payer_input(result)
        for result in comparisons
    )
    same_dimension_conflict = (
        ComparisonOutcome.SAME_DIMENSION_CONFLICT in outcomes
    )

    low_reasons: list[str] = []
    if critical_missing_source:
        low_reasons.append(
            "critical payer source evidence is missing and governed baseline knowledge "
            "does not verify the current source"
        )
    if critical_knowledge_only:
        low_reasons.append(
            "the critical payer conclusion is available only as an unverified governed baseline"
        )
    if same_dimension_conflict:
        low_reasons.append(
            "source evidence and governed knowledge have an unresolved same-dimension conflict"
        )
    if payer_stale_disagreement:
        low_reasons.append(
            "a newer payer source observation conflicts with the governed baseline"
        )

    interpretation = _knowledge_policy_rationale(comparisons)
    if low_reasons:
        if critical_missing_source or critical_knowledge_only:
            freshness = "current payer source verification is not established"
        elif payer_stale_disagreement:
            freshness = "newer source conflicts with governed baseline"
        else:
            freshness = source_assessment.factors.freshness
        factors = ConfidenceFactors(
            source_availability=source_assessment.factors.source_availability,
            relevance=source_assessment.factors.relevance,
            agreement_or_conflict=(
                "unresolved source/knowledge disagreement"
                if same_dimension_conflict or payer_stale_disagreement
                else source_assessment.factors.agreement_or_conflict
            ),
            freshness=freshness,
            required_context_complete=(
                source_assessment.factors.required_context_complete
            ),
            provenance_complete=source_assessment.factors.provenance_complete,
        )
        return ConfidenceAssessment(
            label=ConfidenceLabel.LOW,
            factors=factors,
            rationale=(
                "LOW because "
                + "; ".join(dict.fromkeys(low_reasons))
                + f". Knowledge-aware interpretation: {interpretation}."
            ),
            policy_version_id=KNOWLEDGE_AWARE_POLICY_VERSION_ID,
        )

    return ConfidenceAssessment(
        label=source_assessment.label,
        factors=ConfidenceFactors(
            source_availability=source_assessment.factors.source_availability,
            relevance=source_assessment.factors.relevance,
            agreement_or_conflict=(
                interpretation
                if comparisons
                else source_assessment.factors.agreement_or_conflict
            ),
            freshness=source_assessment.factors.freshness,
            required_context_complete=(
                source_assessment.factors.required_context_complete
            ),
            provenance_complete=source_assessment.factors.provenance_complete,
        ),
        rationale=(
            f"{source_assessment.rationale} Knowledge-aware interpretation: "
            f"{interpretation}."
        ),
        policy_version_id=KNOWLEDGE_AWARE_POLICY_VERSION_ID,
    )


def decide_knowledge_aware_escalation(
    bundle: EvidenceBundle,
    confidence: ConfidenceAssessment,
    findings: tuple[ReasoningFinding, ...],
    comparisons: tuple[ComparisonResult, ...],
    context: RetrievalRequest,
) -> EscalationDecision:
    """Apply comparison-specific escalation without retrieval or mutation."""

    comparisons = tuple(comparisons)
    source_confidence = assess_confidence(bundle, findings, context)
    source_decision = decide_escalation(
        bundle, source_confidence, findings, context
    )
    triggers = list(source_decision.triggers)
    actions = (
        [source_decision.requested_action.removesuffix(".")]
        if source_decision.requested_action
        else []
    )

    critical_missing_source = any(
        result.outcome is ComparisonOutcome.MISSING_SOURCE_CHANNEL
        and _has_critical_payer_input(result)
        for result in comparisons
    )
    critical_knowledge_only = any(
        result.outcome is ComparisonOutcome.KNOWLEDGE_ONLY
        and _has_critical_payer_input(result)
        for result in comparisons
    )
    source_knowledge_conflict = any(
        result.outcome is ComparisonOutcome.SAME_DIMENSION_CONFLICT
        or (
            result.outcome is ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT
            and _has_critical_payer_input(result)
        )
        for result in comparisons
    )

    if critical_missing_source:
        if _status(bundle, SourceType.PAYER_POLICY) is RetrievalStatus.RETRIEVED:
            triggers.append(EscalationTrigger.INSUFFICIENT_EVIDENCE)
            actions.append("Obtain the missing definitive payer conclusion")
        else:
            triggers.append(EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE)
            actions.append("Obtain and verify the applicable current payer policy")
    if critical_knowledge_only:
        triggers.append(EscalationTrigger.INSUFFICIENT_EVIDENCE)
        actions.append("Verify the current payer source against the governed baseline")
    if source_knowledge_conflict:
        triggers.append(EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT)
        actions.append(
            "Review the source and governed knowledge disagreement without selecting a winner"
        )
    if confidence.label is ConfidenceLabel.LOW and not triggers:
        triggers.append(EscalationTrigger.LOW_CONFIDENCE)
        actions.append("Review the evidence conditions producing LOW confidence")

    unique_triggers = tuple(dict.fromkeys(triggers))
    if not unique_triggers:
        return EscalationDecision(False, (), None, None)
    return EscalationDecision(
        requires_escalation=True,
        triggers=unique_triggers,
        reviewer=ESCALATION_REVIEWER,
        requested_action="; ".join(dict.fromkeys(actions)) + ".",
    )


def reason(bundle: EvidenceBundle, context: RetrievalRequest) -> ReasoningResult:
    """Run the complete pure reasoning policy pipeline over an in-memory bundle."""

    findings = reconcile(bundle, context)
    confidence = assess_confidence(bundle, findings, context)
    escalation = decide_escalation(bundle, confidence, findings, context)
    return ReasoningResult(findings, confidence, escalation)
