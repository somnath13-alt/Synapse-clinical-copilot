from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    KnowledgeAssertionState,
    KnowledgeDataError,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.retrieval import RetrievalRequest, RetrievalService, SourceType


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V1_STEP = "AST-SYN-POL-V1-STEP"
V2_PA = "AST-SYN-POL-V2-PA"
V2_STEP = "AST-SYN-POL-V2-STEP"
PAYER_SCOPE = KnowledgeQuery(
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
)


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


@pytest.fixture
def repository(initialized_settings: Settings) -> KnowledgeRepository:
    return KnowledgeRepository(initialized_settings.database_path)


@pytest.fixture
def service(repository: KnowledgeRepository) -> KnowledgeService:
    return KnowledgeService(repository)


def _approve(settings: Settings) -> str:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "The synthetic payer policy has a newer reviewed version.",
    )
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )
    return feedback["feedback_id"]


def _submit_pending_feedback(settings: Settings) -> str:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "The synthetic payer policy has a newer reviewed version.",
    )
    return feedback["feedback_id"]


def _mark_feedback_applied(settings: Settings, feedback_id: str) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            "UPDATE feedback SET status = 'APPLIED', applied_at = ? WHERE feedback_id = ?",
            (demo.APPROVED_TIME, feedback_id),
        )


def _payer_request() -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id="INT-SYN-KNOWLEDGE-PARITY",
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        as_of="2026-06-15T12:00:00Z",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
    )


def test_assertion_contract_is_deeply_immutable_and_truthful(
    repository: KnowledgeRepository,
) -> None:
    assertion = repository.get_assertion(V1_STEP)
    assert assertion is not None
    assert isinstance(assertion.evidence_ids, tuple)
    assert isinstance(assertion.value["therapy_ids"], tuple)  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        assertion.predicate = "CHANGED"  # type: ignore[misc]
    with pytest.raises(TypeError):
        assertion.value["minimum_days_each"] = 31  # type: ignore[index]

    no_scope = repository.get_assertion("AST-SYN-EHR-CONDITION")
    assert no_scope is not None
    assert no_scope.normalized_scope is None


def test_get_assertion_returns_exact_v1_and_unknown_is_none(
    repository: KnowledgeRepository,
) -> None:
    assertion = repository.get_assertion(V1_PA)
    assert assertion is not None
    assert (
        assertion.assertion_id,
        assertion.document_version_id,
        assertion.subject_id,
        assertion.predicate,
        assertion.object_id,
        assertion.value,
        assertion.decision_dimension,
        assertion.state,
        assertion.evidence_ids,
    ) == (
        V1_PA,
        V1,
        V1,
        "REQUIRES_AUTHORIZATION",
        "SYN-MED-VEL",
        True,
        "AUTHORIZATION_REQUIREMENT",
        KnowledgeAssertionState.APPLIED,
        ("EV-SYN-POL-V1-PA-001",),
    )
    assert repository.get_assertion("AST-SYN-UNKNOWN") is None


def test_all_seeded_assertions_are_read_in_deterministic_order(
    repository: KnowledgeRepository,
) -> None:
    assertions = repository.find_assertions(KnowledgeQuery())
    assert len(assertions) == 18
    assert tuple(item.assertion_id for item in assertions) == tuple(
        sorted(item.assertion_id for item in assertions)
    )


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        (KnowledgeQuery(subject_id=V1), V1_PA),
        (KnowledgeQuery(predicate="REQUIRES_PREREQUISITE"), V1_STEP),
        (KnowledgeQuery(decision_dimension="AUTHORIZATION_REQUIREMENT"), V1_PA),
        (KnowledgeQuery(state=KnowledgeAssertionState.CANDIDATE), V2_PA),
    ],
)
def test_exact_schema_filters_work(
    repository: KnowledgeRepository, query: KnowledgeQuery, expected_id: str
) -> None:
    matches = repository.find_assertions(query)
    assert expected_id in {item.assertion_id for item in matches}


