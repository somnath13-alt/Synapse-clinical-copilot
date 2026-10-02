# Synapse — Clinical Knowledge Copilot: Technical Architecture

## 1. Runtime and component boundary

Synapse M7 is a local modular monolith. FastAPI serves a framework-free static UI and JSON API; SQLite stores synthetic source documents, evidence, knowledge assertions, interactions, correction proposals, feedback, reviews, lineage, updates, and audit events. All connections enable foreign keys, and `managed_connection` commits on success or rolls back on exception.

```text
Browser
  -> FastAPI (`backend/main.py`)
  -> application orchestration (`backend/demo.py`)
  -> Retrieval + Knowledge + Reasoning
  -> SQLite
```

The five SQLite-backed mocked adapters run in fixed order: EHR, Guideline, Payer Policy, Formulary, Specialist Notes. There is no LLM, graph database, vector store, network healthcare integration, authentication system, or production compliance control.

## 2. Temporal request contract and public API

The internal retrieval contract has exactly two modes:

```text
TemporalMode.CURRENT
TemporalMode.AS_OF
```

`RetrievalRequest` defaults to CURRENT. AS_OF requires a valid ISO-8601 timestamp with an explicit UTC offset; accepted values are normalized to canonical `Z` form. Naive, malformed, and non-UTC values are rejected.

`POST /api/v1/questions` accepts:

```json
{
  "question": "Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?",
  "as_of": "2026-06-15T14:00:00Z"
}
```

`as_of` is optional. Omission means CURRENT; a valid explicit value means AS_OF. Invalid values produce HTTP 422. `temporal_mode` is not a public request field. Existing optional demo `source_mode` behavior and the response shape remain compatible.

## 3. Source selection

`RetrievalService.retrieve` dispatches each adapter by the request mode.

### CURRENT

Adapters query non-test source versions where `is_current = 1`, then return evidence ordered deterministically by document-version ID and evidence ID.

### AS_OF

Adapters query non-test source versions where `governance_state = 'APPLIED'`. Persisted effective timestamps are parsed and validated, including interval ordering. A version is applicable when:

```text
document.effective_from <= as_of
AND (document.effective_to IS NULL OR as_of <= document.effective_to)
```

All applicable versions are returned in deterministic order. The selector does not use currentness, version-label ordering, recency, `recorded_at`, or approval time as precedence and never falls back to CURRENT.

No applicable governed version returns `RetrievalStatus.NOT_FOUND`. Invalid stored temporal/source data returns `RetrievalStatus.MALFORMED`.

## 4. Knowledge selection and provenance

The v1.3 `KnowledgeAssertion`, `assertion_evidence`, and `assertion_lineage` model remains intact. `KnowledgeRepository` validates required text, JSON, assertion state, evidence ownership, document/evidence provenance, and lineage. Invalid persisted knowledge raises `KnowledgeDataError`.

Two intent-revealing operations define temporal knowledge reads:

```text
get_current_applied_assertions(query)
get_applicable_assertions(query with as_of)
```

Current-applied selection requires:

```text
knowledge_assertion.state = 'APPLIED'
AND source_document_version.is_current = 1
```

Applicable AS_OF selection requires:

```text
knowledge_assertion.state IN ('APPLIED', 'SUPERSEDED')
AND source_document_version.governance_state = 'APPLIED'
AND assertion interval contains as_of
AND source-document interval contains as_of
```

`CANDIDATE` is excluded. A `SUPERSEDED` assertion is not current, but remains eligible for a historical interval it governed. The repository also verifies non-inverted intervals and that each assertion interval is contained within its source-document interval.

Retrieval and Knowledge are independent peer boundaries; neither calls the other.

## 5. Assertion-level applicability and dual-input reasoning

For AS_OF, application orchestration first retains the complete retrieval bundle. It separately asks Knowledge for matching assertion families and their applicable assertions, then builds a reasoning-only bundle that keeps evidence not governed by those families and only evidence linked to applicable governed assertions within a governed family.

For the live prior-authorization path in both CURRENT and AS_OF modes, orchestration then builds an immutable `ReasoningInput` at the post-selection boundary:

```text
Retrieval -> SourceObservation -----------+
                                            +-> ReasoningInput -> deterministic comparison
Knowledge -> GovernedBaselineAssertion ---+                    -> confidence/escalation
                                                                  -> answer composition
```

`SourceObservation` is a payer conclusion projected only from retrieved evidence. `GovernedBaselineAssertion` is a validated, non-candidate persisted payer assertion plus execution-time value, scope, effective facts, source/document provenance, evidence IDs, and lineage/correction identities. Knowledge is a comparison baseline and has no automatic precedence. `ReasoningInput` carries both independently selected channels and the temporal context; Reasoning does not retrieve, select temporal authority, or mutate Knowledge.

