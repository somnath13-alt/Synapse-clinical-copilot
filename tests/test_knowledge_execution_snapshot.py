"""M6.4b execution-time governed-knowledge snapshot persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.knowledge import (
    KnowledgeAssertion,
    KnowledgeAssertionState,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeService,
)
from backend.reasoning import (
    AssertionOrigin,
    ComparisonOutcome,
    DecisionScope,
    DecisionType,
    EscalationTrigger,
    GovernedBaselineAssertion,
    KNOWLEDGE_AWARE_POLICY_VERSION_ID,
    ReasoningInput,
    SourceObservation,
    assess_knowledge_aware_confidence,
    compare_reasoning_input,
    decide_knowledge_aware_escalation,
    normalize_evidence,
    reason,
)
from backend.retrieval import (
    EvidenceBundle,
    RetrievalRequest,
    RetrievalService,
    SourceType,
    TemporalMode,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
V1_PA = "AST-SYN-POL-V1-PA"
V2_PA = "AST-SYN-POL-V2-PA"
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"

PAYER_QUERY = KnowledgeQuery(
    predicate="REQUIRES_AUTHORIZATION",
    decision_dimension="AUTHORIZATION_REQUIREMENT",
    payer_id="SYN-PAYER-NHH",
    plan_id="SYN-PLAN-HLP",
    medication_id="SYN-MED-VEL",
    indication_id="SYN-COND-LDS",
)

# Candidate C: one ordered, FK-backed participation relation, with the mutable
# assertion facts copied at execution.  JSON is retained only for values whose
# natural shape is JSON, not as an unvalidated interaction-wide envelope.
PARTICIPATION_KEY_COLUMNS = {
    "interaction_knowledge_id",
    "interaction_id",
    "assertion_id",
    "ordinal",
}
IMMUTABLE_ASSERTION_COLUMNS = {
    "origin",
    "state_at_execution",
    "decision_type",
    "value_json",
    "normalized_scope_json",
    "effective_from",
    "effective_to",
    "recorded_at",
    "document_version_id",
    "source_id",
    "source_type",
    "document_id",
    "document_version",
    "document_effective_from",
    "document_effective_to",
    "lineage_ids_json",
    "correction_ids_json",
}
PARTICIPATION_EVIDENCE_COLUMNS = {
    "interaction_knowledge_id",
    "ordinal",
    "evidence_id",
}
MATERIAL_COMPARISON_FACTS = {
    "outcome",
    "assertion_ids",
    "source_observation_ids",
    "source_evidence_ids",
    "source_values",
    "source_scopes",
}
DESIGN_PROPERTIES = {
    "A_JSON": {
        "fk_integrity": False,
        "immutable_facts": True,
        "deterministic_order": True,
        "queryable": False,
        "reset_rebuild": True,
        "replay_inputs": True,
        "relational_consistency": False,
    },
    "B_ID_ORDER_ROLE_STATE": {
        "fk_integrity": True,
        "immutable_facts": False,
        "deterministic_order": True,
        "queryable": True,
        "reset_rebuild": True,
        "replay_inputs": False,
        "relational_consistency": True,
    },
    "C_NORMALIZED_IMMUTABLE": {
        "fk_integrity": True,
        "immutable_facts": True,
        "deterministic_order": True,
        "queryable": True,
        "reset_rebuild": True,
        "replay_inputs": True,
        "relational_consistency": True,
    },
}


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _request(
    interaction_id: str,
    *,
    source_mode: str = "BASELINE",
    temporal_mode: TemporalMode = TemporalMode.CURRENT,
    as_of: str = JUNE_15,
) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id=interaction_id,
        intent="PRIOR_AUTHORIZATION",
        case_id="SYN-CASE-001",
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        source_mode=source_mode,
        temporal_mode=temporal_mode,
        as_of=as_of,
    )


def _approve_v2(settings: Settings, interaction_id: str | None = None) -> None:
    if interaction_id is None:
        interaction_id = demo.ask_question(settings, demo.CANONICAL_QUESTION)[
            "interaction_id"
        ]
    feedback = demo.submit_feedback(
        settings,
        interaction_id,
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


def _selected_assertions(
    settings: Settings, request: RetrievalRequest
) -> tuple[KnowledgeAssertion, ...]:
    service = KnowledgeService(KnowledgeRepository(settings.database_path))
    query = replace(PAYER_QUERY, as_of=request.as_of)
    if request.temporal_mode is TemporalMode.AS_OF:
        selected = service.get_applicable_assertions(query)
    else:
        selected = service.get_current_applied_assertions(replace(query, as_of=None))
    return tuple(
        assertion
        for assertion in selected
        if (
            provenance := service.get_provenance(assertion.assertion_id)
        ) is not None
        and provenance.source_type is SourceType.PAYER_POLICY
    )


def _governed_view(
    settings: Settings,
    assertion: KnowledgeAssertion,
    request: RetrievalRequest,
) -> GovernedBaselineAssertion:
    service = KnowledgeService(KnowledgeRepository(settings.database_path))
    provenance = service.get_provenance(assertion.assertion_id)
    assert provenance is not None and assertion.normalized_scope is not None
    lineage = (
        *service.get_predecessors(assertion.assertion_id),
        *service.get_successors(assertion.assertion_id),
    )
    scope = assertion.normalized_scope
    return GovernedBaselineAssertion(
        assertion_id=assertion.assertion_id,
        state=assertion.state,
        decision_type=DecisionType.COVERAGE_AUTHORIZATION,
        value=assertion.value,
        normalized_scope=DecisionScope(
            payer_id=scope.get("payer_id"),
            plan_id=scope.get("plan_id"),
            medication_id=scope.get("medication_id"),
            indication_id=scope.get("condition_id"),
            effective_from=assertion.effective_from,
            effective_to=assertion.effective_to,
            as_of=request.as_of,
        ),
        effective_from=assertion.effective_from,
        effective_to=assertion.effective_to,
        recorded_at=assertion.recorded_at,
        evidence_ids=assertion.evidence_ids,
        source_id=provenance.source_id,
        source_type=provenance.source_type,
        document_id=provenance.document_id,
        document_version_id=provenance.document_version_id,
        document_version=provenance.document_version,
        document_effective_from=provenance.document_effective_from,
        document_effective_to=provenance.document_effective_to,
        temporal_context=request,
        lineage_ids=tuple(dict.fromkeys(edge.lineage_id for edge in lineage)),
        correction_ids=tuple(dict.fromkeys(edge.feedback_id for edge in lineage)),
    )


def _source_observations(
    bundle: EvidenceBundle, request: RetrievalRequest
) -> tuple[SourceObservation, ...]:
    observations: list[SourceObservation] = []
    for assertion in normalize_evidence(bundle, request):
        if (
            assertion.decision_type is not DecisionType.COVERAGE_AUTHORIZATION
            or assertion.source_type is not SourceType.PAYER_POLICY
        ):
            continue
        evidence = bundle.evidence_by_id[assertion.evidence_ids[0]]
        observations.append(
            SourceObservation(
                observation_id=f"OBS-{evidence.evidence_id}",
                decision_type=assertion.decision_type,
                value=assertion.value,
                normalized_scope=assertion.scope,
                source_type=assertion.source_type,
                evidence_ids=assertion.evidence_ids,
                source_id=evidence.source_id,
                document_version_id=evidence.document_version_id,
                effective_from=evidence.document_effective_from,
                effective_to=evidence.document_effective_to,
                temporal_context=request,
            )
        )
    return tuple(observations)


def _dual_input(
    settings: Settings,
    request: RetrievalRequest,
    *,
    source_observations: tuple[SourceObservation, ...] | None = None,
) -> ReasoningInput:
    bundle = RetrievalService(settings.database_path).retrieve(request)
    governed = tuple(
        _governed_view(settings, assertion, request)
        for assertion in _selected_assertions(settings, request)
    )
    return ReasoningInput(
        temporal_context=request,
        evidence_bundle=bundle,
        source_observations=(
            _source_observations(bundle, request)
            if source_observations is None
            else source_observations
        ),
        governed_assertions=governed,
    )


def _payer_comparison(
    reasoning_input: ReasoningInput, outcome: ComparisonOutcome
) -> Any:
    return next(
        comparison
        for comparison in compare_reasoning_input(reasoning_input)
        if comparison.outcome is outcome
        and any(
            item.source_type is SourceType.PAYER_POLICY
            for item in (
                *comparison.source_observations,
                *comparison.governed_assertions,
            )
        )
    )


def _snapshot_rows(
    settings: Settings, interaction_id: str
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with database.managed_connection(settings.database_path) as connection:
        knowledge = connection.execute(
            """SELECT interaction_knowledge_id, assertion_id, ordinal, origin,
                      state_at_execution, decision_type, value_json,
                      normalized_scope_json, effective_from, effective_to,
                      recorded_at, source_id, source_type, document_id,
                      document_version_id, document_version,
                      document_effective_from, document_effective_to,
                      lineage_ids_json, correction_ids_json
               FROM interaction_knowledge WHERE interaction_id = ?
               ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()
        evidence = connection.execute(
            """SELECT ike.interaction_knowledge_id, ike.evidence_id, ike.ordinal
               FROM interaction_knowledge_evidence AS ike
               JOIN interaction_knowledge AS ik
                 ON ik.interaction_knowledge_id = ike.interaction_knowledge_id
               WHERE ik.interaction_id = ?
               ORDER BY ik.ordinal, ike.ordinal""",
            (interaction_id,),
        ).fetchall()
    return knowledge, evidence