@pytest.mark.parametrize(
    ("field", "scope_key"),
    [
        ("payer_id", "payer_id"),
        ("plan_id", "plan_id"),
        ("medication_id", "medication_id"),
        ("indication_id", "condition_id"),
    ],
)
def test_normalized_scope_filters_match_exact_persisted_values(
    repository: KnowledgeRepository, field: str, scope_key: str
) -> None:
    value = getattr(PAYER_SCOPE, field)
    matches = repository.find_assertions(KnowledgeQuery(**{field: value}))
    assert matches
    assert all(
        item.normalized_scope is not None
        and item.normalized_scope.get(scope_key) == value
        for item in matches
    )


def test_null_scope_does_not_match_a_requested_scope(
    repository: KnowledgeRepository,
) -> None:
    assert repository.find_assertions(
        KnowledgeQuery(subject_id="SYN-PAT-001", payer_id="SYN-PAYER-NHH")
    ) == ()


def test_baseline_current_applied_uses_v1_and_excludes_v2_candidate(
    service: KnowledgeService,
) -> None:
    assert {
        item.assertion_id
        for item in service.get_current_applied_assertions(KnowledgeQuery(subject_id=V1))
    } == {V1_PA, V1_STEP}
    assert service.get_current_applied_assertions(KnowledgeQuery(subject_id=V2)) == ()


def test_pending_feedback_leaves_v1_current(
    initialized_settings: Settings, service: KnowledgeService
) -> None:
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    demo.submit_feedback(
        initialized_settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "Please review the newer synthetic policy.",
    )
    assert len(service.get_current_applied_assertions(KnowledgeQuery(subject_id=V1))) == 2
    assert service.get_current_applied_assertions(KnowledgeQuery(subject_id=V2)) == ()


def test_approval_switches_current_applied_to_v2_and_reset_restores_v1(
    initialized_settings: Settings, service: KnowledgeService
) -> None:
    _approve(initialized_settings)
    assert service.get_current_applied_assertions(KnowledgeQuery(subject_id=V1)) == ()
    assert {
        item.assertion_id
        for item in service.get_current_applied_assertions(KnowledgeQuery(subject_id=V2))
    } == {V2_PA, V2_STEP}

    database.reset_demo_database(initialized_settings)
    assert len(service.get_current_applied_assertions(KnowledgeQuery(subject_id=V1))) == 2
    assert service.get_current_applied_assertions(KnowledgeQuery(subject_id=V2)) == ()


def test_provenance_resolves_assertion_evidence_version_and_source(
    repository: KnowledgeRepository,
) -> None:
    provenance = repository.get_assertion_provenance(V1_PA)
    assert provenance is not None
    assert provenance.assertion.assertion_id == V1_PA
    assert tuple(item.evidence_id for item in provenance.evidence_items) == (
        "EV-SYN-POL-V1-PA-001",
    )
    assert provenance.document_version_id == V1
    assert provenance.document_id == "DOC-SYN-POL-VEL"
    assert provenance.source_id == "SRC-SYN-PAYER"
    assert provenance.source_type is SourceType.PAYER_POLICY
    assert provenance.source_title == provenance.evidence_items[0].source_title
    assert provenance.source_title.endswith("Northstar Harbor Veluntra Authorization Policy")


def test_provenance_evidence_order_is_deterministic(
    initialized_settings: Settings, repository: KnowledgeRepository
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "INSERT INTO assertion_evidence VALUES (?, ?)",
            (V1_PA, "EV-SYN-POL-V1-STEP-001"),
        )
    first = repository.get_assertion_provenance(V1_PA)
    second = repository.get_assertion_provenance(V1_PA)
    assert first is not None and second is not None
    expected = ("EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001")
    assert tuple(item.evidence_id for item in first.evidence_items) == expected
    assert tuple(item.evidence_id for item in second.evidence_items) == expected


