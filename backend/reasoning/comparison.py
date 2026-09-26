"""Pure deterministic comparison of source and governed reasoning inputs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import TypeAlias

from backend.reasoning.models import (
    ComparisonOutcome,
    ComparisonResult,
    DecisionScope,
    DecisionType,
    DecisionValue,
    GovernedBaselineAssertion,
    ReasoningInput,
    SourceObservation,
)
from backend.retrieval.models import RetrievalStatus


_ComparisonItem: TypeAlias = SourceObservation | GovernedBaselineAssertion
_ScopeKey: TypeAlias = tuple[DecisionType, str, str, str, str]
_ValueKey: TypeAlias = tuple[str, str]


def _value_key(value: DecisionValue) -> _ValueKey:
    """Return a type-sensitive, sortable identity for a supported value."""

    if isinstance(value, bool):
        return ("bool", "1" if value else "0")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, str):
        return ("str", value)
    return ("tuple", "\x1f".join(value))


def _scope_key(item: _ComparisonItem) -> _ScopeKey | None:
    scope = item.normalized_scope
    values = (
        scope.medication_id,
        scope.indication_id,
        scope.payer_id,
        scope.plan_id,
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    medication_id, indication_id, payer_id, plan_id = values
    return (
        item.decision_type,
        medication_id,
        indication_id,
        payer_id,
        plan_id,
    )


def _item_sort_key(item: _ComparisonItem) -> tuple[object, ...]:
    scope = item.normalized_scope
    item_id = (
        item.observation_id
        if isinstance(item, SourceObservation)
        else item.assertion_id
    )
    return (
        item.decision_type.value,
        scope.medication_id or "",
        scope.indication_id or "",
        scope.payer_id or "",
        scope.plan_id or "",
        _value_key(item.value),
        item.source_type.value,
        item.source_id,
        item.document_version_id,
        item_id,
    )


def _group_by_value(
    items: tuple[_ComparisonItem, ...],
) -> dict[_ValueKey, tuple[_ComparisonItem, ...]]:
    grouped: defaultdict[_ValueKey, list[_ComparisonItem]] = defaultdict(list)
    for item in items:
        grouped[_value_key(item.value)].append(item)
    return {
        key: tuple(sorted(group, key=_item_sort_key))
        for key, group in grouped.items()
    }


def _semantic_key(item: _ComparisonItem) -> tuple[object, ...]:
    scope = item.normalized_scope
    return (
        item.decision_type.value,
        scope.medication_id,
        scope.indication_id,
        scope.payer_id,
        scope.plan_id,
        _value_key(item.value),
    )


def _group_by_semantics(
    items: tuple[_ComparisonItem, ...],
) -> tuple[tuple[_ComparisonItem, ...], ...]:
    grouped: defaultdict[tuple[object, ...], list[_ComparisonItem]] = defaultdict(list)
    for item in items:
        grouped[_semantic_key(item)].append(item)
    return tuple(
        tuple(sorted(grouped[key], key=_item_sort_key))
        for key in sorted(grouped, key=lambda value: repr(value))
    )


def _opposing_values(left: DecisionValue, right: DecisionValue) -> bool:
    return (
        isinstance(left, bool)
        and isinstance(right, bool)
        and left is not right
    )


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(
            f"{value[:-1]}+00:00" if value.endswith("Z") else value
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def _source_is_provably_newer(
    reasoning_input: ReasoningInput,
    sources: tuple[SourceObservation, ...],
    assertions: tuple[GovernedBaselineAssertion, ...],
) -> bool:
    """Use persisted recorded timestamps only when every comparison is valid."""

    source_times = tuple(
        _parse_utc(
            reasoning_input.evidence_bundle.evidence_by_id[
                evidence_id
            ].document_recorded_at
        )
        for source in sources
        for evidence_id in source.evidence_ids
    )
    assertion_times = tuple(_parse_utc(item.recorded_at) for item in assertions)
    if (
        not source_times
        or not assertion_times
        or any(value is None for value in source_times)
        or any(value is None for value in assertion_times)
    ):
        return False
    valid_source_times = tuple(value for value in source_times if value is not None)
    valid_assertion_times = tuple(
        value for value in assertion_times if value is not None
    )
    return min(valid_source_times) > max(valid_assertion_times)


def _clinical_context_matches(
    left: DecisionScope,
    right: DecisionScope,
) -> bool:
    return bool(
        left.medication_id
        and left.indication_id
        and left.medication_id == right.medication_id
        and left.indication_id == right.indication_id
    )


def _compatible_cross_dimension(
    source: SourceObservation,
    assertion: GovernedBaselineAssertion,
) -> bool:
    if not _clinical_context_matches(
        source.normalized_scope, assertion.normalized_scope
    ):
        return False

    items: tuple[_ComparisonItem, _ComparisonItem] = (source, assertion)
    clinical = next(
        (
            item
            for item in items
            if item.decision_type is DecisionType.CLINICAL_APPROPRIATENESS
            and item.value == "SUPPORTED_OPTION"
        ),
        None,
    )
    coverage = next(
        (
            item
            for item in items
            if item.decision_type is DecisionType.COVERAGE_AUTHORIZATION
            and item.value is True
        ),
        None,
    )
    if clinical is None or coverage is None:
        return False

    coverage_scope = coverage.normalized_scope
    if not coverage_scope.payer_id or not coverage_scope.plan_id:
        return False
    clinical_scope = clinical.normalized_scope
    if (
        clinical_scope.payer_id is not None
        and clinical_scope.payer_id != coverage_scope.payer_id
    ):
        return False
    if (
        clinical_scope.plan_id is not None
        and clinical_scope.plan_id != coverage_scope.plan_id
    ):
        return False
    return True


def _result_sort_key(result: ComparisonResult) -> tuple[object, ...]:
    items: tuple[_ComparisonItem, ...] = (
        *result.source_observations,
        *result.governed_assertions,
    )
    item_keys = tuple(sorted((_item_sort_key(item) for item in items)))
    return (item_keys, result.outcome.value, result.pending_assertion_ids)


def compare_reasoning_input(
    reasoning_input: ReasoningInput,
) -> tuple[ComparisonResult, ...]:
    """Classify an already-selected dual-channel input without side effects.

    Exact same-dimension comparison requires decision type, medication,
    indication, payer, and plan.  Missing values never act as wildcards.
    """

    sources = tuple(sorted(reasoning_input.source_observations, key=_item_sort_key))
    assertions = tuple(sorted(reasoning_input.governed_assertions, key=_item_sort_key))

    sources_by_scope: defaultdict[_ScopeKey, list[SourceObservation]] = defaultdict(list)
    assertions_by_scope: defaultdict[
        _ScopeKey, list[GovernedBaselineAssertion]
    ] = defaultdict(list)
    for source in sources:
        key = _scope_key(source)
        if key is not None:
            sources_by_scope[key].append(source)
    for assertion in assertions:
        key = _scope_key(assertion)
        if key is not None:
            assertions_by_scope[key].append(assertion)

    results: list[ComparisonResult] = []
    related_source_ids: set[str] = set()
    related_assertion_ids: set[str] = set()

    for key in sorted(
        set(sources_by_scope) & set(assertions_by_scope),
        key=lambda item: tuple(value.value if isinstance(value, DecisionType) else value for value in item),
    ):
        source_groups = _group_by_value(tuple(sources_by_scope[key]))
        assertion_groups = _group_by_value(tuple(assertions_by_scope[key]))

        for value_key in sorted(set(source_groups) & set(assertion_groups)):
            source_group = tuple(source_groups[value_key])
            assertion_group = tuple(assertion_groups[value_key])
            results.append(
                ComparisonResult(
                    ComparisonOutcome.CORROBORATION,
                    source_group,
                    assertion_group,
                )
            )
            related_source_ids.update(item.observation_id for item in source_group)
            related_assertion_ids.update(item.assertion_id for item in assertion_group)

        for source_key in sorted(source_groups):
            source_group = tuple(source_groups[source_key])
            for assertion_key in sorted(assertion_groups):
                assertion_group = tuple(assertion_groups[assertion_key])
                if not _opposing_values(
                    source_group[0].value, assertion_group[0].value
                ):
                    continue
                outcome = (
                    ComparisonOutcome.STALE_KNOWLEDGE_DISAGREEMENT
                    if _source_is_provably_newer(
                        reasoning_input, source_group, assertion_group
                    )
                    else ComparisonOutcome.SAME_DIMENSION_CONFLICT
                )
                results.append(ComparisonResult(outcome, source_group, assertion_group))
                related_source_ids.update(
                    item.observation_id for item in source_group
                )
                related_assertion_ids.update(
                    item.assertion_id for item in assertion_group
                )

    unmatched_sources = tuple(
        item for item in sources if item.observation_id not in related_source_ids
    )
    unmatched_assertions = tuple(
        item for item in assertions if item.assertion_id not in related_assertion_ids
    )
    source_semantics = _group_by_semantics(unmatched_sources)
    assertion_semantics = _group_by_semantics(unmatched_assertions)

    for source_group in source_semantics:
        typed_source_group = tuple(
            item for item in source_group if isinstance(item, SourceObservation)
        )
        for assertion_group in assertion_semantics:
            typed_assertion_group = tuple(
                item
                for item in assertion_group
                if isinstance(item, GovernedBaselineAssertion)
            )
            if not _compatible_cross_dimension(
                typed_source_group[0], typed_assertion_group[0]
            ):
                continue
            results.append(
                ComparisonResult(
                    ComparisonOutcome.COMPATIBLE_CROSS_DIMENSION_CONSTRAINT,
                    typed_source_group,
                    typed_assertion_group,
                )
            )
            related_source_ids.update(
                item.observation_id for item in typed_source_group
            )
            related_assertion_ids.update(
                item.assertion_id for item in typed_assertion_group
            )

    remaining_sources = _group_by_semantics(
        tuple(
            source
            for source in sources
            if source.observation_id not in related_source_ids
        )
    )
    for group in remaining_sources:
        results.append(
            ComparisonResult(
                ComparisonOutcome.SOURCE_ONLY,
                tuple(
                    item for item in group if isinstance(item, SourceObservation)
                ),
            )
        )

    remaining_assertions = _group_by_semantics(
        tuple(
            assertion
            for assertion in assertions
            if assertion.assertion_id not in related_assertion_ids
        )
    )
    for group in remaining_assertions:
        governed = tuple(
            item
            for item in group
            if isinstance(item, GovernedBaselineAssertion)
        )
        status = reasoning_input.evidence_bundle.source_statuses.get(
            governed[0].source_type
        )
        outcome = (
            ComparisonOutcome.MISSING_SOURCE_CHANNEL
            if status is not None and status is not RetrievalStatus.RETRIEVED
            else ComparisonOutcome.KNOWLEDGE_ONLY
        )
        results.append(ComparisonResult(outcome, governed_assertions=governed))

    return tuple(sorted(results, key=_result_sort_key))