def test_schema_v5_current_snapshot_contents(
    initialized_settings: Settings,
) -> None:
    payload = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    interaction_id = payload["interaction_id"]
    with database.managed_connection(initialized_settings.database_path) as connection:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in ("interaction_knowledge", "interaction_knowledge_evidence")
        }
        interaction = connection.execute(
            """SELECT temporal_mode, requested_as_of, confidence_policy_id,
                      policy_version_id, answer_text, confidence,
                      reconciliation_json, escalation_json
               FROM interaction WHERE interaction_id = ?""",
            (interaction_id,),
        ).fetchone()
        evidence = connection.execute(
            """SELECT evidence_id, ordinal FROM interaction_evidence
               WHERE interaction_id = ? ORDER BY ordinal""",
            (interaction_id,),
        ).fetchall()
        claims = connection.execute(
            "SELECT claim_key, claim_text, evidence_ids_json FROM supported_claim "
            "WHERE interaction_id = ? ORDER BY rowid",
            (interaction_id,),
        ).fetchall()
        citations = connection.execute(
            """SELECT evidence_id, source_id, document_version_id,
                      source_title, source_type, version, timestamp,
                      section, relevant_excerpt
               FROM citation WHERE interaction_id = ? ORDER BY rowid""",
            (interaction_id,),
        ).fetchall()

    knowledge, knowledge_evidence = _snapshot_rows(
        initialized_settings, interaction_id
    )

    assert schema_version == 5
    assert interaction == (
        "CURRENT",
        None,
        "CONF-PA-SYN-V2",
        V1,
        payload["answer"],
        payload["confidence"],
        json.dumps(payload["reconciliation"], separators=(",", ":"), sort_keys=True),
        None,
    )
    assert evidence and tuple(row[1] for row in evidence) == tuple(range(len(evidence)))
    assert claims and citations
    assert {row[2] for row in citations if row[4] == "PAYER_POLICY"} == {V1}

    assert {"interaction_knowledge", "interaction_knowledge_evidence"} <= tables
    assert PARTICIPATION_KEY_COLUMNS | IMMUTABLE_ASSERTION_COLUMNS <= columns[
        "interaction_knowledge"
    ]
    assert PARTICIPATION_EVIDENCE_COLUMNS <= columns[
        "interaction_knowledge_evidence"
    ]
    assert len(knowledge) == 1
    row = knowledge[0]
    assert row[1:7] == (V1_PA, 0, "CORROBORATED", "APPLIED", "COVERAGE_AUTHORIZATION", "true")
    assert json.loads(row[7]) == {
        "condition_id": "SYN-COND-LDS",
        "medication_id": "SYN-MED-VEL",
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
    }
    assert row[8:18] == (
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
        "2025-12-15T12:05:00Z",
        "SRC-SYN-PAYER",
        "PAYER_POLICY",
        "DOC-SYN-POL-VEL",
        V1,
        "1.0",
        "2026-01-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
    )
    assert json.loads(row[18]) == [] and json.loads(row[19]) == []
    assert knowledge_evidence == [
        (row[0], "EV-SYN-POL-V1-PA-001", 0)
    ]