def test_cross_version_evidence_mismatch_fails_clearly(
    initialized_settings: Settings, repository: KnowledgeRepository
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute("DELETE FROM assertion_evidence WHERE assertion_id = ?", (V1_PA,))
        connection.execute(
            "INSERT INTO assertion_evidence VALUES (?, ?)",
            (V1_PA, "EV-SYN-POL-V2-PA-001"),
        )
    with pytest.raises(KnowledgeDataError, match="another document version"):
        repository.get_assertion_provenance(V1_PA)


@pytest.mark.parametrize(
    ("table", "column", "value", "message"),
    [
        ("source_document", "source_type", "INVALID", "source_type is invalid"),
        ("source_document", "source_title", "", "source_title is missing or invalid"),
    ],
)
def test_malformed_provenance_fails_without_partial_result(
    initialized_settings: Settings,
    repository: KnowledgeRepository,
    table: str,
    column: str,
    value: str,
    message: str,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE document_id = 'DOC-SYN-POL-VEL'",
            (value,),
        )
        connection.execute(
            f"UPDATE evidence_item SET {column} = ? WHERE evidence_id = 'EV-SYN-POL-V1-PA-001'",
            (value,),
        )

    with pytest.raises(KnowledgeDataError, match=message):
        repository.get_assertion_provenance(V1_PA)


def test_lineage_is_empty_before_approval_and_reads_both_directions_afterward(
    initialized_settings: Settings, repository: KnowledgeRepository
) -> None:
    assert repository.get_successors(V1_PA) == ()
    assert repository.get_predecessors(V2_PA) == ()
    _approve(initialized_settings)

    successors = repository.get_successors(V1_PA)
    predecessors = repository.get_predecessors(V2_PA)
    assert len(successors) == len(predecessors) == 1
    assert successors[0] == predecessors[0]
    assert successors[0].predecessor_assertion_id == V1_PA
    assert successors[0].successor_assertion_id == V2_PA
    assert {
        (edge.predecessor_assertion_id, edge.successor_assertion_id)
        for assertion_id in (V1_PA, V1_STEP)
        for edge in repository.get_successors(assertion_id)
    } == {(V1_PA, V2_PA), (V1_STEP, V2_STEP)}
    assert repository.get_successors(V1) == ()  # assertion_supersession uses version IDs


@pytest.mark.parametrize(
    ("column", "bad_value", "message"),
    [
        ("value_json", "{broken", "value_json is invalid JSON"),
        ("normalized_scope_json", "[]", "not a JSON object"),
    ],
)
def test_malformed_json_fails_clearly(
    initialized_settings: Settings,
    repository: KnowledgeRepository,
    column: str,
    bad_value: str,
    message: str,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            f"UPDATE knowledge_assertion SET {column} = ? WHERE assertion_id = ?",
            (bad_value, V1_PA),
        )
    with pytest.raises(KnowledgeDataError, match=message):
        repository.get_assertion(V1_PA)


def test_invalid_persisted_state_fails_clearly_when_constraint_is_bypassed(
    initialized_settings: Settings, repository: KnowledgeRepository
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE knowledge_assertion SET state = 'INVALID' WHERE assertion_id = ?",
            (V1_PA,),
        )
    with pytest.raises(KnowledgeDataError, match="assertion state is invalid"):
        repository.get_assertion(V1_PA)


def test_missing_evidence_fails_clearly(
    initialized_settings: Settings, repository: KnowledgeRepository
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute("DELETE FROM assertion_evidence WHERE assertion_id = ?", (V1_PA,))
    with pytest.raises(KnowledgeDataError, match="missing or invalid evidence"):
        repository.get_assertion(V1_PA)


def test_repository_uses_neither_fixtures_nor_reasoning_at_read_time(
    repository: KnowledgeRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_read(*args: object, **kwargs: object) -> str:
        raise AssertionError("repository attempted fixture-file access")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert repository.get_assertion(V1_PA) is not None
    assert "reasoning" not in KnowledgeRepository.__module__


def test_service_exposes_only_required_knowledge_operations(
    service: KnowledgeService,
) -> None:
    public_methods = {
        name
        for name in dir(service)
        if not name.startswith("_") and callable(getattr(service, name))
    }
    assert public_methods == {
        "get_assertion",
        "query_assertions",
        "get_current_applied_assertions",
        "get_provenance",
        "get_predecessors",
        "get_successors",
        "apply_assertion_state_transition",
        "create_assertion_lineage",
    }


def test_candidate_to_applied_succeeds(initialized_settings: Settings) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        service = KnowledgeService(
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            )
        )
        service.apply_assertion_state_transition(
            V2_PA, KnowledgeAssertionState.APPLIED
        )

    assertion = KnowledgeRepository(initialized_settings.database_path).get_assertion(V2_PA)
    assert assertion is not None
    assert assertion.state is KnowledgeAssertionState.APPLIED


def test_applied_to_superseded_succeeds(initialized_settings: Settings) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        repository = KnowledgeRepository(
            initialized_settings.database_path,
            connection=connection,
        )
        repository.apply_assertion_state_transition(
            V1_PA, KnowledgeAssertionState.SUPERSEDED
        )

    assertion = KnowledgeRepository(initialized_settings.database_path).get_assertion(V1_PA)
    assert assertion is not None
    assert assertion.state is KnowledgeAssertionState.SUPERSEDED


@pytest.mark.parametrize(
    ("assertion_id", "new_state", "make_superseded"),
    [
        (V1_PA, KnowledgeAssertionState.CANDIDATE, False),
        (V2_PA, KnowledgeAssertionState.SUPERSEDED, False),
        (V1_PA, KnowledgeAssertionState.APPLIED, True),
        (V1_PA, KnowledgeAssertionState.CANDIDATE, True),
    ],
)
def test_invalid_state_transitions_are_rejected(
    initialized_settings: Settings,
    assertion_id: str,
    new_state: KnowledgeAssertionState,
    make_superseded: bool,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        repository = KnowledgeRepository(
            initialized_settings.database_path,
            connection=connection,
        )
        if make_superseded:
            repository.apply_assertion_state_transition(
                assertion_id, KnowledgeAssertionState.SUPERSEDED
            )
        with pytest.raises(ValueError, match="Invalid knowledge assertion state transition"):
            repository.apply_assertion_state_transition(assertion_id, new_state)


@pytest.mark.parametrize(
    ("predecessor", "successor"),
    [(V1_PA, V2_PA), (V1_STEP, V2_STEP)],
)
def test_applied_feedback_creates_valid_real_lineage_pairs(
    initialized_settings: Settings,
    predecessor: str,
    successor: str,
) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    _mark_feedback_applied(initialized_settings, feedback_id)
    with database.managed_connection(initialized_settings.database_path) as connection:
        repository = KnowledgeRepository(
            initialized_settings.database_path,
            connection=connection,
        )
        repository.create_assertion_lineage(
            predecessor, successor, feedback_id, demo.APPROVED_TIME
        )

    edges = KnowledgeRepository(initialized_settings.database_path).get_successors(predecessor)
    assert len(edges) == 1
    assert edges[0].successor_assertion_id == successor
    assert edges[0].feedback_id == feedback_id


def test_pending_feedback_rejects_lineage_without_inserting_row(
    initialized_settings: Settings,
) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    with pytest.raises(KnowledgeDataError, match="requires APPLIED feedback"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            ).create_assertion_lineage(V1_PA, V2_PA, feedback_id, demo.APPROVED_TIME)

    with database.managed_connection(initialized_settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM assertion_lineage WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "UPDATE knowledge_assertion SET predicate = 'OTHER' "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "incompatible predicate",
        ),
        (
            "UPDATE knowledge_assertion SET decision_dimension = 'OTHER' "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "incompatible decision_dimension",
        ),
        (
            "UPDATE knowledge_assertion SET normalized_scope_json = '{\"payer_id\":\"OTHER\"}' "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "incompatible normalized_scope",
        ),
        (
            "UPDATE knowledge_assertion SET normalized_scope_json = NULL "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "normalized scope is missing",
        ),
        (
            "UPDATE knowledge_assertion SET normalized_scope_json = '{broken' "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "normalized_scope_json is invalid JSON",
        ),
        (
            "UPDATE knowledge_assertion SET document_version_id = 'DV-SYN-POL-VEL-V2' "
            "WHERE assertion_id = 'AST-SYN-POL-V1-PA'",
            "predecessor does not belong",
        ),
        (
            "UPDATE knowledge_assertion SET document_version_id = 'DV-SYN-POL-VEL-V1' "
            "WHERE assertion_id = 'AST-SYN-POL-V2-PA'",
            "successor does not belong",
        ),
    ],
)
def test_lineage_rejects_incompatible_assertion_family(
    initialized_settings: Settings,
    mutation: str,
    message: str,
) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    _mark_feedback_applied(initialized_settings, feedback_id)
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(mutation)

    with pytest.raises(KnowledgeDataError, match=message):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            ).create_assertion_lineage(V1_PA, V2_PA, feedback_id, demo.APPROVED_TIME)


