# Synapse — Clinical Knowledge Copilot: Technical Architecture

## 1. Implemented v1.3 baseline

Synapse is a local modular monolith with two runtime components: a browser and one Python/FastAPI process. SQLite is embedded in the application process. FastAPI serves both the JSON API and a framework-free HTML/CSS/JavaScript UI.

The current release uses only version-controlled synthetic fixtures and local mocked healthcare adapters. Deterministic Python code owns retrieval, evidence normalization, reconciliation, confidence, escalation, cited answer construction, feedback approval, and knowledge currentness. There is no LLM dependency, graph database, vector store, external healthcare connection, authentication system, or production compliance control.

All SQLite connections enable foreign keys. Writes performed through `managed_connection` commit on success and roll back on any exception.

## 2. Runtime components and boundaries

```text
Browser
  -> FastAPI endpoints and static frontend
  -> application services in backend/demo.py
  -> retrieval, reasoning, knowledge, and database modules
  -> SQLite
```

The responsibility boundaries are:

| Boundary | Question answered | Implemented responsibility |
|---|---|---|
| Retrieval | What source evidence was retrieved? | Plans and invokes five SQLite-backed mock adapters, returning a normalized `EvidenceBundle` or explicit failure status. |
| Knowledge | What evidence-backed assertions are persisted/current? | Reads persisted assertions, evidence provenance, current-applied state, and direct assertion lineage; governs assertion state and lineage writes. |
| Reasoning | What do the evidence/assertions imply together? | Normalizes retrieved evidence into in-memory decision assertions, detects supported conflicts, assigns categorical confidence, and decides escalation. |
| Governance | How can approved corrections change knowledge? | Requires pending feedback and an explicit reviewer action, then applies the fixed V1-to-V2 transition atomically. |
| Audit | What happened? | Persists ordered application events plus interaction, claim, citation, feedback, review, and update records. |
| Supervisor/answer | What supported result is presented? | Selects evidence-backed claims, validates citations, exposes conflicts and limitations, and renders a deterministic answer. |

## 3. Live question path

The live v1.3 question path is:

```text
Question
  -> deterministic intent classification
  -> RetrievalService
  -> EvidenceBundle
  -> deterministic Reasoning
  -> supported claims and citations
  -> persisted interaction snapshot
  -> Answer
```

For a supported intent, `RetrievalService` invokes adapters sequentially in this fixed order:

1. EHR
2. Guideline
3. Payer Policy
4. Formulary
5. Specialist Notes

Each adapter queries current, non-test source document versions in SQLite and returns immutable evidence with `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`, plus document/version identity and effective metadata. Special demo modes can make the payer source unavailable or substitute the isolated conflict formulary version.

Reasoning consumes the `EvidenceBundle`, not the persisted knowledge service. It recognizes only the bounded synthetic decision shapes used by the demo: clinical appropriateness, coverage authorization, and prerequisite requirements. It distinguishes a compatible guideline-versus-payer constraint from an opposing same-scope authorization claim. Confidence is deterministically `HIGH`, `MEDIUM`, or `LOW`; `LOW`, missing critical evidence, insufficient evidence, or an unresolved high-severity conflict requires escalation.

`KnowledgeService` is explicitly **not** in the live question path in v1.3.

## 4. V1.3 Knowledge Boundary

The knowledge layer is an **evidence-backed assertion and lineage knowledge layer persisted in SQLite**. This is the implemented foundation for the product's knowledge graph concept. It is not a generalized graph engine.

### 4.1 Persisted `KnowledgeAssertion`

`knowledge_assertion` stores:

- `assertion_id` and its `document_version_id`;
- `subject_id`, `predicate`, optional `object_id`, and JSON value;
- `decision_dimension` and optional normalized JSON scope;
- `recorded_at`, `effective_from`, and optional `effective_to`; and
- state: `CANDIDATE`, `APPLIED`, or `SUPERSEDED`.

The immutable Python `KnowledgeAssertion` contract recursively freezes JSON values and scope data and requires at least one supporting evidence ID when read.

### 4.2 `assertion_evidence`

`assertion_evidence` is the many-to-many provenance link between `knowledge_assertion` and `evidence_item`. Its composite primary key prevents a duplicate assertion/evidence pair. Both references are foreign-key backed.

