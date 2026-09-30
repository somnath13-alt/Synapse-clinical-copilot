"""SQLite persistence for immutable governed correction proposals.

This repository stores M7.2 proposal contracts and their proposal-time
snapshots. It does not decide whether a proposal may be submitted, applied, or
rejected, and it never changes live knowledge authority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend import database
from backend.knowledge.governance_models import (
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    ProposalStatus,
    ReplacementItem,
)


class GovernancePersistenceError(ValueError):
    """Raised when a proposal cannot be represented truthfully in storage."""


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _json(value: object) -> str:
    return json.dumps(
        _mutable_json(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_json(value: object, field: str) -> Any:
    if not isinstance(value, str):
        raise GovernancePersistenceError(f"Persisted {field} is not JSON text")
    try:
        return json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {token}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise GovernancePersistenceError(
            f"Persisted {field} is invalid JSON"
        ) from error


class GovernanceProposalRepository:
    """Persist and load correction proposals without governance transitions.

    Writes require a caller-owned connection and never commit independently.
    The proposal-time snapshot is validated against referenced rows once, at
    insertion. Reads reconstruct the proposal exclusively from the snapshot so
    later mutable assertion fields cannot rewrite historical proposal meaning.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._database_path = database_path
        self._connection = connection

    def create_proposal(
        self,
        proposal: CorrectionProposal,
        *,
        decision: GovernanceDecision | None = None,
    ) -> None:
        """Insert one complete proposal aggregate in the caller transaction."""

        connection = self._write_connection()
        self._validate_decision(proposal, decision)
        logical_document_id = self._logical_document_id(connection, proposal)
        for item in proposal.replacement_items:
            self._validate_item(connection, proposal, item, logical_document_id)

        connection.execute(
            """INSERT INTO correction_proposal
               (proposal_id, feedback_id, logical_document_id,
                target_document_version_id, proposed_document_version_id,
                submitter_actor, submitter_role, rationale, status, created_at,
                decided_at, decision_actor, decision_role, decision_rationale,
                first_replacement_ordinal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                proposal.proposal_id,
                proposal.feedback_id,
                logical_document_id,
                proposal.target_document_version_id,
                proposal.proposed_document_version_id,
                proposal.submitter.actor_id,
                proposal.submitter.role,
                proposal.rationale,
                proposal.status.value,
                proposal.created_at,
                proposal.decided_at,
                decision.actor.actor_id if decision is not None else None,
                decision.actor.role if decision is not None else None,
                decision.rationale if decision is not None else None,
            ),
        )
        for ordinal, item in enumerate(proposal.replacement_items):
            connection.execute(
                """INSERT INTO correction_proposal_replacement_item
                   (proposal_id, ordinal, predecessor_assertion_id,
                    successor_assertion_id, predicate, decision_dimension,
                    normalized_scope_json, predecessor_value_json,
                    successor_value_json, predecessor_effective_from,
                    predecessor_effective_to, successor_effective_from,
                    successor_effective_to, predecessor_document_version_id,
                    successor_document_version_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal.proposal_id,
                    ordinal,
                    item.predecessor_assertion_id,
                    item.successor_assertion_id,
                    item.predicate,
                    item.decision_dimension,
                    _json(item.normalized_scope)
                    if item.normalized_scope is not None
                    else None,
                    _json(item.predecessor_value),
                    _json(item.successor_value),
                    item.predecessor_effective_from,
                    item.predecessor_effective_to,
                    item.successor_effective_from,
                    item.successor_effective_to,
                    item.predecessor_document_version_id,
                    item.successor_document_version_id,
                ),
            )
            connection.executemany(
                """INSERT INTO correction_proposal_predecessor_evidence
                   (proposal_id, replacement_ordinal, predecessor_assertion_id,
                    evidence_ordinal, evidence_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        proposal.proposal_id,
                        ordinal,
                        item.predecessor_assertion_id,
                        evidence_ordinal,
                        evidence_id,
                    )
                    for evidence_ordinal, evidence_id in enumerate(
                        item.predecessor_evidence_ids
                    )
                ),
            )
            connection.executemany(
                """INSERT INTO correction_proposal_successor_evidence
                   (proposal_id, replacement_ordinal, successor_assertion_id,
                    evidence_ordinal, evidence_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        proposal.proposal_id,
                        ordinal,
                        item.successor_assertion_id,
                        evidence_ordinal,
                        evidence_id,
                    )
                    for evidence_ordinal, evidence_id in enumerate(
                        item.successor_evidence_ids
                    )
                ),
            )

    def get_proposal(self, proposal_id: str) -> CorrectionProposal | None:
        if self._connection is not None:
            return self._get_proposal(self._connection, proposal_id)
        with database.managed_connection(self._database_path) as connection:
            return self._get_proposal(connection, proposal_id)

    def get_proposal_for_feedback(
        self, feedback_id: str
    ) -> CorrectionProposal | None:
        """Return the one proposal linked to feedback in the active transaction."""

        connection = self._connection
        if connection is None:
            with database.managed_connection(self._database_path) as managed:
                row = managed.execute(
                    "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?",
                    (feedback_id,),
                ).fetchone()
                return self._get_proposal(managed, row[0]) if row is not None else None
        row = connection.execute(
            "SELECT proposal_id FROM correction_proposal WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        return self._get_proposal(connection, row[0]) if row is not None else None

    def record_decision(
        self,
        proposal_id: str,
        decision: GovernanceDecision,
    ) -> None:
        """Persist one terminal proposal decision in the caller transaction."""

        connection = self._write_connection()
        cursor = connection.execute(
            """UPDATE correction_proposal
               SET status = ?, decided_at = ?, decision_actor = ?,
                   decision_role = ?, decision_rationale = ?
               WHERE proposal_id = ? AND status = 'PENDING'""",
            (
                decision.decision.value,
                decision.timestamp,
                decision.actor.actor_id,
                decision.actor.role,
                decision.rationale,
                proposal_id,
            ),
        )
        if cursor.rowcount != 1:
            raise GovernancePersistenceError(
                "Governance decisions require a persisted PENDING proposal"
            )

    def _get_proposal(
        self,
        connection: sqlite3.Connection,
        proposal_id: str,
    ) -> CorrectionProposal | None:
        previous_factory = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM correction_proposal WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return None
            items = self._replacement_items(connection, proposal_id, row)
            return CorrectionProposal(
                proposal_id=row["proposal_id"],
                feedback_id=row["feedback_id"],
                target_document_version_id=row["target_document_version_id"],
                proposed_document_version_id=row["proposed_document_version_id"],
                submitter=Actor(row["submitter_actor"], row["submitter_role"]),
                rationale=row["rationale"],
                status=ProposalStatus(row["status"]),
                created_at=row["created_at"],
                decided_at=row["decided_at"],
                replacement_items=items,
            )
        finally:
            connection.row_factory = previous_factory

    def get_decision(self, proposal_id: str) -> GovernanceDecision | None:
        if self._connection is not None:
            row = self._connection.execute(
                """SELECT status, decision_actor, decision_role,
                          decision_rationale, decided_at
                   FROM correction_proposal WHERE proposal_id = ?""",
                (proposal_id,),
            ).fetchone()
        else:
            with database.managed_connection(self._database_path) as connection:
                row = connection.execute(
                    """SELECT status, decision_actor, decision_role,
                              decision_rationale, decided_at
                       FROM correction_proposal WHERE proposal_id = ?""",
                    (proposal_id,),
                ).fetchone()
        if row is None or row[0] == ProposalStatus.PENDING.value:
            return None
        return GovernanceDecision(
            proposal_id=proposal_id,
            decision=ProposalStatus(row[0]),
            actor=Actor(row[1], row[2]),
            rationale=row[3],
            timestamp=row[4],
        )

    def _replacement_items(
        self,
        connection: sqlite3.Connection,
        proposal_id: str,
        proposal_row: sqlite3.Row,
    ) -> tuple[ReplacementItem, ...]:
        rows = connection.execute(
            """SELECT * FROM correction_proposal_replacement_item
               WHERE proposal_id = ? ORDER BY ordinal""",
            (proposal_id,),
        ).fetchall()
        if not rows:
            raise GovernancePersistenceError(
                f"Persisted proposal {proposal_id} has no replacement items"
            )
        items: list[ReplacementItem] = []
        for row in rows:
            predecessor_evidence = self._evidence_ids(
                connection,
                "correction_proposal_predecessor_evidence",
                proposal_id,
                row["ordinal"],
            )
            successor_evidence = self._evidence_ids(
                connection,
                "correction_proposal_successor_evidence",
                proposal_id,
                row["ordinal"],
            )
            scope = (
                _decode_json(row["normalized_scope_json"], "normalized_scope_json")
                if row["normalized_scope_json"] is not None
                else None
            )
            items.append(
                ReplacementItem(
                    predecessor_assertion_id=row["predecessor_assertion_id"],
                    successor_assertion_id=row["successor_assertion_id"],
                    predicate=row["predicate"],
                    decision_dimension=row["decision_dimension"],
                    normalized_scope=scope,
                    predecessor_value=_decode_json(
                        row["predecessor_value_json"], "predecessor_value_json"
                    ),
                    successor_value=_decode_json(
                        row["successor_value_json"], "successor_value_json"
                    ),
                    predecessor_effective_from=row["predecessor_effective_from"],
                    predecessor_effective_to=row["predecessor_effective_to"],
                    successor_effective_from=row["successor_effective_from"],
                    successor_effective_to=row["successor_effective_to"],
                    predecessor_document_id=proposal_row["logical_document_id"],
                    predecessor_document_version_id=row[
                        "predecessor_document_version_id"
                    ],
                    predecessor_evidence_ids=predecessor_evidence,
                    successor_document_id=proposal_row["logical_document_id"],
                    successor_document_version_id=row[
                        "successor_document_version_id"
                    ],
                    successor_evidence_ids=successor_evidence,
                )
            )
        return tuple(items)

    @staticmethod
    def _evidence_ids(
        connection: sqlite3.Connection,
        table: str,
        proposal_id: str,
        replacement_ordinal: int,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            f"""SELECT evidence_id FROM {table}
                 WHERE proposal_id = ? AND replacement_ordinal = ?
                 ORDER BY evidence_ordinal""",
            (proposal_id, replacement_ordinal),
        ).fetchall()
        if not rows:
            raise GovernancePersistenceError(
                "Persisted replacement item is missing proposal-time evidence"
            )
        return tuple(row[0] for row in rows)

    @staticmethod
    def _validate_decision(
        proposal: CorrectionProposal,
        decision: GovernanceDecision | None,
    ) -> None:
        if proposal.status is ProposalStatus.PENDING:
            if decision is not None:
                raise GovernancePersistenceError(
                    "PENDING proposals must not include a decision"
                )
            return
        if decision is None:
            raise GovernancePersistenceError(
                "Terminal proposals require decision facts"
            )
        if decision.proposal_id != proposal.proposal_id:
            raise GovernancePersistenceError(
                "Decision proposal_id must match the proposal"
            )
        if decision.decision is not proposal.status:
            raise GovernancePersistenceError(
                "Decision status must match the proposal status"
            )
        if decision.timestamp != proposal.decided_at:
            raise GovernancePersistenceError(
                "Decision timestamp must match proposal decided_at"
            )

    @staticmethod
    def _logical_document_id(
        connection: sqlite3.Connection,
        proposal: CorrectionProposal,
    ) -> str:
        rows = connection.execute(
            """SELECT document_version_id, document_id
               FROM source_document_version
               WHERE document_version_id IN (?, ?)""",
            (
                proposal.target_document_version_id,
                proposal.proposed_document_version_id,
            ),
        ).fetchall()
        documents = {row[0]: row[1] for row in rows}
        for field, version_id in (
            ("target", proposal.target_document_version_id),
            ("proposed", proposal.proposed_document_version_id),
        ):
            if version_id not in documents:
                raise GovernancePersistenceError(
                    f"Proposal {field} document version does not exist"
                )
        if documents[proposal.target_document_version_id] != documents[
            proposal.proposed_document_version_id
        ]:
            raise GovernancePersistenceError(
                "Proposal document versions must belong to the same logical document"
            )
        return documents[proposal.target_document_version_id]

    def _validate_item(
        self,
        connection: sqlite3.Connection,
        proposal: CorrectionProposal,
        item: ReplacementItem,
        logical_document_id: str,
    ) -> None:
        expected_identity = (
            logical_document_id,
            proposal.target_document_version_id,
            logical_document_id,
            proposal.proposed_document_version_id,
        )
        actual_identity = (
            item.predecessor_document_id,
            item.predecessor_document_version_id,
            item.successor_document_id,
            item.successor_document_version_id,
        )
        if actual_identity != expected_identity:
            raise GovernancePersistenceError(
                "Replacement item document identities must match the proposal envelope"
            )

        predecessor = self._assertion_snapshot(
            connection, item.predecessor_assertion_id
        )
        successor = self._assertion_snapshot(connection, item.successor_assertion_id)
        expected_predecessor = (
            item.predecessor_document_version_id,
            item.predicate,
            item.decision_dimension,
            _json(item.normalized_scope) if item.normalized_scope is not None else None,
            _json(item.predecessor_value),
            item.predecessor_effective_from,
            item.predecessor_effective_to,
        )
        expected_successor = (
            item.successor_document_version_id,
            item.predicate,
            item.decision_dimension,
            _json(item.normalized_scope) if item.normalized_scope is not None else None,
            _json(item.successor_value),
            item.successor_effective_from,
            item.successor_effective_to,
        )
        if predecessor[:7] != expected_predecessor:
            raise GovernancePersistenceError(
                "Predecessor proposal-time facts do not match the referenced assertion"
            )
        if successor[:7] != expected_successor:
            raise GovernancePersistenceError(
                "Successor proposal-time facts do not match the referenced assertion"
            )
        if set(predecessor[7]) != set(item.predecessor_evidence_ids):
            raise GovernancePersistenceError(
                "Predecessor proposal-time evidence does not match the assertion"
            )
        if set(successor[7]) != set(item.successor_evidence_ids):
            raise GovernancePersistenceError(
                "Successor proposal-time evidence does not match the assertion"
            )

    @staticmethod
    def _assertion_snapshot(
        connection: sqlite3.Connection,
        assertion_id: str,
    ) -> tuple[object, ...]:
        row = connection.execute(
            """SELECT document_version_id, predicate, decision_dimension,
                      normalized_scope_json, value_json, effective_from, effective_to
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (assertion_id,),
        ).fetchone()
        if row is None:
            raise GovernancePersistenceError(
                f"Referenced assertion does not exist: {assertion_id}"
            )
        scope_json = (
            _json(_decode_json(row[3], "normalized_scope_json"))
            if row[3] is not None
            else None
        )
        value_json = _json(_decode_json(row[4], "value_json"))
        evidence_ids = tuple(
            evidence[0]
            for evidence in connection.execute(
                """SELECT evidence_id FROM assertion_evidence
                   WHERE assertion_id = ? ORDER BY evidence_id""",
                (assertion_id,),
            ).fetchall()
        )
        if not evidence_ids:
            raise GovernancePersistenceError(
                f"Referenced assertion has no evidence: {assertion_id}"
            )
        return (row[0], row[1], row[2], scope_json, value_json, row[5], row[6], evidence_ids)

    def _write_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError(
                "Governance proposal writes require a caller-provided managed connection"
            )
        return self._connection
