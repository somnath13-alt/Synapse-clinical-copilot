from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import database, demo
from backend.config import Settings
from backend.reasoning import EscalationTrigger
from backend.retrieval import (
    EvidenceBundle,
    RetrievalRequest,
    RetrievalResult,
    RetrievalService,
    RetrievalStatus,
    RetrievalTraceItem,
    SourceType,
)


QUESTION = "Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?"
BASELINE_POLICY_VERSION = "DV-SYN-POL-VEL-V1"
UPDATED_POLICY_VERSION = "DV-SYN-POL-VEL-V2"
MINIMUM_PROVENANCE_FIELDS = {
    "source_id",
    "source_type",
    "source_title",
    "version",
    "timestamp",
    "relevant_excerpt",
}
BASELINE_CLAIM_EVIDENCE = {
    "CLM-A-CASE": {"EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"},
    "CLM-A-PA": {"EV-SYN-POL-V1-PA-001"},
    "CLM-A-V1-CRITERIA": {"EV-SYN-POL-V1-STEP-001"},
    "CLM-A-HISTORY": {
        "EV-SYN-EHR-THERAPY-001",
        "EV-SYN-NOTE-HISTORY-001",
    },
    "CLM-A-GUIDE": {"EV-SYN-GUIDE-SUPPORT-001"},
    "CLM-A-FORM": {"EV-SYN-FORM-STATUS-001"},
    "CLM-A-RECON": {
        "EV-SYN-GUIDE-SCOPE-001",
        "EV-SYN-POL-V1-PA-001",
    },
}
UPDATED_CLAIM_EVIDENCE = {
    "CLM-E-V2-PA": {"EV-SYN-POL-V2-PA-001"},
    "CLM-E-V2-CRITERIA": {"EV-SYN-POL-V2-STEP-001"},
    "CLM-E-READY": {
        "EV-SYN-EHR-THERAPY-001",
        "EV-SYN-POL-V2-STEP-001",
    },
    "CLM-A-GUIDE": {"EV-SYN-GUIDE-SUPPORT-001"},
    "CLM-A-FORM": {"EV-SYN-FORM-STATUS-001"},
}


def ask(client: TestClient, source_mode: str = "BASELINE") -> dict[str, Any]:
    response = client.post(
        "/api/v1/questions",
        json={"question": QUESTION, "source_mode": source_mode},
    )
    assert response.status_code == 200
    return response.json()


def claim_evidence(payload: dict[str, Any]) -> dict[str, set[str]]:
    return {
        claim["claim_id"]: set(claim["evidence_ids"])
        for claim in payload["claims"]
    }


def citation_pairs(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (citation["claim_id"], citation["evidence_id"])
        for citation in payload["citations"]
    }