def test_snapshot_does_not_late_bind_mutable_assertion_state_or_provenance(
    initialized_settings: Settings,
) -> None:
    request = _request("INT-M64A-LIFECYCLE")
    selected_at_execution = _selected_assertions(initialized_settings, request)[0]
    interaction = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    snapshot_before = _snapshot_rows(
        initialized_settings, interaction["interaction_id"]
    )
    historical_before = demo.get_interaction(
        initialized_settings, interaction["interaction_id"]
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        governance_before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "feedback",
                "review",
                "assertion_supersession",
                "assertion_lineage",
                "knowledge_update",
            )
        )

    assert selected_at_execution.assertion_id == V1_PA
    assert selected_at_execution.state is KnowledgeAssertionState.APPLIED
    _approve_v2(initialized_settings, interaction["interaction_id"])

    resolved_later = KnowledgeRepository(
        initialized_settings.database_path
    ).get_assertion(V1_PA)
    historical_after = demo.get_interaction(
        initialized_settings, interaction["interaction_id"]
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        governance_after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "feedback",
                "review",
                "assertion_supersession",
                "assertion_lineage",
                "knowledge_update",
            )
        )
    assert resolved_later is not None
    assert resolved_later.assertion_id == selected_at_execution.assertion_id
    assert resolved_later.state is KnowledgeAssertionState.SUPERSEDED
    assert historical_after == historical_before
    assert governance_before == (0, 0, 0, 0, 0)
    assert all(after > before for before, after in zip(governance_before, governance_after))
    assert _snapshot_rows(initialized_settings, interaction["interaction_id"]) == (
        snapshot_before
    )
    assert snapshot_before[0][0][4] == "APPLIED"
    assert snapshot_before[0][0][6] == "true"
    assert snapshot_before[0][0][14] == V1
    assert resolved_later.state is not selected_at_execution.state


