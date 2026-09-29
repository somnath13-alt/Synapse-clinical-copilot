from __future__ import annotations

import builtins
import sqlite3
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from backend.knowledge import (
    Actor,
    CorrectionProposal,
    GovernanceDecision,
    KnowledgeAssertion,
    KnowledgeAssertionState,
    ProposalStatus,
    ReplacementItem,
    TransitionResult,
)
from backend.reasoning import DecisionScope


CREATED_AT = "2026-06-10T12:10:00Z"
DECIDED_AT = "2026-07-01T12:10:00Z"
SCOPE = {
    "payer_id": "SYN-PAYER-NHH",
    "plan_id": "SYN-PLAN-HLP",
    "nested": {"medication_ids": ["SYN-MED-VEL"]},
}


def _item(
    predecessor: str = "AST-SYN-POL-V1-PA",
    successor: str = "AST-SYN-POL-V2-PA",
    *,
    predicate: str = "REQUIRES_AUTHORIZATION",
) -> ReplacementItem:
    return ReplacementItem(
        predecessor_assertion_id=predecessor,
        successor_assertion_id=successor,
        predicate=predicate,
        decision_dimension="AUTHORIZATION_REQUIREMENT",
        normalized_scope=SCOPE,
        predecessor_value={"required": True, "steps": ["SYN-MED-NOR"]},
        successor_value={"required": False, "steps": []},
        predecessor_effective_from="2026-01-01T00:00:00+00:00",
        predecessor_effective_to="2026-06-30T23:59:59Z",
        successor_effective_from="2026-07-01T00:00:00Z",
        successor_effective_to=None,
        predecessor_document_id="DOC-SYN-POL-VEL",
        predecessor_document_version_id="DV-SYN-POL-VEL-V1",
        predecessor_evidence_ids=["EV-SYN-POL-V1-PA-001"],  # type: ignore[arg-type]
        successor_document_id="DOC-SYN-POL-VEL",
        successor_document_version_id="DV-SYN-POL-VEL-V2",
        successor_evidence_ids=["EV-SYN-POL-V2-PA-001"],  # type: ignore[arg-type]
    )


def _proposal(**changes: object) -> CorrectionProposal:
    values: dict[str, object] = {
        "proposal_id": "PROP-SYN-M72-001",
        "feedback_id": "FB-SYN-M72-001",
        "target_document_version_id": "DV-SYN-POL-VEL-V1",
        "proposed_document_version_id": "DV-SYN-POL-VEL-V2",
        "submitter": Actor("Synthetic Care Coordinator", "CARE_COORDINATOR"),
        "rationale": "Synthetic V2 corrects the outdated policy assertion.",
        "status": ProposalStatus.PENDING,
        "created_at": CREATED_AT,
        "decided_at": None,
        "replacement_items": [_item()],
    }
    values.update(changes)
    return CorrectionProposal(**values)  # type: ignore[arg-type]


def _decision() -> GovernanceDecision:
    return GovernanceDecision(
        proposal_id="PROP-SYN-M72-001",
        decision=ProposalStatus.APPLIED,
        actor=Actor("Synthetic Knowledge Reviewer", "KNOWLEDGE_REVIEWER"),
        rationale="Synthetic evidence and provenance were reviewed.",
        timestamp=DECIDED_AT,
    )


