# Synapse — Clinical Knowledge Copilot: Architecture

## 1. Scope and current posture

Synapse M7 is a local, deterministic, synthetic healthcare decision-support MVP. It demonstrates conflict-aware, knowledge-aware prior-authorization synthesis and a first-class governed correction lifecycle without real PHI, external clinical systems, an LLM, or production compliance claims.

The application is a modular monolith: FastAPI serves the API and static UI, SQLite stores source, knowledge, interaction, governance, and audit records, and mocked adapters represent five healthcare source types.

## 2. Responsibility boundaries

| Boundary | Responsibility |
|---|---|
| Retrieval | Which source evidence is applicable? Select source-document versions for `CURRENT` or `AS_OF` and return normalized evidence or explicit failure. |
| Knowledge | Which persisted assertions are applicable? Select current-applied or governed historical assertions and validate assertion/source intervals. |
| Reasoning | What do independently selected source observations and governed baselines imply? Deterministically compare the dual inputs, reconcile applicable evidence, create supported claims and citations, and classify conflicts, confidence, and escalation. It does not decide temporal authority. |
| Governance | Which corrections may be submitted, applied, or rejected? Persist atomic replacement proposals, enforce deterministic transitions and expected-head validation, and preserve history. |
| Interaction snapshot | What execution context and result were actually produced? Persist temporal identity, retrieved evidence membership/order, execution-time governed-knowledge participation, claims, citations, answer, confidence, and escalation. |
| Historical GET | What did Synapse say then? Read the persisted interaction without rerunning selection or reasoning. |

Retrieval and Knowledge do not call one another. Application orchestration invokes them independently and combines their results for reasoning.

## 3. Question paths and three temporal operations

```text
CURRENT question
  -> current source selection + current governed-knowledge selection
  -> deterministic dual-input comparison and reasoning
  -> persisted snapshot

AS_OF question
  -> governed source interval selection + governed assertion interval selection
  -> orchestration builds applicable evidence and dual-input reasoning views
  -> deterministic comparison and reasoning
  -> persisted snapshot

historical interaction GET
  -> persisted snapshot only
```

### CURRENT

CURRENT asks, “What does Synapse consider current now?” Source authority is `source_document_version.is_current = 1`. Knowledge authority is:

```text
knowledge_assertion.state = APPLIED
AND source_document_version.is_current = 1
```

Omitting public `as_of` selects CURRENT and preserves the v1.3 current-query behavior.

The live CURRENT prior-authorization path reasons over both current retrieved source evidence and current-applied governed assertions. Retrieval and Knowledge select independently; orchestration projects their comparable conclusions into the dual-input boundary.

### AS_OF

AS_OF asks, “What governed knowledge is applicable at time T?” `as_of` is applicability time, not execution, encounter, record, review, approval, or historical-lookup time.

Selection requires governance eligibility and closed UTC interval containment:

```text
effective_from <= as_of <= effective_to
```

A null `effective_to` is open-ended. AS_OF does not fall back to current, choose a newest/highest version, or use `recorded_at` or approval time as precedence.

### Historical interaction

A historical interaction asks, “What did Synapse actually produce in this earlier execution?” The GET path loads persisted data. It never reruns retrieval, knowledge selection, reasoning, or temporal selection.

## 4. Source and knowledge temporal selection

Source selection is:

| Mode | Rules |
|---|---|
| CURRENT | `is_current = 1`; deterministic evidence ordering. |
| AS_OF | `governance_state = APPLIED`; document interval contains `as_of`; return all applicable versions in deterministic order. |

Knowledge selection is:

| Mode | Rules |
|---|---|
| CURRENT | assertion is `APPLIED`; source-document version is current. |
| AS_OF | assertion is `APPLIED` or `SUPERSEDED`; source version is governance-eligible; assertion interval and source-document interval both contain `as_of`. |

`CANDIDATE` assertions are excluded. `SUPERSEDED` does not mean current, but it may describe governed knowledge applicable in an earlier interval.

Retrieval may select an applicable source document containing assertions with narrower intervals. Knowledge determines which persisted assertions are applicable, and orchestration excludes evidence tied only to inapplicable assertions from the reasoning view. Raw retrieved evidence remains preserved in the snapshot. Thus, a document can be applicable while a narrower assertion inside it is not materially applicable at the requested instant.

## 5. Overlap and no-winner behavior