def test_agreement_requires_both_independent_chains_and_corroborated_role(
    initialized_settings: Settings,
) -> None:
    dual_input = _dual_input(initialized_settings, _request("INT-M64A-AGREEMENT"))
    comparison = _payer_comparison(dual_input, ComparisonOutcome.CORROBORATION)

    assert {item.value for item in comparison.source_observations} == {True}
    assert {item.value for item in comparison.governed_assertions} == {True}
    assert comparison.origin is AssertionOrigin.CORROBORATED
    assert {item.origin for item in comparison.source_observations} == {
        AssertionOrigin.SOURCE
    }
    assert {item.origin for item in comparison.governed_assertions} == {
        AssertionOrigin.KNOWLEDGE
    }
    assert comparison.evidence_ids == tuple(
        dict.fromkeys(
            evidence_id
            for item in (
                *comparison.source_observations,
                *comparison.governed_assertions,
            )
            for evidence_id in item.evidence_ids
        )
    )
    assert MATERIAL_COMPARISON_FACTS >= {
        "outcome",
        "assertion_ids",
        "source_observation_ids",
        "source_evidence_ids",
    }


def test_disagreement_requires_both_values_scopes_provenance_and_outcome(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        row = connection.execute(
            "SELECT structured_data FROM evidence_item WHERE evidence_id = ?",
            ("EV-SYN-POL-V1-PA-001",),
        ).fetchone()
        source_payload = json.loads(row[0])
        source_payload["prior_authorization_required"] = False
        connection.execute(
            "UPDATE evidence_item SET structured_data = ? WHERE evidence_id = ?",
            (json.dumps(source_payload, sort_keys=True), "EV-SYN-POL-V1-PA-001"),
        )

    dual_input = _dual_input(initialized_settings, _request("INT-M64A-CONFLICT"))
    comparisons = compare_reasoning_input(dual_input)
    comparison = _payer_comparison(
        dual_input, ComparisonOutcome.SAME_DIMENSION_CONFLICT
    )
    source_result = reason(dual_input.evidence_bundle, dual_input.temporal_context)
    confidence = assess_knowledge_aware_confidence(
        dual_input.evidence_bundle,
        source_result.findings,
        comparisons,
        dual_input.temporal_context,
    )
    escalation = decide_knowledge_aware_escalation(
        dual_input.evidence_bundle,
        confidence,
        source_result.findings,
        comparisons,
        dual_input.temporal_context,
    )

    assert {item.value for item in comparison.source_observations} == {False}
    assert {item.value for item in comparison.governed_assertions} == {True}
    assert comparison.origin is None
    assert all(item.normalized_scope for item in comparison.source_observations)
    assert all(item.normalized_scope for item in comparison.governed_assertions)
    assert all(item.evidence_ids for item in comparison.source_observations)
    assert all(item.evidence_ids for item in comparison.governed_assertions)
    assert all(
        item.state is KnowledgeAssertionState.APPLIED
        for item in comparison.governed_assertions
    )
    assert confidence.policy_version_id == KNOWLEDGE_AWARE_POLICY_VERSION_ID
    assert confidence.label.value == "LOW"
    assert EscalationTrigger.UNRESOLVED_HIGH_SEVERITY_CONFLICT in escalation.triggers


def test_unavailable_source_keeps_knowledge_governed_but_unverified(
    initialized_settings: Settings,
) -> None:
    dual_input = _dual_input(
        initialized_settings,
        _request(
            "INT-M64A-UNAVAILABLE", source_mode="PAYER_POLICY_UNAVAILABLE"
        ),
    )
    comparisons = compare_reasoning_input(dual_input)
    comparison = _payer_comparison(
        dual_input, ComparisonOutcome.MISSING_SOURCE_CHANNEL
    )
    source_result = reason(dual_input.evidence_bundle, dual_input.temporal_context)
    confidence = assess_knowledge_aware_confidence(
        dual_input.evidence_bundle,
        source_result.findings,
        comparisons,
        dual_input.temporal_context,
    )
    escalation = decide_knowledge_aware_escalation(
        dual_input.evidence_bundle,
        confidence,
        source_result.findings,
        comparisons,
        dual_input.temporal_context,
    )

    assert not comparison.source_observations
    assert {item.assertion_id for item in comparison.governed_assertions} == {V1_PA}
    assert comparison.origin is AssertionOrigin.KNOWLEDGE
    assert confidence.label.value == "LOW"
    assert "does not verify the current source" in confidence.rationale
    assert EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE in escalation.triggers
    assert dual_input.evidence_bundle.source_statuses[SourceType.PAYER_POLICY].value != (
        "RETRIEVED"
    )


def test_as_of_snapshot_must_keep_selected_state_not_current_state(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)
    june = _dual_input(
        initialized_settings,
        _request("INT-M64A-JUNE", temporal_mode=TemporalMode.AS_OF, as_of=JUNE_15),
    )
    july = _dual_input(
        initialized_settings,
        _request("INT-M64A-JULY", temporal_mode=TemporalMode.AS_OF, as_of=JULY_3),
    )

    assert tuple(
        (item.assertion_id, item.state, item.document_version_id)
        for item in june.governed_assertions
    ) == ((V1_PA, KnowledgeAssertionState.SUPERSEDED, V1),)
    assert tuple(
        (item.assertion_id, item.state, item.document_version_id)
        for item in july.governed_assertions
    ) == ((V2_PA, KnowledgeAssertionState.APPLIED, V2),)
    assert june.temporal_mode is july.temporal_mode is TemporalMode.AS_OF
    assert june.as_of == JUNE_15 and july.as_of == JULY_3


def test_overlapping_assertions_require_every_row_in_selection_order(
    initialized_settings: Settings,
) -> None:
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            """UPDATE source_document_version
               SET governance_state = 'APPLIED', effective_from = '2026-06-01T00:00:00Z'
               WHERE document_version_id = ?""",
            (V2,),
        )
        connection.execute(
            """UPDATE knowledge_assertion
               SET state = 'APPLIED', effective_from = '2026-06-01T00:00:00Z'
               WHERE assertion_id = ?""",
            (V2_PA,),
        )
    request = _request(
        "INT-M64A-OVERLAP", temporal_mode=TemporalMode.AS_OF, as_of=JUNE_15
    )
    first = _selected_assertions(initialized_settings, request)
    second = _selected_assertions(initialized_settings, request)

    assert tuple(item.assertion_id for item in first) == (V1_PA, V2_PA)
    assert second == first
    execution_rows = tuple(
        (ordinal, item.assertion_id, item.state.value, AssertionOrigin.KNOWLEDGE.value)
        for ordinal, item in enumerate(first)
    )
    assert execution_rows == (
        (0, V1_PA, "APPLIED", "KNOWLEDGE"),
        (1, V2_PA, "APPLIED", "KNOWLEDGE"),
    )


