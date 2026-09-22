"""Deterministic application services for the synthetic Synapse demo."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from backend import database
from backend.config import Settings


CANONICAL_QUESTION = (
    "Does this patient's insurance require prior authorization for this medication, "
    "and what evidence supports the answer?"
)
SOURCE_ORDER = ["EHR", "GUIDELINE", "PAYER_POLICY", "FORMULARY", "SPECIALIST_NOTE"]
SOURCE_LABELS = {
    "EHR": "EHR",
    "GUIDELINE": "Guideline",
    "PAYER_POLICY": "Payer",
    "FORMULARY": "Formulary",
    "SPECIALIST_NOTE": "Specialist Notes",
}
BASELINE_TIME = "2026-06-15T14:00:00Z"
POST_APPROVAL_TIME = "2026-07-03T14:00:00Z"
SUBMITTED_TIME = "2026-06-16T15:00:00Z"
APPROVED_TIME = "2026-07-02T13:00:00Z"


def _fixture_root(settings: Settings) -> Path:
    configured = settings.project_root / "data" / "fixtures"
    return configured if configured.exists() else Path(__file__).resolve().parent.parent / "data" / "fixtures"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def initialize_demo(settings: Settings) -> None:
    """Create and seed the complete demo database when needed."""

    database.initialize_database(settings)
    with database.managed_connection(settings.database_path) as connection:
        connection.executescript(Path(__file__).with_name("demo_schema.sql").read_text(encoding="utf-8"))
        row = connection.execute("SELECT COUNT(*) FROM source_document_version").fetchone()
        if row == (0,):
            _seed(connection, settings)


def _seed(connection: sqlite3.Connection, settings: Settings) -> None:
    manifest = json.loads((_fixture_root(settings) / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["fixture_files"]:
        fixture = json.loads((_fixture_root(settings) / item["path"]).read_text(encoding="utf-8"))
        for version in fixture["versions"]:
            connection.execute(
                "INSERT OR IGNORE INTO source_document VALUES (?, ?, ?, ?, 1)",
                (fixture["document_id"], version["source_id"], version["source_type"], version["source_title"]),
            )
            connection.execute(
                """INSERT INTO source_document_version
                   (document_version_id, document_id, version, timestamp, recorded_at,
                    effective_from, effective_to, relevant_excerpt, structured_data, checksum,
                    governance_state, is_current, test_only)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version["document_version_id"], fixture["document_id"], version["version"],
                    version["timestamp"], version["recorded_at"], version["effective_from"],
                    version.get("effective_to"), version["relevant_excerpt"],
                    _json(version["structured_data"]), version["checksum"],
                    version["governance_state"], int(version.get("current_at_seed", False)),
                    int(version.get("test_only", False)),
                ),
            )
            for evidence in version["evidence_items"]:
                connection.execute(
                    """INSERT INTO evidence_item VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence["evidence_id"], evidence["document_version_id"], evidence["source_id"],
                        evidence["source_type"], evidence["source_title"], evidence["version"],
                        evidence["timestamp"], evidence["section"], evidence["relevant_excerpt"],
                        _json(evidence["structured_data"]),
                    ),
                )
            for assertion in version.get("assertions", []):
                connection.execute(
                    """INSERT INTO knowledge_assertion
                       (assertion_id, document_version_id, predicate, object_id, value_json,
                        decision_dimension, evidence_ids_json, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        assertion["assertion_id"], version["document_version_id"], assertion["predicate"],
                        assertion.get("object_id"), _json(assertion.get("value")),
                        assertion["decision_dimension"], _json(assertion["evidence_ids"]),
                        "APPLIED" if version.get("current_at_seed") else "CANDIDATE",
                    ),
                )
    _audit(connection, "DEMO_RESET", BASELINE_TIME, {"baseline": "synthetic-pa-v1", "current_policy": "DV-SYN-POL-VEL-V1"})


def _audit(
    connection: sqlite3.Connection,
    event_type: str,
    occurred_at: str,
    payload: dict[str, Any],
    *,
    interaction_id: str | None = None,
    feedback_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_event(interaction_id, feedback_id, event_type, occurred_at, payload_json) VALUES (?, ?, ?, ?, ?)",
        (interaction_id, feedback_id, event_type, occurred_at, _json(payload)),
    )


def classify_intent(question: str) -> str | None:
    text = question.lower()
    if "prior authorization" in text or ("insurance" in text and "medication" in text):
        return "PRIOR_AUTHORIZATION"
    if "guideline" in text or "clinical guidance" in text:
        return "CLINICAL_GUIDANCE"
    if "specialist" in text or "history" in text:
        return "SPECIALIST_HISTORY"
    return None