@pytest.mark.parametrize(
    ("model", "field_name", "replacement"),
    [
        (_item(), "predicate", "CHANGED"),
        (_proposal(), "rationale", "CHANGED"),
        (_decision(), "rationale", "CHANGED"),
        (
            TransitionResult(
                success=True,
                proposal_id="PROP-SYN-M72-001",
                status=ProposalStatus.APPLIED,
                target_document_version_id="DV-SYN-POL-VEL-V1",
                proposed_document_version_id="DV-SYN-POL-VEL-V2",
                replacement_items=(_item(),),
                decision=_decision(),
            ),
            "success",
            False,
        ),
    ],
)
def test_governance_contracts_are_frozen_and_slotted(
    model: object, field_name: str, replacement: object
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(model, field_name, replacement)
    assert not hasattr(model, "__dict__")


def test_actor_is_immutable_and_retains_asserted_role_metadata() -> None:
    actor = Actor("Synthetic Knowledge Reviewer", "KNOWLEDGE_REVIEWER")

    assert (actor.actor_id, actor.role) == (
        "Synthetic Knowledge Reviewer",
        "KNOWLEDGE_REVIEWER",
    )
    with pytest.raises(FrozenInstanceError):
        actor.role = "ADMIN"  # type: ignore[misc]


def test_proposal_normalizes_replacement_items_to_deterministic_tuple_order() -> None:
    later = _item("AST-SYN-V1-STEP", "AST-SYN-V2-STEP", predicate="STEP")
    earlier = _item("AST-SYN-V1-PA", "AST-SYN-V2-PA")

    proposal = _proposal(replacement_items=[later, earlier])

    assert proposal.replacement_items == (earlier, later)
    assert isinstance(proposal.replacement_items, tuple)


def test_replacement_values_and_scope_are_recursively_immutable() -> None:
    item = _item()

    with pytest.raises(TypeError):
        item.normalized_scope["payer_id"] = "CHANGED"  # type: ignore[index]
    with pytest.raises(TypeError):
        item.normalized_scope["nested"]["medication_ids"] = ()  # type: ignore[index,union-attr]
    assert item.normalized_scope["nested"]["medication_ids"] == (  # type: ignore[index,union-attr]
        "SYN-MED-VEL",
    )
    assert item.predecessor_value["steps"] == ("SYN-MED-NOR",)  # type: ignore[index]
    assert item.successor_value["steps"] == ()  # type: ignore[index]


def test_proposal_requires_an_atomic_nonempty_replacement_set() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _proposal(replacement_items=[])


def test_proposal_rejects_duplicate_replacement_pair() -> None:
    item = _item()
    with pytest.raises(ValueError, match="duplicate replacement pairs"):
        _proposal(replacement_items=[item, item])


def test_proposal_rejects_duplicate_predecessor_identity() -> None:
    with pytest.raises(ValueError, match="duplicate predecessor IDs"):
        _proposal(
            replacement_items=[
                _item(),
                _item(successor="AST-SYN-POL-V2-PA-ALTERNATE"),
            ]
        )


def test_proposal_rejects_duplicate_successor_identity() -> None:
    with pytest.raises(ValueError, match="duplicate successor IDs"):
        _proposal(
            replacement_items=[
                _item(),
                _item(predecessor="AST-SYN-POL-V1-PA-ALTERNATE"),
            ]
        )


def test_proposal_status_vocabulary_is_closed_and_decision_is_terminal() -> None:
    assert tuple(ProposalStatus) == (
        ProposalStatus.PENDING,
        ProposalStatus.APPLIED,
        ProposalStatus.REJECTED,
    )
    with pytest.raises(ValueError, match="APPLIED or REJECTED"):
        replace(_decision(), decision=ProposalStatus.PENDING)
    with pytest.raises(ValueError):
        _proposal(status="APPROVED")


def test_proposal_retains_ownership_actor_rationale_and_timestamps() -> None:
    proposal = _proposal(
        status=ProposalStatus.REJECTED,
        decided_at="2026-07-01T12:10:00+00:00",
    )

    assert proposal.proposal_id == "PROP-SYN-M72-001"
    assert proposal.feedback_id == "FB-SYN-M72-001"
    assert proposal.target_document_version_id == "DV-SYN-POL-VEL-V1"
    assert proposal.proposed_document_version_id == "DV-SYN-POL-VEL-V2"
    assert proposal.submitter.role == "CARE_COORDINATOR"
    assert proposal.rationale == "Synthetic V2 corrects the outdated policy assertion."
    assert proposal.created_at == CREATED_AT
    assert proposal.decided_at == DECIDED_AT


def test_pending_and_terminal_decision_timestamp_pairing_is_validated() -> None:
    with pytest.raises(ValueError, match="PENDING proposals"):
        _proposal(decided_at=DECIDED_AT)
    with pytest.raises(ValueError, match="terminal proposals"):
        _proposal(status=ProposalStatus.APPLIED)


def test_replacement_retains_provenance_temporal_and_candidate_identities() -> None:
    item = _item()

    assert item.predecessor_assertion_id == "AST-SYN-POL-V1-PA"
    assert item.successor_assertion_id == "AST-SYN-POL-V2-PA"
    assert item.predecessor_document_id == item.successor_document_id
    assert item.predecessor_document_version_id == "DV-SYN-POL-VEL-V1"
    assert item.successor_document_version_id == "DV-SYN-POL-VEL-V2"
    assert item.predecessor_evidence_ids == ("EV-SYN-POL-V1-PA-001",)
    assert item.successor_evidence_ids == ("EV-SYN-POL-V2-PA-001",)
    assert item.predecessor_effective_from == "2026-01-01T00:00:00Z"
    assert item.predecessor_effective_to == "2026-06-30T23:59:59Z"
    assert item.successor_effective_from == "2026-07-01T00:00:00Z"
    assert item.successor_effective_to is None


def test_replacement_rejects_inverted_effective_interval() -> None:
    with pytest.raises(ValueError, match="must not be after"):
        replace(
            _item(),
            successor_effective_from="2026-07-02T00:00:00Z",
            successor_effective_to="2026-07-01T00:00:00Z",
        )


def test_governance_decision_retains_audit_facts_without_authority_claims() -> None:
    decision = _decision()

    assert decision.proposal_id == "PROP-SYN-M72-001"
    assert decision.decision is ProposalStatus.APPLIED
    assert decision.actor.actor_id == "Synthetic Knowledge Reviewer"
    assert decision.actor.role == "KNOWLEDGE_REVIEWER"
    assert decision.rationale == "Synthetic evidence and provenance were reviewed."
    assert decision.timestamp == DECIDED_AT


def test_transition_result_retains_structural_and_audit_relevant_identities() -> None:
    item = _item()
    result = TransitionResult(
        success=True,
        proposal_id="PROP-SYN-M72-001",
        status="APPLIED",  # type: ignore[arg-type]
        target_document_version_id="DV-SYN-POL-VEL-V1",
        proposed_document_version_id="DV-SYN-POL-VEL-V2",
        replacement_items=[item],  # type: ignore[arg-type]
        decision=_decision(),
    )

    assert result.success is True
    assert result.status is ProposalStatus.APPLIED
    assert result.replacement_items == (item,)
    assert result.decision == _decision()


def test_contracts_have_no_numeric_confidence_or_probability_fields() -> None:
    for contract in (
        Actor,
        ReplacementItem,
        CorrectionProposal,
        GovernanceDecision,
        TransitionResult,
    ):
        names = {field.name for field in fields(contract)}
        assert "confidence" not in names
        assert "probability" not in names


def test_contract_construction_performs_no_database_or_filesystem_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("governance contract attempted external access")

    monkeypatch.setattr(sqlite3, "connect", unexpected_access)
    monkeypatch.setattr(builtins, "open", unexpected_access)

    proposal = _proposal()
    decision = _decision()
    result = TransitionResult(
        success=False,
        proposal_id=proposal.proposal_id,
        status=ProposalStatus.PENDING,
        target_document_version_id=proposal.target_document_version_id,
        proposed_document_version_id=proposal.proposed_document_version_id,
        replacement_items=proposal.replacement_items,
        decision=decision,
    )

    assert result.status is ProposalStatus.PENDING


def test_existing_knowledge_and_reasoning_contracts_remain_compatible() -> None:
    assertion = KnowledgeAssertion(
        assertion_id="AST-SYN-REGRESSION",
        document_version_id="DV-SYN-REGRESSION",
        subject_id="SYN-SUBJECT",
        predicate="HAS_VALUE",
        object_id=None,
        value={"items": ["synthetic"]},
        decision_dimension="PATIENT_CONTEXT",
        normalized_scope=None,
        recorded_at=CREATED_AT,
        effective_from=CREATED_AT,
        effective_to=None,
        state=KnowledgeAssertionState.CANDIDATE,
        evidence_ids=("EV-SYN-REGRESSION",),
    )
    scope = DecisionScope(
        payer_id=None,
        plan_id=None,
        medication_id="SYN-MED-VEL",
        indication_id="SYN-COND-LDS",
        effective_from=CREATED_AT,
        effective_to=None,
        as_of=CREATED_AT,
    )

    assert assertion.state is KnowledgeAssertionState.CANDIDATE
    assert assertion.value["items"] == ("synthetic",)  # type: ignore[index]
    assert scope.medication_id == "SYN-MED-VEL"