`compare_reasoning_input` is pure and deterministic. Exact same-dimension comparison requires matching decision type, medication, indication, payer, and plan; missing scope values are not wildcards. Equivalent values corroborate, opposing Boolean values conflict or form a stale-knowledge disagreement only when persisted timestamps prove the source is newer, and supported clinical plus payer conclusions can be compatible cross-dimension constraints. Reordered inputs produce deterministic results.

This division is deliberate:

- Retrieval decides which source/document evidence is applicable.
- Knowledge decides which persisted assertions are applicable.
- Orchestration composes an applicable evidence view.
- Reasoning reconciles that view; it does not decide temporal authority.

An applicable source document may contain a narrower assertion that is not applicable at the requested instant. That assertion's evidence remains in the raw retrieved interaction universe but does not materially influence reasoning.

## 6. Overlap composition and safe failure

Selectors return all applicable versions/assertions. They never rank overlaps.

Reasoning can represent opposing same-scope, same-dimension conclusions. It preserves both, records an unresolved high-severity conflict, returns `LOW`, and requires escalation.

The bounded payer answer path cannot safely represent every non-opposing multi-version case. If multiple applicable payer document versions survive applicability composition without a representable conflict, `TemporalCompositionError` causes HTTP 409 with no persisted interaction. No winner is invented. Generalized multi-version rendering is not implemented.

## 7. Pending, approved, retroactive, and future-effective behavior

| State | CURRENT | AS_OF June 15 | AS_OF July 3 |
|---|---|---|---|
| V2 `PENDING` / assertions `CANDIDATE` | V1 | V1 | No applicable payer version; safe `LOW` + escalation |
| V2 approved/applied | V2 | V1 | V2 |

An approved retroactive correction may alter a newly executed AS_OF query for its historical interval. It never changes an already persisted interaction. If governed intervals overlap, all applicable versions remain visible and normal overlap handling applies.

The governed approval transaction marks V1 noncurrent/SUPERSEDED and V2 current/APPLIED only when the proposed document and every successor assertion are effective by the decision timestamp. An early attempt returns `FUTURE_EFFECTIVE`, leaves the proposal `PENDING` and V1 current, and creates no application records. AS_OF continues to honor intervals. No approved-but-not-yet-current state or activation scheduler is introduced.

## 8. Interaction snapshot retained in schema v7

`interaction` stores the pre-v1.4 answer context plus:

- `temporal_mode`: `CURRENT` or `AS_OF`;
- `requested_as_of`: null for CURRENT and canonical UTC for AS_OF; and
- `confidence_policy_id`: the deterministic confidence policy identity.

A database check enforces the temporal mode/requested-time pairing.

`interaction_evidence` records the complete retrieved evidence universe with `(interaction_id, evidence_id, ordinal)`. Ordinals are deterministic, zero-based, and unique per interaction. Evidence membership is foreign-key backed.

`citation` records execution-time `source_id`, `document_version_id`, `evidence_id`, and display provenance. A composite foreign key requires every citation's evidence to belong to that interaction's retrieved universe.

The identities are not interchangeable:

```text
retrieved evidence != claim evidence != citation evidence
```

The complete retrieved set is stored even when some evidence supports no material claim. Claim evidence and citation evidence are subsets.

Interaction, evidence membership/order, supported claims, and citations are written in one transaction. Later governance changes do not mutate temporal identity, policy identity, evidence membership/order, claims, citations, citation source/document identity, answer, confidence, or escalation.

`interaction_knowledge` records every selected governed assertion that participated in the execution, with deterministic zero-based order and an execution-time snapshot of:

- assertion identity and `KNOWLEDGE` or `CORROBORATED` role;
- state at execution, decision type, value, and normalized scope;
- assertion and source-document effective intervals plus assertion `recorded_at`;
- source, document, and document-version identity; and
- lineage and correction identities.

`interaction_knowledge_evidence` records each participating assertion's evidence IDs in deterministic zero-based order. The evidence remains foreign-key backed, but the mutable assertion row is not used to reconstruct historical state. Candidates are not valid governed snapshot rows. Knowledge snapshot writes occur in the same transaction as the interaction, retrieved-evidence membership, claims, and citations, so a downstream failure rolls back the whole answer.

`GET /api/v1/interactions/{interaction_id}` reads the stored interaction, claims, and citations. It does not rerun retrieval, knowledge selection, reasoning, or temporal selection.

Historical display is supported, and later approval or mutation of live assertion state does not change the execution-time knowledge snapshot. Independent deterministic replay is **not implemented**: there is no replay executor, and snapshot completeness supports historical explanation rather than a guarantee that arbitrary executions can be rerun.

