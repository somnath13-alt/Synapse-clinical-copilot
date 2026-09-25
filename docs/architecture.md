# Synapse — Clinical Knowledge Copilot: Architecture

## 1. Scope and current posture

Synapse is an executable local hackathon prototype using only synthetic healthcare data. It supports a bounded medication prior-authorization workflow and demonstrates evidence retrieval, conflict-aware synthesis, categorical confidence, escalation, governed corrections, and preserved history.

The application is a FastAPI modular monolith with a static browser client and embedded SQLite persistence. External EHR, guideline, payer, formulary, and specialist-note systems are mocked behind source-specific adapters. No LLM, graph database, vector store, external healthcare integration, or production identity/compliance control is present.

Synapse is decision support. It does not diagnose, prescribe, determine actual coverage, or make autonomous care decisions.

## 2. Responsibility boundaries

The current architecture keeps six questions separate:

| Boundary | Responsibility | Current implementation |
|---|---|---|
| Retrieval | **What source evidence was retrieved?** | `RetrievalService` invokes five SQLite-backed mock adapters in a fixed order and returns an `EvidenceBundle` with success/failure status and complete provenance. |
| Knowledge | **What evidence-backed assertions are persisted/current?** | `KnowledgeAssertion`, `assertion_evidence`, and `assertion_lineage` are persisted in SQLite and exposed through `KnowledgeRepository` and `KnowledgeService`. |
| Reasoning | **What do the evidence/assertions imply together?** | Deterministic normalization, reconciliation, confidence, and escalation policies reason over the live `EvidenceBundle`. They do not currently consume `KnowledgeService`. |
| Governance | **How can approved corrections change knowledge?** | A pending, pre-seeded V2 correction requires explicit approval. Approval updates document authority and assertion states, creates lineage, and records review/update/audit data in one transaction. |
| Audit | **What happened?** | SQLite retains ordered audit events and the persisted interaction, claims, and citation snapshot. This is application-level history, not tamper-evident storage. |
| Supervisor/answer | **What supported result is presented?** | Deterministic application logic emits only evidence-backed claims, shows conflicts and limitations, assigns `HIGH`, `MEDIUM`, or `LOW`, and creates required escalation output. |

These boundaries prevent source retrieval, persisted authority, synthesis, governance, and historical reporting from being collapsed into one opaque “graph” operation.

## 3. Live question path

The implemented question path is:

```text
Question
  -> intent classification
  -> RetrievalService
  -> five mocked source adapters
  -> EvidenceBundle
  -> deterministic reasoning and reconciliation
  -> confidence and escalation policies
  -> supported claims and citations
  -> persisted interaction snapshot
  -> answer
```

Every material answer claim maps to evidence retrieved for that interaction. The answer distinguishes a guideline's clinical recommendation from a payer's coverage, prior-authorization, step-therapy, or documentation rules. An unavailable critical source is reported as unavailable; its contents are never inferred.

`KnowledgeService` is **not** in this path in v1.3. Retrieval selects rows where the source document version is marked current and builds the `EvidenceBundle`; reasoning consumes that bundle. The knowledge layer provides a parallel, consistent read boundary and the governed write boundary described below.

## 4. V1.3 knowledge layer

The v1.3 knowledge layer is an **evidence-backed assertion and lineage knowledge layer persisted in SQLite**. This is the implemented foundation of the product's knowledge graph concept. It is not a generalized graph engine.

```text
Source document and version
  -> evidence_item
  -> assertion_evidence
  -> knowledge_assertion
  -> KnowledgeRepository / KnowledgeService
  -> assertion, provenance, current-applied, predecessor, and successor reads
```

A `KnowledgeAssertion` preserves assertion and source-document-version identity; subject, predicate, optional object, and JSON value; decision dimension and normalized scope; `recorded_at`, `effective_from`, and optional `effective_to`; `CANDIDATE`, `APPLIED`, or `SUPERSEDED` state; and one or more supporting evidence IDs.

`assertion_evidence` is the many-to-many provenance link. A provenance read joins assertion evidence to immutable evidence, source-document-version, and source-document metadata and rejects malformed or internally inconsistent persisted data.

`assertion_lineage` records one predecessor-to-successor assertion edge associated with applied feedback. It remains distinct from `assertion_supersession`, which records the V1-to-V2 source-document-version transition. The service exposes one-hop predecessor and successor reads; generalized traversal is deferred.

Current applied knowledge has one exact definition:

```text
knowledge_assertion.state = 'APPLIED'
AND source_document_version.is_current = 1
```

`effective_from` and `effective_to` are persisted assertion fields, but the knowledge repository does not implement generalized as-of or temporal-authority selection.

## 5. Governance and atomic transition

Before approval:

| Version | Document status | Assertion state |
|---|---|---|
| V1 | current | `APPLIED` |
| V2 | noncurrent | `CANDIDATE` |

After approval:

| Version | Document status | Assertion state |
|---|---|---|
| V1 | noncurrent | `SUPERSEDED` |
| V2 | current | `APPLIED` |