Fixture seeding verifies that every linked evidence item belongs to the assertion's source document version. Provenance reads repeat that ownership check and also verify source identity, type, title, version, and timestamp consistency.

### 4.3 `assertion_lineage`

`assertion_lineage` records direct predecessor-to-successor assertion edges associated with applied feedback. It is separate from `assertion_supersession`, which records the source-document-version V1-to-V2 relationship.

The database prevents self-links and duplicate predecessor/successor pairs. Application validation requires applied feedback, correct target and proposed versions, the same logical document, and matching predicate, decision dimension, and normalized scope.

### 4.4 `KnowledgeRepository`

`KnowledgeRepository` provides:

- assertion lookup by ID;
- filtered queries by subject, predicate, decision dimension, state, payer, plan, medication, and indication;
- current-applied queries;
- assertion provenance reads;
- direct predecessor and successor lineage reads;
- governed assertion state transitions; and
- validated assertion-lineage creation.

Reads reject malformed required text, invalid JSON, invalid assertion states, missing evidence, cross-version evidence, and inconsistent document/evidence provenance with `KnowledgeDataError`.

### 4.5 `KnowledgeService`

`KnowledgeService` is the intent-revealing facade over the repository. It exposes the repository's required read operations and the two governed write operations used by approval: assertion state transition and assertion-lineage creation.

It is not a generalized correction or graph service.

### 4.6 Current-applied semantics

Current applied knowledge is defined exactly as:

```text
knowledge_assertion.state = 'APPLIED'
AND source_document_version.is_current = 1
```

The query joins assertions to their source document versions and filters both conditions. Optional normalized-scope filters are then applied to the decoded assertion scope.

This does not constitute generalized temporal knowledge. `effective_from` and `effective_to` are persisted assertion data, but the knowledge repository does not accept an as-of instant or use those fields to establish runtime temporal authority.

### 4.7 Provenance chain

```text
source_document
  -> source_document_version
  -> evidence_item
  -> assertion_evidence
  -> knowledge_assertion
  -> KnowledgeRepository / KnowledgeService
  -> assertion / provenance / lineage / current-applied queries
```

The returned `AssertionProvenance` contains the assertion, all linked evidence items, logical document and version identity, source identity/type/title, source-issued timestamp, recorded time, and effective fields.

### 4.8 Governed write boundary and transaction ownership

Knowledge writes require a `KnowledgeRepository` constructed with a caller-provided SQLite connection. The repository never commits, rolls back, or replaces that connection. The approval application service owns the transaction.

Only these assertion state transitions are allowed:

```text
CANDIDATE -> APPLIED
APPLIED -> SUPERSEDED
```

All other transitions are rejected.

## 5. Governance semantics

The implemented correction is the fixed, pre-seeded payer-policy V1-to-V2 transition.

Before approval:

| Version | Source document version | Knowledge assertions |
|---|---|---|
| V1 | current | `APPLIED` |
| V2 | noncurrent | `CANDIDATE` |

Submitting feedback records it as `PENDING`; it does not change source or assertion currentness.

After approval:

| Version | Source document version | Knowledge assertions |
|---|---|---|
| V1 | noncurrent | `SUPERSEDED` |
| V2 | current | `APPLIED` |

One caller-owned SQLite transaction performs all approval mutations:

1. verify that feedback is `PENDING`;
2. mark V1 noncurrent and V2 current/applied at the document level;
3. transition V1 assertions to `SUPERSEDED`;
4. transition V2 assertions to `APPLIED`;
5. mark feedback `APPLIED` and record its application time;
6. insert the approved review;
7. insert document-version lineage;
8. insert assertion lineage for the authorization and prerequisite assertion pairs;
9. insert the knowledge-update record; and
10. append review and knowledge-update audit events.

If any operation raises, `managed_connection` rolls back the entire transaction. The system does not commit a partial V1/V2 transition or an approved-but-not-applied state.

## 6. Integrity boundaries

### Database-enforced

