"""Deterministic policy boundary for governed correction transitions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.knowledge.governance_models import (
    CorrectionProposal,
    GovernanceDecision,
    ProposalStatus,
    ReplacementItem,
    TransitionResult,
)
from backend.knowledge.governance_repository import GovernanceProposalRepository
from backend.knowledge.models import KnowledgeAssertionState
from backend.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeStateTransitionError,
)
from backend.knowledge.service import KnowledgeService


STALE_TARGET = "STALE_TARGET"
FUTURE_EFFECTIVE = "FUTURE_EFFECTIVE"
INVALID_TRANSITION = "INVALID_TRANSITION"
INVALID_REPLACEMENT_SET = "INVALID_REPLACEMENT_SET"
DUPLICATE_LINEAGE = "DUPLICATE_LINEAGE"
APPLIED_BRANCH = "APPLIED_BRANCH"
MERGE_NOT_SUPPORTED = "MERGE_NOT_SUPPORTED"
LINEAGE_CYCLE = "LINEAGE_CYCLE"
FEEDBACK_REQUIRED = "FEEDBACK_REQUIRED"
_MAX_LINEAGE_DEPTH = 64


class GovernancePolicyError(ValueError):
    """Raised when a submitted aggregate violates governance policy."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _StaleTargetDuringMutation(RuntimeError):
    """The expected governed head changed after read-only validation."""


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _json_value(value: object) -> object:
    return json.loads(
        json.dumps(
            _mutable_json(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GovernanceService:
    """Own governance policy while leaving transaction ownership to the caller.

    The injected SQLite connection is used for every mutation. This service
    never opens, commits, or rolls back a transaction.
    """

    def __init__(self, database_path: Path, connection: sqlite3.Connection) -> None:
        self._database_path = database_path
        self._connection = connection
        self._proposals = GovernanceProposalRepository(
            database_path, connection=connection
        )
        self._knowledge = KnowledgeService(
            KnowledgeRepository(database_path, connection=connection)
        )

    @property
    def proposal_repository(self) -> GovernanceProposalRepository:
        return self._proposals

    def submit_correction(self, proposal: CorrectionProposal) -> TransitionResult:
        """Validate and persist a non-authoritative PENDING proposal."""

        if proposal.status is not ProposalStatus.PENDING:
            raise GovernancePolicyError(
                INVALID_TRANSITION, "Submitted corrections must be PENDING"
            )
        self._validate_feedback_envelope(proposal)
        self._validate_replacement_set(proposal, require_current_target=True)
        self._validate_lineage_policy(proposal)
        self._proposals.create_proposal(proposal)
        return self._result(proposal, success=True)

    def validate_transition(
        self,
        proposal_id: str,
        decision: GovernanceDecision,
    ) -> str | None:
        """Return a deterministic failure reason, or ``None`` when valid."""

        proposal = self._proposals.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        if decision.proposal_id != proposal_id:
            return INVALID_TRANSITION
        if proposal.status is not ProposalStatus.PENDING:
            return INVALID_TRANSITION
        if proposal.feedback_id is None:
            return FEEDBACK_REQUIRED
        try:
            self._validate_feedback_envelope(proposal)
        except GovernancePolicyError:
            return INVALID_REPLACEMENT_SET
        if decision.decision is ProposalStatus.REJECTED:
            return None
        if decision.decision is not ProposalStatus.APPLIED:
            return INVALID_TRANSITION
        stale = self._stale_target_reason(proposal)
        if stale is not None:
            return stale
        if self._is_future_effective(proposal, decision.timestamp):
            return FUTURE_EFFECTIVE
        try:
            self._validate_replacement_set(proposal, require_current_target=True)
            self._validate_lineage_policy(proposal)
        except GovernancePolicyError as error:
            return error.reason
        return None

    def approve_correction(
        self,
        proposal_id: str,
        decision: GovernanceDecision,
    ) -> TransitionResult:
        """Apply an entire valid replacement set or no authority changes."""

        proposal = self._required_proposal(proposal_id)
        if decision.decision is not ProposalStatus.APPLIED:
            return self._blocked(proposal, decision, INVALID_TRANSITION)
        failure = self.validate_transition(proposal_id, decision)
        if failure is not None:
            return self._blocked(proposal, decision, failure)

        before = self._state_snapshot(proposal)
        feedback_id = self._required_feedback_id(proposal)
        interaction_id = self._interaction_id(feedback_id)
        review_id = self._new_id("REV")
        supersession_id = self._new_id("SUP")
        update_id = self._new_id("UPD")

        self._connection.execute("SAVEPOINT governance_approval_mutation")
        try:
            target = self._connection.execute(
                """UPDATE source_document_version SET is_current = 0
                   WHERE document_version_id = ? AND is_current = 1""",
                (proposal.target_document_version_id,),
            )
            if target.rowcount != 1:
                raise _StaleTargetDuringMutation
            proposed = self._connection.execute(
                """UPDATE source_document_version
                   SET is_current = 1, governance_state = 'APPLIED'
                   WHERE document_version_id = ? AND is_current = 0""",
                (proposal.proposed_document_version_id,),
            )
            if proposed.rowcount != 1:
                raise _StaleTargetDuringMutation
            try:
                for item in proposal.replacement_items:
                    self._knowledge.apply_assertion_state_transition(
                        item.predecessor_assertion_id,
                        KnowledgeAssertionState.SUPERSEDED,
                    )
                for item in proposal.replacement_items:
                    self._knowledge.apply_assertion_state_transition(
                        item.successor_assertion_id,
                        KnowledgeAssertionState.APPLIED,
                    )
            except KnowledgeStateTransitionError as error:
                raise _StaleTargetDuringMutation from error

            self._connection.execute(
                """UPDATE feedback SET status = 'APPLIED', applied_at = ?
                   WHERE feedback_id = ?""",
                (decision.timestamp, feedback_id),
            )
            self._connection.execute(
                "INSERT INTO review VALUES (?, ?, ?, ?, 'APPROVED', ?, ?)",
                (
                    review_id,
                    feedback_id,
                    decision.actor.actor_id,
                    decision.actor.role,
                    decision.rationale,
                    decision.timestamp,
                ),
            )
            self._connection.execute(
                "INSERT INTO assertion_supersession VALUES (?, ?, ?, ?, ?)",
                (
                    supersession_id,
                    proposal.target_document_version_id,
                    proposal.proposed_document_version_id,
                    feedback_id,
                    decision.timestamp,
                ),
            )
            for item in proposal.replacement_items:
                self._knowledge.create_assertion_lineage(
                    item.predecessor_assertion_id,
                    item.successor_assertion_id,
                    feedback_id,
                    decision.timestamp,
                )
            lineage_ids = self._lineage_ids(proposal)
            self._connection.execute(
                "INSERT INTO knowledge_update VALUES (?, ?, ?, ?, ?)",
                (
                    update_id,
                    feedback_id,
                    proposal.target_document_version_id,
                    proposal.proposed_document_version_id,
                    decision.timestamp,
                ),
            )
            self._proposals.record_decision(proposal_id, decision)
        except _StaleTargetDuringMutation:
            self._connection.execute("ROLLBACK TO governance_approval_mutation")
            self._connection.execute("RELEASE governance_approval_mutation")
            return self._blocked(proposal, decision, STALE_TARGET)
        except BaseException:
            self._connection.execute("ROLLBACK TO governance_approval_mutation")
            self._connection.execute("RELEASE governance_approval_mutation")
            raise
        self._connection.execute("RELEASE governance_approval_mutation")
        after = self._state_snapshot(proposal)
        facts = self._decision_facts(
            proposal,
            decision,
            action="APPLY",
            before=before,
            after=after,
            lineage_ids=lineage_ids,
            review_id=review_id,
            supersession_id=supersession_id,
            update_id=update_id,
        )
        self._audit(
            "REVIEW_APPROVED", decision.timestamp, facts, interaction_id, feedback_id
        )
        self._audit(
            "KNOWLEDGE_UPDATE_APPLIED",
            decision.timestamp,
            facts,
            interaction_id,
            feedback_id,
        )
        return self._result(
            proposal,
            success=True,
            status=ProposalStatus.APPLIED,
            decision=decision,
        )

    def reject_correction(
        self,
        proposal_id: str,
        decision: GovernanceDecision,
    ) -> TransitionResult:
        """Reject a PENDING proposal without changing knowledge authority."""

        proposal = self._required_proposal(proposal_id)
        if decision.decision is not ProposalStatus.REJECTED:
            return self._blocked(proposal, decision, INVALID_TRANSITION)
        failure = self.validate_transition(proposal_id, decision)
        if failure is not None:
            return self._blocked(proposal, decision, failure)

        feedback_id = self._required_feedback_id(proposal)
        interaction_id = self._interaction_id(feedback_id)
        before = self._state_snapshot(proposal)
        review_id = self._new_id("REV")
        self._connection.execute(
            "UPDATE feedback SET status = 'REJECTED' WHERE feedback_id = ?",
            (feedback_id,),
        )
        self._connection.execute(
            "INSERT INTO review VALUES (?, ?, ?, ?, 'REJECTED', ?, ?)",
            (
                review_id,
                feedback_id,
                decision.actor.actor_id,
                decision.actor.role,
                decision.rationale,
                decision.timestamp,
            ),
        )
        self._proposals.record_decision(proposal_id, decision)
        after = self._state_snapshot(proposal)
        facts = self._decision_facts(
            proposal,
            decision,
            action="REJECT",
            before=before,
            after=after,
            lineage_ids=(),
            review_id=review_id,
        )
        self._audit(
            "REVIEW_REJECTED", decision.timestamp, facts, interaction_id, feedback_id
        )
        return self._result(
            proposal,
            success=True,
            status=ProposalStatus.REJECTED,
            decision=decision,
        )

    def _required_proposal(self, proposal_id: str) -> CorrectionProposal:
        proposal = self._proposals.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        return proposal

    def _validate_feedback_envelope(self, proposal: CorrectionProposal) -> None:
        if proposal.feedback_id is None:
            return
        row = self._connection.execute(
            """SELECT target_version_id, proposed_version_id, status
               FROM feedback WHERE feedback_id = ?""",
            (proposal.feedback_id,),
        ).fetchone()
        if row is None:
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET, "Proposal feedback does not exist"
            )
        if row[0:2] != (
            proposal.target_document_version_id,
            proposal.proposed_document_version_id,
        ):
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET,
                "Proposal document versions do not match linked feedback",
            )
        if row[2] != "PENDING":
            raise GovernancePolicyError(
                INVALID_TRANSITION, "Linked feedback must be PENDING"
            )
        existing = self._connection.execute(
            """SELECT proposal_id FROM correction_proposal
               WHERE feedback_id = ? AND proposal_id <> ?""",
            (proposal.feedback_id, proposal.proposal_id),
        ).fetchone()
        if existing is not None:
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET,
                "Linked feedback already has a correction proposal",
            )

    def _stale_target_reason(self, proposal: CorrectionProposal) -> str | None:
        target = self._connection.execute(
            """SELECT is_current FROM source_document_version
               WHERE document_version_id = ?""",
            (proposal.target_document_version_id,),
        ).fetchone()
        if target is None or target[0] != 1:
            return STALE_TARGET
        for item in proposal.replacement_items:
            predecessor = self._connection.execute(
                "SELECT state FROM knowledge_assertion WHERE assertion_id = ?",
                (item.predecessor_assertion_id,),
            ).fetchone()
            successor = self._connection.execute(
                "SELECT state FROM knowledge_assertion WHERE assertion_id = ?",
                (item.successor_assertion_id,),
            ).fetchone()
            if predecessor is None or predecessor[0] != "APPLIED":
                return STALE_TARGET
            if successor is None or successor[0] != "CANDIDATE":
                return STALE_TARGET
        return None

    def _is_future_effective(
        self, proposal: CorrectionProposal, approval_instant: str
    ) -> bool:
        approval = _instant(approval_instant)
        row = self._connection.execute(
            """SELECT effective_from FROM source_document_version
               WHERE document_version_id = ?""",
            (proposal.proposed_document_version_id,),
        ).fetchone()
        if row is None or _instant(row[0]) > approval:
            return True
        return any(
            _instant(item.successor_effective_from) > approval
            for item in proposal.replacement_items
        )

    def _validate_replacement_set(
        self,
        proposal: CorrectionProposal,
        *,
        require_current_target: bool,
    ) -> None:
        version_rows = self._connection.execute(
            """SELECT document_version_id, document_id, is_current
               FROM source_document_version WHERE document_version_id IN (?, ?)""",
            (
                proposal.target_document_version_id,
                proposal.proposed_document_version_id,
            ),
        ).fetchall()
        versions = {row[0]: row for row in version_rows}
        target = versions.get(proposal.target_document_version_id)
        proposed = versions.get(proposal.proposed_document_version_id)
        if target is None or proposed is None or target[1] != proposed[1]:
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET,
                "Proposal versions must exist in one logical document family",
            )
        if require_current_target and target[2] != 1:
            raise GovernancePolicyError(STALE_TARGET, "STALE_TARGET")
        if proposed[2] != 0:
            raise GovernancePolicyError(STALE_TARGET, "STALE_TARGET")

        for item in proposal.replacement_items:
            if item.predecessor_assertion_id == item.successor_assertion_id:
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET, "Self replacement is not allowed"
                )
            predecessor = self._assertion_row(item.predecessor_assertion_id)
            successor = self._assertion_row(item.successor_assertion_id)
            expected_predecessor = self._expected_assertion(item, predecessor=True)
            expected_successor = self._expected_assertion(item, predecessor=False)
            if predecessor[:8] != expected_predecessor:
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET,
                    "Predecessor no longer matches the proposal snapshot",
                )
            if successor[:8] != expected_successor:
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET,
                    "Successor no longer matches the proposal snapshot",
                )
            if require_current_target and predecessor[8] != "APPLIED":
                raise GovernancePolicyError(STALE_TARGET, "STALE_TARGET")
            if successor[8] != "CANDIDATE":
                raise GovernancePolicyError(STALE_TARGET, "STALE_TARGET")
            if set(predecessor[9]) != set(item.predecessor_evidence_ids):
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET,
                    "Predecessor evidence no longer matches the proposal snapshot",
                )
            if set(successor[9]) != set(item.successor_evidence_ids):
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET,
                    "Successor evidence no longer matches the proposal snapshot",
                )

    def _assertion_row(self, assertion_id: str) -> tuple[Any, ...]:
        row = self._connection.execute(
            """SELECT document_version_id, predicate, decision_dimension,
                      normalized_scope_json, value_json, effective_from,
                      effective_to, assertion_id, state
               FROM knowledge_assertion WHERE assertion_id = ?""",
            (assertion_id,),
        ).fetchone()
        if row is None:
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET,
                f"Unknown replacement assertion: {assertion_id}",
            )
        evidence = tuple(
            value[0]
            for value in self._connection.execute(
                """SELECT evidence_id FROM assertion_evidence
                   WHERE assertion_id = ? ORDER BY evidence_id""",
                (assertion_id,),
            ).fetchall()
        )
        scope = json.loads(row[3]) if row[3] is not None else None
        value = json.loads(row[4])
        return (
            row[0],
            row[1],
            row[2],
            _json_value(scope),
            _json_value(value),
            row[5],
            row[6],
            row[7],
            row[8],
            evidence,
        )

    @staticmethod
    def _expected_assertion(
        item: ReplacementItem, *, predecessor: bool
    ) -> tuple[object, ...]:
        if predecessor:
            return (
                item.predecessor_document_version_id,
                item.predicate,
                item.decision_dimension,
                _json_value(item.normalized_scope),
                _json_value(item.predecessor_value),
                item.predecessor_effective_from,
                item.predecessor_effective_to,
                item.predecessor_assertion_id,
            )
        return (
            item.successor_document_version_id,
            item.predicate,
            item.decision_dimension,
            _json_value(item.normalized_scope),
            _json_value(item.successor_value),
            item.successor_effective_from,
            item.successor_effective_to,
            item.successor_assertion_id,
        )

    def _validate_lineage_policy(self, proposal: CorrectionProposal) -> None:
        persisted_edges = self._connection.execute(
            """SELECT predecessor_assertion_id, successor_assertion_id
               FROM assertion_lineage"""
        ).fetchall()
        edges = {(row[0], row[1]) for row in persisted_edges}
        candidate_edges = {
            (item.predecessor_assertion_id, item.successor_assertion_id)
            for item in proposal.replacement_items
        }
        for predecessor, successor in candidate_edges:
            if predecessor == successor:
                raise GovernancePolicyError(
                    INVALID_REPLACEMENT_SET, "Self replacement is not allowed"
                )
            if (predecessor, successor) in edges:
                raise GovernancePolicyError(
                    DUPLICATE_LINEAGE, "Duplicate direct lineage is not allowed"
                )
            if any(left == predecessor for left, _ in edges):
                raise GovernancePolicyError(
                    APPLIED_BRANCH, "Applied lineage branching is not allowed"
                )
            if any(right == successor for _, right in edges):
                raise GovernancePolicyError(
                    MERGE_NOT_SUPPORTED, "Lineage merges are not supported"
                )
        graph_edges = edges | candidate_edges
        for predecessor, successor in candidate_edges:
            if self._path_exists(graph_edges, successor, predecessor):
                raise GovernancePolicyError(
                    LINEAGE_CYCLE, "Replacement would create a bounded lineage cycle"
                )

    @staticmethod
    def _path_exists(
        edges: set[tuple[str, str]], start: str, target: str
    ) -> bool:
        frontier = {start}
        visited: set[str] = set()
        for _ in range(_MAX_LINEAGE_DEPTH):
            if target in frontier:
                return True
            visited.update(frontier)
            frontier = {
                right
                for left, right in edges
                if left in frontier and right not in visited
            }
            if not frontier:
                return False
        raise GovernancePolicyError(
            LINEAGE_CYCLE, "Lineage validation exceeded its bounded depth"
        )

    def _blocked(
        self,
        proposal: CorrectionProposal,
        decision: GovernanceDecision,
        reason: str,
    ) -> TransitionResult:
        if proposal.status is ProposalStatus.PENDING:
            interaction_id = (
                self._interaction_id(proposal.feedback_id)
                if proposal.feedback_id is not None
                else None
            )
            self._audit(
                "GOVERNANCE_APPROVAL_BLOCKED"
                if decision.decision is ProposalStatus.APPLIED
                else "GOVERNANCE_DECISION_BLOCKED",
                decision.timestamp,
                self._decision_facts(
                    proposal,
                    decision,
                    action="APPLY_ATTEMPT"
                    if decision.decision is ProposalStatus.APPLIED
                    else "REJECT_ATTEMPT",
                    before=self._state_snapshot(proposal),
                    after=self._state_snapshot(proposal),
                    lineage_ids=(),
                    failure_reason=reason,
                ),
                interaction_id,
                proposal.feedback_id,
            )
        return self._result(
            proposal,
            success=False,
            status=proposal.status,
            decision=decision,
            failure_reason=reason,
        )

    def _state_snapshot(self, proposal: CorrectionProposal) -> dict[str, object]:
        version_rows = self._connection.execute(
            """SELECT document_version_id, is_current, governance_state
               FROM source_document_version WHERE document_version_id IN (?, ?)
               ORDER BY document_version_id""",
            (
                proposal.target_document_version_id,
                proposal.proposed_document_version_id,
            ),
        ).fetchall()
        assertion_ids = tuple(
            identifier
            for item in proposal.replacement_items
            for identifier in (
                item.predecessor_assertion_id,
                item.successor_assertion_id,
            )
        )
        placeholders = ",".join("?" for _ in assertion_ids)
        assertion_rows = self._connection.execute(
            f"""SELECT assertion_id, state FROM knowledge_assertion
                 WHERE assertion_id IN ({placeholders}) ORDER BY assertion_id""",
            assertion_ids,
        ).fetchall()
        return {
            "document_versions": [
                {
                    "document_version_id": row[0],
                    "is_current": bool(row[1]),
                    "governance_state": row[2],
                }
                for row in version_rows
            ],
            "assertions": [
                {"assertion_id": row[0], "state": row[1]}
                for row in assertion_rows
            ],
        }

    def _decision_facts(
        self,
        proposal: CorrectionProposal,
        decision: GovernanceDecision,
        *,
        action: str,
        before: Mapping[str, object],
        after: Mapping[str, object],
        lineage_ids: tuple[str, ...],
        failure_reason: str | None = None,
        review_id: str | None = None,
        supersession_id: str | None = None,
        update_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "action": action,
            "proposal_id": proposal.proposal_id,
            "feedback_id": proposal.feedback_id,
            "actor": decision.actor.actor_id,
            "actor_role": decision.actor.role,
            "decision": decision.decision.value,
            "rationale": decision.rationale,
            "target_document_version_id": proposal.target_document_version_id,
            "proposed_document_version_id": proposal.proposed_document_version_id,
            "replacement_items": [
                {
                    "predecessor_assertion_id": item.predecessor_assertion_id,
                    "successor_assertion_id": item.successor_assertion_id,
                }
                for item in proposal.replacement_items
            ],
            "before": dict(before),
            "after": dict(after),
            "lineage_ids": list(lineage_ids),
            "review_id": review_id,
            "supersession_id": supersession_id,
            "update_id": update_id,
            "failure_reason": failure_reason,
            "timestamp": decision.timestamp,
        }

    def _lineage_ids(self, proposal: CorrectionProposal) -> tuple[str, ...]:
        ids: list[str] = []
        for item in proposal.replacement_items:
            row = self._connection.execute(
                """SELECT lineage_id FROM assertion_lineage
                   WHERE predecessor_assertion_id = ? AND successor_assertion_id = ?""",
                (item.predecessor_assertion_id, item.successor_assertion_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Applied lineage was not persisted")
            ids.append(row[0])
        return tuple(ids)

    def _interaction_id(self, feedback_id: str) -> str:
        row = self._connection.execute(
            "SELECT interaction_id FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise GovernancePolicyError(
                INVALID_REPLACEMENT_SET, "Proposal feedback does not exist"
            )
        return row[0]

    @staticmethod
    def _required_feedback_id(proposal: CorrectionProposal) -> str:
        if proposal.feedback_id is None:
            raise GovernancePolicyError(
                FEEDBACK_REQUIRED,
                "Schema v6 live governance transitions require linked feedback",
            )
        return proposal.feedback_id

    def _audit(
        self,
        event_type: str,
        occurred_at: str,
        payload: Mapping[str, object],
        interaction_id: str | None,
        feedback_id: str | None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO audit_event
               (interaction_id, feedback_id, event_type, occurred_at, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                interaction_id,
                feedback_id,
                event_type,
                occurred_at,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"

    @staticmethod
    def _result(
        proposal: CorrectionProposal,
        *,
        success: bool,
        status: ProposalStatus | None = None,
        decision: GovernanceDecision | None = None,
        failure_reason: str | None = None,
    ) -> TransitionResult:
        return TransitionResult(
            success=success,
            proposal_id=proposal.proposal_id,
            status=status or proposal.status,
            target_document_version_id=proposal.target_document_version_id,
            proposed_document_version_id=proposal.proposed_document_version_id,
            replacement_items=proposal.replacement_items,
            decision=decision,
            failure_reason=failure_reason,
        )