Multiple applicable versions or assertions are never silently ranked. Opposing same-scope, same-dimension conclusions preserve both evidence sides, remain an unresolved high-severity conflict, produce `LOW`, and require human escalation.

The bounded answer model cannot safely render every multi-version payer case. When multiple applicable payer versions do not produce a representable reasoning conflict, the API fails safely with HTTP 409, selects no winner, and persists no fabricated interaction. This is not generalized multi-version rendering.

## 6. Governance timeline and corrections

| Governance state | CURRENT | AS_OF June 15 | AS_OF July 3 |
|---|---|---|---|
| V2 pending | V1 | V1 | No applicable payer version; safe `LOW`-confidence path |
| V2 approved | V2 | V1 | V2 |

Submission creates a non-authoritative `PENDING` `CorrectionProposal` with immutable normalized assertion replacement mappings. Approval changes V1 assertions from `APPLIED` to `SUPERSEDED`, V2 assertions from `CANDIDATE` to `APPLIED`, and source currentness in one caller-owned transaction. Assertion lineage, document supersession, review, knowledge update, and audit records are additive. Rejection records a terminal review without changing knowledge authority.

An approved retroactive correction may affect new AS_OF queries for the covered historical period. It cannot rewrite old snapshots. Approved overlapping intervals remain jointly applicable; no temporal winner is invented.

Future-effective approval is refused. An early attempt leaves the proposal `PENDING`, leaves CURRENT unchanged, and creates no application, lineage, supersession, or knowledge update. There is no approved-but-not-active state, activation scheduler, or delayed activation job. AS_OF continues to honor effective intervals.

## 7. Temporal and knowledge interaction snapshots

The schema v5 interaction structures retained in schema v7 preserve the temporal snapshot introduced in v1.4 and store:

- on `interaction`: `temporal_mode`, `requested_as_of`, and `confidence_policy_id`;
- on `interaction_evidence`: the complete retrieved evidence universe, each `evidence_id`, and deterministic zero-based `ordinal`; and
- on `citation`: execution-time `source_id`, `document_version_id`, `evidence_id`, and existing display provenance.

M6 adds:

- on `interaction_knowledge`: each selected governed assertion in deterministic zero-based order, with its `KNOWLEDGE` or `CORROBORATED` role, state at execution, decision type, immutable value and normalized scope, assertion and document effective facts, source/document provenance, and lineage/correction identities; and
- on `interaction_knowledge_evidence`: the assertion's evidence membership in deterministic zero-based order.

These sets have different meanings:

```text
retrieved evidence: complete adapter output preserved for the interaction
claim evidence: retrieved evidence materially supporting a claim
citation evidence: retrieved evidence linked to a rendered claim citation
```

Claim and citation evidence are subsets of retrieved evidence. Evidence can be retained in the snapshot without materially contributing to the answer.

Later governance changes do not mutate an interaction's temporal mode, requested as-of time, confidence-policy identity, evidence membership/order, execution-time knowledge facts, claims, citations, citation source/document-version identity, answer, confidence, or escalation. Historical reads consume stored output and do not late-bind mutable assertion state or provenance.

Historical display is supported. Independent deterministic replay is not. The interaction snapshot preserves immutable execution facts for historical explanation, but retrieval membership alone does not snapshot all source contents. M8.1 must characterize completeness before richer explanation is implemented. There is no replay executor or claim that arbitrary executions can be reproduced.

## 8. Confidence, escalation, and safe failure

The live knowledge-aware prior-authorization path uses deterministic `CONF-PA-SYN-V2`; source-only V1 behavior remains separately identified as `CONF-PA-SYN-V1`. Missing sources do not all have equal weight, and governed knowledge cannot silently repair source gaps:

- unavailable applicable payer evidence produces `LOW` and escalation;
- a critical payer conclusion available only from governed knowledge remains unverified, produces `LOW`, and requires escalation;
- a same-dimension source/knowledge conflict or provably newer payer-source disagreement produces `LOW`, preserves both chains, and requires escalation;
- corroboration preserves both independent chains but cannot inflate incomplete source evidence;
- unresolved high-severity conflict produces `LOW` and escalation;
- missing important guideline or formulary evidence may produce `MEDIUM`; and
- missing optional specialist evidence may remain `HIGH`.

Safe-failure surfaces are:

