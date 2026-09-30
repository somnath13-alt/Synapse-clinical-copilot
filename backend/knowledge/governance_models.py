"""Immutable contracts for the governed correction lifecycle.

These types describe proposal and decision data only. They do not authenticate
actors, authorize decisions, persist records, or apply governance transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from backend.knowledge.models import JsonValue, _normalize_utc_instant, freeze_json


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _identifier_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be a sequence of strings")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{field_name} must be a sequence of strings") from error
    if not normalized or any(
        not isinstance(value, str) or not value for value in normalized
    ):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _normalize_timestamp(value: object, field_name: str) -> str:
    try:
        return _normalize_utc_instant(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp") from error


def _normalize_interval(
    effective_from: object,
    effective_to: object,
    field_prefix: str,
) -> tuple[str, str | None]:
    normalized_from = _normalize_timestamp(
        effective_from, f"{field_prefix}_effective_from"
    )
    normalized_to = (
        _normalize_timestamp(effective_to, f"{field_prefix}_effective_to")
        if effective_to is not None
        else None
    )
    parsed_from = datetime.fromisoformat(normalized_from.replace("Z", "+00:00"))
    parsed_to = (
        datetime.fromisoformat(normalized_to.replace("Z", "+00:00"))
        if normalized_to is not None
        else None
    )
    if parsed_to is not None and parsed_from > parsed_to:
        raise ValueError(
            f"{field_prefix}_effective_from must not be after "
            f"{field_prefix}_effective_to"
        )
    return normalized_from, normalized_to


def _freeze_scope(
    value: Mapping[str, JsonValue] | None,
) -> Mapping[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("normalized_scope must be a mapping or None")
    frozen = _freeze_governance_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("normalized_scope must be a mapping or None")
    return frozen


def _freeze_governance_json(value: object) -> JsonValue:
    """Freeze JSON data, including values already frozen by this module."""

    if isinstance(value, Mapping):
        mutable_value = {
            key: _mutable_json_value(item) for key, item in value.items()
        }
        return freeze_json(mutable_value)
    return freeze_json(_mutable_json_value(value))


def _mutable_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json_value(item) for item in value]
    return value


class ProposalStatus(StrEnum):
    """The complete M7 proposal lifecycle vocabulary."""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Actor:
    """Asserted prototype actor metadata, not authentication or authorization."""

    actor_id: str
    role: str

    def __post_init__(self) -> None:
        _required_text(self.actor_id, "actor_id")
        _required_text(self.role, "role")


@dataclass(frozen=True, slots=True)
class ReplacementItem:
    """One proposed predecessor-to-successor assertion replacement."""

    predecessor_assertion_id: str
    successor_assertion_id: str
    predicate: str
    decision_dimension: str
    normalized_scope: Mapping[str, JsonValue] | None
    predecessor_value: JsonValue
    successor_value: JsonValue
    predecessor_effective_from: str
    predecessor_effective_to: str | None
    successor_effective_from: str
    successor_effective_to: str | None
    predecessor_document_id: str
    predecessor_document_version_id: str
    predecessor_evidence_ids: tuple[str, ...]
    successor_document_id: str
    successor_document_version_id: str
    successor_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "predecessor_assertion_id",
            "successor_assertion_id",
            "predicate",
            "decision_dimension",
            "predecessor_document_id",
            "predecessor_document_version_id",
            "successor_document_id",
            "successor_document_version_id",
        ):
            _required_text(getattr(self, field_name), field_name)

        object.__setattr__(
            self, "normalized_scope", _freeze_scope(self.normalized_scope)
        )
        object.__setattr__(
            self, "predecessor_value", _freeze_governance_json(self.predecessor_value)
        )
        object.__setattr__(
            self, "successor_value", _freeze_governance_json(self.successor_value)
        )
        object.__setattr__(
            self,
            "predecessor_evidence_ids",
            _identifier_tuple(
                self.predecessor_evidence_ids, "predecessor_evidence_ids"
            ),
        )
        object.__setattr__(
            self,
            "successor_evidence_ids",
            _identifier_tuple(self.successor_evidence_ids, "successor_evidence_ids"),
        )

        predecessor_interval = _normalize_interval(
            self.predecessor_effective_from,
            self.predecessor_effective_to,
            "predecessor",
        )
        successor_interval = _normalize_interval(
            self.successor_effective_from,
            self.successor_effective_to,
            "successor",
        )
        object.__setattr__(self, "predecessor_effective_from", predecessor_interval[0])
        object.__setattr__(self, "predecessor_effective_to", predecessor_interval[1])
        object.__setattr__(self, "successor_effective_from", successor_interval[0])
        object.__setattr__(self, "successor_effective_to", successor_interval[1])


def _replacement_tuple(
    values: object,
    *,
    require_nonempty: bool,
) -> tuple[ReplacementItem, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("replacement_items must be a sequence of ReplacementItem values")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(
            "replacement_items must be a sequence of ReplacementItem values"
        ) from error
    if require_nonempty and not normalized:
        raise ValueError("replacement_items must contain at least one item")
    if any(not isinstance(item, ReplacementItem) for item in normalized):
        raise TypeError("replacement_items must contain only ReplacementItem values")

    pairs = [
        (item.predecessor_assertion_id, item.successor_assertion_id)
        for item in normalized
    ]
    if len(pairs) != len(set(pairs)):
        raise ValueError("replacement_items must not contain duplicate replacement pairs")
    predecessor_ids = [item.predecessor_assertion_id for item in normalized]
    if len(predecessor_ids) != len(set(predecessor_ids)):
        raise ValueError("replacement_items must not contain duplicate predecessor IDs")
    successor_ids = [item.successor_assertion_id for item in normalized]
    if len(successor_ids) != len(set(successor_ids)):
        raise ValueError("replacement_items must not contain duplicate successor IDs")
    return tuple(sorted(normalized, key=_replacement_sort_key))


def _replacement_sort_key(item: ReplacementItem) -> tuple[str, str]:
    return item.predecessor_assertion_id, item.successor_assertion_id


@dataclass(frozen=True, slots=True)
class CorrectionProposal:
    """An immutable, all-or-nothing assertion replacement proposal."""

    proposal_id: str
    feedback_id: str | None
    target_document_version_id: str
    proposed_document_version_id: str
    submitter: Actor
    rationale: str
    status: ProposalStatus
    created_at: str
    decided_at: str | None
    replacement_items: tuple[ReplacementItem, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "proposal_id",
            "target_document_version_id",
            "proposed_document_version_id",
            "rationale",
        ):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(
            self, "feedback_id", _optional_identifier(self.feedback_id, "feedback_id")
        )
        if not isinstance(self.submitter, Actor):
            raise TypeError("submitter must be an Actor")
        if not isinstance(self.status, ProposalStatus):
            object.__setattr__(self, "status", ProposalStatus(self.status))
        object.__setattr__(
            self, "created_at", _normalize_timestamp(self.created_at, "created_at")
        )
        if self.decided_at is not None:
            object.__setattr__(
                self,
                "decided_at",
                _normalize_timestamp(self.decided_at, "decided_at"),
            )
        if self.status is ProposalStatus.PENDING and self.decided_at is not None:
            raise ValueError("PENDING proposals must not have decided_at")
        if self.status is not ProposalStatus.PENDING and self.decided_at is None:
            raise ValueError("terminal proposals require decided_at")
        object.__setattr__(
            self,
            "replacement_items",
            _replacement_tuple(self.replacement_items, require_nonempty=True),
        )


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """Asserted decision facts for a future governance operation."""

    proposal_id: str
    decision: ProposalStatus
    actor: Actor
    rationale: str
    timestamp: str

    def __post_init__(self) -> None:
        _required_text(self.proposal_id, "proposal_id")
        _required_text(self.rationale, "rationale")
        if not isinstance(self.actor, Actor):
            raise TypeError("actor must be an Actor")
        if not isinstance(self.decision, ProposalStatus):
            object.__setattr__(self, "decision", ProposalStatus(self.decision))
        if self.decision is ProposalStatus.PENDING:
            raise ValueError("governance decisions must be APPLIED or REJECTED")
        object.__setattr__(
            self, "timestamp", _normalize_timestamp(self.timestamp, "timestamp")
        )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Structural result data for a future governance transition."""

    success: bool
    proposal_id: str
    status: ProposalStatus | None
    target_document_version_id: str
    proposed_document_version_id: str
    replacement_items: tuple[ReplacementItem, ...]
    decision: GovernanceDecision | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("success must be a boolean")
        for field_name in (
            "proposal_id",
            "target_document_version_id",
            "proposed_document_version_id",
        ):
            _required_text(getattr(self, field_name), field_name)
        if self.status is not None and not isinstance(self.status, ProposalStatus):
            object.__setattr__(self, "status", ProposalStatus(self.status))
        if self.decision is not None:
            if not isinstance(self.decision, GovernanceDecision):
                raise TypeError("decision must be a GovernanceDecision or None")
            if self.decision.proposal_id != self.proposal_id:
                raise ValueError("decision proposal_id must match result proposal_id")
        if self.failure_reason is not None:
            _required_text(self.failure_reason, "failure_reason")
        if self.success and self.failure_reason is not None:
            raise ValueError("successful transitions must not include failure_reason")
        object.__setattr__(
            self,
            "replacement_items",
            _replacement_tuple(self.replacement_items, require_nonempty=False),
        )
