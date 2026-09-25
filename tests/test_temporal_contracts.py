"""Validated temporal request contracts for current and as-of selection."""

from __future__ import annotations

from datetime import datetime

import pytest

from backend import demo
from backend.config import Settings
from backend.retrieval import PayerPolicyAdapter, RetrievalRequest, TemporalMode


UTC_TIME = "2026-06-15T14:00:00Z"


def _request(**changes: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "interaction_id": "INT-SYN-TEMPORAL-CONTRACT",
        "intent": "PRIOR_AUTHORIZATION",
        "case_id": "SYN-CASE-001",
        "medication_id": "SYN-MED-VEL",
        "indication_id": "SYN-COND-LDS",
        "payer_id": "SYN-PAYER-NHH",
        "plan_id": "SYN-PLAN-HLP",
    }
    values.update(changes)
    return RetrievalRequest(**values)  # type: ignore[arg-type]


def test_temporal_mode_has_only_current_and_as_of_values() -> None:
    assert tuple(TemporalMode) == (TemporalMode.CURRENT, TemporalMode.AS_OF)
    assert TemporalMode.CURRENT.value == "CURRENT"
    assert TemporalMode.AS_OF.value == "AS_OF"


def test_current_is_the_default_and_does_not_require_as_of() -> None:
    request = _request()

    assert request.temporal_mode is TemporalMode.CURRENT
    assert request.as_of is None


def test_explicit_current_without_as_of_is_valid() -> None:
    request = _request(temporal_mode=TemporalMode.CURRENT)

    assert request.temporal_mode is TemporalMode.CURRENT
    assert request.as_of is None


def test_current_accepts_as_of_for_existing_caller_compatibility() -> None:
    request = _request(as_of=UTC_TIME)

    assert request.temporal_mode is TemporalMode.CURRENT
    assert request.as_of == UTC_TIME


def test_as_of_mode_accepts_a_valid_utc_timestamp() -> None:
    request = _request(temporal_mode=TemporalMode.AS_OF, as_of=UTC_TIME)

    assert request.temporal_mode is TemporalMode.AS_OF
    assert request.as_of == UTC_TIME


def test_as_of_mode_requires_as_of() -> None:
    with pytest.raises(ValueError, match="AS_OF requests require as_of"):
        _request(temporal_mode=TemporalMode.AS_OF)


@pytest.mark.parametrize("value", ["not-a-time", "2026-06-15", ""])
def test_as_of_mode_rejects_malformed_or_naive_timestamp(value: str) -> None:
    with pytest.raises(ValueError, match="as_of must"):
        _request(temporal_mode=TemporalMode.AS_OF, as_of=value)


def test_naive_datetime_is_not_silently_interpreted_as_local_time() -> None:
    with pytest.raises(ValueError, match="as_of must"):
        _request(
            temporal_mode=TemporalMode.AS_OF,
            as_of=datetime(2026, 6, 15, 14, 0, 0),
        )


def test_non_utc_offset_is_not_reinterpreted_as_utc() -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        _request(
            temporal_mode=TemporalMode.AS_OF,
            as_of="2026-06-15T10:00:00-04:00",
        )


def test_invalid_temporal_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid temporal_mode"):
        _request(temporal_mode="LATEST")


def test_utc_representation_is_normalized_deterministically() -> None:
    request = _request(
        temporal_mode="AS_OF",
        as_of="2026-06-15T14:00:00.123000+00:00",
    )

    assert request.temporal_mode is TemporalMode.AS_OF
    assert request.as_of == "2026-06-15T14:00:00.123000Z"


def test_current_and_as_of_both_select_v1_at_the_seeded_june_instant(
    settings: Settings,
) -> None:
    demo.initialize_demo(settings)
    adapter = PayerPolicyAdapter(settings.database_path)

    current = adapter.retrieve(_request(as_of=UTC_TIME))
    as_of = adapter.retrieve(
        _request(temporal_mode=TemporalMode.AS_OF, as_of=UTC_TIME)
    )

    assert current.document_version_ids == ("DV-SYN-POL-VEL-V1",)
    assert as_of.document_version_ids == ("DV-SYN-POL-VEL-V1",)