def test_lineage_rejects_feedback_versions_from_different_documents(
    initialized_settings: Settings,
) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE feedback
               SET status = 'APPLIED', applied_at = ?,
                   proposed_version_id = 'DV-SYN-EHR-CASE-001-V1'
               WHERE feedback_id = ?""",
            (demo.APPROVED_TIME, feedback_id),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET document_version_id = 'DV-SYN-EHR-CASE-001-V1'
               WHERE assertion_id = ?""",
            (V2_PA,),
        )

    with pytest.raises(KnowledgeDataError, match="belong to different documents"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            ).create_assertion_lineage(V1_PA, V2_PA, feedback_id, demo.APPROVED_TIME)


def test_self_lineage_is_rejected(initialized_settings: Settings) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE feedback SET status = 'APPLIED', applied_at = ?,
                      proposed_version_id = target_version_id
               WHERE feedback_id = ?""",
            (demo.APPROVED_TIME, feedback_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            ).create_assertion_lineage(
                V1_PA, V1_PA, feedback_id, demo.APPROVED_TIME
            )


def test_duplicate_lineage_is_rejected(initialized_settings: Settings) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    _mark_feedback_applied(initialized_settings, feedback_id)
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            repository = KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            )
            repository.create_assertion_lineage(
                V1_PA, V2_PA, feedback_id, demo.APPROVED_TIME
            )
            repository.create_assertion_lineage(
                V1_PA, V2_PA, feedback_id, demo.APPROVED_TIME
            )


@pytest.mark.parametrize(
    ("predecessor", "successor", "feedback"),
    [
        ("AST-SYN-UNKNOWN", V2_PA, "existing"),
        (V1_PA, "AST-SYN-UNKNOWN", "existing"),
        (V1_PA, V2_PA, "FB-SYN-UNKNOWN"),
    ],
)
def test_lineage_rejects_unknown_references(
    initialized_settings: Settings,
    predecessor: str,
    successor: str,
    feedback: str,
) -> None:
    feedback_id = _submit_pending_feedback(initialized_settings)
    if feedback == "existing":
        feedback = feedback_id
        _mark_feedback_applied(initialized_settings, feedback_id)
    with pytest.raises(KnowledgeDataError, match="Unknown"):
        with database.managed_connection(initialized_settings.database_path) as connection:
            KnowledgeRepository(
                initialized_settings.database_path,
                connection=connection,
            ).create_assertion_lineage(
                predecessor, successor, feedback, demo.APPROVED_TIME
            )


def test_repository_writes_do_not_commit_independently(
    initialized_settings: Settings,
) -> None:
    connection = sqlite3.connect(initialized_settings.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        repository = KnowledgeRepository(
            initialized_settings.database_path,
            connection=connection,
        )
        repository.apply_assertion_state_transition(
            V1_PA, KnowledgeAssertionState.SUPERSEDED
        )
        connection.rollback()
    finally:
        connection.close()

    assertion = KnowledgeRepository(initialized_settings.database_path).get_assertion(V1_PA)
    assert assertion is not None
    assert assertion.state is KnowledgeAssertionState.APPLIED


def test_knowledge_and_retrieval_observe_same_current_version_before_and_after_approval(
    initialized_settings: Settings, service: KnowledgeService
) -> None:
    retrieval = RetrievalService(initialized_settings.database_path)

    assert {
        item.document_version_id
        for item in service.get_current_applied_assertions(PAYER_SCOPE)
        if item.subject_id in {V1, V2}
    } == {V1}
    payer_result = next(
        result
        for result in retrieval.retrieve(_payer_request()).retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )
    assert payer_result.document_version_ids == (V1,)

    _approve(initialized_settings)
    assert {
        item.document_version_id
        for item in service.get_current_applied_assertions(PAYER_SCOPE)
        if item.subject_id in {V1, V2}
    } == {V2}
    payer_result = next(
        result
        for result in retrieval.retrieve(_payer_request()).retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )
    assert payer_result.document_version_ids == (V2,)