## 9. Confidence and failure mapping

The live prior-authorization path uses deterministic knowledge-aware policy `CONF-PA-SYN-V2`; the preserved source-only policy remains `CONF-PA-SYN-V1`. Neither is a probability. V2 starts from the V1 source assessment, cannot inflate incomplete source evidence, and adds source/knowledge interpretation. Evidence criticality matters:

- unavailable applicable payer evidence: `LOW` and escalation;
- a governed payer baseline without comparable current source verification: `LOW` and escalation;
- a same-dimension payer source/knowledge conflict, including a provably newer source disagreement: `LOW` and escalation with no winner;
- source/knowledge corroboration: preserve both chains without confidence inflation;
- unresolved high conflict: `LOW` and escalation;
- missing guideline or formulary evidence: may be `MEDIUM`; and
- missing optional specialist evidence: may remain `HIGH`.

| Boundary result | Public/application behavior |
|---|---|
| Invalid public `as_of` | HTTP 422. |
| Retrieval `NOT_FOUND` | No governed applicable source version; critical payer absence follows the safe `LOW` path. |
| Retrieval `MALFORMED` | Invalid temporal/source persistence; safe evidence-free behavior. |
| Multiple unrenderable payer versions | HTTP 409; no interaction persisted. |
| Critical insufficiency or unresolved conflict | `LOW` and required escalation. |
| `KnowledgeDataError` | Invalid persisted knowledge temporal state; safe evidence-free `LOW` path in question orchestration. |

No failure path silently substitutes current evidence for an AS_OF request.

## 10. Persistence, schema, and reset

| Identifier | Value |
|---|---|
| Schema version | `7` |
| Foundation baseline | `foundation-empty-v7` |
| Demo baseline | `synthetic-pa-v1` |
| SQLite `user_version` | `7` |

Startup initializes an empty database from `backend/schema.sql`, applies `backend/demo_schema.sql`, seeds version-controlled synthetic fixtures, and validates metadata. Any other nonzero version is rejected, explicitly including schema v6. There is no in-place migration framework.

Reset/rebuild is required for existing local databases and is the supported MVP upgrade path. Reset builds and validates a temporary schema-v7 database, including foreign-key and SQLite integrity, then atomically replaces the configured database. Failure preserves the prior database.

## 11. Implemented and deferred boundary

Implemented through M7:

- CURRENT and AS_OF source selection;
- current and AS_OF knowledge selection;
- public optional `as_of` validation;
- assertion-level applicability composition;
- overlap/no-winner behavior;
- deterministic source confidence and escalation;
- live dual-input prior-authorization reasoning with `SourceObservation`, `GovernedBaselineAssertion`, and `ReasoningInput`;
- deterministic source/knowledge corroboration, source-only, knowledge-only, stale-disagreement, conflict, compatible-constraint, missing-source, malformed-knowledge, and governance-pending classifications;
- `CONF-PA-SYN-V2` confidence and escalation for the knowledge-aware PA path;
- temporal plus execution-time knowledge snapshots and historical display;
- atomic snapshot persistence and historical snapshot immutability; and
- first-class governed correction proposals, normalized replacement persistence, approval, rejection, deterministic failure behavior, guarded concurrency, and audit facts.

Deferred:

- independent deterministic replay;
- a generalized temporal framework;
- early approval with an approved-but-not-yet-current governance state or activation scheduler;
- generalized multi-version payer rendering;
- generalized graph traversal;
- graph database and ontology/RDF support;
- vector or LLM reasoning;
- arbitrary uploads, correction workflows, and external healthcare integrations; and
- production authentication, authorization, compliance, privacy, security, resilience, retention, and tamper evidence.

The v1.3 knowledge design remains release history: it established assertions, evidence provenance, lineage, current-applied semantics, and atomic governed writes. V1.4 extends those boundaries rather than rewriting that history.

## 12. Implemented M6 dual-input technical boundary

M6 is live on the prior-authorization question path. Application orchestration independently selects retrieval and governed-knowledge inputs for CURRENT and AS_OF, constructs `ReasoningInput`, runs deterministic source/knowledge comparison, evaluates `CONF-PA-SYN-V2`, composes safe output, and atomically persists the interaction plus execution-time knowledge participation.

The implemented internal flow is:

```text
Retrieval -> SourceObservation -----------+
                                            +-> orchestration-owned ReasoningInput
Knowledge -> GovernedBaselineAssertion ---+     -> deterministic comparison/findings
                                                  -> confidence/escalation
                                                  -> answer composition
```