| Surface | Meaning |
|---|---|
| HTTP 422 | Invalid public `as_of`: malformed, naive, or non-UTC. |
| `NOT_FOUND` | No governed applicable source version. |
| `MALFORMED` | Invalid temporal/source persistence. |
| HTTP 409 | Multiple applicable payer versions cannot be safely represented without inventing a winner. |
| `LOW` + escalation | Critical payer evidence unavailable/insufficient or an unresolved conflict. |
| `KnowledgeDataError` | Invalid persisted knowledge temporal state. |

No temporal safe-failure path silently falls back to current evidence.

## 9. Persistence and release boundary

SQLite schema version **7** is required. Foundation metadata is `foundation-empty-v7`; the deterministic seeded demo baseline remains `synthetic-pa-v1`. Any other nonzero schema version is rejected, explicitly including schema v6. No in-place migration framework exists; existing local databases require reset/rebuild, which is the supported MVP upgrade path.

Implemented through M7: bounded CURRENT/AS_OF source and knowledge selection, public optional `as_of`, assertion-level applicability alignment, overlap safe failure, live dual-input prior-authorization comparison, `CONF-PA-SYN-V2`, immutable temporal plus execution-time knowledge snapshots, and the governed correction lifecycle described in Section 11.

Deferred: independent replay, a generalized temporal framework, early approval with a distinct approved-but-not-yet-current state or activation scheduler, generalized multi-version answer rendering, generalized graph traversal, arbitrary upload/correction ingestion, a graph database, ontology/RDF, vector retrieval, LLM reasoning, arbitrary external healthcare integrations, production authentication/authorization, and production compliance controls.

The v1.3 knowledge release remains historical context: it introduced the persisted assertion, provenance, lineage, and current-applied boundaries upon which v1.4 builds.

## 10. M6 knowledge-aware reasoning (implemented)

M6 extends the v1.4 reasoning boundary without changing its temporal authority rules. Its implemented flow is:

```text
retrieved source evidence
  +
selected governed knowledge assertions
  -> deterministic comparison and reconciliation
  -> reasoning findings
  -> confidence and escalation
  -> answer composition
```

Knowledge supplements retrieval as a **governed baseline**. Here, baseline means a reviewed, persisted point of comparison; it does not mean precedence, a fallback authority, or a probabilistic prior. Knowledge does not replace retrieval or globally gate reasoning. Retrieved evidence does not silently rewrite governed knowledge, and neither input silently wins a disagreement.

### 10.1 Orchestration-owned input

Application orchestration constructs an immutable `ReasoningInput` containing:

- temporal context;
- retrieved evidence;
- selected governed assertions;
- resolved assertion provenance; and
- deterministic origin and selection metadata.

`ReasoningInput` is an implemented internal type, not a public API. Retrieval and Knowledge remain independent selectors. Orchestration owns their composition, while Reasoning owns deterministic comparison and policy evaluation; application orchestration uses those results for safe answer composition. Reasoning does not select temporal authority or write Knowledge.

The two input roles are:

- **SourceObservation:** a conclusion observed from source evidence selected by Retrieval.
- **GovernedBaselineAssertion:** a reviewed and governed persisted assertion selected by Knowledge. “Baseline” identifies its governed comparison role and never gives it automatic priority over a SourceObservation.

### 10.2 Origin and provenance preservation

Normalized reasoning assertions use these origins:

| Origin | Meaning |
|---|---|
| `SOURCE` | Supported by newly or currently retrieved source evidence. |
| `KNOWLEDGE` | Supported by a selected governed persisted assertion. |
| `CORROBORATED` | An equivalent, same-scope conclusion is independently supported by both inputs. |

`CORROBORATED` retains both provenance chains; it is not a merged record that erases either source evidence or knowledge lineage. These values are implemented by `AssertionOrigin`.

A knowledge-aware normalized assertion retains enough immutable execution context to explain its selection and claims, including:

- origin and, where applicable, `assertion_id`;
- assertion state at execution;
- predicate or decision dimension, value, and normalized scope;
- assertion effective interval and source-document effective interval;
- assertion `recorded_at`;
- evidence IDs, source ID, and document-version ID;
- temporal selection mode and requested time; and
- relevant correction or lineage identity when available.

Every material knowledge-backed claim must still resolve to source evidence. “The knowledge layer says so” is not sufficient provenance.

### 10.3 Comparison outcomes

M6 implements the following `ComparisonOutcome` semantics.

