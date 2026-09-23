from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.retrieval import (
    EHRAdapter,
    EvidenceBundle,
    FormularyAdapter,
    GuidelineAdapter,
    PayerPolicyAdapter,
    RetrievalRequest,
    RetrievalResult,
    RetrievalService,
    RetrievalStatus,
    RetrievalTraceItem,
    SourceType,
    SpecialistNotesAdapter,
    build_plan,
)


@pytest.fixture
def retrieval_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def request(**changes: str) -> RetrievalRequest:
    values = {
        "interaction_id": "INT-SYN-RETRIEVAL-TEST",
        "intent": "PRIOR_AUTHORIZATION",
        "case_id": "SYN-CASE-001",
        "as_of": "2026-06-15T14:00:00Z",
        "medication_id": "SYN-MED-VEL",
        "indication_id": "SYN-COND-LDS",
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
        "source_mode": "BASELINE",
    }
    values.update(changes)
    return RetrievalRequest(**values)


def evidence_ids(result: RetrievalResult) -> set[str]:
    return {item.evidence_id for item in result.evidence_items}


def test_ehr_retrieves_all_exact_context_evidence(
    retrieval_settings: Settings,
) -> None:
    result = EHRAdapter(retrieval_settings.database_path).retrieve(request())

    assert result.status is RetrievalStatus.RETRIEVED
    assert result.document_version_ids == ("DV-SYN-EHR-CASE-001-V1",)
    assert evidence_ids(result) == {
        "EV-SYN-EHR-CONTEXT-001",
        "EV-SYN-EHR-PLAN-001",
        "EV-SYN-EHR-THERAPY-001",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "SYN-CASE-UNKNOWN"),
        ("medication_id", "SYN-MED-OTHER"),
        ("indication_id", "SYN-COND-OTHER"),
        ("payer_id", "SYN-PAYER-OTHER"),
        ("plan_id", "SYN-PLAN-OTHER"),
    ],
)
def test_ehr_requires_exact_case_context(
    retrieval_settings: Settings,
    field: str,
    value: str,
) -> None:
    result = EHRAdapter(retrieval_settings.database_path).retrieve(
        request(**{field: value})
    )

    assert result.status is RetrievalStatus.NOT_FOUND
    assert result.evidence_items == ()
    assert result.document_version_ids == ()


def test_guideline_retrieves_recommendation_and_authority_scope(
    retrieval_settings: Settings,
) -> None:
    result = GuidelineAdapter(retrieval_settings.database_path).retrieve(request())

    assert result.status is RetrievalStatus.RETRIEVED
    assert evidence_ids(result) == {
        "EV-SYN-GUIDE-SUPPORT-001",
        "EV-SYN-GUIDE-SCOPE-001",
    }
    assert all("CONFLICT" not in item.evidence_id for item in result.evidence_items)


def test_guideline_reports_missing_applicability(
    retrieval_settings: Settings,
) -> None:
    result = GuidelineAdapter(retrieval_settings.database_path).retrieve(
        request(indication_id="SYN-COND-OTHER")
    )

    assert result.status is RetrievalStatus.NOT_FOUND
    assert result.evidence_items == ()


def test_payer_uses_v1_initially_and_while_feedback_is_pending(
    retrieval_settings: Settings,
) -> None:
    adapter = PayerPolicyAdapter(retrieval_settings.database_path)
    assert adapter.retrieve(request()).document_version_ids == ("DV-SYN-POL-VEL-V1",)

    interaction = demo.ask_question(
        retrieval_settings,
        demo.CANONICAL_QUESTION,
    )
    demo.submit_feedback(
        retrieval_settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )

    pending = adapter.retrieve(request())
    assert pending.document_version_ids == ("DV-SYN-POL-VEL-V1",)
    assert evidence_ids(pending) == {
        "EV-SYN-POL-V1-PA-001",
        "EV-SYN-POL-V1-STEP-001",
    }


def test_payer_uses_v2_after_existing_approval_workflow(
    retrieval_settings: Settings,
) -> None:
    interaction = demo.ask_question(retrieval_settings, demo.CANONICAL_QUESTION)
    feedback = demo.submit_feedback(
        retrieval_settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )
    demo.approve_feedback(
        retrieval_settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "The supplied synthetic policy version is approved.",
    )

    result = PayerPolicyAdapter(retrieval_settings.database_path).retrieve(request())
    assert result.document_version_ids == ("DV-SYN-POL-VEL-V2",)
    assert evidence_ids(result) == {
        "EV-SYN-POL-V2-PA-001",
        "EV-SYN-POL-V2-STEP-001",
    }


def test_payer_unavailable_has_no_fabricated_evidence(
    retrieval_settings: Settings,
) -> None:
    result = PayerPolicyAdapter(retrieval_settings.database_path).retrieve(
        request(source_mode="PAYER_POLICY_UNAVAILABLE")
    )

    assert result.status is RetrievalStatus.SOURCE_UNAVAILABLE
    assert result.evidence_items == ()
    assert result.document_version_ids == ()
    assert result.failure_reason


def test_formulary_baseline_excludes_test_only_conflict(
    retrieval_settings: Settings,
) -> None:
    result = FormularyAdapter(retrieval_settings.database_path).retrieve(request())

    assert result.document_version_ids == ("DV-SYN-FORM-HLP-2026-H1",)
    assert evidence_ids(result) == {
        "EV-SYN-FORM-PREF-001",
        "EV-SYN-FORM-STATUS-001",
    }
    assert "EV-SYN-FORM-CONFLICT-001" not in evidence_ids(result)


