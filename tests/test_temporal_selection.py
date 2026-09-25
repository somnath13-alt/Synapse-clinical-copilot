"""Specification and regressions for temporal source-version selection.

M5-D2 fixes the known v1.3 defect: AS_OF now requires both governance
eligibility and strict effective-interval containment. CURRENT continues to use
the existing ``is_current`` selector.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backend import database, demo
from backend.config import Settings
from backend.retrieval import (
    PayerPolicyAdapter,
    RetrievalRequest,
    RetrievalResult,
    RetrievalService,
    RetrievalStatus,
    SourceType,
    TemporalMode,
)


V1 = "DV-SYN-POL-VEL-V1"
V2 = "DV-SYN-POL-VEL-V2"
JUNE_15 = "2026-06-15T14:00:00Z"
JULY_3 = "2026-07-03T14:00:00Z"
V1_CLOSED_END = "2026-06-30T23:59:59Z"
V2_CLOSED_START = "2026-07-01T00:00:00Z"


@pytest.fixture
def initialized_settings(settings: Settings) -> Settings:
    demo.initialize_demo(settings)
    return settings


def _request(
    *,
    temporal_mode: TemporalMode = TemporalMode.AS_OF,
    as_of: str | None = JUNE_15,
) -> RetrievalRequest:
    values: dict[str, Any] = {
        "interaction_id": "INT-SYN-TEMPORAL-SELECTION",
        "intent": "PRIOR_AUTHORIZATION",
        "case_id": "SYN-CASE-001",
        "medication_id": "SYN-MED-VEL",
        "indication_id": "SYN-COND-LDS",
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
        "temporal_mode": temporal_mode,
    }
    if as_of is not None:
        values["as_of"] = as_of
    return RetrievalRequest(**values)


def _payer_result(bundle: Any) -> RetrievalResult:
    return next(
        result
        for result in bundle.retrieval_results
        if result.source_type is SourceType.PAYER_POLICY
    )


def _submit_v2(settings: Settings) -> dict[str, Any]:
    interaction = demo.ask_question(settings, demo.CANONICAL_QUESTION)
    return demo.submit_feedback(
        settings,
        interaction["interaction_id"],
        "Synthetic Care Coordinator",
        "Payer policy V1 is outdated; use V2.",
    )


def _approve_v2(settings: Settings) -> None:
    feedback = _submit_v2(settings)
    demo.approve_feedback(
        settings,
        feedback["feedback_id"],
        "Synthetic Knowledge Reviewer",
        "Verified the pre-seeded synthetic V2 provenance.",
    )


@pytest.mark.parametrize(
    "as_of",
    [
        pytest.param(JUNE_15, id="case-1-june-15"),
        pytest.param(JULY_3, id="case-2-july-3"),
    ],
)
def test_as_of_pending_v2_uses_strict_governance_and_interval_selection(
    initialized_settings: Settings,
    as_of: str,
) -> None:
    """Pending V2 is excluded and AS_OF never falls back to current V1."""

    feedback = _submit_v2(initialized_settings)
    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(as_of=as_of)
    )

    with database.managed_connection(initialized_settings.database_path) as connection:
        v2_state = connection.execute(
            """SELECT governance_state, is_current, effective_from, effective_to
               FROM source_document_version
               WHERE document_version_id = ?""",
            (V2,),
        ).fetchone()

    assert feedback["status"] == "PENDING"
    assert v2_state == ("CANDIDATE_NOT_CURRENT", 0, V2_CLOSED_START, None)
    if as_of == JUNE_15:
        assert result.status is RetrievalStatus.RETRIEVED
        assert result.document_version_ids == (V1,)
    else:
        assert result.status is RetrievalStatus.NOT_FOUND
        assert result.document_version_ids == ()
        assert result.evidence_items == ()

    # June 15 is inside applied V1. On July 3, V1 is outside its interval and
    # V2 is still a candidate, so strict AS_OF selection returns no evidence.


def test_as_of_june_15_after_v2_approval_returns_applied_v1(
    initialized_settings: Settings,
) -> None:
    """The M5-D2 regression: current V2 cannot displace applicable V1."""

    _approve_v2(initialized_settings)
    request = _request(as_of=JUNE_15)
    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(request)

    assert request.temporal_mode is TemporalMode.AS_OF
    assert request.as_of == JUNE_15
    assert result.document_version_ids == (V1,)


def test_as_of_july_3_after_v2_approval_returns_applied_v2(
    initialized_settings: Settings,
) -> None:
    _approve_v2(initialized_settings)

    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(as_of=JULY_3)
    )

    assert result.document_version_ids == (V2,)


def test_as_of_uses_closed_boundaries_at_the_v1_to_v2_cutover(
    initialized_settings: Settings,
) -> None:
    """Seeded intervals meet at consecutive closed UTC boundary instants."""

    _approve_v2(initialized_settings)
    adapter = PayerPolicyAdapter(initialized_settings.database_path)
    v1_end_result = adapter.retrieve(_request(as_of=V1_CLOSED_END))
    v2_start_result = adapter.retrieve(_request(as_of=V2_CLOSED_START))

    with database.managed_connection(initialized_settings.database_path) as connection:
        intervals = connection.execute(
            """SELECT document_version_id, effective_from, effective_to
               FROM source_document_version
               WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (V1, V2),
        ).fetchall()

    assert intervals == [
        (V1, "2026-01-01T00:00:00Z", V1_CLOSED_END),
        (V2, V2_CLOSED_START, None),
    ]
    assert v1_end_result.document_version_ids == (V1,)
    assert v2_start_result.document_version_ids == (V2,)


