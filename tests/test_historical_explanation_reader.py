"""M8.3 reader tests on isolated synthetic schema-v7 histories."""

from dataclasses import fields, replace
import json
import sqlite3

import pytest

from backend import database, demo
from backend.explanation.models import UnavailableFact
from backend.explanation.reader import (
    ExplanationDataError, ExplanationNotFoundError, HistoricalExplanationReader,
)
from test_historical_explanation_characterization import (
    JUNE, PA, PUBLIC, V1, V2, _ask, _decision, _execute, _get, _rows, _submit,
    history,  # Reuse M8.1's ten real execution/governance scenarios.
)


def read(settings, interaction_id):
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("BEGIN")
        return HistoricalExplanationReader(connection).read(interaction_id)


def corrupt(settings, sql, parameters=()):
    # Only these disposable test databases bypass schema guards.
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(sql, parameters)


def event(settings, interaction_id=None, feedback_id=None, payload=None):
    _execute(settings, "INSERT INTO audit_event(interaction_id, feedback_id, event_type, occurred_at, payload_json) "
             "VALUES (?, ?, 'SYNTHETIC_READER_EVENT', ?, ?)",
             (interaction_id, feedback_id, demo.APPROVED_TIME, json.dumps(payload or {})))
    return _rows(settings, "SELECT max(event_id) AS id FROM audit_event")[0]["id"]


def test_basic_stored_sections_and_determinism(history, settings):
    _, created, stored = history
    interaction_id = created["interaction_id"]
    explanation = read(settings, interaction_id)
    assert read(settings, interaction_id) == explanation
    assert explanation.identity.interaction_id == interaction_id
    assert explanation.identity.question == stored["question"]
    assert explanation.identity.intent == stored["intent"]
    assert explanation.identity.source_mode == stored["source_mode"]
    assert explanation.identity.selected_sources == tuple(stored["selected_sources"])
    assert explanation.identity.orchestration_trace == tuple(stored["orchestration_trace"])
    assert explanation.answer.answer == stored["answer"]
    assert explanation.answer.policy_version_id == stored["policy_version_id"]
    assert explanation.answer.confidence.label == stored["confidence"]
    assert explanation.answer.confidence.rationale == stored["confidence_rationale"]
    assert explanation.answer.confidence.confidence_policy_id == "CONF-PA-SYN-V2"
    assert [dict((f.name, getattr(r, f.name)) for f in fields(r))
            for r in explanation.answer.reconciliation] == stored["reconciliation"]
    escalation = explanation.answer.escalation
    if stored["escalation"] is None:
        assert escalation is None
    else:
        assert {k: getattr(escalation, k) for k in ("required", "reviewer", "reason")} == stored["escalation"]
        assert escalation.trigger_ids is None
    assert [c.claim_key for c in explanation.claims] == [c["claim_id"] for c in stored["claims"]]
    assert [c.text for c in explanation.claims] == [c["text"] for c in stored["claims"]]
    assert [list(c.evidence_ids) for c in explanation.claims] == [c["evidence_ids"] for c in stored["claims"]]
    for citation, original in zip(explanation.citations, stored["citations"], strict=True):
        assert {key: getattr(citation, key) for key in original if key != "claim_id"} == {
            key: value for key, value in original.items() if key != "claim_id"}
    assert explanation.unavailable_facts == tuple(UnavailableFact)


@pytest.mark.parametrize("requested", [None, JUNE])
def test_temporal_identity_is_stored_not_inferred(client, settings, requested):
    created = _ask(client, **({"as_of": requested} if requested else {}))
    interaction_id = created["interaction_id"]
    _execute(settings, "UPDATE interaction SET created_at = '2031-01-01T00:00:00Z' WHERE interaction_id = ?",
             (interaction_id,))
    value = read(settings, interaction_id)
    assert value.temporal.temporal_mode == ("AS_OF" if requested else "CURRENT")
    assert value.temporal.requested_as_of == requested
    assert value.temporal.legacy_as_of == _get(client, interaction_id)["as_of"]
    assert value.identity.created_at == "2031-01-01T00:00:00Z"


