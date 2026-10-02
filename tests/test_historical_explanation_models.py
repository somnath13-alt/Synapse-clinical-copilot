"""M8.2 pure contracts: synthetic stored facts, no explanation reader."""

from __future__ import annotations

import ast
import builtins
import inspect
import sqlite3
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from backend.explanation import InteractionExplanation
from backend.explanation import models as m


NOW = "2026-07-03T16:45:00Z"
THEN = "2026-06-15T15:45:00+00:00"


def participant(**changes):
    values = dict(
        interaction_knowledge_id="IK-SYN-1", assertion_id="AST-SYN-1", ordinal=0,
        origin="KNOWLEDGE", state_at_execution="SUPERSEDED",
        decision_type="COVERAGE_AUTHORIZATION", value=True,
        normalized_scope={"payer_id": "PAYER-SYN-1", "nested": [{"synthetic": True}]},
        effective_from="2026-01-01T00:00:00Z", effective_to="2026-06-30T23:59:59Z",
        recorded_at="2025-12-15T12:05:00Z", source_id="SRC-SYN-1",
        source_type="PAYER_POLICY", document_id="DOC-SYN-1",
        document_version_id="DV-SYN-1", document_version="1.0",
        document_effective_from="2026-01-01T00:00:00Z", document_effective_to=None,
        lineage_ids=["LINEAGE-SYN-1"], correction_ids=["FB-SYN-OLD"],
        evidence=[m.EvidenceMembership("EV-SYN-KNOWLEDGE", 0)],
    )
    return m.KnowledgeParticipation(**(values | changes))


def event(kind="INTERACTION", event_id=1, **changes):
    link = m.GovernanceLink(
        kind, "INT-SYN-1", "FB-SYN-1" if kind != "INTERACTION" else None,
        "PROPOSAL-SYN-1" if kind == "PROPOSAL" else None,
    )
    values = dict(
        event_id=event_id, event_type="REVIEW_APPROVED", occurred_at=NOW,
        interaction_id="INT-SYN-1" if kind == "INTERACTION" else None,
        feedback_id="FB-SYN-1" if kind == "FEEDBACK" else None,
        payload={"proposal_id": "PROPOSAL-SYN-1", "actor": "Synthetic reviewer",
                 "after": {"assertions": [{"state": "APPLIED"}]}},
        relationship=link,
    )
    return m.AuditEventSnapshot(**(values | changes))


def explanation():
    return InteractionExplanation(
        identity=m.InteractionIdentity(
            "INT-SYN-1", "Synthetic authorization question", "PRIOR_AUTHORIZATION", "BASELINE",
            ["PAYER_POLICY", "GUIDELINE"],
            [{"sequence": 1, "source_type": "PAYER_POLICY", "status": "AVAILABLE"}], NOW,
        ),
        temporal=m.TemporalContext("AS_OF", THEN, NOW),
        answer=m.AnswerSnapshot(
            "Synthetic stored answer", m.ConfidenceSnapshot("LOW", "Synthetic stored rationale", "CONF-PA-SYN-V2"),
            [m.ReconciliationSnapshot("SOURCE_UNAVAILABLE", "HIGH", "UNRESOLVED", "Synthetic stored limitation")],
            m.EscalationSnapshot(True, "Synthetic reviewer", "Synthetic stored reason"), "DV-SYN-1",
        ),
        retrieved_evidence=[m.EvidenceMembership("EV-SYN-Z", 0), m.EvidenceMembership("EV-SYN-A", 1)],
        knowledge=[participant()],
        claims=[m.ClaimSnapshot("SC-SYN-1", "synthetic_claim", "Synthetic claim text", ["EV-SYN-Z"])],
        citations=[m.CitationSnapshot("CIT-SYN-1", "SC-SYN-1", "EV-SYN-Z", "SRC-SYN-1", "DV-SYN-1",
                                      "Synthetic source", "PAYER_POLICY", "1.0", THEN, "Synthetic section", "Synthetic excerpt")],
        related_governance=m.GovernanceTimeline([event()]),
    )