def test_pending_v2_current_still_returns_v1(
    initialized_settings: Settings,
) -> None:
    """CURRENT remains independent of strict AS_OF interval selection."""

    feedback = _submit_v2(initialized_settings)
    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(temporal_mode=TemporalMode.CURRENT, as_of=None)
    )

    assert feedback["status"] == "PENDING"
    assert result.status is RetrievalStatus.RETRIEVED
    assert result.document_version_ids == (V1,)


def test_current_selection_parity_remains_v1_before_and_v2_after_approval(
    initialized_settings: Settings,
) -> None:
    """CURRENT remains the exact v1.3 is_current selection contract."""

    adapter = PayerPolicyAdapter(initialized_settings.database_path)
    current_request = _request(temporal_mode=TemporalMode.CURRENT, as_of=None)

    before = adapter.retrieve(current_request)
    _approve_v2(initialized_settings)
    after = adapter.retrieve(current_request)

    assert current_request.as_of is None
    assert before.document_version_ids == (V1,)
    assert after.document_version_ids == (V2,)


def _install_overlapping_applied_versions(settings: Settings) -> tuple[str, str]:
    """Create two matching, governed, overlapping versions in the test DB only."""

    version_ids = ("DV-SYN-POL-OVERLAP-A", "DV-SYN-POL-OVERLAP-B")
    specifications = (
        (
            "A",
            version_ids[0],
            "99.0",
            "2026-08-01T00:00:00Z",
        ),
        (
            "B",
            version_ids[1],
            "0.1",
            "2025-12-01T00:00:00Z",
        ),
    )
    with database.managed_connection(settings.database_path) as connection:
        connection.execute(
            "UPDATE source_document_version SET is_current = 0 "
            "WHERE document_version_id = ?",
            (V1,),
        )
        for suffix, version_id, version_label, recorded_at in specifications:
            document_id = f"DOC-SYN-POL-OVERLAP-{suffix}"
            source_id = f"SRC-SYN-POL-OVERLAP-{suffix}"
            connection.execute(
                """INSERT INTO source_document
                       (document_id, source_id, source_type, source_title, synthetic)
                   SELECT ?, ?, source_type, source_title, synthetic
                   FROM source_document WHERE document_id = ?""",
                (document_id, source_id, "DOC-SYN-POL-VEL"),
            )
            connection.execute(
                """INSERT INTO source_document_version
                       (document_version_id, document_id, version, timestamp,
                        recorded_at, effective_from, effective_to, relevant_excerpt,
                        structured_data, checksum, governance_state, is_current,
                        test_only)
                   SELECT ?, ?, ?, timestamp, ?, ?, ?, relevant_excerpt,
                          structured_data, checksum, 'APPLIED', 1, 0
                   FROM source_document_version WHERE document_version_id = ?""",
                (
                    version_id,
                    document_id,
                    version_label,
                    recorded_at,
                    "2026-06-01T00:00:00Z",
                    "2026-07-31T23:59:59Z",
                    V1,
                ),
            )
            for evidence_suffix, source_evidence_id in (
                ("PA", "EV-SYN-POL-V1-PA-001"),
                ("STEP", "EV-SYN-POL-V1-STEP-001"),
            ):
                connection.execute(
                    """INSERT INTO evidence_item
                           (evidence_id, document_version_id, source_id, source_type,
                            source_title, version, timestamp, section,
                            relevant_excerpt, structured_data)
                       SELECT ?, ?, ?, source_type, source_title, ?, timestamp,
                              section, relevant_excerpt, structured_data
                       FROM evidence_item WHERE evidence_id = ?""",
                    (
                        f"EV-SYN-POL-OVERLAP-{suffix}-{evidence_suffix}",
                        version_id,
                        source_id,
                        version_label,
                        source_evidence_id,
                    ),
                )
    return version_ids