@pytest.mark.parametrize("mode", ["BASELINE", "PAYER_POLICY_UNAVAILABLE"])
def test_retrieved_supporting_cited_and_knowledge_stay_distinct(client, settings, mode):
    created = _ask(client, source_mode=mode)
    value = read(settings, created["interaction_id"])
    members = _rows(settings, "SELECT * FROM interaction_evidence WHERE interaction_id = ? ORDER BY ordinal",
                    (created["interaction_id"],))
    assert [(m.evidence_id, m.ordinal) for m in value.retrieved_evidence] == [
        (r["evidence_id"], r["ordinal"]) for r in members]
    retrieved = {m.evidence_id for m in value.retrieved_evidence}
    supporting = {e for c in value.claims for e in c.evidence_ids}
    cited = {c.evidence_id for c in value.citations}
    assert supporting == cited < retrieved
    assert "EV-SYN-NOTE-REQUEST-001" in retrieved - cited
    if mode != "BASELINE":
        assert value.knowledge[0].origin == "KNOWLEDGE"
        assert value.knowledge[0].evidence[0].evidence_id not in retrieved
    _execute(settings, "DELETE FROM citation WHERE citation_id = ?", (value.citations[0].citation_id,))
    after = read(settings, created["interaction_id"])
    assert after.claims == value.claims
    assert len(after.citations) == len(value.citations) - 1  # Do not invent a citation.


def test_knowledge_facts_and_multiple_persisted_orders(client, settings):
    created = _ask(client)
    interaction_id = created["interaction_id"]
    original = _rows(settings, "SELECT * FROM interaction_knowledge WHERE interaction_id = ?", (interaction_id,))[0]
    copied = original | {"interaction_knowledge_id": "IK-SYN-SECOND", "ordinal": 1, "origin": "KNOWLEDGE"}
    _execute(settings, f"INSERT INTO interaction_knowledge ({', '.join(copied)}) VALUES ({','.join('?' for _ in copied)})",
             tuple(copied.values()))
    _execute(settings, "INSERT INTO interaction_knowledge_evidence VALUES (?, ?, ?)",
             ("IK-SYN-SECOND", "EV-SYN-POL-V1-STEP-001", 0))
    _execute(settings, "INSERT INTO interaction_knowledge_evidence VALUES (?, ?, ?)",
             ("IK-SYN-SECOND", "EV-SYN-POL-V1-PA-001", 1))
    value = read(settings, interaction_id)
    assert [k.ordinal for k in value.knowledge] == [0, 1]
    assert [e.evidence_id for e in value.knowledge[1].evidence] == [
        "EV-SYN-POL-V1-STEP-001", "EV-SYN-POL-V1-PA-001"]
    participant = value.knowledge[0]
    for field in fields(participant):
        if field.name == "evidence":
            continue
        expected = original.get(field.name)
        if field.name in ("value", "normalized_scope", "lineage_ids", "correction_ids"):
            expected = json.loads(original[field.name + "_json"])
            if isinstance(expected, list):
                expected = tuple(expected)
        assert getattr(participant, field.name) == expected
    assert participant.decision_dimension == original["decision_type"]


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_later_governance_is_separate_and_append_ordered(client, settings, action):
    interaction_id = _ask(client)["interaction_id"]
    before = read(settings, interaction_id)
    feedback_id = _submit(client, interaction_id)
    pending = read(settings, interaction_id)
    _decision(client, feedback_id, action)
    after = read(settings, interaction_id)
    assert replace(after, related_governance=before.related_governance) == before
    assert after.related_governance.events[:len(pending.related_governance.events)] == pending.related_governance.events
    assert len(after.related_governance.events) > len(pending.related_governance.events)
    assert all(e.relationship.interaction_id == interaction_id for e in after.related_governance.events)
    assert [e.event_id for e in after.related_governance.events] == sorted(e.event_id for e in after.related_governance.events)
    assert any(e.payload.get("decision") == ("APPLIED" if action == "approve" else "REJECTED")
               for e in after.related_governance.events)


def test_explicit_relationships_and_no_shared_family_expansion(client, settings):
    first, other = _ask(client)["interaction_id"], _ask(client)["interaction_id"]
    feedback_id = _submit(client, first)
    proposal_id = _rows(settings, "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?", (feedback_id,))[0]["proposal_id"]
    direct = event(settings, first)
    feedback = event(settings, feedback_id=feedback_id)
    proposal = event(settings, payload={"proposal_id": proposal_id})
    unrelated = event(settings, other, payload={"assertion_id": PA, "document_version_id": V1})
    family_only = event(settings, payload={"assertion_id": PA, "document_version_id": V1,
                                           "document_id": "DOC-SYN-POL-VEL", "message": proposal_id})
    # A payload feedback/interaction ID alone is not one of the frozen admission paths.
    payload_only = event(settings, payload={"feedback_id": feedback_id, "interaction_id": first})
    _execute(settings, "INSERT INTO audit_event(event_type, occurred_at, payload_json) VALUES ('SYNTHETIC_UNRELATED', ?, '{')",
             (demo.APPROVED_TIME,))
    value = read(settings, first)
    events = {e.event_id: e for e in value.related_governance.events}
    assert events[direct].relationship.kind == "INTERACTION"
    assert events[feedback].relationship.kind == "FEEDBACK"
    assert events[proposal].relationship.kind == "PROPOSAL"
    assert not {unrelated, family_only, payload_only} & events.keys()
    assert len(events) == len(value.related_governance.events)
    assert not events.keys() & {e.event_id for e in read(settings, other).related_governance.events}