| Outcome | Semantics |
|---|---|
| `CORROBORATION` | Equivalent same-scope, same-dimension conclusions are independently supported by SourceObservation and GovernedBaselineAssertion; preserve both chains. |
| `SOURCE_ONLY` | A source-backed conclusion has no selected governed counterpart; do not fabricate governance. |
| `KNOWLEDGE_ONLY` | A selected governed conclusion has no current source observation; it may be shown only with the lack of current verification made explicit. |
| `STALE_KNOWLEDGE_DISAGREEMENT` | Current or newer source evidence differs from an older governed assertion in the same scope and dimension; preserve both and do not auto-update or suppress either. |
| `SAME_DIMENSION_CONFLICT` | Opposing conclusions address the same scope and decision dimension; preserve both, declare an unresolved conflict, and select no winner. |
| `COMPATIBLE_CROSS_DIMENSION_CONSTRAINT` | Conclusions address different authority dimensions and can both be true, such as clinical support plus a payer authorization requirement. |
| `MISSING_SOURCE_CHANNEL` | Required retrieval evidence is unavailable or absent; do not treat knowledge as silent source verification. |
| `MALFORMED_KNOWLEDGE_CHANNEL` | Selected persisted knowledge or its provenance cannot be validated; fail safely and do not use it as authority. |
| `GOVERNANCE_PENDING` | A candidate correction or assertion exists but is not approved and is therefore non-authoritative. |

### 10.4 Authority matrix

| Situation | Required treatment |
|---|---|
| Source present; knowledge agrees | Mark corroborated and preserve both provenance chains. |
| Source present; governed knowledge missing | Use the source-backed conclusion and do not fabricate governance. |
| Source unavailable; governed knowledge present | The governed assertion may be shown as the last governed baseline, explicitly unverified against the current source. Critical payer retrieval remains `LOW` confidence and requires escalation. |
| Source is newer or different; governed knowledge disagrees | Preserve both, expose the disagreement, do not update Knowledge automatically, and do not suppress source evidence. |
| Candidate knowledge only | Classify as governance pending; it is not governed authority and must not be applied as a baseline. |
| `SUPERSEDED` in CURRENT | Exclude it. |
| Applicable `SUPERSEDED` in AS_OF | It remains historically eligible under v1.4 interval and governance rules. |
| Same-scope, same-dimension opposition | Expose an explicit conflict and select no winner. |

Clinical-guideline conclusions and payer authorization rules occupy different decision dimensions. A newer clinical guideline does not override payer authorization because it is newer, and a payer policy does not negate clinical appropriateness. Such cross-dimensional differences can be compatible constraints rather than conflicts.

### 10.5 Governance and temporal boundaries

Reasoning never writes Knowledge. Retrieval never writes Knowledge. New source evidence never becomes shared governed knowledge automatically. Only the existing correction, human-review, and approval path may change shared knowledge state, and candidate assertions remain non-authoritative until approval. Updates remain additive and preserve original evidence, assertions, answers, correction identity, lineage, and audit history.

M6 preserves, rather than redefines, the v1.4 temporal contract:

- **CURRENT:** Retrieval selects current source evidence; Knowledge selects current `APPLIED` assertions.
- **AS_OF:** Retrieval selects governed source versions applicable at `T`; Knowledge selects `APPLIED` or `SUPERSEDED` assertions applicable at `T` under the existing assertion and document interval rules.
- **Historical interaction:** the stored snapshot remains authoritative for what Synapse said at that execution; it is not recomputed after governance changes.

If critical current payer retrieval is unavailable but governed payer knowledge exists, the governed assertion may be presented only as the last governed baseline. It must not be described as current source verification or used as a silent fallback. Confidence remains `LOW`, and human escalation remains required.

If retrieved evidence and governed knowledge make opposing same-scope, same-dimension claims, both claims and both provenance chains remain visible. Reasoning records an explicit unresolved disagreement, does not update Knowledge, and does not suppress the source. A high-severity payer disagreement produces `LOW` confidence and required escalation.

### 10.6 Confidence and snapshot behavior

`CONF-PA-SYN-V2` is the active policy for the knowledge-aware prior-authorization path; `CONF-PA-SYN-V1` remains the separately identified source-only policy. V2 distinguishes source-backed, knowledge-backed, and corroborated conclusions; a provably newer source disagreement; knowledge-only presentation; a missing source channel; source/knowledge conflict; governance pending; and knowledge validation failure. Public confidence labels remain exactly `HIGH`, `MEDIUM`, and `LOW`, and are not clinically validated probabilities. Knowledge can explain or safely downgrade the source-policy result, but corroboration cannot upgrade incomplete source evidence.

