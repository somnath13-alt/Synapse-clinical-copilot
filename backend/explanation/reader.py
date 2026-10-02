"""Read schema-v7 snapshots; never select, reason, repair, or replay.

The caller owns the connection and transaction. For a consistent multi-table
view, begin a read transaction before calling read(). No connection settings,
transactions, or data are changed here. Current tables supply only immutable
identity edges for validation, never historical semantic content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import models as m


class ExplanationNotFoundError(LookupError):
    """The requested interaction does not exist."""


class ExplanationDataError(RuntimeError):
    """Persisted facts cannot form a truthful historical explanation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExplanationDataError(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(token):
    raise ExplanationDataError(f"Nonfinite JSON constant: {token}")


def _json(raw, kind=None, *, nullable=False):
    if raw is None and nullable:
        return None
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    if kind is not None:
        _require(isinstance(value, kind), f"Expected stored JSON {kind.__name__}")
    return value


def _interval(start, end):
    if end is not None:
        _require(datetime.fromisoformat(start.replace("Z", "+00:00")) <=
                 datetime.fromisoformat(end.replace("Z", "+00:00")),
                 "Reversed snapshot interval")


class HistoricalExplanationReader:
    """Bounded relational reader using a caller-owned SQLite connection."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _rows(self, sql, parameters=()):
        cursor = self.connection.cursor()
        cursor.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in cursor.execute(sql, parameters)]
        finally:
            cursor.close()

    def _one(self, sql, parameters, message):
        rows = self._rows(sql, parameters)
        _require(len(rows) == 1, message)
        return rows[0]

    def read(self, interaction_id: str) -> m.InteractionExplanation:
        """Return stored facts or explicit not-found/data errors, without writes.

        SQLite operational errors (for example, a locked database) remain SQLite
        errors so callers do not misreport an operational failure as corruption.
        """
        try:
            return self._read(interaction_id)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise ExplanationDataError(
                f"Invalid historical snapshot for {interaction_id}: {error}"
            ) from error

    def _read(self, interaction_id):
        rows = self._rows("SELECT * FROM interaction WHERE interaction_id = ?", (interaction_id,))
        if not rows:
            raise ExplanationNotFoundError(f"Interaction not found: {interaction_id}")
        _require(len(rows) == 1, "Duplicate interaction identity")
        row = rows[0]
        identity = m.InteractionIdentity(
            row["interaction_id"], row["question"], row["intent"], row["source_mode"],
            _json(row["selected_sources_json"], list), _json(row["retrieval_trace_json"], list),
            row["created_at"],
        )
        temporal = m.TemporalContext(row["temporal_mode"], row["requested_as_of"], row["as_of"])
        reconciliation = _json(row["reconciliation_json"], list)
        reconciliation = tuple(m.ReconciliationSnapshot(**item) for item in reconciliation)
        escalation = _json(row["escalation_json"], dict, nullable=True)
        answer = m.AnswerSnapshot(
            row["answer_text"], m.ConfidenceSnapshot(row["confidence"], row["confidence_rationale"],
                                                    row["confidence_policy_id"]),
            reconciliation, None if escalation is None else m.EscalationSnapshot(**escalation),
            row["policy_version_id"],
        )
        if answer.policy_version_id is not None:
            self._version(answer.policy_version_id)
        retrieved = tuple(m.EvidenceMembership(r["evidence_id"], r["ordinal"]) for r in self._rows(
            "SELECT evidence_id, ordinal FROM interaction_evidence WHERE interaction_id = ? ORDER BY ordinal",
            (interaction_id,),
        ))
        for member in retrieved:
            self._evidence_version(member.evidence_id)
        knowledge = self._knowledge(interaction_id)
        claims = tuple(m.ClaimSnapshot(r["supported_claim_id"], r["claim_key"], r["claim_text"],
                                       _json(r["evidence_ids_json"], list)) for r in self._rows(
            "SELECT * FROM supported_claim WHERE interaction_id = ? ORDER BY rowid", (interaction_id,),
        ))
        retrieved_ids = {item.evidence_id for item in retrieved}
        claim_map = {claim.supported_claim_id: claim for claim in claims}
        _require(len(claim_map) == len(claims), "Duplicate claim identity")
        for claim in claims:
            _require(set(claim.evidence_ids) <= retrieved_ids, "Claim evidence outside retrieved snapshot")
        # Include reverse claim links so cross-interaction citations cannot hide
        # behind an interaction filter or an inner join that drops broken rows.
        citation_rows = self._rows(
            "SELECT * FROM citation WHERE interaction_id = ? OR supported_claim_id IN "
            "(SELECT supported_claim_id FROM supported_claim WHERE interaction_id = ?) ORDER BY rowid",
            (interaction_id, interaction_id),
        )
        citations = []
        evidence_copies, version_copies = {}, {}
        for r in citation_rows:
            _require(r.pop("interaction_id") == interaction_id, "Citation belongs to another interaction")
            citation = m.CitationSnapshot(**r)
            claim = claim_map.get(citation.supported_claim_id)
            _require(claim is not None, "Citation claim outside interaction snapshot")
            _require(citation.evidence_id in retrieved_ids, "Citation evidence outside retrieved snapshot")
            _require(citation.evidence_id in claim.evidence_ids, "Citation evidence does not support linked claim")
            _require(self._evidence_version(citation.evidence_id) == citation.document_version_id,
                     "Citation evidence/version identity mismatch")
            self._version(citation.document_version_id)
            copy = tuple(r[key] for key in r if key not in ("citation_id", "supported_claim_id"))
            previous = evidence_copies.setdefault(citation.evidence_id, copy)
            _require(previous == copy, "Contradictory copied evidence provenance")
            version_copy = (citation.source_id, citation.source_type, citation.source_title,
                            citation.version, citation.timestamp)
            previous = version_copies.setdefault(citation.document_version_id, version_copy)
            _require(previous == version_copy, "Contradictory copied document-version provenance")
            for participant in knowledge:
                if participant.document_version_id == citation.document_version_id:
                    _require((participant.source_id, participant.source_type, participant.document_version) ==
                             (citation.source_id, citation.source_type, citation.version),
                             "Citation/knowledge copied provenance mismatch")
            citations.append(citation)
        _require(len({c.citation_id for c in citations}) == len(citations), "Duplicate citation identity")
        return m.InteractionExplanation(identity, temporal, answer, retrieved, knowledge, claims,
                                        citations, self._timeline(interaction_id))

    def _version(self, version_id):
        m._text(version_id, "document_version_id")
        row = self._one("SELECT document_id FROM source_document_version WHERE document_version_id = ?",
                        (version_id,), "Missing document-version identity")
        m._text(row["document_id"], "document_id")
        self._one("SELECT document_id FROM source_document WHERE document_id = ?",
                  (row["document_id"],), "Missing document identity")
        return row["document_id"]

    def _evidence_version(self, evidence_id):
        version_id = self._one("SELECT document_version_id FROM evidence_item WHERE evidence_id = ?",
                               (evidence_id,), "Missing evidence identity")["document_version_id"]
        self._version(version_id)
        return version_id

    def _knowledge(self, interaction_id):
        # Orphans have no interaction_id at all. Detect rather than silently
        # dropping them in a join; no semantic interpretation of other histories.
        _require(not self._rows(
            "SELECT 1 FROM interaction_knowledge_evidence e WHERE NOT EXISTS "
            "(SELECT 1 FROM interaction_knowledge k WHERE k.interaction_knowledge_id = e.interaction_knowledge_id) LIMIT 1"
        ), "Knowledge evidence references nonexistent participation")
        result, identities, version_copies = [], set(), {}
        for r in self._rows("SELECT * FROM interaction_knowledge WHERE interaction_id = ? ORDER BY ordinal",
                            (interaction_id,)):
            r.pop("interaction_id")
            for target in ("value", "normalized_scope", "lineage_ids", "correction_ids"):
                r[target] = _json(r.pop(target + "_json"), nullable=target in ("value", "normalized_scope"))
            r["evidence"] = tuple(m.EvidenceMembership(e["evidence_id"], e["ordinal"]) for e in self._rows(
                "SELECT evidence_id, ordinal FROM interaction_knowledge_evidence "
                "WHERE interaction_knowledge_id = ? ORDER BY ordinal", (r["interaction_knowledge_id"],),
            ))
            item = m.KnowledgeParticipation(**r)
            key = (item.assertion_id, item.origin)
            _require(key not in identities, "Duplicate assertion/origin participation")
            identities.add(key)
            assertion = self._one("SELECT document_version_id FROM knowledge_assertion WHERE assertion_id = ?",
                                  (item.assertion_id,), "Missing assertion identity")
            _require(assertion["document_version_id"] == item.document_version_id,
                     "Assertion/document-version identity mismatch")
            _require(self._version(item.document_version_id) == item.document_id,
                     "Document/version identity mismatch")
            _interval(item.effective_from, item.effective_to)
            _interval(item.document_effective_from, item.document_effective_to)
            copy = (item.source_id, item.source_type, item.document_id, item.document_version,
                    item.document_effective_from, item.document_effective_to)
            _require(version_copies.setdefault(item.document_version_id, copy) == copy,
                     "Contradictory knowledge document-version snapshot")
            for evidence in item.evidence:
                _require(self._evidence_version(evidence.evidence_id) == item.document_version_id,
                         "Knowledge evidence/document-version identity mismatch")
            for feedback_id in item.correction_ids:
                self._one("SELECT feedback_id FROM feedback WHERE feedback_id = ?", (feedback_id,),
                          "Missing snapshotted correction identity")
            for lineage_id in item.lineage_ids:
                edge = self._one("SELECT predecessor_assertion_id, successor_assertion_id, feedback_id "
                                 "FROM assertion_lineage WHERE lineage_id = ?", (lineage_id,),
                                 "Missing snapshotted lineage identity")
                _require(item.assertion_id in (edge["predecessor_assertion_id"], edge["successor_assertion_id"]),
                         "Lineage does not reference participating assertion")
                _require(edge["feedback_id"] in item.correction_ids, "Lineage/correction identity mismatch")
            result.append(item)
        return tuple(result)

    def _timeline(self, interaction_id):
        # Guard JSON extraction so unrelated malformed payloads cannot break
        # selection. Related malformed payloads still fail in strict decoding.
        rows = self._rows("""
            SELECT * FROM audit_event a WHERE a.interaction_id = ?
            OR a.feedback_id IN (SELECT feedback_id FROM feedback WHERE interaction_id = ?)
            OR EXISTS (
                SELECT 1 FROM json_each(CASE WHEN json_valid(a.payload_json)
                                           THEN a.payload_json ELSE '{}' END) link
                JOIN correction_proposal p ON p.proposal_id = link.value
                JOIN feedback f ON f.feedback_id = p.feedback_id
                WHERE link.key = 'proposal_id' AND f.interaction_id = ?
            )
            ORDER BY a.event_id
        """, (interaction_id, interaction_id, interaction_id))
        events = []
        for row in rows:
            payload = _json(row.pop("payload_json"), dict)
            proposal_id = payload.get("proposal_id")
            feedback_ids = {v for v in (row["feedback_id"], payload.get("feedback_id")) if v is not None}
            if proposal_id is not None:
                m._text(proposal_id, "proposal_id")
                proposal = self._one("SELECT feedback_id FROM correction_proposal WHERE proposal_id = ?",
                                     (proposal_id,), "Missing audit proposal identity")
                proposal_feedback = proposal["feedback_id"]
                if proposal_feedback is not None:
                    feedback_ids.add(proposal_feedback)
            else:
                proposal_feedback = None
            _require(len(feedback_ids) <= 1, "Contradictory audit feedback/proposal links")
            for feedback_id in feedback_ids:
                m._text(feedback_id, "feedback_id")
                feedback = self._one("SELECT interaction_id FROM feedback WHERE feedback_id = ?",
                                     (feedback_id,), "Missing audit feedback identity")
                _require(feedback["interaction_id"] == interaction_id, "Contradictory audit interaction/feedback links")
            for recorded in (row["interaction_id"], payload.get("interaction_id")):
                _require(recorded is None or recorded == interaction_id, "Contradictory audit interaction links")
            # Prefer the most specific verified path, retaining raw row links.
            if proposal_feedback is not None:
                link = m.GovernanceLink("PROPOSAL", interaction_id, proposal_feedback, proposal_id)
            elif row["feedback_id"] is not None:
                link = m.GovernanceLink("FEEDBACK", interaction_id, row["feedback_id"])
            else:
                link = m.GovernanceLink("INTERACTION", interaction_id)
            events.append(m.AuditEventSnapshot(**row, payload=payload, relationship=link))
        return m.GovernanceTimeline(events)