@pytest.mark.parametrize("damage", ["interaction", "feedback", "proposal", "missing_proposal", "payload_interaction", "payload_feedback"])
def test_contradictory_governance_links_fail(client, settings, damage):
    first, other = _ask(client)["interaction_id"], _ask(client)["interaction_id"]
    feedback = _submit(client, first)
    other_feedback = _submit(client, other)
    other_proposal = _rows(settings, "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?", (other_feedback,))[0]["proposal_id"]
    if damage == "interaction":
        event(settings, other, feedback)
    elif damage == "feedback":
        event(settings, first, other_feedback)
    elif damage == "proposal":
        event(settings, first, feedback, {"proposal_id": other_proposal})
    elif damage == "missing_proposal":
        event(settings, first, payload={"proposal_id": "PROPOSAL-SYN-MISSING"})
    elif damage == "payload_interaction":
        event(settings, first, payload={"interaction_id": other})
    else:
        event(settings, first, feedback, {"feedback_id": other_feedback})
    with pytest.raises(ExplanationDataError):
        read(settings, first)


def test_missing_interaction_is_explicit(client, settings):
    with pytest.raises(ExplanationNotFoundError):
        read(settings, "INT-SYN-MISSING")


@pytest.mark.parametrize("table,column,value", [
    ("interaction", "selected_sources_json", "{"),
    ("interaction", "selected_sources_json", "{}"),
    ("interaction", "retrieval_trace_json", "[1]"),
    ("interaction", "reconciliation_json", "{}"),
    ("interaction", "reconciliation_json", '[{"type":"X"}]'),
    ("interaction", "escalation_json", '{"required":1,"reviewer":null,"reason":null}'),
    ("interaction", "escalation_json", '{"required":true,"required":false}'),
    ("interaction_knowledge", "value_json", "NaN"),
    ("interaction_knowledge", "normalized_scope_json", "[]"),
    ("interaction_knowledge", "lineage_ids_json", "{"),
    ("interaction_knowledge", "correction_ids_json", '[""]'),
    ("supported_claim", "evidence_ids_json", "null"),
    ("audit_event", "payload_json", "{"),
])
def test_malformed_json_and_shapes_fail(client, settings, table, column, value):
    interaction_id = _ask(client)["interaction_id"]
    _execute(settings, f"UPDATE {table} SET {column} = ? WHERE interaction_id = ?", (value, interaction_id))
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


@pytest.mark.parametrize("sql", [
    "DELETE FROM interaction_evidence WHERE interaction_id = ? AND ordinal = 0",
    "UPDATE interaction_evidence SET evidence_id = 'EV-SYN-MISSING' WHERE interaction_id = ? AND ordinal = 0",
    "UPDATE interaction_evidence SET ordinal = ordinal + 100 WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET ordinal = -1 WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET state_at_execution = 'CANDIDATE' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET assertion_id = 'AST-SYN-MISSING' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET document_id = 'DOC-SYN-MISSING' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET document_version_id = 'DV-SYN-POL-VEL-V2' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET effective_to = '2020-01-01T00:00:00Z' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET source_id = '' WHERE interaction_id = ?",
    "UPDATE interaction_knowledge SET lineage_ids_json = '[\"LINEAGE-SYN-MISSING\"]' WHERE interaction_id = ?",
    "UPDATE interaction SET temporal_mode = 'AS_OF', requested_as_of = NULL WHERE interaction_id = ?",
    "UPDATE supported_claim SET evidence_ids_json = '[\"EV-SYN-NONMEMBER\"]' WHERE interaction_id = ?",
    "UPDATE citation SET supported_claim_id = 'SC-SYN-MISSING' WHERE interaction_id = ?",
    "UPDATE citation SET evidence_id = 'EV-SYN-POL-V2-PA-001' WHERE interaction_id = ?",
    "UPDATE citation SET document_version_id = 'DV-SYN-POL-VEL-V2' WHERE interaction_id = ?",
    "UPDATE citation SET relevant_excerpt = '' WHERE interaction_id = ?",
    "DELETE FROM supported_claim WHERE interaction_id = ?",
    "DELETE FROM interaction_knowledge WHERE interaction_id = ?",
])
def test_corrupt_snapshot_relationships_fail(client, settings, sql):
    interaction_id = _ask(client)["interaction_id"]
    corrupt(settings, sql, (interaction_id,))
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