The approval operation uses a single caller-owned `managed_connection` transaction. `KnowledgeService` delegates state transitions and assertion-lineage creation to a repository bound to that connection; the repository does not commit, roll back, or substitute its own transaction.

The same transaction changes current document-version flags, transitions V1 and V2 assertions, marks feedback `APPLIED`, inserts the approval review, creates document-version and assertion lineage, inserts the knowledge update, and appends governance audit events. Any exception rolls the entire transition back. No partially approved or partially applied state is committed.

Submission alone leaves V1 current and V2 candidate. The current demo implements the pre-seeded V1-to-V2 payer-policy correction path; it does not implement arbitrary correction workflows or document ingestion.

## 6. Integrity boundaries

### Database-enforced

- foreign keys for assertion, evidence, feedback, and lineage references;
- assertion state `CHECK` limiting values to `CANDIDATE`, `APPLIED`, and `SUPERSEDED`;
- uniqueness of each assertion/evidence pair;
- assertion-lineage foreign keys;
- assertion-lineage self-link prevention;
- duplicate predecessor/successor lineage prevention; and
- at most one current version per logical source document through a partial unique index.

### Application-enforced

- permitted assertion state transitions;
- lineage predicate, decision-dimension, normalized-scope, document, feedback, and version compatibility;
- approval workflow status and reviewer role;
- assertion/evidence ownership by the same document version during seeding and provenance reads;
- malformed persisted-data handling; and
- orchestration of the complete approval transaction.

### Not implemented

- tamper-evident history; and
- generalized graph cycle prevention.

## 7. Historical interactions versus current knowledge

An interaction is a persisted historical snapshot, not a live view. It retains the question, answer text, confidence, reconciliation, escalation, supported claims, and citation metadata created at that time.

Consequently, an interaction generated under V1 retains its V1 answer, claims, and citations after approval makes V2 current. A later interaction retrieves V2. Knowledge currentness changes future reads; it does not rewrite prior interactions or evidence.

## 8. Planned v1.4 temporal authority contract

This section defines the approved semantics for Milestone 5 implementation. It is a contract for v1.4 work, not a description of behavior already available in v1.3.

### 8.1 Three distinct temporal operations

| Operation | Question answered | Authority behavior |
|---|---|---|
| **Current query** | “What does Synapse consider current now?” | Use the existing v1.3 current flags and assertion state. Omitting an explicit temporal request preserves this behavior. |
| **As-of query** | “What governance-eligible source or knowledge was applicable at time T?” | Select all governance-eligible versions and assertions whose effective intervals contain `as_of`. |
| **Historical interaction snapshot** | “What did Synapse actually answer during a previous interaction?” | Load the persisted interaction, answer, claims, citations, confidence, reconciliation, and escalation snapshot. Do not rerun a current or as-of selector. |

For a current source query, authority remains `source_document_version.is_current = 1`. For current knowledge, authority remains exactly:

```text
knowledge_assertion.state = 'APPLIED'
AND source_document_version.is_current = 1
```

For an as-of query, `as_of` is **applicability time**. It is not query execution time, clinical encounter time, historical interaction lookup time, or an approval timestamp. A document version or assertion is temporally applicable when:

```text
effective_from <= as_of <= effective_to
```

The intervals are closed and expressed in UTC. A null `effective_to` is open-ended. As-of selection does not use `is_current`, “latest version,” “highest version,” or approval time as a precedence rule.

A historical interaction snapshot is immutable historical output. A retroactive correction can change the result of a new as-of query, but it must never rewrite a previously persisted interaction.

### 8.2 Governance eligibility and temporal applicability

Temporal applicability alone is insufficient. As-of results require both:

```text
governance eligibility
AND temporal applicability
```

Governance decides whether a source version or assertion has passed the required review/application boundary. For the planned selectors, a source document version must be in the governed `APPLIED` set; an assertion must be approved/applied history (`APPLIED` or retained `SUPERSEDED`) and belong to a governance-eligible source version. Candidate or pending knowledge is not governance-eligible merely because its effective interval contains `as_of`. A superseded assertion remains evidence of previously approved/applied knowledge and may participate in an as-of query within its effective interval; supersession does not convert it back into an unreviewed candidate. Effective dates never bypass human approval where review is required.

Governance eligibility is not a transaction-time cutoff. Once a retroactive correction is approved, a new as-of query may use it for an earlier applicability time covered by its approved interval. The approval, review, application, record, and effective timestamps remain distinct.

### 8.3 Overlaps and reconciliation

If multiple governance-eligible source versions or assertions contain the same `as_of`, retrieval and knowledge selection return **all applicable results**. They do not silently choose a winner or invent temporal precedence.

Reasoning evaluates the complete applicable set as compatible, agreeing, or conflicting evidence. Same-scope, same-dimension opposing conclusions remain an unresolved high-severity conflict: preserve evidence from both sides, assign `LOW` confidence, and require human escalation. Reasoning reconciles applicable evidence; it does not decide which version is temporally authoritative.

### 8.4 Document and assertion intervals