def test_overlapping_applied_versions_are_both_representable_in_deterministic_trace(
    initialized_settings: Settings,
) -> None:
    """The result seam carries all matches without a version-level winner."""

    version_ids = _install_overlapping_applied_versions(initialized_settings)
    bundle = RetrievalService(initialized_settings.database_path).retrieve(
        _request(as_of=JUNE_15)
    )
    payer = _payer_result(bundle)

    expected_version_ids = (*version_ids, V1)
    assert payer.document_version_ids == expected_version_ids
    assert tuple(dict.fromkeys(item.document_version_id for item in payer.evidence_items)) == (
        expected_version_ids
    )
    assert tuple(item.source_type for item in bundle.retrieval_trace) == (
        SourceType.EHR,
        SourceType.GUIDELINE,
        SourceType.PAYER_POLICY,
        SourceType.FORMULARY,
        SourceType.SPECIALIST_NOTE,
    )
    assert bundle.retrieval_trace[2].status is RetrievalStatus.RETRIEVED

    # Both inserted versions and the seeded applicable V1 are retained in
    # deterministic ID order. No winner is chosen by version label (99.0 versus 0.1),
    # recorded_at, source timestamp, approval time, or newest-version heuristics.


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        pytest.param("effective_from", "", id="missing-effective-from"),
        pytest.param(
            "effective_to",
            sqlite3.Binary(b"not-text"),
            id="malformed-effective-to",
        ),
        pytest.param(
            "effective_from",
            "2026-01-01T00:00:00",
            id="naive-effective-from",
        ),
    ],
)
def test_invalid_effective_interval_returns_safe_evidence_free_failure(
    initialized_settings: Settings,
    column: str,
    corrupt_value: object,
) -> None:
    """Missing or malformed persisted interval fields cannot leak evidence."""

    assert column in {"effective_from", "effective_to"}
    with database.managed_connection(initialized_settings.database_path) as connection:
        connection.execute(
            f"UPDATE source_document_version SET {column} = ? "
            "WHERE document_version_id = ?",
            (corrupt_value, V1),
        )

    result = PayerPolicyAdapter(initialized_settings.database_path).retrieve(
        _request(as_of=JUNE_15)
    )

    assert result.status is RetrievalStatus.MALFORMED
    assert result.document_version_ids == ()
    assert result.evidence_items == ()
    assert result.failure_reason is not None
    assert "could not be normalized" in result.failure_reason

    # D2 may strengthen timestamp parsing, but its minimum safe behavior remains
    # this explicit evidence-free failure; invalid intervals must never match.