Because governed knowledge can materially change rendered answers, schema v5 interaction snapshots preserve execution-time knowledge participation, including:

- selected assertion identity and deterministic order;
- origin or input role;
- assertion state at execution; and
- immutable assertion value, scope, and effective facts needed to explain the result.

The snapshot additionally preserves decision type, value, normalized scope, assertion and document effective facts, source/document provenance, lineage/correction identities, and ordered evidence membership. An assertion ID alone is insufficient because assertion state and other mutable records can change after execution. Snapshot rows are written atomically with the interaction and remain historical facts after later approval or supersession.

### 10.7 Technology boundary and staged delivery

The current relational `knowledge_assertion`, `assertion_evidence`, and `assertion_lineage` model plus schema-v5 interaction snapshot tables are sufficient for M6. M6 does not require a graph database, RDF or an ontology, generalized graph traversal, or generic cycle detection. LLM reasoning, embeddings, vector retrieval, and probabilistic arbitration are also deferred; M6 comparison and reconciliation remain deterministic.

The completed staged delivery was:

1. **M6.0 — Architecture decision:** froze the boundary, terminology, authority rules, safety constraints, and release gates; that stage made no runtime behavior changes.
2. **M6.1 — Characterization:** covered agreement, disagreement, stale knowledge, unavailable source, and candidate/superseded temporal behavior.
3. **M6.2 — Internal contracts:** introduced the internal dual-input `ReasoningInput` and comparison contracts.
4. **M6.3 — Policy and presentation:** added separately versioned `CONF-PA-SYN-V2` and safe knowledge-aware presentation.
5. **M6.4 — Snapshot completeness:** persisted immutable execution-time knowledge participation for historical explanation.
6. **M6.5 — Runtime hardening:** completed the live orchestration and persistence release gate.

Explicit M6 non-goals are knowledge replacing retrieval, automatic knowledge updates from source evidence, probabilistic priors, LLM arbitration, vector search, a graph database, generalized lineage traversal, an independent replay engine, production healthcare integrations, and production compliance or authentication capabilities.

## 11. M7 governed correction lifecycle architecture — Implemented

M7 adds a first-class Governance boundary around an **atomic assertion replacement set**. The document-version pair supplies the proposal envelope, but the semantic unit is one or more explicit predecessor-to-successor assertion pairs. The full set is approved or rejected together; partial approval is outside M7.

```text
GovernanceService
├── submit correction
├── approve correction
├── reject correction
└── validate transition
```

`GovernanceService` owns lifecycle and transition policy. It coordinates, rather than absorbs, the existing boundaries: `KnowledgeRepository` / `KnowledgeService` validate and transition persisted assertions; the caller-owned SQLite transaction provides atomicity; persistence records document-version state and separate assertion lineage; and audit persistence records durable decision facts. Retrieval and Reasoning remain outside governance writes.

### 11.1 Proposal and transition invariants

Immutable `CorrectionProposal` uses `proposal_id` as canonical identity and may link to a public compatibility `feedback_id`; a linked feedback has at most one proposal. A proposal carries target and proposed document versions, explicit normalized replacement items, submitter actor/role, rationale, status, and timestamps. Each replacement pair snapshots its predicate, decision dimension, normalized scope, predecessor/successor values, effective intervals, document identities, and ordered evidence memberships. Submission validates the same logical document family, compatible replacement semantics, evidence/provenance validity, valid intervals, an eligible governed/current predecessor, and an eligible candidate successor. Values are allowed to differ. The generalized contract does not depend on seeded V1/V2 identifiers.

The only lifecycle paths are `PENDING -> APPLIED` and `PENDING -> REJECTED`. Terminal decisions are immutable. There is no withdrawal or destructive rollback; a reversal is a new additive proposal.

Rejection records review and rationale without moving CURRENT, applying a successor, superseding a predecessor, creating applied assertion lineage, or creating a knowledge application/update. Rejected candidate material remains persisted and non-authoritative. It is neither deleted nor silently governed.

### 11.2 Concurrency, branch, and cycle invariants

Multiple proposals may be `PENDING`. Approval succeeds only if every predecessor and the target document version still match the expected governed/current target, and every successor remains a valid candidate. Guarded current-head updates plus guarded assertion-state transitions protect the mutation after read-only validation. Once another proposal replaces the target, a sequential or concurrent loser rolls back any partial mutation, fails as **STALE TARGET**, and remains `PENDING`; M7 does not automatically rebase, select, reject, or retarget it. This is SQLite transaction and expected-state guarding, not distributed locking.