def test_minimal_normalized_design_preserves_provenance_without_full_objects(
    initialized_settings: Settings,
) -> None:
    dual_input = _dual_input(initialized_settings, _request("INT-M64A-DESIGN"))
    assertion = dual_input.governed_assertions[0]
    model_fields = {item.name for item in fields(assertion)}

    assert PARTICIPATION_KEY_COLUMNS == {
        "interaction_knowledge_id",
        "interaction_id",
        "assertion_id",
        "ordinal",
    }
    assert PARTICIPATION_EVIDENCE_COLUMNS == {
        "interaction_knowledge_id",
        "ordinal",
        "evidence_id",
    }
    assert {
        "state",
        "value",
        "normalized_scope",
        "effective_from",
        "effective_to",
        "recorded_at",
        "document_version_id",
        "source_id",
        "evidence_ids",
        "lineage_ids",
        "correction_ids",
        "origin",
    } <= model_fields
    assert set(assertion.evidence_ids) <= set(dual_input.evidence_bundle.evidence_by_id)
    assert assertion.source_id
    assert assertion.document_version_id == V1
    # B's five columns omit mutable execution facts; A has no relational FK/order
    # guarantees. C is the smallest candidate that meets both properties.
    assert len(IMMUTABLE_ASSERTION_COLUMNS) > 5
    assert "ordinal" in PARTICIPATION_KEY_COLUMNS
    assert "state_at_execution" in IMMUTABLE_ASSERTION_COLUMNS
    assert DESIGN_PROPERTIES["C_NORMALIZED_IMMUTABLE"] == {
        property_name: True
        for property_name in DESIGN_PROPERTIES["C_NORMALIZED_IMMUTABLE"]
    }
    assert DESIGN_PROPERTIES["A_JSON"]["fk_integrity"] is False
    assert DESIGN_PROPERTIES["A_JSON"]["queryable"] is False
    assert DESIGN_PROPERTIES["B_ID_ORDER_ROLE_STATE"]["immutable_facts"] is False
    assert DESIGN_PROPERTIES["B_ID_ORDER_ROLE_STATE"]["replay_inputs"] is False
    assert MATERIAL_COMPARISON_FACTS < (
        MATERIAL_COMPARISON_FACTS
        | {item.name for item in fields(dual_input)}
        | {item.name for item in fields(assertion)}
    )