def test_formulary_conflict_mode_returns_only_isolated_test_evidence(
    retrieval_settings: Settings,
) -> None:
    result = FormularyAdapter(retrieval_settings.database_path).retrieve(
        request(source_mode="TEST_ONLY_FORMULARY_SUBSTITUTION")
    )

    assert result.document_version_ids == ("DV-SYN-FORM-HLP-CONFLICT",)
    assert evidence_ids(result) == {"EV-SYN-FORM-CONFLICT-001"}
    assert "EV-SYN-FORM-STATUS-001" not in evidence_ids(result)


def test_specialist_note_retrieval_and_optional_absence(
    retrieval_settings: Settings,
) -> None:
    adapter = SpecialistNotesAdapter(retrieval_settings.database_path)
    result = adapter.retrieve(request())
    missing = adapter.retrieve(request(case_id="SYN-CASE-UNKNOWN"))

    assert result.status is RetrievalStatus.RETRIEVED
    assert evidence_ids(result) == {
        "EV-SYN-NOTE-HISTORY-001",
        "EV-SYN-NOTE-REQUEST-001",
    }
    assert missing.status is RetrievalStatus.NOT_FOUND
    assert missing.evidence_items == ()


def test_plan_preserves_exact_deterministic_source_order() -> None:
    expected = (
        SourceType.EHR,
        SourceType.GUIDELINE,
        SourceType.PAYER_POLICY,
        SourceType.FORMULARY,
        SourceType.SPECIALIST_NOTE,
    )

    for intent in (
        "PRIOR_AUTHORIZATION",
        "CLINICAL_GUIDANCE",
        "SPECIALIST_HISTORY",
    ):
        assert build_plan(intent).ordered_sources == expected


def test_unsupported_intent_executes_no_adapters(
    retrieval_settings: Settings,
) -> None:
    class FailingAdapter:
        def retrieve(self, retrieval_request: RetrievalRequest) -> RetrievalResult:
            raise AssertionError(f"Adapter executed for {retrieval_request.intent}")

    service = RetrievalService(
        retrieval_settings.database_path,
        {SourceType.EHR: FailingAdapter()},
    )
    bundle = service.retrieve(request(intent="UNSUPPORTED"))

    assert bundle.retrieval_results == ()
    assert bundle.evidence_by_id == {}
    assert bundle.source_statuses == {}
    assert bundle.retrieval_trace == ()


def test_service_assembles_statuses_trace_and_minimum_provenance(
    retrieval_settings: Settings,
) -> None:
    bundle = RetrievalService(retrieval_settings.database_path).retrieve(
        request(source_mode="PAYER_POLICY_UNAVAILABLE")
    )

    assert tuple(result.source_type for result in bundle.retrieval_results) == (
        SourceType.EHR,
        SourceType.GUIDELINE,
        SourceType.PAYER_POLICY,
        SourceType.FORMULARY,
        SourceType.SPECIALIST_NOTE,
    )
    assert bundle.source_statuses[SourceType.PAYER_POLICY] is RetrievalStatus.SOURCE_UNAVAILABLE
    assert tuple(item.display_label for item in bundle.retrieval_trace) == (
        "EHR",
        "Guideline",
        "Payer",
        "Formulary",
        "Specialist Notes",
    )
    assert all(
        (
            item.source_id,
            item.source_type,
            item.source_title,
            item.version,
            item.timestamp,
            item.relevant_excerpt,
            item.document_id,
            item.document_version_id,
            item.document_recorded_at,
            item.document_effective_from,
        )
        for item in bundle.evidence_by_id.values()
    )
    assert not any(
        item.source_type is SourceType.PAYER_POLICY
        for item in bundle.evidence_by_id.values()
    )


def test_bundle_rejects_duplicate_evidence_ids(
    retrieval_settings: Settings,
) -> None:
    result = EHRAdapter(retrieval_settings.database_path).retrieve(request())
    trace = (
        RetrievalTraceItem(SourceType.EHR, "EHR", RetrievalStatus.RETRIEVED),
        RetrievalTraceItem(SourceType.EHR, "EHR", RetrievalStatus.RETRIEVED),
    )

    with pytest.raises(ValueError, match="Duplicate evidence ID"):
        EvidenceBundle.from_results((result, result), trace)


def test_every_returned_evidence_id_exists_in_sqlite(
    retrieval_settings: Settings,
) -> None:
    bundle = RetrievalService(retrieval_settings.database_path).retrieve(request())
    with database.managed_connection(retrieval_settings.database_path) as connection:
        persisted = {
            row[0]
            for row in connection.execute("SELECT evidence_id FROM evidence_item").fetchall()
        }

    assert set(bundle.evidence_by_id) <= persisted


def test_normal_retrieval_does_not_open_fixture_json(
    retrieval_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any):
        if "fixtures" in path.parts:
            raise AssertionError(f"Retrieval opened fixture data: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    bundle = RetrievalService(retrieval_settings.database_path).retrieve(request())
    assert bundle.source_statuses == {
        source_type: RetrievalStatus.RETRIEVED
        for source_type in (
            SourceType.EHR,
            SourceType.GUIDELINE,
            SourceType.PAYER_POLICY,
            SourceType.FORMULARY,
            SourceType.SPECIALIST_NOTE,
        )
    }


def test_malformed_persisted_json_returns_normalized_failure(
    retrieval_settings: Settings,
) -> None:
    with database.managed_connection(retrieval_settings.database_path) as connection:
        connection.execute(
            "UPDATE source_document_version SET structured_data = ? "
            "WHERE document_version_id = ?",
            ("not-json", "DV-SYN-GUIDE-LDS-2026-1"),
        )

    result = GuidelineAdapter(retrieval_settings.database_path).retrieve(request())
    assert result.status is RetrievalStatus.MALFORMED
    assert result.evidence_items == ()
    assert result.document_version_ids == ()
    assert "could not be normalized" in (result.failure_reason or "")