An applied predecessor may not gain another successor after lineage exists. M7 has no merge operation. Validation also rejects self-replacement, an existing direct edge, successor reuse that would merge lineage, and any replacement that would create a cycle in the bounded lineage. Relational constraints and service validation are sufficient; a graph database and generalized traversal are not part of M7.

### 11.3 Future-effective and temporal invariants

A proposal cannot be applied before the proposed document version or any successor assertion `effective_from` instant. An early attempt returns `FUTURE_EFFECTIVE`, leaves the proposal `PENDING`, preserves currentness and assertion states, creates no lineage, supersession, or update/application, and records the blocked decision facts. This is static currentness, not early approval plus activation: there is no `APPROVED_NOT_ACTIVE`, scheduler, activation job, or clock-derived CURRENT resolver.

The rule preserves existing meanings: CURRENT is explicit authority; AS_OF is effective-interval applicability; historical interaction is a stored execution snapshot; `recorded_at` is explanatory freshness metadata, not temporal authority; and approval time is not effective time. A future requirement for early human approval must explicitly separate approval and activation.

### 11.4 Atomic state change and immutable history

Successful approval is one transaction containing proposal validation, expected-target validation, every replacement-pair validation, document currentness movement, predecessor and successor state transitions, assertion-lineage creation, document-version supersession, review persistence, proposal transition, knowledge update/application persistence, and audit writes. One failure rolls back the complete replacement set. Rejection is likewise transactionally coherent.

Submission audit facts are self-contained enough to preserve the complete immutable proposal and replacement meaning at submission time. Approval, rejection, stale-target, and future-effective events preserve actor/role, action, proposal/feedback identity, review decision and rationale, target/proposed versions, affected assertion IDs, before/after states, created identities where applicable, failure reason, and timestamp without relying only on mutable related rows. This is prototype audit history: it is not compliance-grade, tamper-evident, or a production security/audit certification.

Document-version supersession remains distinct from assertion replacement lineage. One-hop lineage reads are enough for live M7 behavior. Candidate material remains non-authoritative until the complete proposal is applied.

Submitter and reviewer actor/role values are asserted prototype governance metadata only. They do not authenticate or authorize a user, verify identity, implement RBAC, or enforce separation of duties.

### 11.5 Unchanged reasoning and presentation boundaries

M7 changes the governed Knowledge output but does not redesign `ReasoningInput`, `SourceObservation`, `GovernedBaselineAssertion`, comparison outcomes, or `CONF-PA-SYN-V2`. The existing Knowledge boundary continues to feed Reasoning. Removing fixed correction IDs does not imply that every predicate can affect a live answer; the renderer remains bounded to supported prior-authorization behavior until separately extended.

Schema v7 persists `correction_proposal`, ordered `correction_proposal_replacement_item` mappings, and ordered predecessor/successor proposal-time evidence memberships. Proposal and terminal decision facts are stored separately from applied `assertion_lineage` and document-version `assertion_supersession`. Foundation metadata is `foundation-empty-v7`; schema v6 is rejected. There is no migration framework, and reset/rebuild remains the prototype upgrade path.

The public compatibility boundary remains feedback-centered: `POST /api/v1/feedback` and `POST /api/v1/feedback/{feedback_id}/approve` remain compatible, while generalized submission fields are optional and `POST /api/v1/feedback/{feedback_id}/reject` is additive. Public routes use `feedback_id`; `proposal_id` is the canonical governance identity behind the envelope. No additional governance endpoint is implemented.

M7 non-goals are arbitrary uploads, production authentication/authorization, enforced separation of duties, destructive rollback, withdrawal, generic merges, graph infrastructure, RDF/ontology, generalized graph traversal, replay, new reasoning dimensions, generalized answer generation, LLM or vector reasoning, probabilistic arbitration, production integrations, and compliance claims.

The completed delivery includes M7.0 decision freeze; M7.1 lifecycle characterization; M7.2 immutable contracts; M7.3 explicit replacement persistence; M7.4 `GovernanceService`; M7.5 generalized API/demo integration with compatibility preservation; M7.6 rejection, stale-target, competing-proposal, future-effective, concurrent-approval, branch/cycle, rollback, audit, and snapshot hardening; and M7.7 documentation/release. The M7.6d release gate passed.

