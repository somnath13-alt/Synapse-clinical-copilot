"""E2 tests for persisted-knowledge temporal selection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    KnowledgeAssertion,
    KnowledgeAssertionState,
    KnowledgeDataError,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.retrieval import (
    PayerPolicyAdapter,
    RetrievalRequest,
    RetrievalStatus,
    TemporalMode,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
PAYER_VERSION_IDS = {V1, V2}
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
V1_END = "2026-06-30T23:59:59Z"
V2_START = "2026-07-01T00:00:00Z"
PAYER_SCOPE = KnowledgeQuery(
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
)


@pytest.mark.parametrize(
    "value",
    ["not-a-time", "2026-06-15T14:00:00", "2026-06-15T10:00:00-04:00", 123],
)
def test_knowledge_query_rejects_invalid_or_non_utc_as_of(value: object) -> None:
    with pytest.raises(ValueError, match="as_of must"):
        KnowledgeQuery(as_of=value)  # type: ignore[arg-type]


def test_knowledge_query_normalizes_valid_utc_as_of() -> None:
    query = KnowledgeQuery(as_of="2026-06-15T14:00:00.123000+00:00")

    assert query.as_of == "2026-06-15T14:00:00.123000Z"


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _submit_v2(settings: Settings) -> dict[str, Any]:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    return demo.submit_feedback(
        settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )


def _approve_v2(settings: Settings) -> None:
    feedback = _submit_v2(settings)
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def _request(as_of: str) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-SYN-KNOWLEDGE-TEMPORAL-SELECTION",
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        as_of=as_of,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        temporal_mode=TemporalMode.AS_OF,
    )


def _specified_as_of_assertions(
    settings: Settings,
    as_of: str,
    query: KnowledgeQuery = PAYER_SCOPE,
) -> tuple[KnowledgeAssertion, ...]:
    service = KnowledgeService(KnowledgeRepository(settings.database_path))
    return tuple(
        assertion
        for assertion in service.get_applicable_assertions(replace(query, as_of=as_of))
        if assertion.document_version_id in PAYER_VERSION_IDS
    )


def _current_ids(settings: Settings) -> tuple[str, ...]:
    service = KnowledgeService(KnowledgeRepository(settings.database_path))
    return tuple(
        assertion.assertion_id
        for assertion in service.get_current_applied_assertions(PAYER_SCOPE)
        if assertion.document_version_id in PAYER_VERSION_IDS
    )


def test_current_remains_applied_plus_current_and_never_returns_superseded_history(
    initialized_settings: Settings,
) -> None:
    assert _current_ids(initialized_settings) == (V1_PA, V1_STEP)

    feedback = _submit_v2(initialized_settings)
    assert feedback["status"] == "PENDING"
    assert _current_ids(initialized_settings) == (V1_PA, V1_STEP)

    demo.approve_feedback(
        initialized_settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )
    repository = KnowledgeRepository(initialized_settings.database_path)
    v1_assertion = repository.get_assertion(V1_PA)
    v2_assertion = repository.get_assertion(V2_PA)

    assert v1_assertion is not None and v2_assertion is not None
    assert v1_assertion.state is KnowledgeAssertionState.SUPERSEDED
    assert v2_assertion.state is KnowledgeAssertionState.APPLIED
    assert _current_ids(initialized_settings) == (V2_PA, V2_STEP)


@pytest.mark.parametrize(
    ("governance_phase", "as_of", "expected_assertion_ids", "expected_version_ids"),
    [
        pytest.param("PENDING", JUNE_15, (V1_PA, V1_STEP), (V1,), id="pending-june-v1"),
        pytest.param("PENDING", JULY_3, (), (), id="pending-july-not-found"),
        pytest.param("APPROVED", JUNE_15, (V1_PA, V1_STEP), (V1,), id="approved-june-v1"),
        pytest.param("APPROVED", JULY_3, (V2_PA, V2_STEP), (V2,), id="approved-july-v2"),
    ],
)
def test_as_of_v1_v2_matrix_requires_governance_and_both_effective_intervals(
    initialized_settings: Settings,
    governance_phase: str,
    as_of: str,
    expected_assertion_ids: tuple[str, ...],
    expected_version_ids: tuple[str, ...],
) -> None:
    if governance_phase == "PENDING":
        _submit_v2(initialized_settings)
    else:
        _approve_v2(initialized_settings)

    assertions = _specified_as_of_assertions(initialized_settings, as_of)
    retrieval = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(as_of)
    )

    assert tuple(assertion.assertion_id for assertion in assertions) == expected_assertion_ids
    assert tuple(dict.fromkeys(a.document_version_id for a in assertions)) == (
        expected_version_ids
    )
    assert retrieval.document_version_ids == expected_version_ids
    assert retrieval.status is (
        RetrievalStatus.RETRIEVED if expected_version_ids else RetrievalStatus.NOT_FOUND
    )
    assert _current_ids(initialized_settings) == (
        (V1_PA, V1_STEP) if governance_phase == "PENDING" else (V2_PA, V2_STEP)
    )


def test_applicable_assertions_require_as_of_without_falling_back_to_current(
    initialized_settings: Settings,
) -> None:
    service = KnowledgeService(KnowledgeRepository(initialized_settings.database_path))

    with pytest.raises(ValueError, match="require as_of"):
        service.get_applicable_assertions(PAYER_SCOPE)


def test_candidate_state_filter_is_always_excluded_from_as_of(
    initialized_settings: Settings,
) -> None:
    service = KnowledgeService(KnowledgeRepository(initialized_settings.database_path))
    query = replace(
        PAYER_SCOPE,
        state=KnowledgeAssertionState.CANDIDATE,
        as_of=JULY_3,
    )

    assert service.get_applicable_assertions(query) == ()


def test_as_of_closed_boundaries_switch_from_superseded_v1_to_applied_v2(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)

    at_v1_end = _specified_as_of_assertions(initialized_settings, V1_END)
    at_v2_start = _specified_as_of_assertions(initialized_settings, V2_START)

    assert tuple(assertion.assertion_id for assertion in at_v1_end) == (V1_PA, V1_STEP)
    assert all(
        assertion.state is KnowledgeAssertionState.SUPERSEDED
        for assertion in at_v1_end
    )
    assert tuple(assertion.assertion_id for assertion in at_v2_start) == (V2_PA, V2_STEP)
    assert all(
        assertion.state is KnowledgeAssertionState.APPLIED
        for assertion in at_v2_start
    )


def test_overlapping_applied_assertions_return_both_in_deterministic_order(
    initialized_settings: Settings,
) -> None:
    """AS_OF does not choose by currentness, label, recorded time, or lineage."""

    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET governance_state = 'APPLIED', version = '0.1',
                   recorded_at = '1900-01-01T00:00:00Z',
                   effective_from = '2026-06-01T00:00:00Z'
               WHERE document_version_id = ?""",
            (V2,),
        )
        connection.execute(
            """UPDATE source_document_version
               SET version = '99.0', recorded_at = '2099-01-01T00:00:00Z'
               WHERE document_version_id = ?""",
            (V1,),
        )
        connection.execute(
            "UPDATE evidence_item SET version = '0.1' WHERE document_version_id = ?",
            (V2,),
        )
        connection.execute(
            "UPDATE evidence_item SET version = '99.0' WHERE document_version_id = ?",
            (V1,),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'APPLIED', effective_from = '2026-06-01T00:00:00Z'
               WHERE assertion_id = ?""",
            (V2_PA,),
        )

    query = KnowledgeQuery(
        predicate="REQUIRES_AUTHORIZATION",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
    )
    first = _specified_as_of_assertions(initialized_settings, JUNE_15, query)
    second = _specified_as_of_assertions(initialized_settings, JUNE_15, query)

    assert tuple(assertion.assertion_id for assertion in first) == (V1_PA, V2_PA)
    assert second == first
    assert {assertion.state for assertion in first} == {KnowledgeAssertionState.APPLIED}

    retrieval = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(JUNE_15)
    )
    assert retrieval.document_version_ids == (V1, V2)


def test_as_of_does_not_use_approval_time_as_temporal_precedence(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET effective_from = '2026-06-01T00:00:00Z'
               WHERE document_version_id = ?""",
            (V2,),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET effective_from = '2026-06-01T00:00:00Z'
               WHERE assertion_id = ?""",
            (V2_PA,),
        )
        connection.execute(
            "UPDATE feedback SET applied_at = '2099-01-01T00:00:00Z'",
        )

    query = KnowledgeQuery(
        predicate="REQUIRES_AUTHORIZATION",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
    )
    assertions = _specified_as_of_assertions(initialized_settings, JUNE_15, query)

    assert tuple(item.assertion_id for item in assertions) == (V1_PA, V2_PA)
    assert tuple(item.state for item in assertions) == (
        KnowledgeAssertionState.SUPERSEDED,
        KnowledgeAssertionState.APPLIED,
    )


def test_broader_assertion_interval_is_read_today_but_invalid_for_e2_applicability(
    initialized_settings: Settings,
) -> None:
    """E2 must reject or ignore applicability when assertion exceeds its document."""

    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE knowledge_assertion
               SET effective_from = '2025-12-01T00:00:00Z'
               WHERE assertion_id = ?""",
            (V1_PA,),
        )

    repository = KnowledgeRepository(initialized_settings.database_path)
    assertion = repository.get_assertion(V1_PA)
    provenance = repository.get_assertion_provenance(V1_PA)

    assert assertion is not None and provenance is not None
    assert assertion.effective_from < provenance.document_effective_from
    assert V1_PA in _current_ids(initialized_settings)
    with pytest.raises(KnowledgeDataError, match="outside its source document interval"):
        _specified_as_of_assertions(initialized_settings, JUNE_15)


