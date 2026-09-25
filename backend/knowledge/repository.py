"""SQLite-backed access to persisted knowledge assertions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend import database
from backend.knowledge.models import (
    AssertionLineageEdge,
    AssertionProvenance,
    KnowledgeAssertion,
    KnowledgeAssertionState,
    KnowledgeQuery,
    freeze_json,
)
from backend.retrieval.models import EvidenceItem, SourceType


class KnowledgeDataError(RuntimeError):
    """Raised when persisted knowledge cannot be represented truthfully."""


class KnowledgeStateTransitionError(ValueError):
    """Raised when an assertion state change violates the governed workflow."""


_ASSERTION_COLUMNS = """
    ka.assertion_id, ka.document_version_id, ka.subject_id, ka.predicate,
    ka.object_id, ka.value_json, ka.decision_dimension, ka.normalized_scope_json,
    ka.recorded_at, ka.effective_from, ka.effective_to, ka.state
"""


def _required_text(row: sqlite3.Row, field: str) -> str:
    return _required_value(row[field], field)


def _required_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise KnowledgeDataError(f"Persisted {field} is missing or invalid")
    return value


def _optional_text(row: sqlite3.Row, field: str) -> str | None:
    value = row[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise KnowledgeDataError(f"Persisted {field} is invalid")
    return value


def _decode_json(raw: Any, field: str, *, allow_sql_null: bool = False) -> Any:
    if raw is None and allow_sql_null:
        return None
    if not isinstance(raw, str):
        raise KnowledgeDataError(f"Persisted {field} is not JSON text")
    try:
        return json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {token}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise KnowledgeDataError(f"Persisted {field} is invalid JSON") from error


def _source_type(value: str) -> SourceType:
    try:
        return SourceType(value)
    except ValueError as error:
        raise KnowledgeDataError(f"Persisted source_type is invalid: {value}") from error


def _parse_persisted_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise KnowledgeDataError(f"Persisted {field} is missing or invalid")
    try:
        parsed = datetime.fromisoformat(
            f"{value[:-1]}+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise KnowledgeDataError(
            f"Persisted {field} is not a valid ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise KnowledgeDataError(
            f"Persisted {field} must include an explicit UTC offset"
        )
    return parsed


def _parse_persisted_interval(
    effective_from: Any,
    effective_to: Any,
    field_prefix: str,
) -> tuple[datetime, datetime | None]:
    start = _parse_persisted_utc_timestamp(
        effective_from, f"{field_prefix}.effective_from"
    )
    end = (
        _parse_persisted_utc_timestamp(effective_to, f"{field_prefix}.effective_to")
        if effective_to is not None
        else None
    )
    if end is not None and end < start:
        raise KnowledgeDataError(f"Persisted {field_prefix} interval is inverted")
    return start, end


class KnowledgeRepository:
    """Read and govern schema-v3 knowledge without changing runtime authority.

    Write methods require an injected connection. That connection and its
    transaction remain caller-owned: this repository never commits, rolls back,
    or replaces the transaction used by the approval workflow.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._database_path = database_path
        self._connection = connection

    def apply_assertion_state_transition(
        self,
        assertion_id: str,
        new_state: KnowledgeAssertionState,
    ) -> None:
        """Apply one allowed transition in the caller-owned transaction."""

        connection = self._write_connection()
        row = connection.execute(
            "SELECT state FROM knowledge_assertion WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown knowledge assertion: {assertion_id}")
        try:
            current_state = KnowledgeAssertionState(row[0])
        except ValueError as error:
            raise KnowledgeDataError(
                f"Persisted assertion state is invalid for {assertion_id}"
            ) from error
        allowed_transitions = {
            KnowledgeAssertionState.CANDIDATE: KnowledgeAssertionState.APPLIED,
            KnowledgeAssertionState.APPLIED: KnowledgeAssertionState.SUPERSEDED,
        }
        if allowed_transitions.get(current_state) is not new_state:
            raise KnowledgeStateTransitionError(
                f"Invalid knowledge assertion state transition: "
                f"{current_state.value} -> {new_state.value}"
            )
        connection.execute(
            "UPDATE knowledge_assertion SET state = ? WHERE assertion_id = ?",
            (new_state.value, assertion_id),
        )

    def create_assertion_lineage(
        self,
        predecessor_assertion_id: str,
        successor_assertion_id: str,
        feedback_id: str,
        created_at: str,
    ) -> None:
        """Create one semantically valid assertion edge in the caller-owned transaction."""

        connection = self._write_connection()
        feedback = connection.execute(
            """SELECT status, target_version_id, proposed_version_id
               FROM feedback WHERE feedback_id = ?""",
            (feedback_id,),
        ).fetchone()
        if feedback is None:
            raise KnowledgeDataError(f"Unknown feedback for assertion lineage: {feedback_id}")
        if _required_value(feedback[0], "feedback status") != "APPLIED":
            raise KnowledgeDataError("Assertion lineage requires APPLIED feedback")

        assertions: list[tuple[Any, ...]] = []
        for role, assertion_id in (
            ("predecessor", predecessor_assertion_id),
            ("successor", successor_assertion_id),
        ):
            assertion = connection.execute(
                """SELECT document_version_id, predicate,
                          decision_dimension, normalized_scope_json
                   FROM knowledge_assertion WHERE assertion_id = ?""",
                (assertion_id,),
            ).fetchone()
            if assertion is None:
                raise KnowledgeDataError(
                    f"Unknown {role} assertion for lineage: {assertion_id}"
                )
            assertions.append(assertion)

        predecessor, successor = assertions
        target_version_id = _required_value(feedback[1], "feedback target_version_id")
        proposed_version_id = _required_value(feedback[2], "feedback proposed_version_id")
        if _required_value(predecessor[0], "predecessor document_version_id") != target_version_id:
            raise KnowledgeDataError(
                "Lineage predecessor does not belong to the feedback target version"
            )
        if _required_value(successor[0], "successor document_version_id") != proposed_version_id:
            raise KnowledgeDataError(
                "Lineage successor does not belong to the feedback proposed version"
            )

        version_documents = connection.execute(
            """SELECT document_version_id, document_id
               FROM source_document_version
               WHERE document_version_id IN (?, ?)""",
            (target_version_id, proposed_version_id),
        ).fetchall()
        document_by_version = {
            _required_value(row[0], "document_version_id"): _required_value(
                row[1], "document_id"
            )
            for row in version_documents
        }
        if target_version_id not in document_by_version:
            raise KnowledgeDataError("Feedback target document version does not exist")
        if proposed_version_id not in document_by_version:
            raise KnowledgeDataError("Feedback proposed document version does not exist")
        if document_by_version[target_version_id] != document_by_version[proposed_version_id]:
            raise KnowledgeDataError(
                "Feedback target and proposed versions belong to different documents"
            )

        for index, field in ((1, "predicate"), (2, "decision_dimension")):
            if _required_value(predecessor[index], field) != _required_value(
                successor[index], field
            ):
                raise KnowledgeDataError(
                    f"Lineage assertions have incompatible {field}"
                )

        scopes: list[dict[str, Any]] = []
        for role, assertion in (("predecessor", predecessor), ("successor", successor)):
            raw_scope = assertion[3]
            if raw_scope is None:
                raise KnowledgeDataError(f"Lineage {role} normalized scope is missing")
            scope = _decode_json(raw_scope, f"{role} normalized_scope_json")
            if not isinstance(scope, dict):
                raise KnowledgeDataError(
                    f"Lineage {role} normalized_scope_json is not a JSON object"
                )
            scopes.append(scope)
        if scopes[0] != scopes[1]:
            raise KnowledgeDataError("Lineage assertions have incompatible normalized_scope")

        connection.execute(
            """INSERT INTO assertion_lineage
               (lineage_id, predecessor_assertion_id, successor_assertion_id,
                feedback_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                f"LIN-{uuid.uuid4().hex[:12].upper()}",
                predecessor_assertion_id,
                successor_assertion_id,
                feedback_id,
                created_at,
            ),
        )

    def _write_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError(
                "Knowledge writes require a caller-provided managed connection"
            )
        return self._connection

    def get_assertion(self, assertion_id: str) -> KnowledgeAssertion | None:
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"SELECT {_ASSERTION_COLUMNS} FROM knowledge_assertion AS ka "
                "WHERE ka.assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            return None if row is None else self._assertion_from_row(connection, row)

    def find_assertions(self, query: KnowledgeQuery) -> tuple[KnowledgeAssertion, ...]:
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            return self._find_assertions(connection, query)

    def current_applied_assertions(
        self, query: KnowledgeQuery = KnowledgeQuery()
    ) -> tuple[KnowledgeAssertion, ...]:
        """Return APPLIED assertions whose persisted document version is current.

        This deliberately reflects the v1.3 current-version flag; it is not an
        as-of or generalized temporal-authority query.
        """

        if query.state not in (None, KnowledgeAssertionState.APPLIED):
            return ()
        applied_query = replace(query, state=KnowledgeAssertionState.APPLIED)
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            return self._find_assertions(connection, applied_query, current_only=True)

    def get_applicable_assertions(
        self, query: KnowledgeQuery
    ) -> tuple[KnowledgeAssertion, ...]:
        """Return every governed assertion applicable at ``query.as_of``.

        This operation is intentionally distinct from current selection and
        never consults or falls back to the persisted current-version marker.
        """

        if query.as_of is None:
            raise ValueError("Applicable assertion queries require as_of")
        if query.state is KnowledgeAssertionState.CANDIDATE:
            return ()

        requested_at = datetime.fromisoformat(
            f"{query.as_of[:-1]}+00:00" if query.as_of.endswith("Z") else query.as_of
        )
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = self._applicable_assertion_rows(connection, query)
            applicable: list[KnowledgeAssertion] = []
            for row in rows:
                assertion = self._assertion_from_row(connection, row)
                if not self._scope_matches(assertion, query):
                    continue
                assertion_start, assertion_end = _parse_persisted_interval(
                    row["effective_from"],
                    row["effective_to"],
                    "assertion",
                )
                document_start, document_end = _parse_persisted_interval(
                    row["document_effective_from"],
                    row["document_effective_to"],
                    "source_document_version",
                )
                if assertion_start < document_start or (
                    document_end is not None
                    and (assertion_end is None or assertion_end > document_end)
                ):
                    raise KnowledgeDataError(
                        "Persisted assertion interval is outside its source document interval"
                    )
                if assertion_start <= requested_at and (
                    assertion_end is None or requested_at <= assertion_end
                ) and document_start <= requested_at and (
                    document_end is None or requested_at <= document_end
                ):
                    applicable.append(assertion)
            return tuple(applicable)

    def get_assertion_provenance(self, assertion_id: str) -> AssertionProvenance | None:
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            assertion_row = connection.execute(
                f"SELECT {_ASSERTION_COLUMNS} FROM knowledge_assertion AS ka "
                "WHERE ka.assertion_id = ?",
                (assertion_id,),
            ).fetchone()
            if assertion_row is None:
                return None
            assertion = self._assertion_from_row(connection, assertion_row)
            rows = connection.execute(
                """SELECT
                       e.evidence_id, e.document_version_id AS evidence_document_version_id,
                       e.source_id AS evidence_source_id,
                       e.source_type AS evidence_source_type,
                       e.source_title AS evidence_source_title,
                       e.version AS evidence_version, e.timestamp AS evidence_timestamp,
                       e.section, e.relevant_excerpt,
                       e.structured_data AS evidence_structured_data,
                       dv.document_version_id, dv.document_id, dv.version AS document_version,
                       dv.timestamp AS document_timestamp, dv.recorded_at,
                       dv.effective_from, dv.effective_to,
                       d.source_id, d.source_type, d.source_title
                   FROM assertion_evidence AS ae
                   JOIN evidence_item AS e ON e.evidence_id = ae.evidence_id
                   JOIN source_document_version AS dv
                     ON dv.document_version_id = e.document_version_id
                   JOIN source_document AS d ON d.document_id = dv.document_id
                   WHERE ae.assertion_id = ?
                   ORDER BY e.evidence_id""",
                (assertion_id,),
            ).fetchall()
            if not rows:
                raise KnowledgeDataError(f"Assertion {assertion_id} has no supporting evidence")

            evidence_items: list[EvidenceItem] = []
            first = rows[0]
            for row in rows:
                self._validate_provenance_row(assertion, first, row)
                structured_data = _decode_json(
                    row["evidence_structured_data"], "evidence structured_data"
                )
                if not isinstance(structured_data, dict):
                    raise KnowledgeDataError(
                        "Persisted evidence structured_data is not a JSON object"
                    )
                evidence_items.append(
                    EvidenceItem(
                        evidence_id=_required_text(row, "evidence_id"),
                        source_id=_required_text(row, "source_id"),
                        source_type=_source_type(_required_text(row, "source_type")),
                        source_title=_required_text(row, "source_title"),
                        document_id=_required_text(row, "document_id"),
                        document_version_id=_required_text(row, "document_version_id"),
                        version=_required_text(row, "document_version"),
                        timestamp=_required_text(row, "document_timestamp"),
                        section=_required_text(row, "section"),
                        relevant_excerpt=_required_text(row, "relevant_excerpt"),
                        structured_data=structured_data,
                        document_recorded_at=_required_text(row, "recorded_at"),
                        document_effective_from=_required_text(row, "effective_from"),
                        document_effective_to=_optional_text(row, "effective_to"),
                    )
                )

            return AssertionProvenance(
                assertion=assertion,
                evidence_items=tuple(evidence_items),
                document_id=_required_text(first, "document_id"),
                document_version_id=_required_text(first, "document_version_id"),
                document_version=_required_text(first, "document_version"),
                source_id=_required_text(first, "source_id"),
                source_type=_source_type(_required_text(first, "source_type")),
                source_title=_required_text(first, "source_title"),
                document_timestamp=_required_text(first, "document_timestamp"),
                document_recorded_at=_required_text(first, "recorded_at"),
                document_effective_from=_required_text(first, "effective_from"),
                document_effective_to=_optional_text(first, "effective_to"),
            )

    def get_predecessors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._lineage(
            "successor_assertion_id", assertion_id, "predecessor_assertion_id"
        )

    def get_successors(self, assertion_id: str) -> tuple[AssertionLineageEdge, ...]:
        return self._lineage(
            "predecessor_assertion_id", assertion_id, "successor_assertion_id"
        )

    def _find_assertions(
        self,
        connection: sqlite3.Connection,
        query: KnowledgeQuery,
        *,
        current_only: bool = False,
    ) -> tuple[KnowledgeAssertion, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("ka.subject_id", query.subject_id),
            ("ka.predicate", query.predicate),
            ("ka.decision_dimension", query.decision_dimension),
            ("ka.state", query.state.value if query.state is not None else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        join = ""
        if current_only:
            join = (
                " JOIN source_document_version AS dv"
                " ON dv.document_version_id = ka.document_version_id"
            )
            clauses.append("dv.is_current = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT {_ASSERTION_COLUMNS} FROM knowledge_assertion AS ka"
            f"{join}{where} ORDER BY ka.assertion_id",
            tuple(parameters),
        ).fetchall()
        assertions = tuple(self._assertion_from_row(connection, row) for row in rows)
        return tuple(assertion for assertion in assertions if self._scope_matches(assertion, query))

    def _applicable_assertion_rows(
        self,
        connection: sqlite3.Connection,
        query: KnowledgeQuery,
    ) -> tuple[sqlite3.Row, ...]:
        clauses = [
            "dv.governance_state = 'APPLIED'",
            "ka.state IN ('APPLIED', 'SUPERSEDED')",
        ]
        parameters: list[str] = []
        for column, value in (
            ("ka.subject_id", query.subject_id),
            ("ka.predicate", query.predicate),
            ("ka.decision_dimension", query.decision_dimension),
            ("ka.state", query.state.value if query.state is not None else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        rows = connection.execute(
            f"""SELECT {_ASSERTION_COLUMNS},
                       dv.effective_from AS document_effective_from,
                       dv.effective_to AS document_effective_to
                FROM knowledge_assertion AS ka
                JOIN source_document_version AS dv
                  ON dv.document_version_id = ka.document_version_id
                WHERE {' AND '.join(clauses)}
                ORDER BY ka.assertion_id""",
            tuple(parameters),
        ).fetchall()
        return tuple(rows)

    def _assertion_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> KnowledgeAssertion:
        assertion_id = _required_text(row, "assertion_id")
        evidence_rows = connection.execute(
            """SELECT evidence_id FROM assertion_evidence
               WHERE assertion_id = ? ORDER BY evidence_id""",
            (assertion_id,),
        ).fetchall()
        evidence_ids = tuple(item[0] for item in evidence_rows)
        if not evidence_ids or any(not isinstance(item, str) or not item for item in evidence_ids):
            raise KnowledgeDataError(f"Assertion {assertion_id} has missing or invalid evidence")

        value = _decode_json(row["value_json"], "value_json", allow_sql_null=True)
        scope_raw = row["normalized_scope_json"]
        scope = None
        if scope_raw is not None:
            decoded_scope = _decode_json(scope_raw, "normalized_scope_json")
            if not isinstance(decoded_scope, dict):
                raise KnowledgeDataError(
                    "Persisted normalized_scope_json is not a JSON object"
                )
            frozen_scope = freeze_json(decoded_scope)
            if not isinstance(frozen_scope, Mapping):
                raise KnowledgeDataError("Persisted normalized scope could not be represented")
            scope = frozen_scope
        try:
            state = KnowledgeAssertionState(_required_text(row, "state"))
        except ValueError as error:
            raise KnowledgeDataError("Persisted assertion state is invalid") from error
        return KnowledgeAssertion(
            assertion_id=assertion_id,
            document_version_id=_required_text(row, "document_version_id"),
            subject_id=_required_text(row, "subject_id"),
            predicate=_required_text(row, "predicate"),
            object_id=_optional_text(row, "object_id"),
            value=value,
            decision_dimension=_required_text(row, "decision_dimension"),
            normalized_scope=scope,
            recorded_at=_required_text(row, "recorded_at"),
            effective_from=_required_text(row, "effective_from"),
            effective_to=_optional_text(row, "effective_to"),
            state=state,
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _scope_matches(assertion: KnowledgeAssertion, query: KnowledgeQuery) -> bool:
        requested = {
            "payer_id": query.payer_id,
            "plan_id": query.plan_id,
            "medication_id": query.medication_id,
            "condition_id": query.indication_id,
        }
        requested = {key: value for key, value in requested.items() if value is not None}
        if not requested:
            return True
        if assertion.normalized_scope is None:
            return False
        return all(assertion.normalized_scope.get(key) == value for key, value in requested.items())

    @staticmethod
    def _validate_provenance_row(
        assertion: KnowledgeAssertion, first: sqlite3.Row, row: sqlite3.Row
    ) -> None:
        if _required_text(row, "document_version_id") != assertion.document_version_id:
            raise KnowledgeDataError(
                f"Evidence for assertion {assertion.assertion_id} belongs to another document version"
            )
        stable_fields = (
            "document_version_id",
            "document_id",
            "document_version",
            "document_timestamp",
            "recorded_at",
            "effective_from",
            "effective_to",
            "source_id",
            "source_type",
            "source_title",
        )
        if any(row[field] != first[field] for field in stable_fields):
            raise KnowledgeDataError("Assertion evidence resolves to inconsistent document provenance")
        comparisons = (
            ("evidence_document_version_id", "document_version_id"),
            ("evidence_source_id", "source_id"),
            ("evidence_source_type", "source_type"),
            ("evidence_source_title", "source_title"),
            ("evidence_version", "document_version"),
            ("evidence_timestamp", "document_timestamp"),
        )
        if any(row[evidence_field] != row[document_field] for evidence_field, document_field in comparisons):
            raise KnowledgeDataError("Evidence metadata does not match its source document version")

    def _lineage(
        self, endpoint_column: str, assertion_id: str, order_column: str
    ) -> tuple[AssertionLineageEdge, ...]:
        with database.managed_connection(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""SELECT lineage_id, predecessor_assertion_id,
                           successor_assertion_id, feedback_id, created_at
                    FROM assertion_lineage WHERE {endpoint_column} = ?
                    ORDER BY {order_column}, lineage_id""",
                (assertion_id,),
            ).fetchall()
        return tuple(
            AssertionLineageEdge(
                lineage_id=_required_text(row, "lineage_id"),
                predecessor_assertion_id=_required_text(row, "predecessor_assertion_id"),
                successor_assertion_id=_required_text(row, "successor_assertion_id"),
                feedback_id=_required_text(row, "feedback_id"),
                created_at=_required_text(row, "created_at"),
            )
            for row in rows
        )