`SourceObservation` means a conclusion observed from source evidence selected by Retrieval. `GovernedBaselineAssertion` means a reviewed/governed persisted assertion selected by Knowledge; baseline is a comparison point and confers no precedence. The orchestration-owned `ReasoningInput` carries temporal context, retrieved evidence, selected governed assertions, resolved assertion provenance, and deterministic origin/selection metadata.

Implemented origins are `SOURCE`, `KNOWLEDGE`, and `CORROBORATED`. Corroboration requires equivalent same-scope support from both inputs and retains both provenance chains. Comparison contracts represent `CORROBORATION`, `SOURCE_ONLY`, `KNOWLEDGE_ONLY`, `STALE_KNOWLEDGE_DISAGREEMENT`, `SAME_DIMENSION_CONFLICT`, `COMPATIBLE_CROSS_DIMENSION_CONSTRAINT`, `MISSING_SOURCE_CHANNEL`, `MALFORMED_KNOWLEDGE_CHANNEL`, and `GOVERNANCE_PENDING`.

Selection remains outside Reasoning. CURRENT source and assertion selection and AS_OF source/assertion interval selection retain the rules in sections 3 and 4. `CANDIDATE` is never an authoritative input; `SUPERSEDED` is excluded from CURRENT but can be historically eligible in AS_OF. Retrieval and Reasoning never write Knowledge, and source evidence cannot bypass the caller-owned correction/approval transaction.

The implementation must compare only compatible scopes and dimensions. Clinical appropriateness and payer authorization are distinct dimensions, so recency across them creates no override. Opposing same-scope, same-dimension conclusions retain both inputs and provenance with no winner. Critical payer source unavailability cannot be repaired by knowledge: a last governed payer baseline may be displayed as unverified, but confidence remains `LOW` and escalation remains mandatory.

`CONF-PA-SYN-V2` is active for the knowledge-aware PA path. It preserves the V1 source completeness/provenance assessment, adds comparison-specific rationale and downgrades, and never treats corroboration as permission to manufacture `HIGH`. Schema v5 persists selected assertion identity, deterministic order, origin/role, execution-time state, value, scope, effective facts, provenance, lineage/correction identities, and ordered evidence membership; an assertion foreign key alone is not treated as a historical snapshot.

The existing relational `knowledge_assertion`, `assertion_evidence`, and `assertion_lineage` model plus the schema-v5 interaction snapshot tables are sufficient for M6. Graph databases, RDF/ontology, generalized traversal/cycle detection, embeddings, vector retrieval, LLM reasoning, probabilistic arbitration, and an independent replay engine remain deferred.

## 13. M7 governed correction lifecycle technical boundary — Implemented

M7 adds governance contracts, persistence, service policy, and live API/demo integration without changing the M6 retrieval, knowledge selection, reasoning comparison, confidence policy, or interaction-snapshot semantics.

### 13.1 Immutable conceptual contracts

The correction aggregate is an atomic assertion replacement set with:

- canonical `proposal_id` and optional linked public compatibility `feedback_id`;
- target and proposed document-version identities;
- one or more ordered or otherwise explicitly identified replacement items;
- submitter actor and role, rationale, status, and timestamps; and
- for each item, an explicit predecessor assertion ID and proposed successor assertion ID plus immutable proposal-time semantic and provenance facts.

The whole document version is not the sole semantic correction unit, and M7 permits no partial item decision. `CorrectionProposal` snapshots predicate, decision dimension, normalized scope, predecessor/successor values, effective intervals, document identities, and ordered evidence membership for every normalized mapping. Each pair validates logical document family, compatible replacement semantics, evidence/provenance, effective intervals, predecessor eligibility, and successor candidate eligibility. A changed value is valid when the rest of the replacement contract holds. These contracts do not hard-code the demo V1/V2 identities. A unique index enforces one proposal at most for each non-null `feedback_id`.

Proposal states are exactly `PENDING`, `APPLIED`, and `REJECTED`, with only `PENDING -> APPLIED` and `PENDING -> REJECTED` permitted. Terminal decisions are immutable. No withdrawal or destructive rollback exists; reversal uses another proposal.

### 13.2 Governance service and transaction ownership

The application boundary is:

```text
GovernanceService.submit_correction(...)
GovernanceService.approve_correction(...)
GovernanceService.reject_correction(...)
GovernanceService.validate_transition(...)
```

It owns governance policy, including status, target, replacement-set, effective-time, branch, merge, and bounded-cycle validation. It coordinates `GovernanceProposalRepository`, `KnowledgeRepository` / `KnowledgeService`, document-version persistence, assertion lineage, audit persistence, and knowledge update/application records. It does not replace those components or take transaction ownership away from the caller-managed SQLite boundary.

Approval must execute all of the following in one transaction:

1. load and validate the `PENDING` proposal;
2. validate the expected current target and every predecessor;
3. validate every successor candidate and replacement pair;
4. validate future-effective, branch, duplicate-edge, self-edge, and bounded-cycle rules;
5. move document currentness;
6. transition all predecessor and successor assertion states;
7. create separate assertion lineage and document-version supersession records;
8. persist the review and transition the proposal to `APPLIED`;
9. persist the knowledge update/application record; and
10. append complete audit events.

Any error rolls back every step. Rejection uses one transaction to validate `PENDING`, record the review and rationale, set `REJECTED`, and append the audit facts. It performs none of steps 5–9 that apply knowledge.

### 13.3 Deterministic transition failures

An approval attempt before the proposed document version or any successor assertion `effective_from` returns `FUTURE_EFFECTIVE` without changing `PENDING`, `is_current`, assertion states, lineage, supersession, or update/application records. The blocked decision facts are appended to audit history. M7 deliberately has no `APPROVED_NOT_ACTIVE`, scheduler, activation job, or clock-derived current resolver.

Concurrent or sequential competing proposals may remain `PENDING`, but approval rechecks that each predecessor and the target version are still the expected governed/current head. The mutation uses guarded `is_current` updates plus expected assertion-state transitions inside a savepoint. If another approval wins after validation, the loser rolls back the attempted mutation and returns `STALE_TARGET`; its proposal remains `PENDING` and is not automatically rebased, selected, rejected, or retargeted. This is not a distributed locking system.

Service and relational invariants prevent another applied successor from an already linked predecessor and prevent successor reuse that would merge lineages. Self-replacement, a duplicate direct edge, and a replacement that would introduce a cycle within the bounded lineage depth are invalid. This requires bounded relational queries, not generalized graph traversal or graph infrastructure.

### 13.4 Schema-v7 persistence and audit

Schema v7 provides an unambiguous pre-approval mapping:

```text
proposal
  -> replacement item
       -> predecessor assertion
       -> proposed successor assertion
```

`correction_proposal` stores proposal identity, its optional feedback link, logical and version envelope, submitter facts, lifecycle state, and terminal decision facts. `correction_proposal_replacement_item` stores ordered normalized pair facts. `correction_proposal_predecessor_evidence` and `correction_proposal_successor_evidence` preserve ordered proposal-time evidence membership. Applied `assertion_lineage` and document-version `assertion_supersession` remain separate. Schema v6 is rejected; there is no in-place migration framework, so reset/rebuild is required.

`FEEDBACK_SUBMITTED` contains the complete immutable proposal-time meaning: proposal/feedback identity, submitter actor/role, rationale, target/proposed versions, every replacement pair's semantic/effective/document facts and ordered evidence IDs, and timestamp. Approval/rejection events and blocked stale-target/future-effective events preserve decision actor/role, action, decision/rationale, target/proposed versions, affected assertion IDs, before/after state, created review/lineage/supersession/update identities where applicable, failure reason, and timestamp. Historical interpretation does not depend solely on mutable current rows. These events are prototype audit history, **not compliance-grade**, **not tamper-evident**, and **not a production security or audit certification**.

Rejected proposals leave candidate document versions and assertions persisted but non-authoritative. They create no applied lineage and no knowledge update/application. Candidate cleanup is outside M7.

Submitter/reviewer actors and roles are asserted metadata only. The technical contract does not authenticate, authorize, verify identity, enforce RBAC, or enforce separation of duties.

### 13.5 Preserved boundaries and delivery stages

Document-version supersession and assertion replacement lineage remain distinct. One-hop live lineage is sufficient. `CURRENT`, `AS_OF`, historical snapshots, and the non-authoritative role of `recorded_at` and approval time remain as defined above; the future-effective approval guard does not alter AS_OF selection.

`ReasoningInput`, `SourceObservation`, `GovernedBaselineAssertion`, comparison, and `CONF-PA-SYN-V2` are unchanged. Governance can change selected knowledge, but arbitrary corrected predicates do not affect answers unless the existing reasoning projection and renderer support them. Generalized governance does not imply generalized answer rendering.

The live public routes are `POST /api/v1/feedback`, `POST /api/v1/feedback/{feedback_id}/approve`, and additive `POST /api/v1/feedback/{feedback_id}/reject`. Existing submission/approval requests remain compatible. Generalized submission optionally accepts `actor_role`, `target_version_id`, `proposed_version_id`, and non-empty explicit predecessor/successor `replacement_items`; when those fields are omitted, the bounded legacy demo inference remains. Public routes use `feedback_id`, while service and persistence use canonical `proposal_id`. No additional governance endpoint exists.