def _current_policy_version(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """SELECT document_version_id FROM source_document_version
           WHERE document_id = 'DOC-SYN-POL-VEL' AND is_current = 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("No current synthetic payer policy")
    return str(row[0])


def _evidence(connection: sqlite3.Connection, evidence_ids: list[str]) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = connection.execute(
        f"""SELECT evidence_id, document_version_id, source_id, source_type, source_title,
                   version, timestamp, section, relevant_excerpt
            FROM evidence_item WHERE evidence_id IN ({placeholders})""",
        evidence_ids,
    ).fetchall()
    by_id = {
        row[0]: {
            "evidence_id": row[0], "document_version_id": row[1], "source_id": row[2],
            "source_type": row[3], "source_title": row[4], "version": row[5],
            "timestamp": row[6], "section": row[7], "relevant_excerpt": row[8],
        }
        for row in rows
    }
    return [by_id[item] for item in evidence_ids if item in by_id]


def ask_question(
    settings: Settings,
    question: str,
    *,
    source_mode: str = "BASELINE",
) -> dict[str, Any]:
    intent = classify_intent(question)
    interaction_id = f"INT-{uuid.uuid4().hex[:12].upper()}"
    with database.managed_connection(settings.database_path) as connection:
        if intent is None:
            created_at = BASELINE_TIME
            response = {
                "interaction_id": interaction_id, "question": question, "intent": "UNSUPPORTED",
                "status": "UNSUPPORTED_SCOPE", "selected_sources": [], "orchestration_trace": [],
                "answer": "This question is outside the supported synthetic demo. Ask about prior authorization, clinical guidance, or specialist history.",
                "claims": [], "citations": [], "confidence": None, "confidence_rationale": None,
                "reconciliation": [], "escalation": None, "policy_version_id": None,
            }
            _persist_interaction(connection, response, created_at, source_mode)
            _audit(connection, "UNSUPPORTED_SCOPE", created_at, {"question": question}, interaction_id=interaction_id)
            return response

        selected = list(SOURCE_ORDER)
        current_policy = _current_policy_version(connection)
        created_at = POST_APPROVAL_TIME if current_policy.endswith("V2") else BASELINE_TIME
        missing_payer = source_mode == "PAYER_POLICY_UNAVAILABLE"
        true_conflict = source_mode == "TEST_ONLY_FORMULARY_SUBSTITUTION"
        trace = []
        for source in selected:
            status = "SOURCE_UNAVAILABLE" if source == "PAYER_POLICY" and missing_payer else "RETRIEVED"
            trace.append({"sequence": len(trace) + 1, "source_type": source, "label": SOURCE_LABELS[source], "status": status})

        if missing_payer:
            claims = [
                ("CLM-C-CASE", "The synthetic case requests Veluntra for Lumen Drift Syndrome and has active Harborlight Plus coverage.", ["EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"]),
                ("CLM-C-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure, but it does not determine coverage.", ["EV-SYN-GUIDE-SUPPORT-001", "EV-SYN-GUIDE-SCOPE-001"]),
                ("CLM-C-FORM", "The formulary lists Veluntra as Tier 3 subject to PA, but cannot replace the unavailable payer policy.", ["EV-SYN-FORM-STATUS-001"]),
            ]
            answer = "The applicable payer policy could not be verified, so Synapse cannot determine whether prior authorization is required. Available clinical and formulary evidence is shown, but payer review is required."
            confidence = "LOW"
            rationale = "Critical payer evidence is unavailable; relevance remains strong but required coverage context is incomplete."
            reconciliation = [{"type": "SOURCE_UNAVAILABLE", "severity": "HIGH", "resolution_state": "UNRESOLVED", "explanation": "The authoritative payer-policy dimension could not be verified."}]
            escalation = {"required": True, "reviewer": "Synthetic coverage-policy reviewer", "reason": "Obtain and verify the applicable current payer policy."}
            policy_id = None
        elif true_conflict:
            claims = [
                ("CLM-F-POLICY", "Current Payer Policy V1 says prior authorization is required.", ["EV-SYN-POL-V1-PA-001"]),
                ("CLM-F-FORM", "The isolated test formulary says prior authorization is not required.", ["EV-SYN-FORM-CONFLICT-001"]),
                ("CLM-F-CONFLICT", "Two same-scope authorization sources disagree, so the requirement cannot be determined.", ["EV-SYN-POL-V1-PA-001", "EV-SYN-FORM-CONFLICT-001"]),
            ]
            answer = "Synapse cannot determine the prior-authorization requirement because two current same-scope synthetic sources disagree. A coverage-policy reviewer must resolve which source governs."
            confidence = "LOW"
            rationale = "Evidence is available and relevant, but an unresolved high-severity same-dimension disagreement remains."
            reconciliation = [{"type": "SAME_DIMENSION_DISAGREEMENT", "severity": "HIGH", "resolution_state": "UNRESOLVED", "explanation": "Payer Policy V1 requires PA while the isolated test formulary says no PA is required."}]
            escalation = {"required": True, "reviewer": "Synthetic coverage-policy reviewer", "reason": "Resolve the authorization-source disagreement."}
            policy_id = "DV-SYN-POL-VEL-V1"
        elif current_policy.endswith("V2"):
            claims = [
                ("CLM-E-V2-PA", "Payer Policy V2 requires prior authorization for Veluntra.", ["EV-SYN-POL-V2-PA-001"]),
                ("CLM-E-V2-CRITERIA", "V2 requires at least 30 days of either Norlaxa or Bravex.", ["EV-SYN-POL-V2-STEP-001"]),
                ("CLM-E-READY", "The documented 35-day Norlaxa failure satisfies V2's one-of-two prerequisite.", ["EV-SYN-EHR-THERAPY-001", "EV-SYN-POL-V2-STEP-001"]),
                ("CLM-A-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure.", ["EV-SYN-GUIDE-SUPPORT-001"]),
                ("CLM-A-FORM", "Veluntra is formulary-listed as Tier 3 subject to prior authorization.", ["EV-SYN-FORM-STATUS-001"]),
            ]
            answer = "Yes. Harborlight Plus Payer Policy V2 requires prior authorization for Veluntra. V2 accepts either Norlaxa or Bravex for at least 30 days; the documented 35-day Norlaxa failure satisfies that prerequisite. Clinical support does not itself establish coverage approval."
            confidence, policy_id = "HIGH", current_policy
            rationale = "All five selected sources were retrieved with complete provenance; V2 is approved, effective, relevant, and no high-severity conflict is present."
            reconciliation = [{"type": "COMPATIBLE_CONSTRAINT", "severity": "INFORMATIONAL", "resolution_state": "NOT_APPLICABLE", "explanation": "The guideline addresses clinical appropriateness; Payer Policy V2 addresses authorization and coverage workflow. Both apply without logical contradiction."}]
            escalation = None
        else:
            claims = [
                ("CLM-A-CASE", "The synthetic case requests Veluntra for Lumen Drift Syndrome and has active Harborlight Plus coverage.", ["EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"]),
                ("CLM-A-PA", "Payer Policy V1 requires prior authorization for Veluntra.", ["EV-SYN-POL-V1-PA-001"]),
                ("CLM-A-V1-CRITERIA", "V1 requires both Norlaxa and Bravex prerequisites.", ["EV-SYN-POL-V1-STEP-001"]),
                ("CLM-A-HISTORY", "A 35-day Norlaxa failure is documented; a Bravex trial is not documented.", ["EV-SYN-EHR-THERAPY-001", "EV-SYN-NOTE-HISTORY-001"]),
                ("CLM-A-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure.", ["EV-SYN-GUIDE-SUPPORT-001"]),
                ("CLM-A-FORM", "Veluntra is formulary-listed as Tier 3 subject to prior authorization.", ["EV-SYN-FORM-STATUS-001"]),
                ("CLM-A-RECON", "Clinical support and payer authorization are separate decision dimensions.", ["EV-SYN-GUIDE-SCOPE-001", "EV-SYN-POL-V1-PA-001"]),
            ]
            answer = "Yes. Harborlight Plus Payer Policy V1 requires prior authorization for Veluntra and requires both Norlaxa and Bravex prerequisites. Norlaxa failure is documented, but a Bravex trial is not documented, so the V1 prerequisite is not yet satisfied. The guideline supports Veluntra clinically; that support does not replace payer authorization rules."
            confidence, policy_id = "HIGH", current_policy
            rationale = "All five selected sources were retrieved with complete provenance and current exact-scope evidence; the guideline/payer difference is a compatible cross-dimensional constraint."
            reconciliation = [{"type": "COMPATIBLE_CONSTRAINT", "severity": "INFORMATIONAL", "resolution_state": "NOT_APPLICABLE", "explanation": "The guideline addresses clinical appropriateness; Payer Policy V1 addresses authorization and prerequisites. Both apply without logical contradiction."}]
            escalation = None

        claim_payload = [{"claim_id": key, "text": text, "evidence_ids": ids} for key, text, ids in claims]
        # Preserve claim linkage even when one evidence item supports multiple claims.
        citations = []
        for claim in claim_payload:
            for item in _evidence(connection, claim["evidence_ids"]):
                citations.append({**item, "claim_id": claim["claim_id"]})
        response = {
            "interaction_id": interaction_id, "question": question, "intent": intent,
            "status": "ANSWERED", "selected_sources": selected, "orchestration_trace": trace,
            "answer": answer, "claims": claim_payload, "citations": citations,
            "confidence": confidence, "confidence_rationale": rationale,
            "reconciliation": reconciliation, "escalation": escalation, "policy_version_id": policy_id,
        }
        _persist_interaction(connection, response, created_at, source_mode)
        _audit(connection, "QUESTION_RECEIVED", created_at, {"intent": intent}, interaction_id=interaction_id)
        _audit(connection, "SOURCES_RETRIEVED", created_at, {"trace": trace}, interaction_id=interaction_id)
        _audit(connection, "ANSWER_PERSISTED", created_at, {"confidence": confidence, "policy_version_id": policy_id}, interaction_id=interaction_id)
        return response


def _persist_interaction(connection: sqlite3.Connection, payload: dict[str, Any], created_at: str, source_mode: str) -> None:
    connection.execute(
        """INSERT INTO interaction VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["interaction_id"], payload["question"], payload["intent"], created_at,
            source_mode, _json(payload["selected_sources"]), _json(payload["orchestration_trace"]),
            payload["answer"], payload["confidence"], payload["confidence_rationale"],
            _json(payload["reconciliation"]), _json(payload["escalation"]) if payload["escalation"] else None,
            payload["policy_version_id"], created_at,
        ),
    )
    for claim in payload["claims"]:
        supported_id = f"{payload['interaction_id']}-{claim['claim_id']}"
        connection.execute(
            "INSERT INTO supported_claim VALUES (?, ?, ?, ?, ?)",
            (supported_id, payload["interaction_id"], claim["claim_id"], claim["text"], _json(claim["evidence_ids"])),
        )
        for item in [c for c in payload["citations"] if c["claim_id"] == claim["claim_id"]]:
            connection.execute(
                """INSERT INTO citation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"CIT-{supported_id}-{item['evidence_id']}", payload["interaction_id"], supported_id,
                    item["evidence_id"], item["source_title"], item["source_type"], item["version"],
                    item["timestamp"], item["section"], item["relevant_excerpt"],
                ),
            )


def get_interaction(settings: Settings, interaction_id: str) -> dict[str, Any] | None:
    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute("SELECT * FROM interaction WHERE interaction_id = ?", (interaction_id,)).fetchone()
        if row is None:
            return None
        claims = connection.execute(
            "SELECT claim_key, claim_text, evidence_ids_json FROM supported_claim WHERE interaction_id = ? ORDER BY rowid",
            (interaction_id,),
        ).fetchall()
        citations = connection.execute(
            """SELECT sc.claim_key, c.evidence_id, e.document_version_id, c.source_title, c.source_type,
                      c.version, c.timestamp, c.section, c.relevant_excerpt, e.source_id
               FROM citation c JOIN supported_claim sc ON sc.supported_claim_id = c.supported_claim_id
               JOIN evidence_item e ON e.evidence_id = c.evidence_id
               WHERE c.interaction_id = ? ORDER BY c.rowid""",
            (interaction_id,),
        ).fetchall()
        return {
            "interaction_id": row[0], "question": row[1], "intent": row[2], "as_of": row[3],
            "source_mode": row[4], "selected_sources": json.loads(row[5]),
            "orchestration_trace": json.loads(row[6]), "answer": row[7], "confidence": row[8],
            "confidence_rationale": row[9], "reconciliation": json.loads(row[10]),
            "escalation": json.loads(row[11]) if row[11] else None, "policy_version_id": row[12],
            "claims": [{"claim_id": c[0], "text": c[1], "evidence_ids": json.loads(c[2])} for c in claims],
            "citations": [{"claim_id": c[0], "evidence_id": c[1], "document_version_id": c[2], "source_title": c[3], "source_type": c[4], "version": c[5], "timestamp": c[6], "section": c[7], "relevant_excerpt": c[8], "source_id": c[9]} for c in citations],
        }


def submit_feedback(settings: Settings, interaction_id: str, actor: str, message: str) -> dict[str, Any]:
    feedback_id = f"FDB-{uuid.uuid4().hex[:12].upper()}"
    with database.managed_connection(settings.database_path) as connection:
        if connection.execute("SELECT 1 FROM interaction WHERE interaction_id = ?", (interaction_id,)).fetchone() is None:
            raise KeyError(interaction_id)
        connection.execute(
            """INSERT INTO feedback VALUES (?, ?, ?, 'CARE_COORDINATOR', ?,
               'DV-SYN-POL-VEL-V1', 'DV-SYN-POL-VEL-V2', 'PENDING', ?, NULL)""",
            (feedback_id, interaction_id, actor, message, SUBMITTED_TIME),
        )
        _audit(connection, "FEEDBACK_SUBMITTED", SUBMITTED_TIME, {"status": "SUBMITTED", "message": message}, interaction_id=interaction_id, feedback_id=feedback_id)
        _audit(connection, "FEEDBACK_PENDING", SUBMITTED_TIME, {"status": "PENDING", "proposed_version_id": "DV-SYN-POL-VEL-V2"}, interaction_id=interaction_id, feedback_id=feedback_id)
    return {"feedback_id": feedback_id, "interaction_id": interaction_id, "status": "PENDING", "target_version_id": "DV-SYN-POL-VEL-V1", "proposed_version_id": "DV-SYN-POL-VEL-V2", "message": message}


def approve_feedback(settings: Settings, feedback_id: str, reviewer: str, rationale: str) -> dict[str, Any]:
    with database.managed_connection(settings.database_path) as connection:
        row = connection.execute("SELECT interaction_id, status FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        if row is None:
            raise KeyError(feedback_id)
        if row[1] != "PENDING":
            raise ValueError("Only pending feedback can be approved")
        connection.execute("UPDATE source_document_version SET is_current = 0 WHERE document_version_id = 'DV-SYN-POL-VEL-V1'")
        connection.execute("UPDATE source_document_version SET is_current = 1, governance_state = 'APPLIED' WHERE document_version_id = 'DV-SYN-POL-VEL-V2'")
        connection.execute("UPDATE knowledge_assertion SET state = 'SUPERSEDED' WHERE document_version_id = 'DV-SYN-POL-VEL-V1'")
        connection.execute("UPDATE knowledge_assertion SET state = 'APPLIED' WHERE document_version_id = 'DV-SYN-POL-VEL-V2'")
        connection.execute("UPDATE feedback SET status = 'APPLIED', applied_at = ? WHERE feedback_id = ?", (APPROVED_TIME, feedback_id))
        connection.execute("INSERT INTO review VALUES (?, ?, ?, 'KNOWLEDGE_REVIEWER', 'APPROVED', ?, ?)", (f"REV-{uuid.uuid4().hex[:12].upper()}", feedback_id, reviewer, rationale, APPROVED_TIME))
        connection.execute("INSERT INTO assertion_supersession VALUES (?, 'DV-SYN-POL-VEL-V1', 'DV-SYN-POL-VEL-V2', ?, ?)", (f"SUP-{uuid.uuid4().hex[:12].upper()}", feedback_id, APPROVED_TIME))
        connection.execute("INSERT INTO knowledge_update VALUES (?, ?, 'DV-SYN-POL-VEL-V1', 'DV-SYN-POL-VEL-V2', ?)", (f"UPD-{uuid.uuid4().hex[:12].upper()}", feedback_id, APPROVED_TIME))
        _audit(connection, "REVIEW_APPROVED", APPROVED_TIME, {"decision": "APPROVED", "reviewer_role": "KNOWLEDGE_REVIEWER"}, interaction_id=row[0], feedback_id=feedback_id)
        _audit(connection, "KNOWLEDGE_UPDATE_APPLIED", APPROVED_TIME, {"prior_version_id": "DV-SYN-POL-VEL-V1", "current_version_id": "DV-SYN-POL-VEL-V2"}, interaction_id=row[0], feedback_id=feedback_id)
    return {"feedback_id": feedback_id, "status": "APPLIED", "review_decision": "APPROVED", "current_policy_version_id": "DV-SYN-POL-VEL-V2", "supersedes": "DV-SYN-POL-VEL-V1"}


def get_audit(settings: Settings, interaction_id: str) -> dict[str, Any]:
    with database.managed_connection(settings.database_path) as connection:
        rows = connection.execute(
            """SELECT event_id, event_type, occurred_at, payload_json, feedback_id
               FROM audit_event WHERE interaction_id = ? OR feedback_id IN
               (SELECT feedback_id FROM feedback WHERE interaction_id = ?)
               ORDER BY event_id""",
            (interaction_id, interaction_id),
        ).fetchall()
    return {"interaction_id": interaction_id, "events": [{"event_id": r[0], "event_type": r[1], "occurred_at": r[2], "payload": json.loads(r[3]), "feedback_id": r[4]} for r in rows]}