def test_null_effective_to_remains_as_of_applicable_at_later_governed_instants(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    repository = KnowledgeRepository(initialized_settings.database_path)
    assertion = repository.get_assertion(V2_PA)
    provenance = repository.get_assertion_provenance(V2_PA)

    assert assertion is not None and provenance is not None
    assert assertion.effective_to is None
    assert provenance.document_effective_to is None
    assert tuple(
        item.assertion_id
        for item in _specified_as_of_assertions(
            initialized_settings,
            "2035-01-01T00:00:00Z",
        )
    ) == (V2_PA, V2_STEP)


@pytest.mark.parametrize(
    ("column", "persisted_value", "message"),
    [
        pytest.param(
            "effective_from",
            "not-a-time",
            "not a valid ISO 8601 timestamp",
            id="malformed-effective-from",
        ),
        pytest.param(
            "effective_to",
            "not-a-time",
            "not a valid ISO 8601 timestamp",
            id="malformed-effective-to",
        ),
        pytest.param(
            "effective_from",
            "2026-01-01T00:00:00",
            "explicit UTC offset",
            id="naive-effective-from",
        ),
        pytest.param(
            "effective_to",
            "2026-06-30T23:59:59",
            "explicit UTC offset",
            id="naive-effective-to",
        ),
    ],
)
def test_repository_currently_returns_nonempty_malformed_assertion_timestamps_verbatim(
    initialized_settings: Settings,
    column: str,
    persisted_value: str,
    message: str,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            f"UPDATE knowledge_assertion SET {column} = ? WHERE assertion_id = ?",
            (persisted_value, V1_PA),
        )

    assertion = KnowledgeRepository(initialized_settings.database_path).get_assertion(V1_PA)

    assert assertion is not None
    assert getattr(assertion, column) == persisted_value
    with pytest.raises(KnowledgeDataError, match=message):
        _specified_as_of_assertions(initialized_settings, JUNE_15)


def test_repository_currently_rejects_missing_effective_from(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_assertion SET effective_from = '' WHERE assertion_id = ?",
            (V1_PA,),
        )

    with pytest.raises(KnowledgeDataError, match="effective_from is missing or invalid"):
        KnowledgeRepository(initialized_settings.database_path).get_assertion(V1_PA)


def test_repository_currently_returns_an_inverted_assertion_interval(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE knowledge_assertion
               SET effective_from = '2026-06-16T00:00:00Z',
                   effective_to = '2026-06-15T00:00:00Z'
               WHERE assertion_id = ?""",
            (V1_PA,),
        )

    assertion = KnowledgeRepository(initialized_settings.database_path).get_assertion(V1_PA)

    assert assertion is not None
    assert assertion.effective_from > assertion.effective_to  # type: ignore[operator]
    with pytest.raises(KnowledgeDataError, match="assertion interval is inverted"):
        _specified_as_of_assertions(initialized_settings, JUNE_15)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("effective_from", "not-a-time", "not a valid ISO 8601 timestamp"),
        ("effective_to", "2026-06-30T23:59:59", "explicit UTC offset"),
    ],
)
def test_as_of_rejects_malformed_document_timestamps_without_partial_results(
    initialized_settings: Settings,
    column: str,
    value: str,
    message: str,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            f"UPDATE source_document_version SET {column} = ? "
            "WHERE document_version_id = ?",
            (value, V1),
        )

    with pytest.raises(KnowledgeDataError, match=message):
        _specified_as_of_assertions(initialized_settings, JUNE_15)


def test_as_of_rejects_inverted_document_interval(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET effective_from = '2026-06-16T00:00:00Z',
                   effective_to = '2026-06-15T00:00:00Z'
               WHERE document_version_id = ?""",
            (V1,),
        )

    with pytest.raises(KnowledgeDataError, match="source_document_version interval is inverted"):
        _specified_as_of_assertions(initialized_settings, JUNE_15)


def test_as_of_results_keep_existing_provenance_resolution(
    initialized_settings: Settings,
) -> None:
    repository = KnowledgeRepository(initialized_settings.database_path)
    service = KnowledgeService(repository)
    assertion = next(
        item
        for item in service.get_applicable_assertions(
            replace(PAYER_SCOPE, predicate="REQUIRES_AUTHORIZATION", as_of=JUNE_15)
        )
        if item.assertion_id == V1_PA
    )

    provenance = service.get_provenance(assertion.assertion_id)
    assert provenance is not None
    assert provenance.assertion == assertion
    assert provenance.document_version_id == V1
    assert tuple(item.evidence_id for item in provenance.evidence_items) == (
        "EV-SYN-POL-V1-PA-001",
    )


def test_reset_restores_baseline_as_of_selection(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    assert tuple(
        item.assertion_id
        for item in _specified_as_of_assertions(initialized_settings, JULY_3)
    ) == (V2_PA, V2_STEP)

    database.reset_demo_database(initialized_settings)

    assert tuple(
        item.assertion_id
        for item in _specified_as_of_assertions(initialized_settings, JUNE_15)
    ) == (V1_PA, V1_STEP)
    assert _specified_as_of_assertions(initialized_settings, JULY_3) == ()