@pytest.mark.parametrize("table", ["interaction_evidence", "interaction_knowledge", "interaction_knowledge_evidence"])
def test_duplicate_ordinals_are_not_repaired(client, settings, table):
    interaction_id = _ask(client)["interaction_id"]
    # Recreate just the damaged table without constraints to model storage corruption.
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(f"CREATE TABLE synthetic_damage AS SELECT * FROM {table}")
        connection.execute(f"DROP TABLE {table}")
        connection.execute(f"ALTER TABLE synthetic_damage RENAME TO {table}")
        connection.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


@pytest.mark.parametrize("column,value", [("evidence_id", "EV-SYN-MISSING"),
                                          ("evidence_id", "EV-SYN-POL-V2-PA-001"),
                                          ("interaction_knowledge_id", "IK-SYN-MISSING"),
                                          ("ordinal", 2)])
def test_invalid_knowledge_evidence_membership(client, settings, column, value):
    interaction_id = _ask(client)["interaction_id"]
    corrupt(settings, f"UPDATE interaction_knowledge_evidence SET {column} = ?", (value,))
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


def test_cross_interaction_citation_reverse_link_fails(client, settings):
    first, other = _ask(client)["interaction_id"], _ask(client)["interaction_id"]
    corrupt(settings, "UPDATE citation SET interaction_id = ? WHERE interaction_id = ?", (other, first))
    for interaction_id in (first, other):
        with pytest.raises(ExplanationDataError):
            read(settings, interaction_id)