- foreign keys;
- assertion state `CHECK` for `CANDIDATE`, `APPLIED`, or `SUPERSEDED`;
- assertion/evidence pair uniqueness;
- assertion-lineage foreign keys;
- assertion-lineage self-link prevention;
- duplicate assertion-lineage prevention; and
- one current source document version per logical document through a partial unique index.

### Application-enforced

- permitted assertion state transitions;
- approval workflow status and synthetic reviewer role;
- assertion-lineage compatibility and feedback/version ownership;
- assertion/evidence ownership by the same document version;
- malformed persisted-data handling; and
- orchestration of the complete approval transaction.

### Not implemented

- tamper-evident history; and
- generalized graph cycle prevention.

## 7. Persistence, schema, and reset

The required SQLite schema version is **3**.

| Identifier | Value |
|---|---|
| Foundation baseline | `foundation-empty-v3` |
| Demo baseline | `synthetic-pa-v1` |
| SQLite `user_version` | `3` |

Startup initializes an empty database from `backend/schema.sql`, verifies the schema version and foundation metadata, applies the demo tables from `backend/demo_schema.sql`, and seeds them from version-controlled fixtures when no source versions exist.

A database with a nonzero version other than 3 is rejected. Schema-v2 databases are not migrated automatically. No in-place migration framework exists.

Reset/rebuild is the supported local MVP path. Reset creates a temporary database in the configured data directory, initializes schema v3, creates demo tables, seeds synthetic fixtures, validates schema metadata, foreign-key enforcement, foreign-key integrity, and SQLite integrity, then atomically replaces the configured database. A failed reset removes its temporary files and preserves the prior database.

## 8. Historical interaction snapshots

Completed interactions persist their question, intent, source mode, selected sources, retrieval trace, answer text, confidence and rationale, reconciliation, escalation, policy version, supported claims, and citation metadata.

Historical interaction snapshots remain distinct from current knowledge. An interaction generated under V1 retains its V1 answer, claims, and citations after V2 becomes current. A new interaction retrieves the now-current V2 evidence. Approval never rewrites the prior interaction or its cited evidence.

## 9. HTTP boundary

The currently implemented endpoints are:

| Method and path | Purpose |
|---|---|
| `GET /` | Serve the static demo UI. |
| `GET /api/health` | Return basic application health. |
| `GET /api/preflight` | Report required database/foreign-key/data-directory checks and optional FTS5 capability. |
| `POST /api/demo/reset` | Rebuild the synthetic baseline when demo mode and the confirmation token permit it. |
| `POST /api/v1/questions` | Run a supported question or return explicit unsupported scope. |
| `POST /api/v1/feedback` | Record the fixed synthetic correction as pending. |
| `POST /api/v1/feedback/{feedback_id}/approve` | Apply the governed correction transaction. |
| `GET /api/v1/interactions/{interaction_id}` | Return the persisted interaction snapshot. |
| `GET /api/v1/audit/{interaction_id}` | Return ordered audit events linked to an interaction and its feedback. |

No API for arbitrary assertion mutation, arbitrary document upload, rejection, generic feedback review, graph traversal, external integration, or production authentication is implemented.

## 10. Implemented and deferred capabilities

### Implemented

- local deterministic synthetic evidence retrieval across five mocked sources;
- explicit missing, unavailable, and malformed source states;
- evidence-backed claims and claim-level citations;
- compatible-constraint and same-dimension-conflict handling;
- categorical confidence and mandatory escalation rules;
- first-class persisted assertions, evidence links, provenance, and direct lineage;
- currentness integrity across assertion state and source document currentness;
- governed V1-to-V2 persistence with atomic approval; and
- locally persisted interaction and governance history.

### Deferred / future

- generalized as-of selection;
- temporal authority;
- generalized multi-hop lineage traversal;
- generic cycle detection;
- arbitrary correction workflows;
- graph database;
- ontology/RDF;
- vector retrieval;
- LLM reasoning;
- arbitrary document uploads;
- external healthcare integrations;
- authentication and production authorization; and
- production compliance, privacy, security, retention, resilience, and tamper-evidence controls.

The deferred items must not be inferred from the persisted effective fields, lineage records, modular boundaries, or knowledge-graph terminology. Synapse remains a synthetic decision-support prototype, not a clinically validated or production-ready system.