A document interval states when a source document version applies. An assertion interval states when its normalized proposition applies and may be narrower than the source document interval.

The approved v1.4 implementation invariant is that an assertion's effective interval must be contained within its source document version's interval. Commit A records this invariant only; validation or enforcement belongs to later implementation work.

The temporal fields have noninterchangeable meanings:

- `recorded_at`: when Synapse learned or recorded the object;
- `effective_from` / `effective_to`: when the object is applicable; and
- `reviewed_at` / `applied_at`: when governance events occurred.

### 8.5 Authority boundaries

| Boundary | Temporal responsibility |
|---|---|
| Governance | Decide whether a version or assertion is eligible. |
| Retrieval | Select all applicable source versions for current or as-of mode. |
| Knowledge | Select all applicable persisted assertions for current or as-of operations. |
| Reasoning | Reconcile all selected evidence and assertions without choosing temporal authority. |
| Historical interaction retrieval | Return the stored answer snapshot without reselection or recomputation. |

Neither retrieval nor reasoning may infer that a newer or higher-numbered version wins.

### 8.6 Canonical V1/V2 timeline

| Event or query | Expected result |
|---|---|
| V1 effective `2026-01-01T00:00:00Z` through `2026-06-30T23:59:59Z` | V1 is applicable throughout the closed interval. |
| V2 effective from `2026-07-01T00:00:00Z`; approval/application on `2026-07-02` | V2 is temporally applicable starting July 1 but is excluded before approval because it is not governance-eligible. |
| Before V2 approval: current query | V1. |
| Before V2 approval: as-of query after July 1 | V2 remains excluded; effective dates do not bypass governance. |
| After V2 approval: current query | V2. |
| After V2 approval: as-of `2026-06-15` | V1. |
| After V2 approval: as-of `2026-07-03` | V2. |
| Historical interaction originally answered June 15 | Return the stored V1 answer regardless of V2's later approval. |

This distinguishes “What applies now when we ask about T?” from “What did Synapse actually say at T?”

### 8.7 Unresolved future-effective approval behavior

Approval of a version before its `effective_from` remains an implementation-planning decision:

- **Option A:** reject activation before `effective_from`; or
- **Option B:** allow an approved-but-not-current state and activate it later.

Neither option is implemented or selected by this contract. The unresolved choice does not block work on the current/as-of selector foundation.

### 8.8 Current v1.3 defect and reproducibility limits

V1.3 does not implement the selector contract above. After V2 approval, a `RetrievalRequest` carrying a June 15 `as_of` still retrieves V2 because adapters select `is_current = 1`. Reasoning then rejects V2 as temporally inapplicable but cannot fall back to V1. Milestone 5 will resolve this temporal-selection defect.

Historical display is supported in v1.3, but independent deterministic replay is not yet fully self-contained. Persisted snapshots do not yet carry all identities needed for replay, including the complete retrieved evidence-ID set, the confidence/reasoning policy identity, and self-contained citation source/document-version IDs. Capturing those identities is later v1.4 work; this documentation commit does not change the schema.

## 9. Persistence, schema, and reset

SQLite schema version **3** is required. Foundation metadata identifies `foundation-empty-v3`, and the deterministic seeded demo identifies `synthetic-pa-v1`.

An empty database is initialized at v3. A nonzero schema version other than 3 is rejected, so schema-v2 databases are not migrated automatically. There is no in-place migration framework. Reset/rebuild from the schema and version-controlled synthetic fixtures is the supported local MVP path.

Reset builds and seeds a temporary v3 database, verifies metadata, foreign keys, foreign-key integrity, and SQLite integrity, then atomically replaces the configured database. If construction or validation fails, the existing database is preserved.

## 10. Implemented versus deferred

### Implemented in v1.3

- deterministic five-source retrieval over local synthetic fixtures;
- normalized evidence with mandatory provenance;
- first-class persisted assertions and evidence links;
- one-hop assertion lineage and separate document-version lineage;
- typed repository/service reads for assertions, provenance, current-applied knowledge, predecessors, and successors;
- currentness integrity across document and assertion state;
- governed V1-to-V2 correction persistence with atomic approval;
- deterministic conflict, confidence, and escalation behavior; and
- locally persisted interactions, claims, citations, feedback, reviews, updates, and audit events.

### Planned for v1.4, not yet implemented

- the bounded `CURRENT` and `AS_OF` selector contract defined in Section 8; and
- later snapshot-identity work needed for independently reproducible replay.

### Deferred beyond the bounded v1.4 selector contract

- a generic temporal framework;
- generalized multi-hop graph traversal and generalized cycle detection;
- arbitrary correction workflows and arbitrary document uploads;
- a graph database or generalized graph query engine;
- ontology/RDF support;
- vector retrieval and embeddings;
- LLM reasoning or phrasing;
- external healthcare integrations;
- authentication and production authorization; and
- production privacy, security, compliance, retention, resilience, and tamper-evidence controls.

Any future production work requires separate product, clinical, security, privacy, legal, and operational review.