def test_citation_must_support_its_own_claim(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    value = read(settings, interaction_id)
    wrong = next(c for c in value.claims if value.citations[0].evidence_id not in c.evidence_ids)
    corrupt(settings, "UPDATE citation SET supported_claim_id = ? WHERE citation_id = ?",
            (wrong.supported_claim_id, value.citations[0].citation_id))
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


def test_copied_provenance_cannot_contradict_other_snapshot_copies(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    _execute(settings, "UPDATE citation SET source_id = 'SRC-SYN-CONTRADICTION' "
             "WHERE interaction_id = ? AND document_version_id = ?", (interaction_id, V1))
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


def test_current_mutations_cannot_change_execution_or_recorded_audit(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    feedback = _submit(client, interaction_id)
    before = read(settings, interaction_id)
    _execute(settings, "UPDATE knowledge_assertion SET state = 'SUPERSEDED', value_json = 'false', "
             "normalized_scope_json = '{}', effective_from = '2040-01-01T00:00:00Z', effective_to = NULL, "
             "recorded_at = '2040-01-01T00:00:00Z'")
    _execute(settings, "UPDATE evidence_item SET source_id = 'SRC-SYN-LATER', source_title = 'Synthetic later', "
             "source_type = 'SYNTHETIC_LATER', version = 'later', timestamp = '2040-01-01T00:00:00Z', "
             "section = 'later', relevant_excerpt = 'Synthetic later text', structured_data = '{}'")
    _execute(settings, "UPDATE source_document_version SET is_current = 0, version = 'later', "
             "effective_from = '2040-01-01T00:00:00Z', effective_to = NULL")
    _execute(settings, "UPDATE source_document SET source_id = 'SRC-SYN-LATER', source_title = 'Synthetic later'")
    _execute(settings, "UPDATE correction_proposal SET status = 'REJECTED', decided_at = ?, "
             "decision_actor = 'Synthetic reviewer', decision_role = 'SYNTHETIC', decision_rationale = 'Synthetic later' "
             "WHERE feedback_id = ?", (demo.APPROVED_TIME, feedback))
    assert read(settings, interaction_id) == before


def test_no_live_service_or_policy_calls_and_no_writes(client, settings, monkeypatch):
    from backend.knowledge import service, governance_service
    from backend.retrieval import service as retrieval_service
    from backend.reasoning import comparison, policies

    interaction_id = _ask(client)["interaction_id"]
    expected = read(settings, interaction_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("Historical reader attempted live execution")

    for module, names in (
        (demo, ("RetrievalService", "KnowledgeService", "GovernanceService", "compare_reasoning_input", "reason",
                "assess_knowledge_aware_confidence", "decide_knowledge_aware_escalation", "ask_question",
                "_supported_claims", "_resolve_claim_citations", "_reconciliation_payload", "_escalation_payload")),
        (retrieval_service, ("RetrievalService",)), (service, ("KnowledgeService",)),
        (governance_service, ("GovernanceService",)), (comparison, ("compare_reasoning_input",)),
        (policies, ("reason", "assess_confidence", "decide_escalation",
                    "assess_knowledge_aware_confidence", "decide_knowledge_aware_escalation")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("BEGIN")
        connection.execute("PRAGMA query_only = ON")
        before_changes = connection.total_changes
        statements = []
        connection.set_trace_callback(statements.append)
        identities_only = {
            "evidence_item": {"evidence_id", "document_version_id"},
            "knowledge_assertion": {"assertion_id", "document_version_id"},
            "source_document": {"document_id"},
            "source_document_version": {"document_version_id", "document_id"},
            "correction_proposal": {"proposal_id", "feedback_id"},
            "feedback": {"feedback_id", "interaction_id"},
        }

        def authorize(action, table, column, *unused):
            if action == sqlite3.SQLITE_READ and table in identities_only:
                return sqlite3.SQLITE_OK if column in identities_only[table] else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        reader = HistoricalExplanationReader(connection)
        assert reader.read(interaction_id) == expected
        assert connection.total_changes == before_changes
        assert connection.in_transaction
        assert connection.row_factory is None
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_public_get_and_route_surface_unchanged(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    before = _get(client, interaction_id)
    read(settings, interaction_id)
    assert _get(client, interaction_id) == before
    assert set(before) == PUBLIC
    assert client.get(f"/api/v1/interactions/{interaction_id}/explanation").status_code == 404


def test_intentional_absence_and_unavailable_are_different(client, settings):
    created = client.post("/api/v1/questions", json={"question": "Synthetic weather question"}).json()
    value = read(settings, created["interaction_id"])
    assert value.retrieved_evidence == value.knowledge == value.claims == value.citations == ()
    assert value.answer.confidence.label is None
    assert value.answer.confidence.rationale is None
    assert value.answer.confidence.confidence_policy_id is None
    assert value.answer.escalation is None
    assert value.answer.reconciliation == ()
    assert value.unavailable_facts == tuple(UnavailableFact)
    _execute(settings, "DELETE FROM audit_event WHERE interaction_id = ?", (created["interaction_id"],))
    assert read(settings, created["interaction_id"]).related_governance.events == ()


def test_reset_schema_and_integrity(settings):
    database.reset_demo_database(settings)
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_stored_decisions_are_never_recalculated(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    # Deliberately unlike current policies: this reader exposes recorded output.
    escalation = {"required": False, "reviewer": "Synthetic stored reviewer", "reason": "Synthetic stored reason"}
    reconciliation = [{"type": "SYNTHETIC_STORED", "severity": "HIGH", "resolution_state": "UNRESOLVED",
                       "explanation": "Synthetic stored finding"}]
    _execute(settings, "UPDATE interaction SET confidence = 'LOW', confidence_rationale = 'Synthetic stored rationale', "
             "confidence_policy_id = 'CONF-SYN-HISTORICAL', escalation_json = ?, reconciliation_json = ? "
             "WHERE interaction_id = ?", (json.dumps(escalation), json.dumps(reconciliation), interaction_id))
    value = read(settings, interaction_id)
    assert value.answer.confidence.label == "LOW"
    assert value.answer.confidence.rationale == "Synthetic stored rationale"
    assert value.answer.confidence.confidence_policy_id == "CONF-SYN-HISTORICAL"
    assert value.answer.escalation.required is False
    assert value.answer.escalation.trigger_ids is None
    assert value.answer.reconciliation[0].type == "SYNTHETIC_STORED"


def test_uncited_evidence_still_requires_valid_version_identity(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    corrupt(settings, "UPDATE evidence_item SET document_version_id = 'DV-SYN-MISSING' "
            "WHERE evidence_id = 'EV-SYN-NOTE-REQUEST-001'")
    with pytest.raises(ExplanationDataError):
        read(settings, interaction_id)


def test_duplicate_proposal_json_key_cannot_hide_related_corruption(client, settings):
    interaction_id = _ask(client)["interaction_id"]
    feedback_id = _submit(client, interaction_id)
    proposal_id = _rows(settings, "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?",
                        (feedback_id,))[0]["proposal_id"]
    payload = '{"proposal_id":"PROPOSAL-SYN-UNRELATED","proposal_id":' + json.dumps(proposal_id) + '}'
    _execute(settings, "INSERT INTO audit_event(event_type, occurred_at, payload_json) VALUES (?, ?, ?)",
             ("SYNTHETIC_DUPLICATE_KEY", demo.APPROVED_TIME, payload))
    with pytest.raises(ExplanationDataError, match="Duplicate JSON key"):
        read(settings, interaction_id)
