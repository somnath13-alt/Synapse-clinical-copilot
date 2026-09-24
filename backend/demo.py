"""Deterministic application services for the synthetic Synapse demo."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend import database
from backend.config import Settings
from backend.reasoning import (
    ConfidenceLabel,
    EscalationTrigger,
    FindingType,
    ReasoningFinding,
    ReasoningResult,
    reason,
)
from backend.retrieval import (
    EvidenceBundle,
    EvidenceItem,
    RetrievalRequest,
    RetrievalResult,
    RetrievalService,
    RetrievalStatus,
    SourceType,
)


CANONICAL_QUESTION = (
    "Does this patient's insurance require prior authorization for this medication, "
    "and what evidence supports the answer?"
)
BASELINE_TIME = "2026-06-15T14:00:00Z"
POST_APPROVAL_TIME = "2026-07-03T14:00:00Z"
SUBMITTED_TIME = "2026-06-16T15:00:00Z"
APPROVED_TIME = "2026-07-02T13:00:00Z"


class CitationResolutionError(RuntimeError):
    """A deterministic claim referenced evidence absent from its retrieval bundle."""


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
                evidence_ids = assertion["evidence_ids"]
                for evidence_id in evidence_ids:
                    evidence_row = connection.execute(
                        "SELECT document_version_id FROM evidence_item WHERE evidence_id = ?",
                        (evidence_id,),
                    ).fetchone()
                    if evidence_row is None:
                        raise database.DatabaseValidationError(
                            f"Assertion {assertion['assertion_id']} references unknown evidence"
                        )
                    if evidence_row[0] != version["document_version_id"]:
                        raise database.DatabaseValidationError(
                            f"Assertion {assertion['assertion_id']} and evidence {evidence_id} "
                            "must belong to the same document version"
                        )
                connection.execute(
                    """INSERT INTO knowledge_assertion
                       (assertion_id, document_version_id, subject_id, predicate, object_id,
                        value_json, decision_dimension, normalized_scope_json, recorded_at,
                        effective_from, effective_to, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        assertion["assertion_id"], version["document_version_id"],
                        assertion["subject_id"], assertion["predicate"],
                        assertion.get("object_id"), _json(assertion.get("value")),
                        assertion["decision_dimension"],
                        _json(assertion["normalized_scope"])
                        if assertion.get("normalized_scope") is not None
                        else None,
                        assertion.get("recorded_at", version["recorded_at"]),
                        assertion.get("effective_from", version["effective_from"]),
                        assertion.get("effective_to", version.get("effective_to")),
                        "APPLIED" if version.get("current_at_seed") else "CANDIDATE",
                    ),
                )
                connection.executemany(
                    "INSERT INTO assertion_evidence(assertion_id, evidence_id) VALUES (?, ?)",
                    (
                        (assertion["assertion_id"], evidence_id)
                        for evidence_id in evidence_ids
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


def _retrieval_request(
    interaction_id: str,
    intent: str,
    source_mode: str,
) -> RetrievalRequest:
    return RetrievalRequest(
        interaction_id=interaction_id,
        intent=intent,
        case_id="SYN-CASE-001",
        as_of=BASELINE_TIME,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        payer_id="SYN-PAYER-NHH",
        plan_id="SYN-PLAN-HLP",
        source_mode=source_mode,
    )


def _result_for(bundle: EvidenceBundle, source_type: SourceType) -> RetrievalResult:
    return next(
        result
        for result in bundle.retrieval_results
        if result.source_type is source_type
    )


def _single_document_version(result: RetrievalResult) -> str | None:
    if result.status is not RetrievalStatus.RETRIEVED:
        return None
    if len(result.document_version_ids) != 1:
        raise RuntimeError(
            f"Expected one {result.source_type.value} document version, "
            f"received {len(result.document_version_ids)}"
        )
    return result.document_version_ids[0]


def _citation_payload(evidence: EvidenceItem, claim_id: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "document_version_id": evidence.document_version_id,
        "source_id": evidence.source_id,
        "source_type": evidence.source_type.value,
        "source_title": evidence.source_title,
        "version": evidence.version,
        "timestamp": evidence.timestamp,
        "section": evidence.section,
        "relevant_excerpt": evidence.relevant_excerpt,
        "claim_id": claim_id,
    }


def _resolve_claim_citations(
    bundle: EvidenceBundle,
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for claim in claims:
        for evidence_id in claim["evidence_ids"]:
            evidence = bundle.evidence_by_id.get(evidence_id)
            if evidence is None:
                raise CitationResolutionError(
                    f"Claim {claim['claim_id']} requested evidence {evidence_id} "
                    "that was not returned by retrieval"
                )
            citations.append(_citation_payload(evidence, claim["claim_id"]))
    return citations


def _supported_claims(
    bundle: EvidenceBundle,
    claims: list[tuple[str, str, list[str]]],
) -> list[dict[str, Any]]:
    """Emit only claims whose required evidence was retrieved for this interaction."""

    available = bundle.evidence_by_id
    return [
        {"claim_id": key, "text": text, "evidence_ids": ids}
        for key, text, ids in claims
        if ids and all(evidence_id in available for evidence_id in ids)
    ]


def _finding(
    reasoning_result: ReasoningResult,
    finding_type: FindingType,
) -> ReasoningFinding | None:
    return next(
        (
            finding
            for finding in reasoning_result.findings
            if finding.finding_type is finding_type
        ),
        None,
    )


def _reconciliation_payload(
    reasoning_result: ReasoningResult,
) -> list[dict[str, str]]:
    public_findings = reasoning_result.findings
    conflicts = tuple(
        finding
        for finding in public_findings
        if finding.finding_type is FindingType.SAME_DIMENSION_DISAGREEMENT
    )
    if conflicts:
        public_findings = conflicts
    findings = [
        {
            "type": finding.finding_type.value,
            "severity": finding.severity.value,
            "resolution_state": finding.resolution_state.value,
            "explanation": finding.explanation,
        }
        for finding in public_findings
    ]
    if findings:
        return findings
    if (
        EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE
        in reasoning_result.escalation.triggers
    ):
        return [
            {
                "type": "SOURCE_UNAVAILABLE",
                "severity": "HIGH",
                "resolution_state": "UNRESOLVED",
                "explanation": "The authoritative payer-policy dimension could not be verified.",
            }
        ]
    return []


def _escalation_payload(
    reasoning_result: ReasoningResult,
) -> dict[str, Any] | None:
    decision = reasoning_result.escalation
    if not decision.requires_escalation:
        return None
    return {
        "required": True,
        "reviewer": decision.reviewer,
        "reason": decision.requested_action,
    }


def _conflict_claims(finding: ReasoningFinding) -> list[tuple[str, str, list[str]]]:
    payer_ids = list(
        dict.fromkeys(
            evidence_id
            for side in finding.sides
            if side.source_type is SourceType.PAYER_POLICY
            for evidence_id in side.evidence_ids
        )
    )
    formulary_ids = list(
        dict.fromkeys(
            evidence_id
            for side in finding.sides
            if side.source_type is SourceType.FORMULARY
            for evidence_id in side.evidence_ids
        )
    )
    return [
        (
            "CLM-F-POLICY",
            "The current payer policy says prior authorization is required.",
            payer_ids,
        ),
        (
            "CLM-F-FORM",
            "The current formulary evidence says prior authorization is not required.",
            formulary_ids,
        ),
        (
            "CLM-F-CONFLICT",
            "Two same-scope authorization sources disagree, so the requirement cannot be determined.",
            list(finding.evidence_ids),
        ),
    ]


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

        retrieval_request = _retrieval_request(interaction_id, intent, source_mode)
        bundle = RetrievalService(settings.database_path).retrieve(retrieval_request)
        selected = [result.source_type.value for result in bundle.retrieval_results]
        trace = [
            {
                "sequence": sequence,
                "source_type": item.source_type.value,
                "label": item.display_label,
                "status": item.status.value,
            }
            for sequence, item in enumerate(bundle.retrieval_trace, start=1)
        ]
        payer_result = _result_for(bundle, SourceType.PAYER_POLICY)
        current_policy = _single_document_version(payer_result)
        created_at = (
            POST_APPROVAL_TIME
            if current_policy == "DV-SYN-POL-VEL-V2"
            else BASELINE_TIME
        )
        reasoning_context = replace(retrieval_request, as_of=created_at)
        reasoning_result = reason(bundle, reasoning_context)
        critical_source_unavailable = (
            EscalationTrigger.CRITICAL_SOURCE_UNAVAILABLE
            in reasoning_result.escalation.triggers
        )
        conflict_finding = _finding(
            reasoning_result, FindingType.SAME_DIMENSION_DISAGREEMENT
        )

        if critical_source_unavailable:
            claims = [
                ("CLM-C-CASE", "The synthetic case requests Veluntra for Lumen Drift Syndrome and has active Harborlight Plus coverage.", ["EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"]),
                ("CLM-C-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure, but it does not determine coverage.", ["EV-SYN-GUIDE-SUPPORT-001", "EV-SYN-GUIDE-SCOPE-001"]),
                ("CLM-C-FORM", "The formulary lists Veluntra as Tier 3 subject to PA, but cannot replace the unavailable payer policy.", ["EV-SYN-FORM-STATUS-001"]),
            ]
            answer = "The applicable payer policy could not be verified, so Synapse cannot determine whether prior authorization is required. Available evidence is shown, but payer review is required."
            policy_id = None
        elif conflict_finding is not None:
            claims = _conflict_claims(conflict_finding)
            answer = "Synapse cannot determine the prior-authorization requirement because two current same-scope synthetic sources disagree. A coverage-policy reviewer must resolve which source governs."
            policy_id = current_policy
        elif current_policy == "DV-SYN-POL-VEL-V2":
            claims = [
                ("CLM-E-V2-PA", "Payer Policy V2 requires prior authorization for Veluntra.", ["EV-SYN-POL-V2-PA-001"]),
                ("CLM-E-V2-CRITERIA", "V2 requires at least 30 days of either Norlaxa or Bravex.", ["EV-SYN-POL-V2-STEP-001"]),
                ("CLM-E-READY", "The documented 35-day Norlaxa failure satisfies V2's one-of-two prerequisite.", ["EV-SYN-EHR-THERAPY-001", "EV-SYN-POL-V2-STEP-001"]),
                ("CLM-A-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure.", ["EV-SYN-GUIDE-SUPPORT-001"]),
                ("CLM-A-FORM", "Veluntra is formulary-listed as Tier 3 subject to prior authorization.", ["EV-SYN-FORM-STATUS-001"]),
            ]
            answer = "Yes. Harborlight Plus Payer Policy V2 requires prior authorization for Veluntra. V2 accepts either Norlaxa or Bravex for at least 30 days; the documented 35-day Norlaxa failure satisfies that prerequisite. Clinical support does not itself establish coverage approval."
            policy_id = current_policy
        else:
            claims = [
                ("CLM-A-CASE", "The synthetic case requests Veluntra for Lumen Drift Syndrome and has active Harborlight Plus coverage.", ["EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"]),
                ("CLM-A-PA", "Payer Policy V1 requires prior authorization for Veluntra.", ["EV-SYN-POL-V1-PA-001"]),
                ("CLM-A-V1-CRITERIA", "V1 requires both Norlaxa and Bravex prerequisites.", ["EV-SYN-POL-V1-STEP-001"]),
                (
                    "CLM-A-HISTORY",
                    "A 35-day Norlaxa failure is documented; a Bravex trial is not documented.",
                    [
                        "EV-SYN-EHR-THERAPY-001",
                        *(
                            ["EV-SYN-NOTE-HISTORY-001"]
                            if "EV-SYN-NOTE-HISTORY-001" in bundle.evidence_by_id
                            else []
                        ),
                    ],
                ),
                ("CLM-A-GUIDE", "The guideline supports Veluntra clinically after one preferred therapy failure.", ["EV-SYN-GUIDE-SUPPORT-001"]),
                ("CLM-A-FORM", "Veluntra is formulary-listed as Tier 3 subject to prior authorization.", ["EV-SYN-FORM-STATUS-001"]),
                ("CLM-A-RECON", "Clinical support and payer authorization are separate decision dimensions.", ["EV-SYN-GUIDE-SCOPE-001", "EV-SYN-POL-V1-PA-001"]),
            ]
            answer = "Yes. Harborlight Plus Payer Policy V1 requires prior authorization for Veluntra and requires both Norlaxa and Bravex prerequisites. Norlaxa failure is documented, but a Bravex trial is not documented, so the V1 prerequisite is not yet satisfied. The guideline supports Veluntra clinically; that support does not replace payer authorization rules."
            policy_id = current_policy

        claim_payload = _supported_claims(bundle, claims)
        claim_ids = {claim["claim_id"] for claim in claim_payload}
        if reasoning_result.confidence.label is ConfidenceLabel.LOW and conflict_finding is None and not critical_source_unavailable:
            answer = "The available evidence is insufficient for a definitive prior-authorization conclusion. Human review is required before acting on this synthetic case."
        elif "CLM-A-GUIDE" not in claim_ids:
            answer = answer.replace(
                " The guideline supports Veluntra clinically; that support does not replace payer authorization rules.",
                "",
            ).replace(
                " Clinical support does not itself establish coverage approval.",
                "",
            )
        confidence = reasoning_result.confidence.label.value
        rationale = reasoning_result.confidence.rationale
        reconciliation = _reconciliation_payload(reasoning_result)
        escalation = _escalation_payload(reasoning_result)
        # Preserve claim linkage even when one evidence item supports multiple claims.
        citations = _resolve_claim_citations(bundle, claim_payload)
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
        connection.executemany(
            """INSERT INTO assertion_lineage
               (lineage_id, predecessor_assertion_id, successor_assertion_id,
                feedback_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                (
                    f"LIN-{uuid.uuid4().hex[:12].upper()}",
                    "AST-SYN-POL-V1-PA",
                    "AST-SYN-POL-V2-PA",
                    feedback_id,
                    APPROVED_TIME,
                ),
                (
                    f"LIN-{uuid.uuid4().hex[:12].upper()}",
                    "AST-SYN-POL-V1-STEP",
                    "AST-SYN-POL-V2-STEP",
                    feedback_id,
                    APPROVED_TIME,
                ),
            ),
        )
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
