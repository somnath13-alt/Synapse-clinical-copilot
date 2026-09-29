CREATE TABLE IF NOT EXISTS source_document (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_title TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS source_document_version (
    document_version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    relevant_excerpt TEXT NOT NULL,
    structured_data TEXT NOT NULL,
    checksum TEXT NOT NULL,
    governance_state TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    test_only INTEGER NOT NULL DEFAULT 0 CHECK (test_only IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_document_version_one_current
ON source_document_version(document_id)
WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS evidence_item (
    evidence_id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_title TEXT NOT NULL,
    version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    section TEXT NOT NULL,
    relevant_excerpt TEXT NOT NULL,
    structured_data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_assertion (
    assertion_id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT,
    value_json TEXT,
    decision_dimension TEXT NOT NULL,
    normalized_scope_json TEXT,
    recorded_at TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    state TEXT NOT NULL CHECK (state IN ('CANDIDATE', 'APPLIED', 'SUPERSEDED'))
);

CREATE TABLE IF NOT EXISTS assertion_evidence (
    assertion_id TEXT NOT NULL REFERENCES knowledge_assertion(assertion_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_item(evidence_id),
    PRIMARY KEY (assertion_id, evidence_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_document_version_identity
ON source_document_version(document_version_id, document_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_assertion_version_identity
ON knowledge_assertion(assertion_id, document_version_id);

CREATE TABLE IF NOT EXISTS interaction (
    interaction_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    intent TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    selected_sources_json TEXT NOT NULL,
    retrieval_trace_json TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    confidence TEXT CHECK (confidence IN ('HIGH', 'MEDIUM', 'LOW') OR confidence IS NULL),
    confidence_rationale TEXT,
    reconciliation_json TEXT NOT NULL,
    escalation_json TEXT,
    policy_version_id TEXT,
    temporal_mode TEXT NOT NULL CHECK (temporal_mode IN ('CURRENT', 'AS_OF')),
    requested_as_of TEXT,
    confidence_policy_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (temporal_mode = 'CURRENT' AND requested_as_of IS NULL)
        OR (temporal_mode = 'AS_OF' AND requested_as_of IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS interaction_evidence (
    interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_item(evidence_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (interaction_id, evidence_id),
    UNIQUE (interaction_id, ordinal)
);

CREATE TABLE IF NOT EXISTS interaction_knowledge (
    interaction_knowledge_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id),
    assertion_id TEXT NOT NULL REFERENCES knowledge_assertion(assertion_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    origin TEXT NOT NULL CHECK (origin IN ('SOURCE', 'KNOWLEDGE', 'CORROBORATED')),
    state_at_execution TEXT NOT NULL
        CHECK (state_at_execution IN ('APPLIED', 'SUPERSEDED')),
    decision_type TEXT NOT NULL,
    value_json TEXT,
    normalized_scope_json TEXT,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    recorded_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    document_version_id TEXT NOT NULL
        REFERENCES source_document_version(document_version_id),
    document_version TEXT NOT NULL,
    document_effective_from TEXT NOT NULL,
    document_effective_to TEXT,
    lineage_ids_json TEXT NOT NULL,
    correction_ids_json TEXT NOT NULL,
    UNIQUE (interaction_id, ordinal),
    UNIQUE (interaction_id, assertion_id, origin)
);

CREATE TABLE IF NOT EXISTS interaction_knowledge_evidence (
    interaction_knowledge_id TEXT NOT NULL
        REFERENCES interaction_knowledge(interaction_knowledge_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_item(evidence_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (interaction_knowledge_id, evidence_id),
    UNIQUE (interaction_knowledge_id, ordinal)
);

CREATE TABLE IF NOT EXISTS supported_claim (
    supported_claim_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id),
    claim_key TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citation (
    citation_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id),
    supported_claim_id TEXT NOT NULL REFERENCES supported_claim(supported_claim_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_item(evidence_id),
    source_id TEXT NOT NULL,
    document_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    source_title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    section TEXT NOT NULL,
    relevant_excerpt TEXT NOT NULL,
    FOREIGN KEY (interaction_id, evidence_id)
        REFERENCES interaction_evidence(interaction_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interaction(interaction_id),
    actor TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    message TEXT NOT NULL,
    target_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    proposed_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    status TEXT NOT NULL CHECK (status IN ('SUBMITTED', 'PENDING', 'APPLIED', 'REJECTED')),
    submitted_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS correction_proposal (
    proposal_id TEXT PRIMARY KEY,
    feedback_id TEXT REFERENCES feedback(feedback_id),
    logical_document_id TEXT NOT NULL REFERENCES source_document(document_id),
    target_document_version_id TEXT NOT NULL,
    proposed_document_version_id TEXT NOT NULL,
    submitter_actor TEXT NOT NULL,
    submitter_role TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPLIED', 'REJECTED')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_actor TEXT,
    decision_role TEXT,
    decision_rationale TEXT,
    first_replacement_ordinal INTEGER NOT NULL DEFAULT 0
        CHECK (first_replacement_ordinal = 0),
    CHECK (target_document_version_id <> proposed_document_version_id),
    CHECK (
        (status = 'PENDING'
         AND decided_at IS NULL
         AND decision_actor IS NULL
         AND decision_role IS NULL
         AND decision_rationale IS NULL)
        OR
        (status IN ('APPLIED', 'REJECTED')
         AND decided_at IS NOT NULL
         AND decision_actor IS NOT NULL
         AND decision_role IS NOT NULL
         AND decision_rationale IS NOT NULL)
    ),
    FOREIGN KEY (target_document_version_id, logical_document_id)
        REFERENCES source_document_version(document_version_id, document_id),
    FOREIGN KEY (proposed_document_version_id, logical_document_id)
        REFERENCES source_document_version(document_version_id, document_id),
    FOREIGN KEY (proposal_id, first_replacement_ordinal)
        REFERENCES correction_proposal_replacement_item(proposal_id, ordinal)
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (proposal_id, target_document_version_id),
    UNIQUE (proposal_id, proposed_document_version_id)
);

CREATE TABLE IF NOT EXISTS correction_proposal_replacement_item (
    proposal_id TEXT NOT NULL REFERENCES correction_proposal(proposal_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    predecessor_assertion_id TEXT NOT NULL,
    successor_assertion_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    decision_dimension TEXT NOT NULL,
    normalized_scope_json TEXT,
    predecessor_value_json TEXT NOT NULL,
    successor_value_json TEXT NOT NULL,
    predecessor_effective_from TEXT NOT NULL,
    predecessor_effective_to TEXT,
    successor_effective_from TEXT NOT NULL,
    successor_effective_to TEXT,
    predecessor_document_version_id TEXT NOT NULL,
    successor_document_version_id TEXT NOT NULL,
    PRIMARY KEY (proposal_id, ordinal),
    CHECK (predecessor_assertion_id <> successor_assertion_id),
    UNIQUE (proposal_id, predecessor_assertion_id),
    UNIQUE (proposal_id, successor_assertion_id),
    UNIQUE (proposal_id, predecessor_assertion_id, successor_assertion_id),
    UNIQUE (proposal_id, ordinal, predecessor_assertion_id),
    UNIQUE (proposal_id, ordinal, successor_assertion_id),
    FOREIGN KEY (proposal_id, predecessor_document_version_id)
        REFERENCES correction_proposal(proposal_id, target_document_version_id),
    FOREIGN KEY (proposal_id, successor_document_version_id)
        REFERENCES correction_proposal(proposal_id, proposed_document_version_id),
    FOREIGN KEY (predecessor_assertion_id, predecessor_document_version_id)
        REFERENCES knowledge_assertion(assertion_id, document_version_id),
    FOREIGN KEY (successor_assertion_id, successor_document_version_id)
        REFERENCES knowledge_assertion(assertion_id, document_version_id)
);

CREATE TABLE IF NOT EXISTS correction_proposal_predecessor_evidence (
    proposal_id TEXT NOT NULL,
    replacement_ordinal INTEGER NOT NULL,
    predecessor_assertion_id TEXT NOT NULL,
    evidence_ordinal INTEGER NOT NULL CHECK (evidence_ordinal >= 0),
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (proposal_id, replacement_ordinal, evidence_ordinal),
    UNIQUE (proposal_id, replacement_ordinal, evidence_id),
    FOREIGN KEY (proposal_id, replacement_ordinal, predecessor_assertion_id)
        REFERENCES correction_proposal_replacement_item(
            proposal_id, ordinal, predecessor_assertion_id
        ),
    FOREIGN KEY (predecessor_assertion_id, evidence_id)
        REFERENCES assertion_evidence(assertion_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS correction_proposal_successor_evidence (
    proposal_id TEXT NOT NULL,
    replacement_ordinal INTEGER NOT NULL,
    successor_assertion_id TEXT NOT NULL,
    evidence_ordinal INTEGER NOT NULL CHECK (evidence_ordinal >= 0),
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (proposal_id, replacement_ordinal, evidence_ordinal),
    UNIQUE (proposal_id, replacement_ordinal, evidence_id),
    FOREIGN KEY (proposal_id, replacement_ordinal, successor_assertion_id)
        REFERENCES correction_proposal_replacement_item(
            proposal_id, ordinal, successor_assertion_id
        ),
    FOREIGN KEY (successor_assertion_id, evidence_id)
        REFERENCES assertion_evidence(assertion_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS review (
    review_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES feedback(feedback_id),
    reviewer TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assertion_supersession (
    supersession_id TEXT PRIMARY KEY,
    prior_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    successor_version_id TEXT NOT NULL REFERENCES source_document_version(document_version_id),
    feedback_id TEXT NOT NULL REFERENCES feedback(feedback_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assertion_lineage (
    lineage_id TEXT PRIMARY KEY,
    predecessor_assertion_id TEXT NOT NULL REFERENCES knowledge_assertion(assertion_id),
    successor_assertion_id TEXT NOT NULL REFERENCES knowledge_assertion(assertion_id),
    feedback_id TEXT NOT NULL REFERENCES feedback(feedback_id),
    created_at TEXT NOT NULL,
    CHECK (predecessor_assertion_id <> successor_assertion_id),
    UNIQUE (predecessor_assertion_id, successor_assertion_id)
);

CREATE TABLE IF NOT EXISTS knowledge_update (
    update_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES feedback(feedback_id),
    prior_version_id TEXT NOT NULL,
    current_version_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id TEXT,
    feedback_id TEXT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_source_type ON evidence_item(source_type);
CREATE INDEX IF NOT EXISTS idx_audit_interaction ON audit_event(interaction_id, event_id);
CREATE INDEX IF NOT EXISTS idx_audit_feedback ON audit_event(feedback_id, event_id);
CREATE INDEX IF NOT EXISTS idx_correction_proposal_status
ON correction_proposal(status, created_at, proposal_id);
CREATE INDEX IF NOT EXISTS idx_correction_proposal_target_version
ON correction_proposal(target_document_version_id, proposal_id);
CREATE INDEX IF NOT EXISTS idx_correction_proposal_proposed_version
ON correction_proposal(proposed_document_version_id, proposal_id);
CREATE INDEX IF NOT EXISTS idx_correction_replacement_predecessor
ON correction_proposal_replacement_item(predecessor_assertion_id, proposal_id);
CREATE INDEX IF NOT EXISTS idx_correction_replacement_successor
ON correction_proposal_replacement_item(successor_assertion_id, proposal_id);