## 12. M8 historical explanation boundary — Planned

M8.0 is a documentation decision for historical explanation and an audit surface, not runtime implementation. [Product scope and stages](product-spec.md#17-m8-historical-explanation-and-audit-surface--decision-only) distinguish implemented historical display from planned explanation and future independent replay. Explanation reads persisted execution/governance facts; it does not recompute the historical decision.

The future architectural path is:

```text
Historical explanation request
  -> persistence reader and snapshot validation
  -> immutable execution-time projection
  + explicitly related governance/audit history (separate section)
```

This path must not invoke `RetrievalService`, `KnowledgeService` selection, `GovernanceService` mutation/selection, `compare_reasoning_input`, `reason` or reasoning policies, confidence recomputation, escalation recomputation, or answer composition. Repository reads for explicit identity relationships are permitted; live domain selection is not. Reading an explanation must not mutate knowledge or historical records.

Execution-time snapshot facts take authority over current mutable evidence, source-document currentness, assertion state/value/scope/effective interval, current knowledge selection, and current reasoning output. Snapshot identities may establish relationships but do not authorize loading current semantic values as historical facts. Preserve CURRENT/AS_OF and `requested_as_of`; `created_at` is not a substitute and applicability must not be reevaluated. Preserve original claims/citations, stored reconciliation, confidence policy/result/rationale, and escalation.

The complete ordered `interaction_evidence` universe is RETRIEVED; stored claim support identifies CLAIM-SUPPORTING; stored public citation rows identify CITED. These sets are not equivalent. Knowledge evidence membership is a separate execution-time relationship and does not confer public citation status. `interaction_knowledge` and its ordered evidence memberships explain governed participation without late-binding assertion state. Missing unsnapshotted detail is subject to the [technical failure boundary](technical-architecture.md#143-incomplete-and-malformed-snapshots), never filled from live state.

### 12.1 Exact related-history boundary

For interaction I, the planned timeline admits only the following persisted relationships:

1. Audit events whose explicit `interaction_id` equals I.
2. Feedback whose persisted `interaction_id` equals I, and events whose explicit `feedback_id` equals one of those feedback IDs.
3. Proposals whose persisted `feedback_id` links to that feedback, and events whose structured persisted `proposal_id` equals one of those proposal IDs. In v7 this event identity may be in `payload_json`; there is no dedicated audit `proposal_id` column.

The reader must validate IDs and their consistency, deduplicate by event identity, retain the relationship that justified inclusion, and preserve deterministic event order (`event_id` append order with `occurred_at` displayed separately). Timestamps alone do not prove execution participation or causality. Conflicting explicit links are an inconsistency, not permission to attach an event to both interactions. Free-text mentions, matching source/document/assertion families, and temporal proximity never establish relatedness. Proposals without a feedback link to I are excluded unless an event independently has the direct interaction link above; no generalized family or lineage traversal expands the event set.

Snapshotted lineage/correction IDs remain execution-time provenance in the execution section. They do not broaden the related-event rule to arbitrary future events about the same assertions. Review, lineage, supersession, and update identities may be displayed when explicitly recorded by admitted events; present each event's recorded facts rather than reconstructing an earlier status from a proposal's current row.

Separate execution-time explanation from later related governance history. A later approval, rejection, blocked stale-target attempt, or blocked future-effective attempt may be shown only as related history, never as a cause of the original answer. The timeline may grow; feedback, approval, rejection, supersession, lineage, source/assertion mutation, and current-version changes must never rewrite execution-time snapshot facts.

### 12.2 Prototype boundary and schema direction

Audit facts are **not tamper-evident**, **not compliance-grade**, and **not a certified audit trail**; actor/role metadata is **not authenticated identity**. These limitations must remain visible in future UI/API design. Logical append-only history and immutable execution projections do not certify the underlying SQLite store against tampering.

Use schema v7 first, with no presentation-only tables. Only M8.1 characterization proving an essential execution-time fact was never persisted can justify considering v8. Missing intermediate comparison objects must not be reconstructed from current data. No replay executor, historical policy/code execution, semantic equivalence, or byte-identical reproducibility is implied. Policy identity alone is insufficient for replay. The additive explanation endpoint and bounded reopen UI remain future work; preserve the existing historical GET response shape. Reset/readiness maintenance remains separate from M8 product behavior.
