"""SQLite-backed access to persisted knowledge assertions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import replace
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
    value = row[field]
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


class KnowledgeRepository:
    """Read and govern schema-v2 knowledge without changing runtime authority.

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
        """Create one assertion edge in the caller-owned transaction.

        SQLite foreign keys and the schema's self-edge and uniqueness constraints
        remain authoritative for endpoint, feedback, and duplicate validation.
        """

        connection = self._write_connection()
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