def test_aggregate_and_every_nested_contract_are_frozen_and_slotted():
    value = explanation()
    nodes = [value, value.identity, value.temporal, value.answer, value.answer.confidence,
             value.answer.escalation, *value.answer.reconciliation, *value.retrieved_evidence,
             *value.knowledge, *value.knowledge[0].evidence, *value.claims, *value.citations,
             value.related_governance, *value.related_governance.events,
             value.related_governance.events[0].relationship]
    for node in nodes:
        assert not hasattr(node, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(node, fields(node)[0].name, "mutated")


def test_recursive_maps_and_arrays_are_defensively_copied():
    mutable = {"nested": [{"value": [True, None, 1, 1.5]}]}
    knowledge = participant(value=mutable, normalized_scope=mutable)
    audit = event(payload=mutable)
    identity = replace(explanation().identity, orchestration_trace=[mutable])
    mutable["nested"][0]["value"].append("later")
    for frozen in (knowledge.value, knowledge.normalized_scope, audit.payload, identity.orchestration_trace[0]):
        assert frozen["nested"][0]["value"] == (True, None, 1, 1.5)
        with pytest.raises(TypeError):
            frozen["nested"][0]["value"] = ()
    assert replace(knowledge) == knowledge  # Already-frozen maps can be copied too.


def test_all_sequences_are_tuples_and_preserve_supplied_order():
    value = explanation()
    for collection in (value.identity.selected_sources, value.identity.orchestration_trace,
                       value.retrieved_evidence, value.knowledge, value.claims, value.citations,
                       value.answer.reconciliation, value.related_governance.events,
                       value.knowledge[0].evidence, value.knowledge[0].lineage_ids,
                       value.knowledge[0].correction_ids, value.claims[0].evidence_ids):
        assert isinstance(collection, tuple)
    assert [item.evidence_id for item in value.retrieved_evidence] == ["EV-SYN-Z", "EV-SYN-A"]
    assert replace(value.claims[0], evidence_ids=["EV-SYN-Z", "EV-SYN-A"]).evidence_ids == ("EV-SYN-Z", "EV-SYN-A")


@pytest.mark.parametrize("members", [
    [m.EvidenceMembership("EV-SYN-A", 1)],
    [m.EvidenceMembership("EV-SYN-A", 1), m.EvidenceMembership("EV-SYN-B", 0)],
    [m.EvidenceMembership("EV-SYN-A", 0), m.EvidenceMembership("EV-SYN-B", 0)],
    [m.EvidenceMembership("EV-SYN-A", 0), m.EvidenceMembership("EV-SYN-A", 1)],
])
def test_invalid_membership_order_is_rejected_not_silently_repaired(members):
    with pytest.raises(ValueError):
        replace(explanation(), retrieved_evidence=members)
    with pytest.raises(ValueError):
        participant(evidence=members)


@pytest.mark.parametrize("ordinal", [-1, True, 0.5, "0"])
def test_ordinals_must_be_nonnegative_integers(ordinal):
    with pytest.raises(ValueError):
        m.EvidenceMembership("EV-SYN-1", ordinal)
    with pytest.raises(ValueError):
        participant(ordinal=ordinal)


def test_temporal_request_and_legacy_execution_times_remain_distinct():
    value = explanation()
    assert value.temporal.requested_as_of == THEN
    assert value.temporal.legacy_as_of == value.identity.created_at == NOW
    current = m.TemporalContext("CURRENT", None, NOW)
    assert current.requested_as_of is None
    assert current.temporal_mode == "CURRENT"
    assert m.TemporalContext("AS_OF", THEN, None).legacy_as_of is None


@pytest.mark.parametrize("mode,requested", [("CURRENT", THEN), ("AS_OF", None),
                                           ("REPLAY", THEN), ("AS_OF", "2026-01-01"),
                                           ("AS_OF", "2026-01-01T00:00:00+01:00")])
def test_invalid_temporal_pairing(mode, requested):
    with pytest.raises(ValueError):
        m.TemporalContext(mode, requested, NOW)


def test_answer_confidence_and_payer_policy_identity_are_stored_separately():
    stored = explanation().answer
    assert stored.answer == "Synthetic stored answer"
    assert stored.confidence == m.ConfidenceSnapshot("LOW", "Synthetic stored rationale", "CONF-PA-SYN-V2")
    assert stored.policy_version_id == "DV-SYN-1"
    assert stored.policy_version_id != stored.confidence.confidence_policy_id


def test_knowledge_retains_execution_facts_without_selection_or_predicate():
    stored = participant()
    assert (stored.interaction_knowledge_id, stored.assertion_id, stored.ordinal) == ("IK-SYN-1", "AST-SYN-1", 0)
    assert (stored.origin, stored.state_at_execution, stored.value) == ("KNOWLEDGE", "SUPERSEDED", True)
    assert stored.decision_dimension == stored.decision_type == "COVERAGE_AUTHORIZATION"
    assert stored.normalized_scope["payer_id"] == "PAYER-SYN-1"
    assert (stored.effective_from, stored.effective_to, stored.recorded_at) == (
        "2026-01-01T00:00:00Z", "2026-06-30T23:59:59Z", "2025-12-15T12:05:00Z")
    assert (stored.source_id, stored.source_type, stored.document_id, stored.document_version_id,
            stored.document_version, stored.document_effective_from, stored.document_effective_to) == (
        "SRC-SYN-1", "PAYER_POLICY", "DOC-SYN-1", "DV-SYN-1", "1.0", "2026-01-01T00:00:00Z", None)
    assert stored.lineage_ids == ("LINEAGE-SYN-1",)
    assert stored.correction_ids == ("FB-SYN-OLD",)
    assert not hasattr(stored, "predicate")
    assert not hasattr(stored, "current_state")
    # No applicability/clock policy: a structurally valid future interval remains data.
    assert participant(effective_from="2030-01-01T00:00:00Z", effective_to=None).effective_from.startswith("2030")


def test_retrieved_supporting_cited_and_knowledge_evidence_are_distinct():
    value = explanation()
    assert {e.evidence_id for e in value.retrieved_evidence} == {"EV-SYN-A", "EV-SYN-Z"}
    assert value.claims[0].evidence_ids == ("EV-SYN-Z",)
    assert tuple(c.evidence_id for c in value.citations) == ("EV-SYN-Z",)
    assert value.knowledge[0].evidence == (m.EvidenceMembership("EV-SYN-KNOWLEDGE", 0),)
    assert {f.name for f in fields(m.EvidenceMembership)} == {"evidence_id", "ordinal"}


def test_claim_and_copied_citation_fields_retained():
    value = explanation()
    assert value.claims[0] == m.ClaimSnapshot("SC-SYN-1", "synthetic_claim", "Synthetic claim text", ("EV-SYN-Z",))
    citation = value.citations[0]
    assert tuple(getattr(citation, f.name) for f in fields(citation)) == (
        "CIT-SYN-1", "SC-SYN-1", "EV-SYN-Z", "SRC-SYN-1", "DV-SYN-1", "Synthetic source",
        "PAYER_POLICY", "1.0", THEN, "Synthetic section", "Synthetic excerpt")
    assert not hasattr(value.claims[0], "assertion_id")


def test_reconciliation_is_only_the_stored_public_projection():
    finding = explanation().answer.reconciliation[0]
    assert {f.name for f in fields(finding)} == {"type", "severity", "resolution_state", "explanation"}
    assert (finding.type, finding.severity, finding.resolution_state, finding.explanation) == (
        "SOURCE_UNAVAILABLE", "HIGH", "UNRESOLVED", "Synthetic stored limitation")


def test_escalation_retains_persisted_fields_and_unavailable_triggers():
    escalation = explanation().answer.escalation
    assert (escalation.required, escalation.reviewer, escalation.reason) == (
        True, "Synthetic reviewer", "Synthetic stored reason")
    assert escalation.trigger_ids is None
    assert {f.name for f in fields(escalation) if f.init} == {"required", "reviewer", "reason"}
    with pytest.raises(TypeError):
        m.EscalationSnapshot(True, None, None, trigger_ids=("LOW_CONFIDENCE",))


@pytest.mark.parametrize("kind", ["INTERACTION", "FEEDBACK", "PROPOSAL"])
def test_explicit_governance_path_and_audit_facts_retained(kind):
    audit = event(kind)
    assert audit.relationship.kind == kind
    assert audit.relationship.interaction_id == "INT-SYN-1"
    assert (audit.event_id, audit.event_type, audit.occurred_at) == (1, "REVIEW_APPROVED", NOW)
    assert audit.payload["actor"] == "Synthetic reviewer"
    assert audit.payload["after"]["assertions"][0]["state"] == "APPLIED"
    if kind == "PROPOSAL":
        assert audit.interaction_id is audit.feedback_id is None
        assert audit.relationship.feedback_id == "FB-SYN-1"
        assert audit.relationship.proposal_id == audit.payload["proposal_id"]


@pytest.mark.parametrize("kind", ["DOCUMENT", "DOCUMENT_VERSION", "ASSERTION", "TIMESTAMP", "TEXT"])
def test_shared_families_and_free_text_are_not_governance_paths(kind):
    with pytest.raises(ValueError):
        m.GovernanceLink(kind, "INT-SYN-1")


def test_shared_ids_cannot_replace_explicit_event_linkage():
    with pytest.raises(ValueError, match="structured proposal"):
        event("PROPOSAL", payload={"document_id": "DOC-SYN-1", "assertion_id": "AST-SYN-1",
                                   "message": "PROPOSAL-SYN-1"})
    with pytest.raises(ValueError, match="direct interaction"):
        event(interaction_id=None)
    with pytest.raises(ValueError, match="explicit feedback"):
        event("FEEDBACK", feedback_id=None)


@pytest.mark.parametrize("changes", [
    {"interaction_id": "INT-SYN-OTHER"}, {"feedback_id": "FB-SYN-OTHER"},
    {"payload": {"proposal_id": "PROPOSAL-SYN-OTHER"}},
    {"payload": {"proposal_id": "PROPOSAL-SYN-1", "feedback_id": "FB-SYN-OTHER"}},
])
def test_conflicting_explicit_event_paths_are_rejected(changes):
    with pytest.raises(ValueError):
        event("PROPOSAL", **changes)


def test_later_history_is_separate_and_append_order_is_not_timestamp_order():
    original = replace(explanation(), related_governance=m.GovernanceTimeline(()))
    later = replace(original, related_governance=m.GovernanceTimeline([
        event(event_id=1, occurred_at=NOW), event("PROPOSAL", event_id=9, occurred_at=THEN),
    ]))
    for name in ("identity", "temporal", "answer", "retrieved_evidence", "knowledge", "claims", "citations"):
        assert getattr(later, name) is getattr(original, name)
    assert original.related_governance.events == ()
    assert tuple(e.event_id for e in later.related_governance.events) == (1, 9)
    with pytest.raises(ValueError):
        m.GovernanceTimeline(list(reversed(later.related_governance.events)))
    with pytest.raises(ValueError):
        m.GovernanceTimeline([event(), event()])
    with pytest.raises(ValueError, match="different interaction"):
        replace(later, identity=replace(later.identity, interaction_id="INT-SYN-OTHER"))


def test_intentional_absence_and_never_recorded_facts_are_explicit():
    empty = replace(explanation(), knowledge=[], retrieved_evidence=[], claims=[], citations=[],
                    answer=m.AnswerSnapshot("Synthetic unsupported answer", m.ConfidenceSnapshot(None, None, None), [], None, None),
                    related_governance=m.GovernanceTimeline([]))
    assert empty.answer.confidence.label is empty.answer.confidence.rationale is None
    assert empty.answer.confidence.confidence_policy_id is empty.answer.policy_version_id is None
    assert empty.answer.escalation is None
    assert empty.knowledge == empty.retrieved_evidence == empty.claims == empty.citations == ()
    assert set(empty.unavailable_facts) == set(m.UnavailableFact)
    assert participant(value=None, normalized_scope=None).value is None


@pytest.mark.parametrize("label", [0.8, 1, True, "0.8", "CERTAIN", []])
def test_numeric_or_unknown_confidence_is_not_accepted(label):
    with pytest.raises(ValueError):
        m.ConfidenceSnapshot(label, None, None)


@pytest.mark.parametrize("value", [{1: "bad key"}, {"a": {"not-json"}}, {"a": float("nan")},
                                   {"a": float("inf")}, {"a": object()}])
def test_invalid_json_is_rejected(value):
    with pytest.raises(TypeError):
        participant(value=value)
    with pytest.raises(TypeError):
        event(payload=value)


@pytest.mark.parametrize("value", [[], "{}", None])
def test_audit_requires_mapping_not_arbitrary_json(value):
    with pytest.raises(TypeError):
        event(payload=value)


@pytest.mark.parametrize("identifier", ["", " ", None, 123])
def test_identity_fields_must_be_nonempty_strings(identifier):
    with pytest.raises(ValueError):
        replace(explanation().identity, interaction_id=identifier)
    with pytest.raises(ValueError):
        participant(assertion_id=identifier)


def test_wrong_nested_types_cannot_smuggle_mutable_sections():
    value = explanation()
    for name in ("identity", "temporal", "answer", "related_governance"):
        with pytest.raises(TypeError):
            replace(value, **{name: {}})
    for name in ("retrieved_evidence", "knowledge", "claims", "citations"):
        with pytest.raises(TypeError):
            replace(value, **{name: [{}]})
    with pytest.raises(TypeError):
        participant(normalized_scope=[])


def test_no_replay_or_current_lookup_fields_and_no_live_dependencies():
    forbidden = {"algorithm_version", "renderer_version", "replay", "comparison", "source_observations",
                 "knowledge_observations", "current_state", "current_value", "numeric_confidence", "request_id"}
    for contract in vars(m).values():
        if isinstance(contract, type) and hasattr(contract, "__dataclass_fields__"):
            assert forbidden.isdisjoint(f.name for f in fields(contract))
    tree = ast.parse(inspect.getsource(m))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {"__future__", "collections.abc", "dataclasses", "datetime", "enum", "math", "types"}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))


def test_constructing_contracts_never_accesses_database_files_or_services(monkeypatch):
    from backend import demo

    def forbidden(*args, **kwargs):
        raise AssertionError("contract attempted live access")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        for name in ("RetrievalService", "KnowledgeService", "GovernanceService", "compare_reasoning_input",
                     "reason", "assess_knowledge_aware_confidence", "decide_knowledge_aware_escalation"):
            patch.setattr(demo, name, forbidden)
        value = explanation()
        assert value.answer.answer == "Synthetic stored answer"
