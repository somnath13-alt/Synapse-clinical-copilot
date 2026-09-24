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
    created_at TEXT NOT NULL
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
    source_title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    section TEXT NOT NULL,
    relevant_excerpt TEXT NOT NULL
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