Delivery completed M7.0 decision documentation; M7.1 characterization; M7.2 immutable contracts; M7.3 schema-v7 replacement persistence; M7.4 deterministic `GovernanceService`; M7.5 live API/demo integration and fixed-ID removal; M7.6 rejection, stale-target, competing-proposal, future-effective, guarded concurrent approval, branch/cycle, rollback, audit, and snapshot hardening; and M7.7 documentation/release. The M7.6d release gate passed.

Explicit non-goals are arbitrary uploads, production authentication/authorization, verified identity, RBAC, separation-of-duties enforcement, destructive rollback, withdrawal, generic merges, graph database, RDF/ontology, generalized traversal, independent replay, new reasoning dimensions, generalized answer generation, LLM reasoning, embeddings/vector search, probabilistic arbitration, production integrations, compliance controls, tamper evidence, and certification claims.

## 14. M8 explanation technical direction — Planned

M8.0 freezes documentation only against schema v7 / `foundation-empty-v7`. Existing historical display is implemented; the richer `InteractionExplanation`, reader, endpoint, and UI are future M8 work. [Product sections A–J and staged delivery](product-spec.md#17-m8-historical-explanation-and-audit-surface--decision-only) define the conceptual contract without prescribing a final Python dataclass.

### 14.1 Persistence authority and available facts

| Section | Schema-v7 facts and limits |
|---|---|
| Interaction/request and temporal context | `interaction` records identity, question, intent, source mode, selected-source/retrieval trace JSON, `temporal_mode`, `requested_as_of`, and execution metadata. CURRENT pairs with null requested time; AS_OF pairs with its recorded UTC time. Do not infer requested time from `created_at` or legacy `as_of`, and do not recompute applicability. |
| Answer | Read `answer_text` as stored. Do not compose a new answer. |
| Retrieval | `interaction_evidence` stores the full retrieved evidence membership and deterministic zero-based ordinal, not a full copy of each `evidence_item`. Expose all memberships in order, including uncited evidence. `retrieval_trace_json` is a stored trace, not a reason to rerun adapters. |
| Governed participation | `interaction_knowledge` snapshots assertion ID, ordinal, origin, `state_at_execution`, `decision_type`, value/scope JSON, assertion effective interval and `recorded_at`, source ID/type, document ID/version ID/version label, document effective interval, and lineage/correction ID JSON. `interaction_knowledge_evidence` stores ordered evidence identities. |
| Claims/citations | `supported_claim` stores claim key/text and evidence IDs; `citation` stores the claim link, evidence/source/document-version identity, title/type/version/timestamp/section/excerpt. Citation provenance is copied at execution. Do not create citations for uncited knowledge evidence. |
| Reconciliation | `reconciliation_json` stores the public reconciliation projection. It does not promise every internal reasoning finding or a serialized intermediate comparison object. Expose only persisted findings, not a recalculated comparison. |
| Confidence/escalation | Read stored `confidence`, `confidence_rationale`, `confidence_policy_id`, and `escalation_json` decision/triggers as available. Null output for an intentionally unsupported interaction must stay explicitly absent, not become a fabricated confidence label or escalation. |
| Related history | Read audit event identity/type/time/payload and validated explicit interaction/feedback/proposal relationships under the architecture's bounded timeline rule. Proposal-time replacement snapshots and event payloads preserve governance meaning separately from execution. |

The knowledge snapshot's `decision_type` is the persisted decision dimension; it does not have a separate generic `predicate` column. Do not fetch a current assertion predicate and label it snapshotted. M8.1 must characterize what semantic detail can truthfully be projected. Evidence identities in membership tables are not copies of source excerpts, structured data, or all minimum provenance fields. Citation rows preserve `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt` for cited evidence; do not imply uncited memberships preserve those fields independently of mutable evidence rows. Characterize any essential gap before deciding whether v7 is sufficient.

Execution-time snapshots are authoritative wherever present. Never silently substitute current evidence state, document currentness, assertion state/value/scope/effective interval, current knowledge selection, or current reasoning output. A foreign key proves a relationship, not historical semantic immutability. Minimum provenance that is unavailable historically must be marked unavailable or cause failure under the next section, never invented.

RETRIEVED means the complete `interaction_evidence` membership; CLAIM-SUPPORTING means the persisted claim evidence IDs; CITED means actual stored citation rows. Claim support and citations must reference the retrieved universe, but those sets are not equivalent. Keep knowledge evidence memberships distinct, even where IDs overlap. Relationships may be projected from stored IDs without adding a public citation or new material claim.

### 14.2 Reader, API, and timeline constraints

The future reader is a persistence read/projection only. It must not call `RetrievalService`, `KnowledgeService` selection, `GovernanceService` mutation/selection, `compare_reasoning_input`, `reason` or reasoning policies, confidence recomputation, escalation recomputation, or answer composition. No live-source adapter invocation, temporal reselection, or knowledge mutation is permitted. Validation checks persisted shape and relationship consistency; it does not reevaluate the clinical or operational decision.

Prefer additive `GET /api/v1/interactions/{interaction_id}/explanation`, leaving `GET /api/v1/interactions/{interaction_id}` compatibility unchanged. This direction is not an implemented route or final wire contract. Future UI work is bounded to historical reopening and the product's A–J sections, not a frontend redesign.

Use exactly the [related-history rule](architecture.md#121-exact-related-history-boundary): direct interaction events, linked-feedback events, and events with a structured proposal ID linked through that feedback. Existing `get_audit` reads direct interaction/linked-feedback events in `event_id` order; the planned projection must characterize proposal payload linkage rather than invent a schema-v7 audit proposal column. Free-text matches and shared document/assertion families do not qualify. Preserve recorded timestamps and append order without claiming that either proves original influence.

Later related governance history may grow independently. It cannot rewrite execution facts after feedback, approval, rejection, supersession, lineage, source/assertion mutation, or current-version changes. Audit display must say **not tamper-evident**, **not compliance-grade**, **not a certified audit trail**, and actor/role metadata is **not authenticated identity**. Neither UI nor API wording may weaken these limits.

### 14.3 Incomplete and malformed snapshots

The following is the frozen failure direction; concrete error types and wire status mapping remain M8.2/M8.3 work.

| Condition | Direction |
|---|---|
| Interaction does not exist | Explicit not-found failure; no fallback query or fabricated explanation. |
| Core identity/temporal pairing is inconsistent; required execution JSON is malformed; evidence/knowledge order is duplicate or materially incomplete; claim/citation references contradict the retrieved universe or interaction identity; material snapshotted provenance is missing/contradictory | Fail the request explicitly. Do not return a plausible complete explanation by dropping rows or substituting live data. |
| Related audit payload is malformed or explicit interaction/feedback/proposal links conflict | Fail explicitly rather than silently omit an event or attach unrelated history. |
| A detail was never persisted in v7, such as an intermediate comparison object or uncited source excerpt, while core membership, identity, and stored output remain valid | A partial explanation may expose the valid sections with an explicit unavailable/not-recorded marker and completeness limitation. Never present the absent detail as empty, verified, or reconstructed. If essential to interpreting a material claim, fail instead and record the characterization gap. |
| Valid intentional absence, such as no governed participants, no related events, or an unsupported-scope interaction's null confidence | Preserve the recorded absence. Distinguish it from corruption and from an unavailable source; do not manufacture a result. |

No failure may silently substitute current data, drop malformed rows, invent provenance, or recompute a missing fact. Failure to read history is not permission to rerun the question or recalculate escalation. An original missing-source/LOW/conflict escalation remains the stored decision, with its uncertainty visible. M8.1 must characterize valid empty cases versus missing required records before immutable contracts are finalized.

### 14.4 Schema and release gates

Attempt M8 from v7 first. Add no tables merely for presentation. Consider v8 only when test-only characterization proves an essential execution-time fact was never persisted; do not design v8 speculatively or retrofit old executions from current state. No migration framework is introduced.

M8.1 characterizes persistence and gaps; M8.2 freezes immutable contracts; M8.3 implements the snapshot-only reader; M8.4 adds the API; M8.5 adds bounded UI; M8.6 hardens immutability, malformed/incomplete snapshots, ordering, provenance, governance-history separation, and prohibited-service call guarantees; M8.7 covers documentation/demo/release. None of those runtime stages is implemented in M8.0.

Independent replay remains a separate milestone with its own reproducibility definition. M8 promises neither the same answer today, byte-identical output, semantic replay equivalence, historical code execution, nor historical policy execution. `confidence_policy_id` alone enables none of those claims. Generalized deterministic rendering/reasoning dimensions, uploads, graph/RDF/ontology, LLMs, embeddings/vector retrieval, production auth/RBAC/compliance, external healthcare integrations, delayed future-effective activation, and an unapproved migration framework remain non-goals.

### 14.5 M8.3 reader implementation

`backend.explanation.reader.HistoricalExplanationReader(connection).read(interaction_id)`
returns the immutable M8.2 `InteractionExplanation`. The caller owns the SQLite
connection and must begin a read transaction for a coherent multi-table view.
The reader executes SELECTs only: it does not open, close, commit, roll back, or
change settings on the connection. No live service, policy, or composition runs.
All five healthcare integrations remain mocked; reader tests use isolated
synthetic schema-v7 databases.

Execution sections come from `interaction`, ordered retrieval and knowledge
membership, `supported_claim`, and copied `citation` rows. Retrieval, knowledge,
and knowledge-evidence ordinals must be unique, contiguous, and zero-based.
Claims and citations retain SQLite rowid order, matching historical GET; audit
events retain event-ID append order. Reconciliation exposes only its stored four
fields, confidence its stored label/rationale/policy identity, and escalation its
stored required/reviewer/reason. Requested time is never inferred or reselected.

Immutable identity edges in evidence, assertion, document/version, feedback,
proposal, and lineage tables are read only to validate references. No current
semantic columns supply explanation content. Copied provenance is checked
against other copies within this execution, not current source metadata. Knowledge
evidence need not be retrieved or cited. Citation evidence must both belong to
the retrieved universe and support its linked claim. The reader checks reverse
claim/citation links as well as forward links. Because orphan knowledge-evidence
rows have no interaction identity in v7, an existence-only orphan check fails
explicitly even when the lost parent cannot be assigned to this interaction.

The governance timeline follows section 14.2's admission paths, preferring a
verified proposal path, then feedback, then direct interaction when several
qualify. Raw event links and payloads are retained. Every supplied related
interaction/feedback/proposal link must agree, including structured payload
links; shared document/assertion identities never expand the timeline. Current
proposal status is not read. Unrelated malformed audit payloads are excluded;
malformed admitted events fail. A malformed payload with no surviving explicit
link cannot be attributed by guessing. Later history may grow without changing
execution facts.

`ExplanationNotFoundError` means the interaction is absent.
`ExplanationDataError` means material stored shape, ordering, JSON, provenance,
or relationship validation failed. SQLite operational errors propagate separately.
No malformed row is silently dropped or repaired. Intentional nulls/empty
collections remain distinct from the contract's four `unavailable_facts`:
uncopied evidence payloads, independent knowledge predicate, comparison
internals, and escalation trigger IDs. Schema v7 has no snapshot row-count
manifest: removal of an unreferenced final membership cannot always be
distinguished from an originally shorter valid snapshot. This is not tamper
detection, replay, or proof of completeness beyond persisted relationships.

M8.4 remains responsible for the additive
`GET /api/v1/interactions/{interaction_id}/explanation`: scope a read transaction,
call this reader, serialize frozen dataclasses/mappings/tuples and unavailable
markers without recomputation, map not-found to 404, and define an explicit
non-success wire response for data errors (no partial success on corruption).
It must preserve existing historical GET exactly, expose governance separately,
retain prototype audit/actor limitations, and test serialization, errors,
immutability, and the no-live-service boundary. No route, HTTP error mapping,
frontend, or schema change is implemented in M8.3.

## 15. Local runtime prerequisite and operational debt

Read-only M8.0 inspection on 2026-10-01 confirmed `develop` at `d6364308f0bbb2cf20d2405cd042091061ef9468` and the ignored `data/synapse.sqlite3` at SQLite `user_version = 1`. Source expects v7 / `foundation-empty-v7`; `Settings.from_environment()` currently resolves to that local file. This is an environment finding, not M8 product behavior. The database has not been confirmed disposable, so no reset was executed.

The current supported reset/rebuild implementation is `backend.database.reset_demo_database(settings)`, also used by `POST /api/demo/reset`. It builds and seeds a temporary database, validates schema/foundation metadata, foreign keys and integrity, and atomically replaces the target; failure preserves the old database. Startup rejects incompatible nonzero versions before serving the reset endpoint, so use the existing function directly for an incompatible local database.

After confirming the configured target is disposable synthetic development data and stopping the app/other database users, the exact repository-root PowerShell command is:

```powershell
.\.venv\Scripts\python.exe -B -c "from backend.config import Settings; from backend.database import reset_demo_database; s = Settings.from_environment(); assert s.demo_mode, 'Demo mode must be enabled'; reset_demo_database(s)"
```

This is a documented destructive maintenance command, not an instruction to run it automatically. It uses the actual settings factory, not a guessed constructor. Check `SYNAPSE_DATABASE_PATH` and `SYNAPSE_DATA_DIRECTORY` overrides and the resolved target before confirming disposability. The direct function does not enforce the HTTP confirmation-token check; operator confirmation is required here. When the application can start, the existing HTTP reset additionally checks demo mode and the configured confirmation token (default `RESET_SYNTHETIC_DEMO`). Do not silently migrate or automatically reset any local database.

Separate operational debt: ignored local databases can lag source schema, while tests use fresh temporary databases and therefore do not establish local runtime readiness. Startup validates schema identity and foundation metadata and rejects incompatible nonzero schemas. Readiness (`preflight_checks`) checks database connectivity and foreign-key enforcement, plus directory writability and optional FTS5, but does not independently verify schema identity/foundation metadata. No readiness change is part of M8.0. The reported release suite baseline of 510 passed is historical evidence, not a test run performed for this documentation task.