def test_current_and_as_of_snapshots_preserve_execution_state(
    initialized_settings: Settings,
) -> None:
    current_v1 = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    _approve_v2(initialized_settings, current_v1["interaction_id"])
    june_v1 = demo.ask_question(
        initialized_settings, demo.CANONICAL_QUESTION, as_of=JUNE_15
    )
    current_v2 = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    july_v2 = demo.ask_question(
        initialized_settings, demo.CANONICAL_QUESTION, as_of=JULY_3
    )

    snapshots = {
        key: _snapshot_rows(initialized_settings, payload["interaction_id"])[0][0]
        for key, payload in (
            ("current_v1", current_v1),
            ("june_v1", june_v1),
            ("current_v2", current_v2),
            ("july_v2", july_v2),
        )
    }
    assert (snapshots["current_v1"][1], snapshots["current_v1"][4]) == (
        V1_PA,
        "APPLIED",
    )
    assert (snapshots["june_v1"][1], snapshots["june_v1"][4]) == (
        V1_PA,
        "SUPERSEDED",
    )
    assert (snapshots["current_v2"][1], snapshots["current_v2"][4]) == (
        V2_PA,
        "APPLIED",
    )
    assert (snapshots["july_v2"][1], snapshots["july_v2"][4]) == (
        V2_PA,
        "APPLIED",
    )
    assert snapshots["current_v1"][6:18] == snapshots["june_v1"][6:18]
    assert json.loads(snapshots["current_v2"][18])
    assert json.loads(snapshots["current_v2"][19])


def test_unavailable_source_still_snapshots_unverified_knowledge_baseline(
    initialized_settings: Settings,
) -> None:
    payload = demo.ask_question(
        initialized_settings,
        demo.CANONICAL_QUESTION,
        source_mode="PAYER_POLICY_UNAVAILABLE",
    )
    knowledge, evidence = _snapshot_rows(
        initialized_settings, payload["interaction_id"]
    )

    assert [(row[1], row[3], row[4]) for row in knowledge] == [
        (V1_PA, "KNOWLEDGE", "APPLIED")
    ]
    assert [row[1] for row in evidence] == ["EV-SYN-POL-V1-PA-001"]
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True