def approve_v2(client: TestClient, interaction_id: str) -> dict[str, Any]:
    feedback_response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": interaction_id,
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )
    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["status"] == "PENDING"

    approval = client.post(
        f"/api/v1/feedback/{feedback['feedback_id']}/approve",
        json={},
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "APPLIED"
    return feedback


def install_retrieval_transform(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    transform: Any,
) -> None:
    service = RetrievalService(settings.database_path)

    class TransformedRetrievalService:
        def __init__(self, database_path: Path) -> None:
            assert database_path == settings.database_path

        def retrieve(self, request: Any) -> EvidenceBundle:
            return transform(service, request)

    monkeypatch.setattr(demo, "RetrievalService", TransformedRetrievalService)


def replace_source_result(
    bundle: EvidenceBundle,
    source_type: SourceType,
    replacement: RetrievalResult,
) -> EvidenceBundle:
    results = tuple(
        replacement if result.source_type is source_type else result
        for result in bundle.retrieval_results
    )
    labels = {
        item.source_type: item.display_label for item in bundle.retrieval_trace
    }
    trace = tuple(
        RetrievalTraceItem(result.source_type, labels[result.source_type], result.status)
        for result in results
    )
    return EvidenceBundle.from_results(results, trace)


def failed_result(
    source_type: SourceType,
    status: RetrievalStatus,
) -> RetrievalResult:
    return RetrievalResult(
        source_type=source_type,
        status=status,
        failure_reason=f"Controlled {source_type.value} {status.value} result",
    )


def test_canonical_question_returns_characterized_v1_answer(
    client: TestClient,
) -> None:
    payload = ask(client)

    assert payload["status"] == "ANSWERED"
    assert payload["intent"] == "PRIOR_AUTHORIZATION"
    assert payload["policy_version_id"] == BASELINE_POLICY_VERSION
    assert payload["confidence"] == "HIGH"
    assert payload["escalation"] is None
    assert claim_evidence(payload) == BASELINE_CLAIM_EVIDENCE

    evidence_ids = {citation["evidence_id"] for citation in payload["citations"]}
    assert {
        "EV-SYN-EHR-CONTEXT-001",
        "EV-SYN-POL-V1-PA-001",
        "EV-SYN-POL-V1-STEP-001",
        "EV-SYN-GUIDE-SUPPORT-001",
        "EV-SYN-FORM-STATUS-001",
    } <= evidence_ids
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= evidence_ids
    assert not any(evidence_id.startswith("EV-SYN-POL-V2-") for evidence_id in evidence_ids)


def test_question_decisions_are_serialized_from_reasoning_output(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_reason = demo.reason

    def controlled_reason(bundle: EvidenceBundle, context: RetrievalRequest):
        result = policy_reason(bundle, context)
        confidence = replace(
            result.confidence,
            label=type(result.confidence.label).MEDIUM,
            rationale="Controlled reasoning rationale.",
        )
        findings = (
            replace(result.findings[0], explanation="Controlled reasoning explanation."),
        )
        escalation = replace(
            result.escalation,
            requires_escalation=True,
            triggers=(EscalationTrigger.LOW_CONFIDENCE,),
            reviewer="Controlled reviewer",
            requested_action="Controlled action.",
        )
        return replace(
            result,
            findings=findings,
            confidence=confidence,
            escalation=escalation,
        )

    monkeypatch.setattr(demo, "reason", controlled_reason)
    payload = ask(client)

    assert payload["confidence"] == "MEDIUM"
    assert payload["confidence_rationale"] == "Controlled reasoning rationale."
    assert payload["reconciliation"][0]["explanation"] == "Controlled reasoning explanation."
    assert payload["escalation"] == {
        "required": True,
        "reviewer": "Controlled reviewer",
        "reason": "Controlled action.",
    }


def test_retrieval_trace_preserves_current_order_labels_and_statuses(
    client: TestClient,
) -> None:
    payload = ask(client)

    assert payload["selected_sources"] == [
        "EHR",
        "GUIDELINE",
        "PAYER_POLICY",
        "FORMULARY",
        "SPECIALIST_NOTE",
    ]
    assert payload["orchestration_trace"] == [
        {"sequence": 1, "source_type": "EHR", "label": "EHR", "status": "RETRIEVED"},
        {
            "sequence": 2,
            "source_type": "GUIDELINE",
            "label": "Guideline",
            "status": "RETRIEVED",
        },
        {
            "sequence": 3,
            "source_type": "PAYER_POLICY",
            "label": "Payer",
            "status": "RETRIEVED",
        },
        {
            "sequence": 4,
            "source_type": "FORMULARY",
            "label": "Formulary",
            "status": "RETRIEVED",
        },
        {
            "sequence": 5,
            "source_type": "SPECIALIST_NOTE",
            "label": "Specialist Notes",
            "status": "RETRIEVED",
        },
    ]


def test_canonical_question_uses_retrieval_request_bundle_trace_and_payer_result(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def capture(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(request)
        observed.update(request=request, bundle=bundle)
        return bundle

    install_retrieval_transform(monkeypatch, settings, capture)
    payload = ask(client)
    request = observed["request"]
    bundle = observed["bundle"]

    assert request.intent == "PRIOR_AUTHORIZATION"
    assert request.case_id == "SYN-CASE-001"
    assert request.medication_id == "SYN-MED-VEL"
    assert request.indication_id == "SYN-COND-LDS"
    assert request.payer_id == "SYN-PAYER-NHH"
    assert request.plan_id == "SYN-PLAN-HLP"
    assert payload["selected_sources"] == [
        result.source_type.value for result in bundle.retrieval_results
    ]
    assert payload["orchestration_trace"] == [
        {
            "sequence": sequence,
            "source_type": item.source_type.value,
            "label": item.display_label,
            "status": item.status.value,
        }
        for sequence, item in enumerate(bundle.retrieval_trace, start=1)
    ]
    payer_result = next(
        result
        for result in bundle.retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )
    assert payer_result.document_version_ids == (BASELINE_POLICY_VERSION,)
    assert {
        item.evidence_id for item in payer_result.evidence_items
    } == {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"}
    assert all(
        evidence_id in bundle.evidence_by_id
        for claim in payload["claims"]
        for evidence_id in claim["evidence_ids"]
    )


def test_material_claims_map_to_existing_citations_with_minimum_provenance(
    client: TestClient,
) -> None:
    payload = ask(client)

    expected_pairs = {
        (claim_id, evidence_id)
        for claim_id, evidence_ids in BASELINE_CLAIM_EVIDENCE.items()
        for evidence_id in evidence_ids
    }
    assert all(claim["evidence_ids"] for claim in payload["claims"])
    assert citation_pairs(payload) == expected_pairs

    cited_evidence_ids = {citation["evidence_id"] for citation in payload["citations"]}
    for claim in payload["claims"]:
        assert set(claim["evidence_ids"]) <= cited_evidence_ids
        assert {
            citation["evidence_id"]
            for citation in payload["citations"]
            if citation["claim_id"] == claim["claim_id"]
        } == set(claim["evidence_ids"])

    for citation in payload["citations"]:
        assert MINIMUM_PROVENANCE_FIELDS <= citation.keys()
        assert all(citation[field] for field in MINIMUM_PROVENANCE_FIELDS)


def test_guideline_payer_reconciliation_is_cross_dimensional_without_winner(
    client: TestClient,
) -> None:
    payload = ask(client)
    reconciliation = payload["reconciliation"]

    assert len(reconciliation) == 1
    assert reconciliation[0]["type"] == "COMPATIBLE_CONSTRAINT"
    assert reconciliation[0]["severity"] == "INFORMATIONAL"
    assert reconciliation[0]["resolution_state"] == "NOT_APPLICABLE"
    assert reconciliation[0]["type"] != "SAME_DIMENSION_DISAGREEMENT"
    assert "winner" not in reconciliation[0]

    reconciliation_citations = {
        citation["source_type"]
        for citation in payload["citations"]
        if citation["claim_id"] == "CLM-A-RECON"
    }
    assert reconciliation_citations == {"GUIDELINE", "PAYER_POLICY"}


def test_missing_payer_uses_current_unavailable_behavior_without_payer_conclusion(
    client: TestClient,
) -> None:
    # Milestone 3 policy note: this freezes only today's SOURCE_UNAVAILABLE path.
    # NOT_FOUND/MALFORMED and other missing-source combinations are intentionally
    # deferred until the answer policy implements them; retrieval contracts for
    # those statuses are characterized in test_retrieval.py.
    payload = ask(client, "PAYER_POLICY_UNAVAILABLE")
    payer_trace = next(
        item
        for item in payload["orchestration_trace"]
        if item["source_type"] == "PAYER_POLICY"
    )

    assert payer_trace == {
        "sequence": 3,
        "source_type": "PAYER_POLICY",
        "label": "Payer",
        "status": "SOURCE_UNAVAILABLE",
    }
    assert payload["policy_version_id"] is None
    assert claim_evidence(payload) == {
        "CLM-C-CASE": {"EV-SYN-EHR-CONTEXT-001", "EV-SYN-EHR-PLAN-001"},
        "CLM-C-GUIDE": {
            "EV-SYN-GUIDE-SUPPORT-001",
            "EV-SYN-GUIDE-SCOPE-001",
        },
        "CLM-C-FORM": {"EV-SYN-FORM-STATUS-001"},
    }
    assert not any(
        citation["source_type"] == "PAYER_POLICY"
        for citation in payload["citations"]
    )
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert len(payload["reconciliation"]) == 1
    assert payload["reconciliation"][0]["type"] == "SOURCE_UNAVAILABLE"
    assert payload["reconciliation"][0]["severity"] == "HIGH"
    assert payload["reconciliation"][0]["resolution_state"] == "UNRESOLVED"
    assert "cannot determine whether prior authorization is required" in payload["answer"]


def test_missing_payer_behavior_is_driven_by_retrieval_status(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def payer_unavailable(service: RetrievalService, request: Any) -> EvidenceBundle:
        return service.retrieve(
            replace(request, source_mode="PAYER_POLICY_UNAVAILABLE")
        )

    install_retrieval_transform(monkeypatch, settings, payer_unavailable)
    payload = ask(client, "BASELINE")

    assert payload["policy_version_id"] is None
    assert payload["confidence"] == "LOW"
    assert payload["reconciliation"][0]["type"] == "SOURCE_UNAVAILABLE"


@pytest.mark.parametrize(
    "status",
    [RetrievalStatus.NOT_FOUND, RetrievalStatus.MALFORMED],
)
def test_other_unusable_payer_statuses_are_safe_and_escalated(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: RetrievalStatus,
) -> None:
    def unusable_payer(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(request)
        return replace_source_result(
            bundle,
            SourceType.PAYER_POLICY,
            failed_result(SourceType.PAYER_POLICY, status),
        )

    install_retrieval_transform(monkeypatch, settings, unusable_payer)
    payload = ask(client)

    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert payload["policy_version_id"] is None
    assert payload["reconciliation"][0]["type"] == "SOURCE_UNAVAILABLE"
    assert not any(
        citation["source_type"] == "PAYER_POLICY"
        for citation in payload["citations"]
    )
    assert "cannot determine whether prior authorization is required" in payload["answer"]


@pytest.mark.parametrize(
    ("source_type", "missing_claims"),
    [
        (SourceType.GUIDELINE, {"CLM-A-GUIDE", "CLM-A-RECON"}),
        (SourceType.FORMULARY, {"CLM-A-FORM"}),
    ],
)
def test_important_source_gap_is_medium_without_unsupported_claims(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    source_type: SourceType,
    missing_claims: set[str],
) -> None:
    def missing_source(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(request)
        return replace_source_result(
            bundle,
            source_type,
            failed_result(source_type, RetrievalStatus.NOT_FOUND),
        )

    install_retrieval_transform(monkeypatch, settings, missing_source)
    payload = ask(client)

    assert payload["confidence"] == "MEDIUM"
    assert payload["escalation"] is None
    assert missing_claims.isdisjoint(claim_evidence(payload))
    assert not any(
        citation["source_type"] == source_type.value
        for citation in payload["citations"]
    )
    if source_type is SourceType.GUIDELINE:
        assert "guideline supports Veluntra" not in payload["answer"]


def test_optional_specialist_gap_remains_high_and_uses_ehr_history(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_note(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(request)
        return replace_source_result(
            bundle,
            SourceType.SPECIALIST_NOTE,
            failed_result(SourceType.SPECIALIST_NOTE, RetrievalStatus.NOT_FOUND),
        )

    install_retrieval_transform(monkeypatch, settings, missing_note)
    payload = ask(client)

    assert payload["confidence"] == "HIGH"
    assert payload["escalation"] is None
    assert claim_evidence(payload)["CLM-A-HISTORY"] == {
        "EV-SYN-EHR-THERAPY-001"
    }
    assert not any(
        citation["source_type"] == "SPECIALIST_NOTE"
        for citation in payload["citations"]
    )


def test_true_conflict_represents_both_sides_and_does_not_choose_winner(
    client: TestClient,
) -> None:
    payload = ask(client, "TEST_ONLY_FORMULARY_SUBSTITUTION")
    conflict = payload["reconciliation"][0]

    assert claim_evidence(payload) == {
        "CLM-F-POLICY": {"EV-SYN-POL-V1-PA-001"},
        "CLM-F-FORM": {"EV-SYN-FORM-CONFLICT-001"},
        "CLM-F-CONFLICT": {
            "EV-SYN-POL-V1-PA-001",
            "EV-SYN-FORM-CONFLICT-001",
        },
    }
    assert conflict["type"] == "SAME_DIMENSION_DISAGREEMENT"
    assert conflict["severity"] == "HIGH"
    assert conflict["resolution_state"] == "UNRESOLVED"
    assert "winner" not in conflict
    assert citation_pairs(payload) == {
        ("CLM-F-POLICY", "EV-SYN-POL-V1-PA-001"),
        ("CLM-F-FORM", "EV-SYN-FORM-CONFLICT-001"),
        ("CLM-F-CONFLICT", "EV-SYN-POL-V1-PA-001"),
        ("CLM-F-CONFLICT", "EV-SYN-FORM-CONFLICT-001"),
    }
    assert payload["policy_version_id"] == BASELINE_POLICY_VERSION
    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True


def test_true_conflict_behavior_is_driven_by_formulary_retrieval_result(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflicting_formulary(
        service: RetrievalService,
        request: Any,
    ) -> EvidenceBundle:
        return service.retrieve(
            replace(request, source_mode="TEST_ONLY_FORMULARY_SUBSTITUTION")
        )

    install_retrieval_transform(monkeypatch, settings, conflicting_formulary)
    payload = ask(client, "BASELINE")

    assert payload["reconciliation"][0]["type"] == "SAME_DIMENSION_DISAGREEMENT"
    assert "EV-SYN-FORM-CONFLICT-001" in {
        citation["evidence_id"] for citation in payload["citations"]
    }


def test_true_conflict_classification_does_not_depend_on_evidence_id(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renamed_id = "EV-STRUCTURED-AUTHORIZATION-DISAGREEMENT"

    def renamed_conflict(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(
            replace(request, source_mode="TEST_ONLY_FORMULARY_SUBSTITUTION")
        )
        formulary = next(
            result
            for result in bundle.retrieval_results
            if result.source_type is SourceType.FORMULARY
        )
        renamed = replace(formulary.evidence_items[0], evidence_id=renamed_id)
        replacement = RetrievalResult(
            source_type=SourceType.FORMULARY,
            status=RetrievalStatus.RETRIEVED,
            evidence_items=(renamed,),
            document_version_ids=formulary.document_version_ids,
        )
        return replace_source_result(bundle, SourceType.FORMULARY, replacement)

    install_retrieval_transform(monkeypatch, settings, renamed_conflict)
    monkeypatch.setattr(demo, "_persist_interaction", lambda *args, **kwargs: None)
    payload = ask(client)

    assert payload["reconciliation"][0]["type"] == "SAME_DIMENSION_DISAGREEMENT"
    assert renamed_id in claim_evidence(payload)["CLM-F-CONFLICT"]
    assert renamed_id in {
        citation["evidence_id"] for citation in payload["citations"]
    }


def test_unsupported_question_does_not_retrieve_or_fabricate_clinical_output(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/questions",
        json={"question": "Can this demo schedule a home delivery?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "UNSUPPORTED"
    assert payload["status"] == "UNSUPPORTED_SCOPE"
    assert payload["selected_sources"] == []
    assert payload["orchestration_trace"] == []
    assert payload["claims"] == []
    assert payload["citations"] == []
    assert payload["confidence"] is None
    assert payload["confidence_rationale"] is None
    assert payload["reconciliation"] == []
    assert payload["escalation"] is None
    assert payload["policy_version_id"] is None


def test_unsupported_question_does_not_construct_retrieval_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRetrievalService:
        def __init__(self, database_path: Path) -> None:
            raise AssertionError(f"Retrieval service constructed for {database_path}")

    def fail_reason(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"Reasoning executed: {args!r} {kwargs!r}")

    monkeypatch.setattr(demo, "RetrievalService", FailingRetrievalService)
    monkeypatch.setattr(demo, "reason", fail_reason)
    response = client.post(
        "/api/v1/questions",
        json={"question": "Can this demo schedule a home delivery?"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "UNSUPPORTED_SCOPE"


def test_claim_with_missing_evidence_is_omitted_without_citation_failure(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def omit_requested_evidence(
        service: RetrievalService,
        request: Any,
    ) -> EvidenceBundle:
        bundle = service.retrieve(request)
        results: list[RetrievalResult] = []
        for result in bundle.retrieval_results:
            if result.source_type is SourceType.PAYER_POLICY:
                result = RetrievalResult(
                    source_type=result.source_type,
                    status=result.status,
                    evidence_items=tuple(
                        item
                        for item in result.evidence_items
                        if item.evidence_id != "EV-SYN-POL-V1-PA-001"
                    ),
                    document_version_ids=result.document_version_ids,
                )
            results.append(result)
        return EvidenceBundle.from_results(tuple(results), bundle.retrieval_trace)

    install_retrieval_transform(monkeypatch, settings, omit_requested_evidence)

    payload = ask(client)

    assert payload["confidence"] == "LOW"
    assert payload["escalation"]["required"] is True
    assert "CLM-A-PA" not in claim_evidence(payload)
    assert not any(
        citation["evidence_id"] == "EV-SYN-POL-V1-PA-001"
        for citation in payload["citations"]
    )


def test_citation_resolution_guard_still_rejects_unknown_evidence(
    settings: Settings,
) -> None:
    demo.initialize_demo(settings)
    bundle = RetrievalService(settings.database_path).retrieve(
        RetrievalRequest(
            interaction_id="INT-CITATION-GUARD",
            intent="PRIOR_AUTHORIZATION",
            case_id="SYN-CASE-001",
            as_of=demo.BASELINE_TIME,
            medication_id="SYN-MED-VEL",
            indication_id="SYN-COND-LDS",
            payer_id="SYN-PAYER-NHH",
            plan_id="SYN-PLAN-HLP",
        )
    )

    with pytest.raises(
        demo.CitationResolutionError,
        match="EV-UNKNOWN.*not returned by retrieval",
    ):
        demo._resolve_claim_citations(
            bundle,
            [{"claim_id": "CLM-UNSAFE", "evidence_ids": ["EV-UNKNOWN"]}],
        )


def test_question_path_does_not_open_fixture_json(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any):
        if "fixtures" in path.parts:
            raise AssertionError(f"Question path opened fixture data: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    assert ask(client)["status"] == "ANSWERED"


def test_pending_feedback_leaves_v1_policy_and_evidence_current(
    client: TestClient,
) -> None:
    initial = ask(client)
    feedback_response = client.post(
        "/api/v1/feedback",
        json={
            "interaction_id": initial["interaction_id"],
            "message": "Payer policy V1 is outdated; use V2.",
        },
    )

    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["status"] == "PENDING"
    assert feedback["target_version_id"] == BASELINE_POLICY_VERSION
    assert feedback["proposed_version_id"] == UPDATED_POLICY_VERSION

    pending_result = ask(client)
    pending_evidence_ids = {
        citation["evidence_id"] for citation in pending_result["citations"]
    }
    assert pending_result["policy_version_id"] == BASELINE_POLICY_VERSION
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= (
        pending_evidence_ids
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V2-")
        for evidence_id in pending_evidence_ids
    )


def test_approval_activates_v2_claims_and_evidence(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = ask(client)
    approve_v2(client, initial["interaction_id"])

    observed: dict[str, EvidenceBundle] = {}

    def capture(service: RetrievalService, request: Any) -> EvidenceBundle:
        bundle = service.retrieve(request)
        observed["bundle"] = bundle
        return bundle

    install_retrieval_transform(monkeypatch, settings, capture)

    updated = ask(client)
    assert updated["policy_version_id"] == UPDATED_POLICY_VERSION
    assert claim_evidence(updated) == UPDATED_CLAIM_EVIDENCE
    assert citation_pairs(updated) == {
        (claim_id, evidence_id)
        for claim_id, evidence_ids in UPDATED_CLAIM_EVIDENCE.items()
        for evidence_id in evidence_ids
    }
    assert len(updated["reconciliation"]) == 1
    updated_reconciliation = updated["reconciliation"][0]
    assert updated_reconciliation["type"] == "COMPATIBLE_CONSTRAINT"
    assert updated_reconciliation["severity"] == "INFORMATIONAL"
    assert updated_reconciliation["resolution_state"] == "NOT_APPLICABLE"
    assert "winner" not in updated_reconciliation
    assert {
        citation["source_type"]
        for citation in updated["citations"]
        if citation["claim_id"] in {"CLM-E-V2-PA", "CLM-A-GUIDE"}
    } == {"GUIDELINE", "PAYER_POLICY"}
    assert updated["confidence"] == "HIGH"
    assert updated["escalation"] is None

    payer_claim_ids = {"CLM-E-V2-PA", "CLM-E-V2-CRITERIA", "CLM-E-READY"}
    payer_claim_evidence = {
        evidence_id
        for claim_id, evidence_ids in claim_evidence(updated).items()
        if claim_id in payer_claim_ids
        for evidence_id in evidence_ids
    }
    assert {"EV-SYN-POL-V2-PA-001", "EV-SYN-POL-V2-STEP-001"} <= (
        payer_claim_evidence
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V1-")
        for evidence_id in payer_claim_evidence
    )
    payer_result = next(
        result
        for result in observed["bundle"].retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )
    assert payer_result.document_version_ids == (UPDATED_POLICY_VERSION,)


def test_approval_preserves_original_v1_interaction_snapshot(
    client: TestClient,
) -> None:
    initial = ask(client)
    original_snapshot = {
        "policy_version_id": initial["policy_version_id"],
        "claims": initial["claims"],
        "citations": [
            {
                "claim_id": citation["claim_id"],
                "evidence_id": citation["evidence_id"],
                "document_version_id": citation["document_version_id"],
                "version": citation["version"],
            }
            for citation in initial["citations"]
        ],
        "confidence": initial["confidence"],
        "confidence_rationale": initial["confidence_rationale"],
        "reconciliation": initial["reconciliation"],
        "escalation": initial["escalation"],
    }
    approve_v2(client, initial["interaction_id"])

    response = client.get(f"/api/v1/interactions/{initial['interaction_id']}")
    assert response.status_code == 200
    historical = response.json()
    historical_snapshot = {
        "policy_version_id": historical["policy_version_id"],
        "claims": historical["claims"],
        "citations": [
            {
                "claim_id": citation["claim_id"],
                "evidence_id": citation["evidence_id"],
                "document_version_id": citation["document_version_id"],
                "version": citation["version"],
            }
            for citation in historical["citations"]
        ],
        "confidence": historical["confidence"],
        "confidence_rationale": historical["confidence_rationale"],
        "reconciliation": historical["reconciliation"],
        "escalation": historical["escalation"],
    }
    assert historical_snapshot == original_snapshot
    assert historical["policy_version_id"] == BASELINE_POLICY_VERSION
    assert all(
        citation["document_version_id"] != UPDATED_POLICY_VERSION
        for citation in historical["citations"]
        if citation["source_type"] == "PAYER_POLICY"
    )


def test_approval_records_lineage_and_audit(client: TestClient, settings) -> None:
    initial = ask(client)
    feedback = approve_v2(client, initial["interaction_id"])

    audit = client.get(f"/api/v1/audit/{initial['interaction_id']}").json()
    types = [event["event_type"] for event in audit["events"]]
    assert "FEEDBACK_SUBMITTED" in types
    assert "REVIEW_APPROVED" in types
    assert "KNOWLEDGE_UPDATE_APPLIED" in types
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT prior_version_id, successor_version_id FROM assertion_supersession "
            "WHERE feedback_id = ?",
            (feedback["feedback_id"],),
        ).fetchone() == (BASELINE_POLICY_VERSION, UPDATED_POLICY_VERSION)


def test_demo_reset_restores_v1_behavior_and_evidence(client: TestClient) -> None:
    initial = ask(client)
    approve_v2(client, initial["interaction_id"])
    assert ask(client)["policy_version_id"] == UPDATED_POLICY_VERSION

    reset = client.post(
        "/api/demo/reset",
        json={"confirmation": "RESET_SYNTHETIC_DEMO"},
    )
    assert reset.status_code == 200

    restored = ask(client)
    restored_evidence_ids = {
        citation["evidence_id"] for citation in restored["citations"]
    }
    assert restored["policy_version_id"] == BASELINE_POLICY_VERSION
    assert claim_evidence(restored) == BASELINE_CLAIM_EVIDENCE
    assert {"EV-SYN-POL-V1-PA-001", "EV-SYN-POL-V1-STEP-001"} <= (
        restored_evidence_ids
    )
    assert not any(
        evidence_id.startswith("EV-SYN-POL-V2-")
        for evidence_id in restored_evidence_ids
    )
    assert restored["confidence"] == "HIGH"
    assert restored["escalation"] is None
    assert restored["reconciliation"][0]["type"] == "COMPATIBLE_CONSTRAINT"
    assert restored["reconciliation"][0]["severity"] == "INFORMATIONAL"
    assert restored["reconciliation"][0]["resolution_state"] == "NOT_APPLICABLE"
