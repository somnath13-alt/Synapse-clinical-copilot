"""M8.1: characterize v7, not the planned explanation reader or replay.

Explanation gap inventory (labels apply to the named facts, not whole sections):
A identity: PERSISTED/EXPOSED id, question, intent; ABSENT standalone request ID.
B temporal: PERSISTED mode/requested_as_of, NOT EXPOSED; public as_of is the
  synthetic execution timestamp. DERIVABLE FROM IMMUTABLE SNAPSHOT: request mode.
C answer: PERSISTED/EXPOSED answer_text; POST status is ABSENT from storage/GET.
D retrieval: PERSISTED membership/order/trace; EXPOSED trace only. DERIVABLE FROM
  IMMUTABLE SNAPSHOT: retrieved/supporting/cited relationships. REQUIRES MUTABLE
  LOOKUP: uncited evidence excerpts, structured data and full source provenance.
E knowledge: PERSISTED execution facts and ordered evidence IDs, NOT EXPOSED.
  REQUIRES MUTABLE LOOKUP: predicate and unsnapshotted evidence text. ABSENT:
  independent execution-time predicate, full serialized comparison input.
F claims/citations: PERSISTED/EXPOSED text, support IDs and copied provenance.
G reconciliation: PERSISTED/EXPOSED public projection; ABSENT intermediate
  comparison objects, their independent source values/scopes and full findings.
H confidence/escalation: PERSISTED/EXPOSED result/rationale and escalation
  required/reviewer/reason; PERSISTED confidence_policy_id NOT EXPOSED. ABSENT
  structured escalation trigger IDs and historical policy implementation.
I governance: PERSISTED explicit feedback/proposal/review/lineage/update links
  and event payloads, NOT EXPOSED by historical GET. Separate audit GET exposes
  direct interaction/feedback events, but not proposal-payload-only matches.
  DERIVABLE FROM IMMUTABLE SNAPSHOT: recorded lineage/correction IDs and event
  facts. REQUIRES MUTABLE LOOKUP: current proposal status; it is not old status.

These are prototype records, not tamper-evident, compliance-grade or a certified
audit trail. Actor/role metadata is not authenticated identity. Corruption below
uses only each test's disposable database, deliberately bypassing constraints
where needed; it specifies current behavior, not the future failure contract.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.reasoning import policies


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
PA = "AST-SYN-POL-V1-PA"
JUNE = "2026-06-15T15:45:00Z"
JULY = "2026-07-03T16:45:00Z"
PUBLIC = set("interaction_id question intent as_of source_mode selected_sources "
             "orchestration_trace answer confidence confidence_rationale "
             "reconciliation escalation policy_version_id claims citations".split())
COLUMNS = {
    "interaction": "interaction_id question intent as_of source_mode selected_sources_json "
    "retrieval_trace_json answer_text confidence confidence_rationale reconciliation_json "
    "escalation_json policy_version_id temporal_mode requested_as_of confidence_policy_id created_at",
    "interaction_evidence": "interaction_id evidence_id ordinal",
    "interaction_knowledge": "interaction_knowledge_id interaction_id assertion_id ordinal origin "
    "state_at_execution decision_type value_json normalized_scope_json effective_from effective_to "
    "recorded_at source_id source_type document_id document_version_id document_version "
    "document_effective_from document_effective_to lineage_ids_json correction_ids_json",
    "interaction_knowledge_evidence": "interaction_knowledge_id evidence_id ordinal",
    "supported_claim": "supported_claim_id interaction_id claim_key claim_text evidence_ids_json",
    "citation": "citation_id interaction_id supported_claim_id evidence_id source_id "
    "document_version_id source_title source_type version timestamp section relevant_excerpt",
}
DECISION_KEYS = set("action proposal_id feedback_id actor actor_role decision rationale "
                    "target_document_version_id proposed_document_version_id replacement_items "
                    "before after lineage_ids review_id supersession_id update_id failure_reason timestamp".split())
SUBMISSION_KEYS = set("action proposal_id feedback_id actor actor_role rationale message status "
                      "target_document_version_id proposed_document_version_id replacement_items timestamp".split())
REPLACEMENT_KEYS = set("predecessor_assertion_id successor_assertion_id predicate decision_dimension "
                       "normalized_scope predecessor_value successor_value predecessor_effective_from "
                       "predecessor_effective_to successor_effective_from successor_effective_to "
                       "predecessor_document_version_id successor_document_version_id predecessor_evidence_ids "
                       "successor_evidence_ids predecessor_state successor_state".split())


def _rows(settings, sql, parameters=()):
    with database.managed_connection(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def _execute(settings, sql, parameters=()):
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(sql, parameters)


def _snapshot(settings, interaction_id):
    # Raw rows for equality/inventory only; this is not an explanation projection.
    result = {
        table: _rows(settings, f"SELECT * FROM {table} WHERE interaction_id = ? ORDER BY rowid",
                     (interaction_id,))
        for table in COLUMNS if table != "interaction_knowledge_evidence"
    }
    result["interaction_knowledge_evidence"] = _rows(
        settings, "SELECT * FROM interaction_knowledge_evidence WHERE interaction_knowledge_id IN "
        "(SELECT interaction_knowledge_id FROM interaction_knowledge WHERE interaction_id = ?) ORDER BY rowid",
        (interaction_id,))
    return result


def _ask(client, **extra):
    response = client.post("/api/v1/questions", json={"question": demo.CANONICAL_QUESTION, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _get(client, interaction_id):
    response = client.get(f"/api/v1/interactions/{interaction_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _submit(client, interaction_id):
    response = client.post("/api/v1/feedback", json={
        "interaction_id": interaction_id, "actor": "Synthetic M8 Clinician",
        "actor_role": "CLINICIAN", "message": "Synthetic newer policy correction.",
        "target_version_id": V1, "proposed_version_id": V2,
        "replacement_items": [
            {"predecessor_assertion_id": f"AST-SYN-POL-V1-{suffix}",
             "successor_assertion_id": f"AST-SYN-POL-V2-{suffix}"}
            for suffix in ("PA", "STEP")],
    })
    assert response.status_code == 200, response.text
    return response.json()["feedback_id"]


def _decision(client, feedback_id, action="approve", expected=200):
    response = client.post(f"/api/v1/feedback/{feedback_id}/{action}", json={
        "reviewer": "Synthetic M8 Reviewer", "rationale": "Synthetic review rationale."})
    assert response.status_code == expected, response.text
    return response.json()


SCENARIOS = ("current", "pending", "approved_current", "approved_june", "approved_july",
             "unavailable", "conflict", "rejected", "stale", "future")


@pytest.fixture(params=SCENARIOS)
def history(request, client, settings):
    scenario = request.param
    original = _ask(client)
    original_rows = _snapshot(settings, original["interaction_id"])
    if scenario == "future":
        _execute(settings, "UPDATE source_document_version SET effective_from = ? WHERE document_version_id = ?",
                 ("2030-01-01T00:00:00Z", V2))
        _execute(settings, "UPDATE knowledge_assertion SET effective_from = ? WHERE document_version_id = ?",
                 ("2030-01-01T00:00:00Z", V2))
    if scenario in {"pending", "approved_current", "approved_june", "approved_july", "rejected", "stale", "future"}:
        feedback = _submit(client, original["interaction_id"])
        if scenario.startswith("approved"):
            _decision(client, feedback)
        elif scenario == "rejected":
            _decision(client, feedback, "reject")
        elif scenario == "stale":
            competing = _submit(client, original["interaction_id"])
            _decision(client, competing)
            _decision(client, feedback, expected=409)
        elif scenario == "future":
            _decision(client, feedback, expected=409)
    if scenario == "approved_june":
        created = _ask(client, as_of=JUNE)
    elif scenario == "approved_july":
        created = _ask(client, as_of=JULY)
    elif scenario in {"approved_current", "pending"}:
        created = _ask(client)
    elif scenario == "unavailable":
        created = _ask(client, source_mode="PAYER_POLICY_UNAVAILABLE")
    elif scenario == "conflict":
        created = _ask(client, source_mode="TEST_ONLY_FORMULARY_SUBSTITUTION")
    else:
        created = original
    assert _snapshot(settings, original["interaction_id"]) == original_rows
    return scenario, created, _get(client, created["interaction_id"])


def test_actual_historical_response_and_snapshot_inventory(history, settings):
    scenario, created, stored = history
    snapshot = _snapshot(settings, created["interaction_id"])
    row = snapshot["interaction"][0]
    assert set(stored) == PUBLIC
    assert set(created) == PUBLIC - {"as_of", "source_mode"} | {"status"}
    for key in set(created) & PUBLIC:
        assert stored[key] == created[key]
    assert stored["as_of"] == row["as_of"] == row["created_at"]
    assert stored["source_mode"] == row["source_mode"]
    assert stored["answer"] == row["answer_text"]
    for column, key in (("selected_sources_json", "selected_sources"),
                        ("retrieval_trace_json", "orchestration_trace"),
                        ("reconciliation_json", "reconciliation"), ("escalation_json", "escalation")):
        assert (json.loads(row[column]) if row[column] is not None else None) == stored[key]
    assert row["confidence_policy_id"] == "CONF-PA-SYN-V2"
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        for table, columns in COLUMNS.items():
            assert [r[1] for r in connection.execute(f"PRAGMA table_info({table})")] == columns.split()
    assert {"temporal_mode", "requested_as_of", "created_at", "confidence_policy_id",
            "interaction_evidence", "knowledge", "governance", "comparison"}.isdisjoint(stored)
    expected_version = V2 if scenario in {"approved_current", "approved_july"} else V1
    assert row["policy_version_id"] == (None if scenario == "unavailable" else expected_version)
    if scenario in {"unavailable", "conflict"}:
        assert stored["confidence"] == "LOW"
        assert stored["escalation"]["required"] is True
        assert set(stored["escalation"]) == {"required", "reviewer", "reason"}
    else:
        assert stored["confidence"] == ("MEDIUM" if scenario == "approved_july" else "HIGH")
        assert stored["escalation"] is None
    if scenario == "conflict":
        assert any(item["severity"] == "HIGH" and item["resolution_state"] == "UNRESOLVED"
                   for item in stored["reconciliation"])
    for item in stored["reconciliation"]:
        assert set(item) == {"type", "severity", "resolution_state", "explanation"}
    claims = snapshot["supported_claim"]
    assert stored["claims"] == [{"claim_id": c["claim_key"], "text": c["claim_text"],
                                 "evidence_ids": json.loads(c["evidence_ids_json"])} for c in claims]
    claim_keys = {c["supported_claim_id"]: c["claim_key"] for c in claims}
    assert stored["citations"] == [
        {**{key: value for key, value in c.items()
            if key not in {"citation_id", "interaction_id", "supported_claim_id"}},
         "claim_id": claim_keys[c["supported_claim_id"]]} for c in snapshot["citation"]]
    knowledge = snapshot["interaction_knowledge"]
    assert len(knowledge) == 1
    participant = knowledge[0]
    assert participant["ordinal"] == 0
    assert participant["document_version_id"] == expected_version
    assert participant["assertion_id"] == f"AST-SYN-POL-{'V2' if expected_version == V2 else 'V1'}-PA"
    assert participant["state_at_execution"] == ("SUPERSEDED" if scenario == "approved_june" else "APPLIED")
    lineage_ids = json.loads(participant["lineage_ids_json"])
    correction_ids = json.loads(participant["correction_ids_json"])
    if scenario.startswith("approved"):
        assert lineage_ids and correction_ids
        assert set(lineage_ids) <= {r["lineage_id"] for r in _rows(settings, "SELECT lineage_id FROM assertion_lineage")}
        assert set(correction_ids) <= {r["feedback_id"] for r in _rows(settings, "SELECT feedback_id FROM feedback")}
    else:
        assert lineage_ids == correction_ids == []


def test_historical_get_never_reexecutes_services_policies_or_composition(history, client, settings, monkeypatch):
    _, created, stored = history
    before = _snapshot(settings, created["interaction_id"])
    def forbidden(*args: Any, **kwargs: Any):
        raise AssertionError("Historical display attempted live execution")
    for name in ("RetrievalService", "KnowledgeService", "GovernanceService", "compare_reasoning_input",
                 "reason", "assess_knowledge_aware_confidence", "decide_knowledge_aware_escalation",
                 "ask_question", "_as_of_reasoning_bundle", "_knowledge_reasoning_input", "_supported_claims",
                 "_resolve_claim_citations", "_reconciliation_payload", "_comparison_reconciliation_payload",
                 "_escalation_payload", "_conflict_claims", "_knowledge_conflict_claims"):
        monkeypatch.setattr(demo, name, forbidden)
    for name in ("reason", "assess_confidence", "decide_escalation",
                 "assess_knowledge_aware_confidence", "decide_knowledge_aware_escalation"):
        monkeypatch.setattr(policies, name, forbidden)
    assert _get(client, created["interaction_id"]) == stored
    assert _snapshot(settings, created["interaction_id"]) == before


def test_temporal_identity_is_not_public_as_of_or_created_at(client, settings):
    first = _ask(client)
    _decision(client, _submit(client, first["interaction_id"]))
    for requested, mode, version in ((None, "CURRENT", V2), (JUNE, "AS_OF", V1), (JULY, "AS_OF", V2)):
        created = _ask(client, **({"as_of": requested} if requested else {}))
        row = _snapshot(settings, created["interaction_id"])["interaction"][0]
        assert (row["temporal_mode"], row["requested_as_of"], row["policy_version_id"]) == (mode, requested, version)
        assert row["created_at"] != requested
        assert _get(client, created["interaction_id"])["as_of"] == row["created_at"]


def test_retrieved_supporting_cited_and_knowledge_memberships_are_distinct(client, settings, monkeypatch):
    observed = []
    retrieve = demo.RetrievalService.retrieve
    def capture(self, request):
        bundle = retrieve(self, request)
        observed.append(tuple(bundle.evidence_by_id))
        return bundle
    monkeypatch.setattr(demo.RetrievalService, "retrieve", capture)
    for mode in ("BASELINE", "PAYER_POLICY_UNAVAILABLE"):
        created = _ask(client, source_mode=mode)
        snapshot = _snapshot(settings, created["interaction_id"])
        members = snapshot["interaction_evidence"]
        assert tuple(r["evidence_id"] for r in members) == observed[-1]
        assert [r["ordinal"] for r in members] == list(range(len(members)))
        retrieved = set(observed[-1])
        supporting = {e for c in created["claims"] for e in c["evidence_ids"]}
        cited = {c["evidence_id"] for c in created["citations"]}
        assert supporting == cited < retrieved  # Actual fixtures, not a general equivalence.
        assert {"EV-SYN-FORM-PREF-001", "EV-SYN-NOTE-REQUEST-001"} <= retrieved - cited
        knowledge = snapshot["interaction_knowledge"]
        assert len(knowledge) == 1
        assert knowledge[0]["origin"] == ("KNOWLEDGE" if mode != "BASELINE" else "CORROBORATED")
        assert [(r["evidence_id"], r["ordinal"]) for r in snapshot["interaction_knowledge_evidence"]] == [
            ("EV-SYN-POL-V1-PA-001", 0)]
        if mode != "BASELINE":
            assert "EV-SYN-POL-V1-PA-001" not in retrieved | cited | supporting


def test_snapshot_facts_survive_mutable_assertion_document_and_evidence_changes(client, settings):
    created = _ask(client)
    interaction_id = created["interaction_id"]
    before = _snapshot(settings, interaction_id)
    stored = _get(client, interaction_id)
    knowledge = before["interaction_knowledge"][0]
    assert (knowledge["assertion_id"], knowledge["ordinal"], knowledge["origin"],
            knowledge["state_at_execution"], knowledge["decision_type"], knowledge["value_json"]) == (
                PA, 0, "CORROBORATED", "APPLIED", "COVERAGE_AUTHORIZATION", "true")
    assert json.loads(knowledge["normalized_scope_json"]) == {
        "condition_id": "SYN-COND-LDS", "medication_id": "SYN-MED-VEL",
        "payer_id": "SYN-PAYER-NHH", "plan_id": "SYN-PLAN-HLP"}
    assert (knowledge["effective_from"], knowledge["effective_to"], knowledge["recorded_at"]) == (
        "2026-01-01T00:00:00Z", "2026-06-30T23:59:59Z", "2025-12-15T12:05:00Z")
    assert (knowledge["source_id"], knowledge["source_type"], knowledge["document_id"],
            knowledge["document_version_id"], knowledge["document_version"]) == (
        "SRC-SYN-PAYER", "PAYER_POLICY", "DOC-SYN-POL-VEL", V1, "1.0")
    assert knowledge["document_effective_from"] == knowledge["effective_from"]
    assert knowledge["document_effective_to"] == knowledge["effective_to"]
    assert json.loads(knowledge["lineage_ids_json"]) == json.loads(knowledge["correction_ids_json"]) == []
    _execute(settings, "UPDATE knowledge_assertion SET state = 'SUPERSEDED', value_json = 'false', "
             "normalized_scope_json = '{}', effective_from = '2040-01-01T00:00:00Z', effective_to = NULL, "
             "recorded_at = '2040-01-01T00:00:00Z' WHERE assertion_id = ?", (PA,))
    _execute(settings, "UPDATE source_document_version SET is_current = 0, version = 'synthetic-mutated', "
             "effective_from = '2040-01-01T00:00:00Z', effective_to = NULL WHERE document_version_id = ?", (V1,))
    _execute(settings, "UPDATE source_document SET source_id = 'SRC-SYN-MUTATED' WHERE document_id = 'DOC-SYN-POL-VEL'")
    _execute(settings, "UPDATE evidence_item SET relevant_excerpt = 'Synthetic later text', structured_data = '{}' ")
    assert _snapshot(settings, interaction_id) == before
    assert _get(client, interaction_id) == stored
    # Full uncited provenance is only on mutable evidence_item; membership isn't a copy.
    uncited = "EV-SYN-NOTE-REQUEST-001"
    assert uncited in {r["evidence_id"] for r in before["interaction_evidence"]}
    assert uncited not in {r["evidence_id"] for r in before["citation"]}
    assert _rows(settings, "SELECT relevant_excerpt FROM evidence_item WHERE evidence_id = ?", (uncited,)) == [
        {"relevant_excerpt": "Synthetic later text"}]


@pytest.mark.parametrize("action", ["approve", "reject", "stale", "future"])
def test_later_governance_payloads_links_and_execution_immutability(client, settings, action):
    original = _ask(client)
    interaction_id = original["interaction_id"]
    before = _snapshot(settings, interaction_id)
    stored = _get(client, interaction_id)
    if action == "future":
        _execute(settings, "UPDATE source_document_version SET effective_from = '2030-01-01T00:00:00Z' "
                 "WHERE document_version_id = ?", (V2,))
        _execute(settings, "UPDATE knowledge_assertion SET effective_from = '2030-01-01T00:00:00Z' "
                 "WHERE document_version_id = ?", (V2,))
    feedback_id = _submit(client, interaction_id)
    submitted_events = _rows(settings, "SELECT * FROM audit_event WHERE feedback_id = ? ORDER BY event_id", (feedback_id,))
    submission = next(r for r in submitted_events if r["event_type"] == "FEEDBACK_SUBMITTED")
    payload = json.loads(submission["payload_json"])
    assert set(payload) == SUBMISSION_KEYS
    assert (payload["action"], payload["status"], payload["actor"], payload["actor_role"]) == (
        "SUBMIT", "PENDING", "Synthetic M8 Clinician", "CLINICIAN")
    assert payload["message"] == payload["rationale"] == "Synthetic newer policy correction."
    assert payload["timestamp"] == submission["occurred_at"] == demo.SUBMITTED_TIME
    assert payload["feedback_id"] == feedback_id
    assert payload["target_document_version_id"] == V1
    assert payload["proposed_document_version_id"] == V2
    assert len(payload["replacement_items"]) == 2
    for item, suffix in zip(payload["replacement_items"], ("PA", "STEP")):
        assert set(item) == REPLACEMENT_KEYS
        assert item["predecessor_assertion_id"] == f"AST-SYN-POL-V1-{suffix}"
        assert item["successor_assertion_id"] == f"AST-SYN-POL-V2-{suffix}"
        assert item["predecessor_state"] == "APPLIED"
        assert item["successor_state"] == "CANDIDATE"
        assert item["predecessor_evidence_ids"] and item["successor_evidence_ids"]
    if action == "stale":
        _decision(client, _submit(client, interaction_id))
    _decision(client, feedback_id, "reject" if action == "reject" else "approve",
              expected=409 if action in {"stale", "future"} else 200)
    events = _rows(settings, "SELECT * FROM audit_event WHERE feedback_id = ? ORDER BY event_id", (feedback_id,))
    assert events[:len(submitted_events)] == submitted_events
    event_type = {"approve": "REVIEW_APPROVED", "reject": "REVIEW_REJECTED"}.get(action, "GOVERNANCE_APPROVAL_BLOCKED")
    decision_row = next(r for r in events if r["event_type"] == event_type)
    decision = json.loads(decision_row["payload_json"])
    assert set(decision) == DECISION_KEYS
    assert decision["proposal_id"] == payload["proposal_id"]
    assert decision["feedback_id"] == feedback_id
    assert decision["timestamp"] == decision_row["occurred_at"] == demo.APPROVED_TIME
    assert decision["actor"] == "Synthetic M8 Reviewer"
    assert decision["actor_role"] == "KNOWLEDGE_REVIEWER"
    assert decision["rationale"] == "Synthetic review rationale."
    assert decision["target_document_version_id"] == V1
    assert decision["proposed_document_version_id"] == V2
    assert decision["replacement_items"] == [
        {key: item[key] for key in ("predecessor_assertion_id", "successor_assertion_id")}
        for item in payload["replacement_items"]]
    for phase in ("before", "after"):
        state = decision[phase]
        assert set(state) == {"document_versions", "assertions"}
        assert [v["document_version_id"] for v in state["document_versions"]] == [V1, V2]
        assert all(set(v) == {"document_version_id", "is_current", "governance_state"}
                   for v in state["document_versions"])
        applied = action == "stale" or (action == "approve" and phase == "after")
        assert [v["is_current"] for v in state["document_versions"]] == [not applied, applied]
        assert state["assertions"] == [
            {"assertion_id": f"AST-SYN-POL-{version}-{suffix}", "state": status}
            for version, status in (("V1", "SUPERSEDED" if applied else "APPLIED"),
                                    ("V2", "APPLIED" if applied else "CANDIDATE"))
            for suffix in ("PA", "STEP")]
    assert "relevant_excerpt" not in json.dumps(payload)
    assert "predicate" not in json.dumps(decision)  # Decision has pair IDs, submission has semantics.
    if action in {"stale", "future"}:
        assert decision["failure_reason"] == {"stale": "STALE_TARGET", "future": "FUTURE_EFFECTIVE"}[action]
        assert decision["before"] == decision["after"]
        assert decision["action"] == "APPLY_ATTEMPT"
    else:
        assert decision["failure_reason"] is None
        assert decision["action"] == ("APPLY" if action == "approve" else "REJECT")
        assert decision["decision"] == ("APPLIED" if action == "approve" else "REJECTED")
    proposal = _rows(settings, "SELECT * FROM correction_proposal WHERE feedback_id = ?", (feedback_id,))[0]
    assert proposal["proposal_id"] == payload["proposal_id"]
    assert proposal["status"] == {"approve": "APPLIED", "reject": "REJECTED"}.get(action, "PENDING")
    assert _rows(settings, "SELECT interaction_id FROM feedback WHERE feedback_id = ?", (feedback_id,)) == [
        {"interaction_id": interaction_id}]
    for table, key in (("review", "review_id"), ("assertion_supersession", "supersession_id"),
                       ("knowledge_update", "update_id")):
        linked = _rows(settings, f"SELECT {key} FROM {table} WHERE feedback_id = ?", (feedback_id,))
        assert [r[key] for r in linked] == ([decision[key]] if decision[key] else [])
    assert {r["lineage_id"] for r in _rows(settings, "SELECT lineage_id FROM assertion_lineage WHERE feedback_id = ?", (feedback_id,))} == set(decision["lineage_ids"])
    audit = client.get(f"/api/v1/audit/{interaction_id}").json()["events"]
    assert {r["event_id"] for r in events} <= {r["event_id"] for r in audit}
    assert [r["event_id"] for r in audit] == sorted(r["event_id"] for r in audit)
    assert _snapshot(settings, interaction_id) == before
    assert _get(client, interaction_id) == stored
    if action in {"approve", "stale"}:
        assert _rows(settings, "SELECT state FROM knowledge_assertion WHERE assertion_id = ?", (PA,)) == [{"state": "SUPERSEDED"}]
        assert before["interaction_knowledge"][0]["state_at_execution"] == "APPLIED"
    _execute(settings, "UPDATE knowledge_assertion SET value_json = 'false', normalized_scope_json = '{}' "
             "WHERE assertion_id = ?", (PA,))
    _execute(settings, "UPDATE source_document_version SET version = 'synthetic-later-label' "
             "WHERE document_version_id = ?", (V1,))
    assert _rows(settings, "SELECT * FROM audit_event WHERE feedback_id = ? ORDER BY event_id", (feedback_id,)) == events
    assert _snapshot(settings, interaction_id) == before
    assert _get(client, interaction_id) == stored


def test_audit_linkage_does_not_include_shared_family_or_proposal_payload_only(client, settings):
    first, other = _ask(client), _ask(client)
    feedback_id = _submit(client, first["interaction_id"])
    proposal_id = _rows(settings, "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?", (feedback_id,))[0]["proposal_id"]
    _execute(settings, "INSERT INTO audit_event(interaction_id, feedback_id, event_type, occurred_at, payload_json) "
             "VALUES (NULL, NULL, 'SYNTHETIC_PROPOSAL_ONLY', ?, ?)",
             (demo.APPROVED_TIME, json.dumps({"proposal_id": proposal_id})))
    first_events = demo.get_audit(settings, first["interaction_id"])["events"]
    other_events = demo.get_audit(settings, other["interaction_id"])["events"]
    assert "SYNTHETIC_PROPOSAL_ONLY" not in {e["event_type"] for e in first_events}
    assert not any(e["feedback_id"] for e in other_events)
    assert {e["event_id"] for e in first_events}.isdisjoint(e["event_id"] for e in other_events)
    # Same document/assertion identity does not establish interaction feedback causality.
    assert _snapshot(settings, first["interaction_id"])["interaction_knowledge"][0]["assertion_id"] == (
        _snapshot(settings, other["interaction_id"])["interaction_knowledge"][0]["assertion_id"])
    with database.managed_connection(settings.database_path) as connection:
        assert [r[1] for r in connection.execute("PRAGMA table_info(audit_event)")] == [
            "event_id", "interaction_id", "feedback_id", "event_type", "occurred_at", "payload_json"]


@pytest.mark.parametrize("mutation", [
    "DELETE FROM interaction_evidence WHERE interaction_id = ?",
    "DELETE FROM interaction_evidence WHERE interaction_id = ? AND ordinal = 0",
    "DELETE FROM interaction_knowledge WHERE interaction_id = ?",
    "DELETE FROM interaction_knowledge_evidence WHERE interaction_knowledge_id IN "
    "(SELECT interaction_knowledge_id FROM interaction_knowledge WHERE interaction_id = ?)",
    "UPDATE interaction_knowledge SET assertion_id = 'AST-SYN-MISSING' WHERE interaction_id = ?",
    "UPDATE interaction_evidence SET ordinal = ordinal + 100 WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET ordinal = -1 WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET source_id = '' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET normalized_scope_json = '{' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET lineage_ids_json = '{' WHERE interaction_id = ?",
    "UPDATE interaction SET temporal_mode = 'AS_OF', requested_as_of = NULL WHERE interaction_id = ?",
])
def test_get_silently_ignores_damage_to_unread_snapshot_facts(client, settings, mutation):
    created = _ask(client)
    interaction_id = created["interaction_id"]
    stored = _get(client, interaction_id)
    before = _snapshot(settings, interaction_id)
    # A fresh raw connection disables FK/CHECK guards only in this disposable DB.
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        assert connection.execute(mutation, (interaction_id,)).rowcount > 0
    assert _snapshot(settings, interaction_id) != before
    assert _get(client, interaction_id) == stored


@pytest.mark.parametrize("table,column", [
    ("interaction", "selected_sources_json"), ("interaction", "retrieval_trace_json"),
    ("interaction", "reconciliation_json"), ("interaction", "escalation_json"),
    ("supported_claim", "evidence_ids_json"),
])
def test_get_fails_on_malformed_json_it_actually_reads(client, settings, table, column):
    created = _ask(client)
    _execute(settings, f"UPDATE {table} SET {column} = '{{' WHERE interaction_id = ?", (created["interaction_id"],))
    with pytest.raises(json.JSONDecodeError):
        client.get(f"/api/v1/interactions/{created['interaction_id']}")
    # TestClient normally reraises; real HTTP behavior is an unhandled 500.
    with TestClient(client.app, raise_server_exceptions=False) as http_client:
        response = http_client.get(f"/api/v1/interactions/{created['interaction_id']}")
        assert response.status_code == 500


def test_missing_interaction_and_partial_citations_are_not_validated(client, settings):
    assert client.get("/api/v1/interactions/INT-SYN-MISSING").json() == {"detail": "Interaction not found."}
    assert client.get("/api/v1/interactions/INT-SYN-MISSING").status_code == 404
    created = _ask(client)
    interaction_id = created["interaction_id"]
    stored = _get(client, interaction_id)
    _execute(settings, "UPDATE citation SET relevant_excerpt = '' WHERE interaction_id = ?", (interaction_id,))
    partial = _get(client, interaction_id)
    assert all(c["relevant_excerpt"] == "" for c in partial["citations"])
    assert partial["answer"] == stored["answer"]
    _execute(settings, "DELETE FROM citation WHERE interaction_id = ?", (interaction_id,))
    partial = _get(client, interaction_id)
    assert partial["citations"] == [] and partial["claims"] == stored["claims"]
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("DELETE FROM interaction WHERE interaction_id = ?", (interaction_id,))
    assert client.get(f"/api/v1/interactions/{interaction_id}").status_code == 404


def test_valid_unsupported_absence_is_preserved_without_inventing_explanation(client, settings):
    response = client.post("/api/v1/questions", json={"question": "Synthetic unsupported weather question"})
    assert response.status_code == 200
    created = response.json()
    assert created["status"] == "UNSUPPORTED_SCOPE"
    snapshot = _snapshot(settings, created["interaction_id"])
    stored = _get(client, created["interaction_id"])
    assert set(stored) == PUBLIC
    assert stored["confidence"] is stored["confidence_rationale"] is stored["escalation"] is None
    assert stored["claims"] == stored["citations"] == stored["reconciliation"] == []
    assert all(not rows for table, rows in snapshot.items() if table != "interaction")
    assert snapshot["interaction"][0]["confidence_policy_id"] is None


def test_wrong_json_shapes_and_orphaned_claim_links_partially_render(client, settings):
    created = _ask(client)
    interaction_id = created["interaction_id"]
    _execute(settings, "UPDATE interaction SET reconciliation_json = '{}' WHERE interaction_id = ?", (interaction_id,))
    assert _get(client, interaction_id)["reconciliation"] == {}
    _execute(settings, "UPDATE supported_claim SET evidence_ids_json = 'null' WHERE interaction_id = ?", (interaction_id,))
    assert all(c["evidence_ids"] is None for c in _get(client, interaction_id)["claims"])
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("DELETE FROM supported_claim WHERE interaction_id = ?", (interaction_id,))
    assert _rows(settings, "SELECT * FROM citation WHERE interaction_id = ?", (interaction_id,))
    historical = _get(client, interaction_id)
    assert historical["claims"] == historical["citations"] == []  # Inner join drops orphan citations.
    assert historical["answer"] == created["answer"]


def test_malformed_audit_is_ignored_by_interaction_get_but_fails_audit_get(client, settings):
    created = _ask(client)
    interaction_id = created["interaction_id"]
    stored = _get(client, interaction_id)
    _execute(settings, "UPDATE audit_event SET payload_json = '{' WHERE interaction_id = ?", (interaction_id,))
    assert _get(client, interaction_id) == stored
    with pytest.raises(json.JSONDecodeError):
        client.get(f"/api/v1/audit/{interaction_id}")