def test_snapshot_constraints_reject_candidates_duplicates_and_broken_fks(
    initialized_settings: Settings,
) -> None:
    payload = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    knowledge, evidence = _snapshot_rows(
        initialized_settings, payload["interaction_id"]
    )
    row = knowledge[0]
    assert {item[1] for item in knowledge} == {V1_PA}
    assert V2_PA not in {item[1] for item in knowledge}

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                """INSERT INTO interaction_knowledge
                   SELECT ?, interaction_id, assertion_id, ordinal, origin,
                          state_at_execution, decision_type, value_json,
                          normalized_scope_json, effective_from, effective_to,
                          recorded_at, source_id, source_type, document_id,
                          document_version_id, document_version,
                          document_effective_from, document_effective_to,
                          lineage_ids_json, correction_ids_json
                   FROM interaction_knowledge WHERE interaction_knowledge_id = ?""",
                ("IK-DUPLICATE", row[0]),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                """INSERT INTO interaction_knowledge_evidence
                   (interaction_knowledge_id, evidence_id, ordinal)
                   VALUES (?, ?, ?)""",
                evidence[0],
            )
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                """INSERT INTO interaction_knowledge_evidence
                   (interaction_knowledge_id, evidence_id, ordinal)
                   VALUES ('IK-MISSING', 'EV-SYN-POL-V1-PA-001', 0)"""
            )
    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(initialized_settings.database_path) as connection:
            connection.execute(
                "UPDATE interaction_knowledge SET state_at_execution = 'CANDIDATE' "
                "WHERE interaction_knowledge_id = ?",
                (row[0],),
            )


def test_roles_and_disagreement_participation_are_representable(
    initialized_settings: Settings,
) -> None:
    first = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    second = demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            "UPDATE interaction_knowledge SET origin = 'CORROBORATED' "
            "WHERE interaction_id = ?",
            (first["interaction_id"],),
        )
        connection.execute(
            "UPDATE interaction_knowledge SET value_json = 'false' "
            "WHERE interaction_id = ?",
            (second["interaction_id"],),
        )
        represented = connection.execute(
            """SELECT origin, value_json FROM interaction_knowledge
               WHERE interaction_id IN (?, ?) ORDER BY interaction_id""",
            (first["interaction_id"], second["interaction_id"]),
        ).fetchall()
    assert {row[0] for row in represented} == {"CORROBORATED"}
    assert {row[1] for row in represented} == {"true", "false"}


def test_downstream_failure_rolls_back_interaction_and_knowledge_snapshot(
    initialized_settings: Settings,
) -> None:
    tables = (
        "interaction",
        "interaction_evidence",
        "interaction_knowledge",
        "interaction_knowledge_evidence",
        "supported_claim",
        "citation",
    )
    with database.managed_connection(initialized_settings.database_path) as connection:
        before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        )
        connection.execute(
            """CREATE TRIGGER fail_m64b_claim
               BEFORE INSERT ON supported_claim
               BEGIN SELECT RAISE(ABORT, 'forced M6.4b failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced M6.4b failure"):
        demo.ask_question(initialized_settings, demo.CANONICAL_QUESTION)

    with database.managed_connection(initialized_settings.database_path) as connection:
        after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        )
    assert after == before


def test_historical_get_is_snapshot_only_and_public_shape_stays_unchanged(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/v1/questions", json={"question": demo.CANONICAL_QUESTION}
    )
    assert created.status_code == 200
    original = created.json()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("historical GET must not execute live services")

    monkeypatch.setattr(demo, "RetrievalService", forbidden)
    monkeypatch.setattr(demo, "KnowledgeService", forbidden)
    monkeypatch.setattr(demo, "reason", forbidden)
    monkeypatch.setattr(demo, "_as_of_reasoning_bundle", forbidden)

    response = client.get(f"/api/v1/interactions/{original['interaction_id']}")
    assert response.status_code == 200
    historical = response.json()
    immutable_public_fields = {
        "question",
        "intent",
        "selected_sources",
        "orchestration_trace",
        "answer",
        "claims",
        "citations",
        "confidence",
        "confidence_rationale",
        "reconciliation",
        "escalation",
        "policy_version_id",
    }
    assert all(historical[key] == original[key] for key in immutable_public_fields)
    public_keys = set(original) | set(historical)
    assert not any(
        "knowledge" in key or "assertion" in key or "comparison" in key
        for key in public_keys
    )
    # Historical display consumes stored outputs. M6.4b may improve replay inputs,
    # but neither this GET nor M6.4a defines or invokes a replay executor.
