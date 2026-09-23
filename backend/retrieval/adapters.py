"""Source-specific SQLite adapters for the synthetic retrieval corpus."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import database
from backend.retrieval.models import (
    EvidenceItem,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
    SourceType,
)


PAYER_UNAVAILABLE_MODE = "PAYER_POLICY_UNAVAILABLE"
FORMULARY_CONFLICT_MODE = "TEST_ONLY_FORMULARY_SUBSTITUTION"


class _MalformedPersistedData(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _VersionEvidence:
    document_data: dict[str, Any]
    evidence_items: tuple[EvidenceItem, ...]

    @property
    def document_version_id(self) -> str:
        return self.evidence_items[0].document_version_id


_SELECT_EVIDENCE = """
    SELECT
        e.evidence_id,
        e.source_id,
        e.source_type,
        e.source_title,
        d.document_id,
        dv.document_version_id,
        e.version,
        e.timestamp,
        e.section,
        e.relevant_excerpt,
        e.structured_data AS evidence_structured_data,
        dv.recorded_at,
        dv.effective_from,
        dv.effective_to,
        dv.structured_data AS document_structured_data,
        d.source_id AS document_source_id,
        d.source_type AS document_source_type
    FROM source_document AS d
    JOIN source_document_version AS dv ON dv.document_id = d.document_id
    JOIN evidence_item AS e ON e.document_version_id = dv.document_version_id
    WHERE d.source_type = ? AND dv.is_current = ? AND dv.test_only = ?
    ORDER BY dv.document_version_id, e.evidence_id
"""


def _required_text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise _MalformedPersistedData(f"Persisted {field} is missing or invalid")
    return value


def _object_json(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise _MalformedPersistedData(f"Persisted {field} is not JSON text")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise _MalformedPersistedData(f"Persisted {field} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise _MalformedPersistedData(f"Persisted {field} is not a JSON object")
    return value


def _optional_text(row: sqlite3.Row, field: str) -> str | None:
    value = row[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _MalformedPersistedData(f"Persisted {field} is invalid")
    return value


def _load_versions(
    database_path: Path,
    source_type: SourceType,
    *,
    is_current: bool,
    test_only: bool,
) -> tuple[_VersionEvidence, ...]:
    with database.managed_connection(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            _SELECT_EVIDENCE,
            (source_type.value, int(is_current), int(test_only)),
        ).fetchall()

    grouped: dict[str, tuple[dict[str, Any], list[EvidenceItem]]] = {}
    for row in rows:
        version_id = _required_text(row, "document_version_id")
        document_data = _object_json(row["document_structured_data"], "document structured_data")
        evidence_data = _object_json(row["evidence_structured_data"], "evidence structured_data")
        persisted_source_type = _required_text(row, "source_type")
        if persisted_source_type != source_type.value:
            raise _MalformedPersistedData("Evidence source type does not match its document")
        if (
            _required_text(row, "source_id")
            != _required_text(row, "document_source_id")
            or _required_text(row, "document_source_type") != source_type.value
        ):
            raise _MalformedPersistedData("Evidence provenance does not match its document")

        evidence = EvidenceItem(
            evidence_id=_required_text(row, "evidence_id"),
            source_id=_required_text(row, "source_id"),
            source_type=source_type,
            source_title=_required_text(row, "source_title"),
            document_id=_required_text(row, "document_id"),
            document_version_id=version_id,
            version=_required_text(row, "version"),
            timestamp=_required_text(row, "timestamp"),
            section=_required_text(row, "section"),
            relevant_excerpt=_required_text(row, "relevant_excerpt"),
            structured_data=evidence_data,
            document_recorded_at=_required_text(row, "recorded_at"),
            document_effective_from=_required_text(row, "effective_from"),
            document_effective_to=_optional_text(row, "effective_to"),
        )
        existing = grouped.get(version_id)
        if existing is None:
            grouped[version_id] = (document_data, [evidence])
        else:
            if existing[0] != document_data:
                raise _MalformedPersistedData("Document structured_data is inconsistent")
            existing[1].append(evidence)

    return tuple(
        _VersionEvidence(document_data, tuple(evidence_items))
        for document_data, evidence_items in grouped.values()
    )


def _matches(data: dict[str, Any], expected: dict[str, str]) -> bool:
    return all(data.get(key) == value for key, value in expected.items())


def _failure(
    source_type: SourceType,
    status: RetrievalStatus,
    reason: str,
) -> RetrievalResult:
    return RetrievalResult(source_type=source_type, status=status, failure_reason=reason)


class _SQLiteAdapter:
    source_type: SourceType

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _retrieve_matching(
        self,
        expected: dict[str, str],
        *,
        is_current: bool = True,
        test_only: bool = False,
    ) -> RetrievalResult:
        try:
            versions = _load_versions(
                self.database_path,
                self.source_type,
                is_current=is_current,
                test_only=test_only,
            )
        except _MalformedPersistedData as exc:
            return _failure(
                self.source_type,
                RetrievalStatus.MALFORMED,
                f"Persisted {self.source_type.value} data could not be normalized: {exc}",
            )

        matches = tuple(version for version in versions if _matches(version.document_data, expected))
        if not matches:
            return _failure(
                self.source_type,
                RetrievalStatus.NOT_FOUND,
                f"No applicable persisted {self.source_type.value} evidence was found",
            )
        evidence = tuple(item for version in matches for item in version.evidence_items)
        version_ids = tuple(version.document_version_id for version in matches)
        return RetrievalResult(
            source_type=self.source_type,
            status=RetrievalStatus.RETRIEVED,
            evidence_items=evidence,
            document_version_ids=version_ids,
        )


class EHRAdapter(_SQLiteAdapter):
    source_type = SourceType.EHR

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return self._retrieve_matching(
            {
                "case_id": request.case_id,
                "requested_medication_id": request.medication_id,
                "condition_id": request.indication_id,
                "payer_id": request.payer_id,
                "plan_id": request.plan_id,
            }
        )


class GuidelineAdapter(_SQLiteAdapter):
    source_type = SourceType.GUIDELINE

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return self._retrieve_matching(
            {
                "medication_id": request.medication_id,
                "condition_id": request.indication_id,
            }
        )


class PayerPolicyAdapter(_SQLiteAdapter):
    source_type = SourceType.PAYER_POLICY

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        if request.source_mode == PAYER_UNAVAILABLE_MODE:
            return _failure(
                self.source_type,
                RetrievalStatus.SOURCE_UNAVAILABLE,
                "The applicable payer policy source could not be verified",
            )
        return self._retrieve_matching(
            {
                "payer_id": request.payer_id,
                "plan_id": request.plan_id,
                "medication_id": request.medication_id,
                "condition_id": request.indication_id,
            }
        )


class FormularyAdapter(_SQLiteAdapter):
    source_type = SourceType.FORMULARY

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        conflict_mode = request.source_mode == FORMULARY_CONFLICT_MODE
        return self._retrieve_matching(
            {
                "payer_id": request.payer_id,
                "plan_id": request.plan_id,
                "medication_id": request.medication_id,
                "condition_id": request.indication_id,
            },
            is_current=not conflict_mode,
            test_only=conflict_mode,
        )


class SpecialistNotesAdapter(_SQLiteAdapter):
    source_type = SourceType.SPECIALIST_NOTE

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return self._retrieve_matching(
            {
                "case_id": request.case_id,
                "requested_medication_id": request.medication_id,
                "condition_id": request.indication_id,
            }
        )
