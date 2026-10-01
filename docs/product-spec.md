# Synapse — Clinical Knowledge Copilot: Product Specification

## 1. Purpose and Status

This document defines the product intent and implemented hackathon minimum viable product (MVP) for Synapse — Clinical Knowledge Copilot. The repository contains an executable, deterministic synthetic prior-authorization demo. This specification describes the completed M7 governed-correction lifecycle, including the M6 knowledge-aware and v1.4 temporal behavior it preserves, and separates it from deferred or possible production work.

MVP requirements are separated from possible production aspirations. Nothing in the future-production section is a commitment for the hackathon.

## 2. Problem Statement

Clinicians and care coordinators often need to assemble an answer from fragmented sources: the patient chart, clinical guidelines, internal pathways, payer policies, drug formularies, and specialist notes. These sources may be incomplete, use different terminology, become outdated at different times, or disagree for legitimate reasons.

Conventional search or chat experiences can hide those differences by returning a single fluent response. Synapse should instead retrieve relevant evidence, preserve its provenance, identify material conflicts, and synthesize one usable answer that states its confidence and limitations. It should also turn reviewed clinician corrections into durable, shared organizational knowledge without erasing history.

## 3. Product Principles

- **Evidence before fluency:** every material answer claim is traceable to retrieved evidence.
- **Conflicts remain visible:** source disagreement is a first-class output, not an implementation detail.
- **Uncertainty is explicit:** missing facts and insufficient evidence are never fabricated away.
- **Humans retain authority:** missing required information, `LOW` confidence, and unresolved high-severity conflicts are escalated.
- **Feedback is governed knowledge:** clinician corrections require provenance and approval before changing current shared knowledge.
- **History is preserved:** original evidence, answers, decisions, and superseded assertions remain auditable.
- **Prototype honesty:** use synthetic data and make no production-readiness or HIPAA-compliance claims.

## 4. Approved MVP Decisions

The following decisions are approved and form the current product baseline:

1. **Primary persona:** Care coordinator or nurse navigator. A clinician is the secondary persona, and the interface must remain generic enough for either.
2. **Flagship scenario:** Medication prior authorization, centered on the canonical question: **“Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?”**
3. **Flagship data:** The scenario uses synthetic patient, payer, formulary, clinical-guideline, and specialist-note data.
4. **Source reconciliation:** The clinical guideline supports the medication clinically, while the payer policy imposes prior-authorization and/or step-therapy requirements. Synapse must show both and explain that clinical appropriateness and coverage or authorization are different dimensions; it must not silently select one source.
5. **Feedback loop:** A clinician flags a payer policy as outdated and supplies a newer version. Synapse records the feedback, a human explicitly approves it, the knowledge graph records a provenance-preserving update, and a subsequent query uses the updated information. All prior information remains auditable.
6. **Confidence:** The user-facing value is `HIGH`, `MEDIUM`, or `LOW`, accompanied by an explanation based on evidence availability, relevance, source agreement or conflict, freshness, and completeness of required context. The current synthetic workflow uses deterministic categorical rules. It is not a clinically validated probability.
7. **Human escalation:** Escalate when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved. Never guess when evidence is insufficient.
8. **Minimum source provenance:** Every retrieved source preserves `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
9. **Unavailable critical source:** Do not infer or fabricate its contents. Explicitly state that it could not be verified, provide the evidence that is available, and escalate or request human review when appropriate.
10. **Local persistence:** SQLite persists synthetic source data, evidence-backed assertions and lineage, questions, answers, feedback, and audit events locally for the MVP.

## 5. Target Users

### Primary user

- **Care coordinator or nurse navigator** resolving coverage, prior-authorization, formulary, pathway, evidence, and follow-up questions across sources.

### Secondary user

- **Clinician** seeking a consolidated, evidence-backed view of clinical and operational constraints.

The interface must use role-neutral interaction patterns and language where practical so either persona can use it.

### Supporting users

- **Human clinical or operational reviewers** who receive escalations and review proposed corrections.
- **Hackathon evaluators and demo operators** who need a repeatable, understandable workflow.

Production roles such as compliance administrators, security teams, knowledge stewards, and integration operators are future considerations, not full MVP personas.

## 6. Primary Use Cases

1. Ask the canonical medication prior-authorization question for a synthetic patient: “Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?”
2. Compare clinical guidance supporting the medication with payer prior-authorization or step-therapy requirements, while explaining the distinct dimensions addressed by each source.
3. Summarize relevant specialist-note context alongside structured mock EHR facts.
4. Respond with insufficient information when a required fact or source is missing.
5. Escalate when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved.
6. Inspect citations and provenance for each material answer claim.
7. Capture clinician confirmation or correction, review it, and apply an approved correction as a new knowledge-graph assertion while retaining prior history.

## 7. Hackathon MVP Goals

- Demonstrate the canonical medication prior-authorization question end to end using synthetic patient, payer, formulary, clinical-guideline, and specialist-note data.
- Show specialized retrieval across relevant mock source types selected by an orchestrator.
- Produce a concise answer with claim-level citations, source metadata, confidence, and limitations.
- Make at least one meaningful cross-source conflict prominent and actionable.
- Demonstrate explicit insufficient-information and human-escalation behavior.
- Demonstrate feedback capture, review status, and an approved, provenance-preserving knowledge-graph update.
- Maintain a readable audit trail covering the question, retrieved evidence, synthesis, confidence, escalation or actions, and feedback.
- Persist synthetic sources, knowledge-graph state, questions, answers, feedback, and audit events locally without selecting a database technology yet.

## 8. MVP Functional Requirements

### Implemented M7 capability

M7 retains the deterministic local synthetic workflow, first-class evidence-backed assertions, provenance, lineage, currentness integrity, bounded temporal querying, and M6 knowledge-aware prior-authorization reasoning. It completes a first-class governed correction lifecycle for atomic assertion replacement sets and advances persistence to schema v7. The persisted knowledge layer remains the foundation for the product's knowledge graph concept, not a generalized graph engine.

Three product operations are implemented and must not be conflated:

1. A **CURRENT query** asks what Synapse considers current now. Source authority is `source_document_version.is_current = 1`; knowledge authority is an `APPLIED` assertion whose source-document version is current.
2. An **AS_OF query** asks what governance-eligible source or knowledge was applicable at time `T`. It requires governance eligibility and closed UTC effective-interval containment (`effective_from <= as_of <= effective_to`), with null `effective_to` open-ended.
3. A **historical interaction** asks what Synapse actually produced in an earlier execution. It loads the persisted snapshot and does not rerun retrieval, knowledge selection, reasoning, or temporal selection.

`POST /api/v1/questions` accepts the existing `question` plus optional `as_of`. Omitted `as_of` means `CURRENT`; a valid explicit UTC value means `AS_OF`. Malformed, naive, or non-UTC values return HTTP 422. The public request does not expose `temporal_mode`, and the response shape remains compatible.

AS_OF selection never falls back to current and never chooses the newest version, highest version, `recorded_at`, or approval time as temporal precedence. Retrieval returns every governed `APPLIED` source-document version whose document interval contains `as_of`, in deterministic order. Knowledge returns every `APPLIED` or `SUPERSEDED` assertion whose own interval and governance-eligible source-document interval contain `as_of`; `CANDIDATE` assertions are excluded. `SUPERSEDED` means not current, but a previously governed assertion may remain historically applicable.

Retrieval selects applicable source/document evidence. Knowledge independently selects applicable persisted assertions. Application orchestration converts comparable retrieved payer conclusions to `SourceObservation` values, selected governed assertions to `GovernedBaselineAssertion` values, and owns their composition in a dual-input `ReasoningInput`; reasoning does not decide temporal authority. Raw retrieved evidence remains in the interaction snapshot even when a narrower assertion interval makes that evidence immaterial to the answer.

Overlaps do not create a silent winner. Opposing same-scope, same-dimension conclusions remain an unresolved high-severity conflict, preserve both sides, produce `LOW`, and require escalation. Multiple applicable payer versions that cannot be represented safely without inventing a temporal winner fail with HTTP 409 and do not create an interaction. Generalized multi-version answer rendering is not implemented.

The canonical pending/approved matrix is:

| Governance state | CURRENT | AS_OF June 15 | AS_OF July 3 |
|---|---|---|---|
| V2 pending | V1 | V1 | No applicable payer version; safe `LOW`-confidence path |
| V2 approved | V2 | V1 | V2 |

An approved retroactive correction can affect a **new** AS_OF query for the covered historical period. It never rewrites an old interaction snapshot. If approved intervals overlap, all applicable governed versions remain visible and no temporal winner is invented.

Future-effective early approval is intentionally refused. An approval attempt before the proposed document or successor assertion `effective_from` leaves the proposal `PENDING`, leaves CURRENT authority unchanged, applies no assertion or lineage, and records the blocked decision facts. There is no approved-but-active-later state, activation scheduler, or delayed activation job. AS_OF continues to use effective-interval applicability and is not redefined by this governance guard.

The interaction snapshots introduced through schema v5 persist `temporal_mode`, `requested_as_of`, `confidence_policy_id`, the complete retrieved evidence universe as `interaction_evidence` rows with deterministic zero-based ordinals, and execution-time citation `source_id`, `document_version_id`, and evidence identity alongside display provenance. For every selected governed assertion that participates in reasoning, `interaction_knowledge` also snapshots deterministic order, `KNOWLEDGE` or `CORROBORATED` role, state at execution, decision type, value, normalized scope, assertion and document effective facts, source/document provenance, and lineage/correction identities. `interaction_knowledge_evidence` snapshots the assertion's evidence IDs in deterministic order. Retrieved evidence, claim evidence, citation evidence, and knowledge evidence are related but distinct sets; governed evidence may be outside the current retrieved bundle while still retaining its own provenance chain.

Historical display and execution-time knowledge snapshot immutability are implemented. Later approval, rejection, supersession, or other governance changes do not late-bind or rewrite stored knowledge state, value, scope, provenance, order, or evidence membership. Independent deterministic replay is not implemented: no replay executor exists, and snapshot completeness is for historical explanation rather than a guarantee that an execution can be rerun.

### FR-1: Question and synthetic context intake

- Accept a natural-language question and a reference to a clearly synthetic patient context.
- Reject or clearly flag requests whose necessary patient or clinical context is absent.
- Do not infer missing patient facts.

### FR-2: Source planning and orchestration

- Determine which available sources are relevant to the question.
- Record the retrieval plan and the source types selected.
- For the flagship scenario, use synthetic patient information from the mock EHR plus payer policy, formulary, clinical-guideline, and specialist-note sources.

### FR-3: Specialized retrieval

- Retrieve evidence through source-specific interfaces or adapters.
- Return normalized evidence containing, at minimum, `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
- Report source failure, absence, or staleness explicitly.
- If a critical source such as payer policy is unavailable, state that it could not be verified, return the remaining available evidence, and signal the need for escalation or human review when appropriate. Never infer or fabricate the unavailable source's contents.

### FR-4: Shared knowledge representation

- Represent evidence-backed assertions, normalized scope, provenance, assertion status, and predecessor/successor lineage in a shared knowledge layer.
- Link each assertion to its supporting evidence.
- Allow multiple, potentially conflicting assertions to coexist.

The knowledge layer introduced in v1.3 persists in relational SQLite tables. A graph database and arbitrary graph traversal are not implemented or required.

### FR-5: Conflict detection

- Compare retrieved assertions that address the same decision or fact.
- Identify material agreement, conflict, and distinct-but-compatible constraints.
- Name the involved sources and explain the conflict without silently selecting a winner.
- In the flagship scenario, distinguish the guideline's clinical-appropriateness recommendation from payer coverage, prior-authorization, step-therapy, and formulary requirements.

### FR-6: Supervised synthesis

- Evaluate evidence relevance, provenance, recency where available, completeness, and conflicts.
- Produce one answer that separates clinical guidance, operational or coverage constraints, missing information, and next actions.
- Avoid unsupported conclusions.

### FR-7: Citations

- Attach one or more evidence references to every material claim.
- Allow a user or evaluator to inspect the cited source's `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
- Never generate a citation for evidence that was not retrieved.

### FR-8: Confidence and limitations

- Provide exactly one user-facing confidence category: `HIGH`, `MEDIUM`, or `LOW`.
- Base the explanation on evidence availability, relevance, source agreement or conflict, freshness, and completeness of required context.
- Display a plain-language rationale and known limitations.
- Do not present the category as a clinically validated probability. The exact scoring algorithm is deferred.

### FR-9: Insufficient information and escalation

- Return an explicit insufficient-information result when the evidence cannot support a safe answer.
- Escalate when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved.
- Never guess when evidence is insufficient.
- State the reason for escalation and what information or review is needed.
- The MVP may record and display an escalation rather than integrate with a live paging or ticketing system.

### FR-10: Audit trail

- Record the question, synthetic context reference, retrieval plan, evidence references, conflicts, answer, citations, confidence and rationale, escalation decision, relevant actions, and timestamps.
- Preserve historical records; subsequent feedback or knowledge changes must not overwrite the original interaction.

### FR-11: Clinician feedback and knowledge update

- Allow a clinician to confirm an answer or propose a correction with rationale and optional supporting evidence.
- Record feedback as a public compatibility envelope and link it to at most one canonically identified `CorrectionProposal` using `proposal_id`.
- Persist an immutable, normalized proposal-time mapping from each predecessor assertion to its proposed successor, including values, scope, effective facts, document identities, and evidence membership.
- Record the feedback author or synthetic role, timestamp, target assertion or answer, and review status as prototype governance metadata only.
- Require an explicit approval step before a proposed correction changes current shared knowledge. Submission itself creates only a non-authoritative `PENDING` proposal.
- Support only `PENDING -> APPLIED` and `PENDING -> REJECTED`; terminal decisions are immutable.
- Apply an approved replacement set atomically as new, versioned assertions linked to the feedback and superseded assertions; partial approval is unsupported.
- Leave stale-target and future-effective approval attempts `PENDING`, with CURRENT unchanged and without rebase, retarget, automatic rejection, delayed activation, or scheduler behavior.
- Retain rejected, pending, original, and superseded records in history.
- For the flagship feedback demonstration, accept a clinician report that the payer policy is outdated and capture the supplied newer policy version as provenance-bearing evidence. After explicit human approval, use the resulting current assertion in a subsequent query.

### FR-12: Demo reset and repeatability

- Provide a repeatable synthetic scenario with a known initial state and deterministic expected outcomes where practical.
- Allow the demo state to be reset without affecting any external clinical system.

### FR-13: Local persistence

- Persist synthetic source data, knowledge-graph state, questions, answers, feedback, and audit events in local storage.
- Persisted history must retain original and superseded records across subsequent queries.
- Use the implemented local SQLite store and deterministic reset/rebuild lifecycle for the current MVP. Long-term production storage remains a future architecture decision.

## 9. MVP Non-Functional Requirements

- **Safety:** clearly identify the experience as prototype decision support and expose uncertainty, conflicts, and escalation.
- **Privacy:** use synthetic or mock data only; no real PHI, patient identifiers, or production credentials.
- **Traceability:** maintain end-to-end links from answer claims to evidence and from approved corrections to provenance and prior assertions.
- **Auditability:** make the event history inspectable and preserve original records.
- **Local durability:** keep the approved persisted data categories in local persistent storage rather than only transient process memory.
- **Reliability:** prioritize a stable golden path and explicit failure states over breadth.
- **Explainability:** make source selection, conflict status, confidence rationale, and escalation reason understandable to a demo user.
- **Modularity:** keep external source behavior behind replaceable interfaces without choosing production vendors.
- **Usability:** present the answer, conflicts, citations, limitations, and required actions without requiring users to inspect internal agent traces.
- **Performance:** provide an interactive demo experience; a numeric latency target remains an open decision until the demo environment is known.
- **Accessibility:** use clear labels and avoid conveying conflict, confidence, or escalation by color alone.
- **Testability:** domain behavior should be deterministic under fixtures and independently testable from presentation and mocked integrations.

## 10. Explicit MVP Non-Goals

- Use of real PHI, production EHR records, or real patient identifiers.
- Production EHR, payer, formulary, pharmacy, messaging, or specialist-system integrations.
- Autonomous diagnosis, prescribing, treatment authorization, or changes to a clinical record.
- Claims of HIPAA compliance, regulatory clearance, clinical validation, or production readiness.
- Comprehensive medical coverage or support for every specialty, payer, medication, and workflow.
- Automated approval of clinician corrections without a review step.
- A production-grade identity, consent, role-based access, security, tenancy, retention, or disaster-recovery program.
- Selection of a long-term framework, database, vector store, agent framework, cloud provider, or model vendor.
- Training or fine-tuning a model from clinician feedback.
- Production-scale knowledge governance, terminology services, observability, or analytics.

## 11. MVP Acceptance Criteria

The MVP is acceptable when all of the following can be demonstrated with synthetic data:

1. A care coordinator or nurse navigator asks: “Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?” The same interface remains usable by a clinician.
2. The orchestrator records a source plan and retrieves relevant synthetic patient, payer-policy, formulary, clinical-guideline, and specialist-note evidence for the flagship scenario.
3. Every retrieved source exposes `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
4. Every material answer claim has at least one valid reference to evidence retrieved for that interaction.
5. The clinical guideline's support for the medication and the payer's prior-authorization or step-therapy requirement are both displayed and cited.
6. The answer explains that clinical appropriateness and coverage or authorization are different dimensions and does not silently select one source as the winner.
7. Confidence is displayed as exactly `HIGH`, `MEDIUM`, or `LOW`, with a plain-language explanation based on evidence availability, relevance, agreement or conflict, freshness, and completeness of required context.
8. The confidence display does not imply a clinically validated probability.
9. Missing required information produces explicit insufficient-information behavior and human escalation without a fabricated value.
10. `LOW` confidence produces a visible human escalation with a reason and requested next step.
11. An unresolved high-severity conflict produces a visible human escalation with a reason and requested next step.
12. When a critical payer-policy source is unavailable, the result states that the source could not be verified, does not infer its contents, presents other available evidence, and requests human review when appropriate.
13. The audit view preserves the original question, evidence, answer, confidence, conflicts, escalation or actions, and timestamps.
14. A clinician can flag a payer policy as outdated, provide the newer policy version, and create a pending feedback record without changing current knowledge.
15. Explicit human approval adds a linked, provenance-bearing current assertion while preserving the prior policy assertion, original answer, and feedback history.
16. A subsequent query uses the approved payer-policy update.
17. Rejecting or leaving a correction pending produces no current-knowledge change and remains auditable.
18. Synthetic source data, evidence-backed assertions and lineage, questions, answers, feedback, and audit events are persisted locally in SQLite.
19. All displayed people, identifiers, records, policies, notes, and clinical details are visibly synthetic.
20. The application makes no production-use, HIPAA-compliance, or autonomous-decision claim.
21. A submitted correction has a canonical `proposal_id`, links to the public `feedback_id`, and persists a complete immutable replacement set; one feedback cannot create multiple proposals.
22. Approval applies every replacement item atomically, while rejection is terminal and changes no knowledge authority.
23. Competing or concurrently approved proposals cannot create a second applied branch: the loser remains `PENDING` with `STALE_TARGET` and no partial state change.
24. A future-effective approval attempt remains `PENDING`, preserves CURRENT, and creates no delayed-activation state.

## 12. Golden Demo Scenarios

### Scenario A: Flagship medication prior-authorization reconciliation

A care coordinator or nurse navigator asks: **“Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?”** A synthetic patient meets the clinical-guideline criteria for the medication. The synthetic payer policy imposes prior authorization and/or step therapy. Synthetic formulary data, patient information, and a specialist note add the context needed to assess the requirement and support the answer.

Expected demonstration:

- the orchestrator selects synthetic patient, guideline, payer-policy, formulary, and specialist-note sources;
- Synapse shows the clinically supported option and the separate coverage constraint;
- Synapse explains that the guideline addresses clinical appropriateness while the payer policy addresses coverage or authorization;
- the constraint is prominent, cited with the minimum provenance fields, and reflected in the `HIGH`, `MEDIUM`, or `LOW` confidence explanation;
- the answer recommends a concrete information-gathering or authorization next step rather than claiming approval.

### Scenario B: Missing required information or unavailable critical source

The flagship question is asked, but required patient information is missing or the critical payer-policy source is unavailable.

Expected demonstration:

- Synapse identifies the missing information or explicitly states that payer policy could not be verified;
- no fact or unavailable source content is invented;
- available evidence is still presented;
- the result is marked insufficient information when appropriate and escalated for human review;
- the missing input or unavailable source and escalation reason appear in the audit trail.

### Scenario C: Approved payer-policy correction updates shared knowledge

A clinician flags the synthetic payer policy used in Scenario A as outdated and supplies a newer synthetic policy version.

Expected demonstration:

- the proposed correction is captured with provenance and remains pending initially;
- current knowledge is unchanged until a reviewer approves it;
- an explicit human approval creates a new current policy assertion linked to the feedback and supplied policy version, and marks the prior assertion superseded rather than deleting it;
- a subsequent run of the canonical question uses the approved policy information while the audit view retains the original policy, answer, feedback, approval, and full change history.

## 13. Assumptions

- All personas, records, identifiers, clinical details, policies, and source documents used in the MVP will be synthetic.
- A small, curated evidence corpus is sufficient for the hackathon demonstration.
- Source adapters may read locally persisted fixtures but will expose contracts that make their mocked nature explicit.
- The implemented evidence-backed assertion and lineage layer in SQLite satisfies the current MVP and provides a foundation for the product's knowledge graph concept; a graph database is not required.
- A human reviewer can be represented by an explicit demo action rather than an external review service.
- Confidence is rules-based and deterministic for the synthetic workflow and is not a calibrated clinical probability.
- Auditability for the MVP means inspectable, preserved application events, not a production compliance control.
- “Clinician” and “reviewer” identities may be synthetic roles until identity and authorization are designed.

## 14. Remaining Open Decisions

### Product and workflow details

1. Which synthetic medication, indication, patient facts, payer, formulary status, specialist statement, and policy criteria should populate the flagship fixtures?
2. What makes a conflict “high severity” for the MVP, and who assigns or confirms that severity?
3. Which authenticated production role may approve a payer-policy correction, and whether production separation of duties should be enforced? The MVP records asserted actor/role metadata but does not authenticate, authorize, verify identity, provide RBAC, or enforce separation of duties.
4. When a critical source is unavailable, what rule determines whether review is optional versus mandatory beyond the already-required missing-information, `LOW`-confidence, and high-severity-conflict triggers?
5. What exact wording should identify the prototype as synthetic decision support and not for clinical use?

### Deferred capabilities and decisions

The bounded `CURRENT` / `AS_OF` contract, M6 knowledge-aware prior-authorization path, and M7 governed correction lifecycle above are implemented. Broader work remains deferred: independent deterministic replay, a generic temporal framework, early approval with a distinct approved-but-not-yet-current state or activation scheduler, generalized multi-version payer rendering, generalized multi-hop graph traversal, arbitrary upload/correction ingestion, a graph database, ontology/RDF, vector retrieval, LLM reasoning, external healthcare integrations, production authentication/authorization, and production compliance controls. The current MVP also does not accept arbitrary document uploads.

Production storage, deployment, numeric latency targets, broader audit presentation, and source timestamp policy remain future decisions. These do not change the implemented schema-v7 local MVP boundary.

## 15. M6 knowledge-aware reasoning — Implemented

M6 makes governed knowledge a peer comparison input to deterministic prior-authorization reasoning while preserving retrieval as an independent input. The implemented flow is retrieved source evidence plus selected governed assertions, followed by deterministic comparison/reconciliation, findings, confidence/escalation, answer composition, and atomic interaction persistence. Knowledge is a governed baseline for comparison; “baseline” does not confer precedence, replace retrieval, act as a silent fallback, or mean a probabilistic prior.

The implemented product vocabulary distinguishes a **SourceObservation**, meaning a conclusion observed from source evidence selected by Retrieval, from a **GovernedBaselineAssertion**, meaning a reviewed/governed persisted assertion selected by Knowledge. Equivalent same-scope conclusions are marked `CORROBORATED`, while both evidence and knowledge provenance chains remain visible. Source-only conclusions do not acquire fabricated governance, and candidate assertions are reported as `GOVERNANCE_PENDING` rather than authoritative or snapshotted as governed participation.

Deterministic source/knowledge comparison distinguishes corroboration, source-only, knowledge-only, stale-knowledge disagreement, same-dimension conflict, compatible cross-dimension constraints, a missing source channel, a malformed knowledge channel, and governance pending. Knowledge-only output is explicitly unverified against a comparable current source and cannot manufacture source authority. A clinical guideline and a payer authorization policy address different dimensions: neither overrides the other merely because it is newer, and both may form compatible constraints. Opposing same-scope, same-dimension conclusions remain an explicit unresolved conflict with no silent winner or automatic knowledge update.

The critical-source safety rule remains strict. If current payer retrieval is unavailable, governed payer knowledge may be shown only as the last governed baseline, explicitly unverified against the current source. It is not current verification or a silent fallback; confidence remains `LOW` and escalation is required. A high-severity payer disagreement between retrieved evidence and governed knowledge likewise remains `LOW` with required escalation.

M6 preserves the v1.4 CURRENT, AS_OF, and historical-interaction semantics. It also preserves the existing human correction and approval boundary: Retrieval and Reasoning never write shared Knowledge, new evidence does not become governed knowledge automatically, and only approved additive corrections may change shared state.

`CONF-PA-SYN-V2` is active for the live knowledge-aware prior-authorization path; the source-only v1 policy remains separately identified as `CONF-PA-SYN-V1`. V2 keeps the public `HIGH`, `MEDIUM`, and `LOW` labels, cannot upgrade incomplete source evidence merely because knowledge agrees, and forces safe `LOW` plus escalation for an unverified critical payer baseline or a material payer source/knowledge disagreement. Schema v5 preserves execution-time knowledge participation rather than storing only a mutable assertion identity.

The completed staged delivery was M6.0 architecture decision; M6.1 characterization of agreement, disagreement, stale knowledge, source unavailability, and candidate/superseded temporal cases; M6.2 internal dual-input `ReasoningInput` and comparison contracts; M6.3 `CONF-PA-SYN-V2` and safe knowledge-aware presentation; M6.4 execution-time knowledge snapshot completeness; and M6.5 runtime hardening.

M6 non-goals are knowledge replacing retrieval, automatic source-to-knowledge updates, probabilistic priors or arbitration, LLM arbitration, embeddings or vector search, a graph database, generalized lineage traversal, an independent replay engine, production healthcare integrations, and production compliance or authentication. The existing relational assertion, evidence, lineage, and interaction-snapshot model remains sufficient unless a demonstrated future requirement proves otherwise.

## 16. M7 governed correction lifecycle — Implemented

M7 implements a first-class generalized governed correction lifecycle for persisted assertion replacement sets. Governance is generalized across eligible persisted target/proposed document versions and explicit assertion pairs; reasoning projections and answer rendering remain bounded to supported behavior.

### 16.1 Correction unit and replacement validity

The correction unit is an **atomic assertion replacement set**, not an entire document version treated as the sole semantic correction. Immutable `CorrectionProposal` uses `proposal_id` as the canonical governance identity. `feedback_id` is the public compatibility envelope, and a unique persistence constraint permits at most one proposal per linked feedback. A proposal contains target and proposed document versions, one or more explicit replacement items, asserted submitter metadata, rationale, status, and timestamps. Each item identifies exactly one predecessor assertion and one proposed successor assertion:

```text
predecessor assertion -> proposed successor assertion
```

Replacement mappings are normalized, ordered, and persisted before approval. They snapshot predicate, decision dimension, normalized scope, predecessor/successor values, effective intervals, document identities, and ordered evidence memberships so proposal-time meaning does not depend on later-mutable rows. Approval applies the complete set or none of it. M7 does not permit partial item approval. The replacement value may differ; changing that value is the purpose of a correction. Before application, every pair validates:

- membership in the same logical document family;
- predicate and decision dimension compatibility;
- normalized-scope compatibility;
- valid evidence and provenance;
- valid effective intervals;
- predecessor eligibility; and
- successor candidate eligibility.

This contract is identity-agnostic. The seeded V1/V2 IDs remain a supported demonstration, not identifiers embedded in the generalized contract.

### 16.2 Proposal lifecycle and rejection

The only M7 proposal transitions are:

```text
PENDING -> APPLIED
PENDING -> REJECTED
```

Both terminal decisions are immutable. M7 has no withdrawal and no destructive rollback. Reversing an applied decision requires a new additive correction proposal; an applied or rejected proposal is never edited back into another state.

Rejection records the review decision and rationale and changes the proposal or linked feedback to `REJECTED`. It does not change CURRENT document authority, apply successor assertions, supersede predecessors, create applied assertion lineage, or create a knowledge application/update. The rejected decision remains auditable. Candidate assertions and document versions remain persisted, non-authoritative candidate material; they are not deleted and do not silently become governed knowledge. A later cleanup policy would require a separate decision.

### 16.3 Future-effective and competing proposals

M7 uses a minimal static-currentness rule: a proposal is not applied before the proposed document version or any successor assertion `effective_from` instant. An early approval attempt returns `FUTURE_EFFECTIVE`, leaves the proposal `PENDING`, leaves `is_current` and CURRENT authority unchanged, applies no successors, supersedes no predecessors, and creates no applied lineage, supersession, or knowledge-update record. The blocked attempt and its decision facts are audited. This is not early approval followed by delayed activation: M7 has no `APPROVED_NOT_ACTIVE` state, scheduler, activation job, or clock-derived CURRENT resolver. If early human approval becomes a product requirement, a later milestone must explicitly separate approval from activation.

Multiple `PENDING` proposals may coexist, but only one may successfully replace a particular expected current target. Approval revalidates that every predecessor and the target document version are still the expected governed/current target and that every successor remains a valid candidate. Guarded current-head updates and expected assertion-state transitions protect the mutation itself; if another approval wins, the concurrent loser rolls back its partial attempt, returns `STALE_TARGET`, and remains `PENDING`. It is not rebased, reordered, auto-selected, auto-rejected, or retargeted. This is SQLite transaction and expected-state guarding, not a distributed locking system.

### 16.4 Branch, merge, and cycle rules

M7 prevents accidental applied branching: a governed predecessor cannot acquire another applied successor after a lineage edge exists. M7 supports no merges.

Generalized replacement validation rejects self-replacement, a duplicate direct edge, a reused successor that would merge lineages, and any edge that would create a cycle in the bounded lineage. Relational and service-level validation is sufficient. M7 does not introduce graph infrastructure or generalized traversal.

### 16.5 Prototype actors, governance ownership, and atomicity

Each proposal and decision records asserted prototype metadata for submitter actor and role, reviewer actor and role, rationale, and timestamps. These values do not establish authentication, authorization, verified identity, RBAC, enforced separation of duties, or any other security boundary. M7 may record that submitter and reviewer differ without enforcing that distinction.

A first-class `GovernanceService` owns submission, approval, rejection, and transition validation policy. It coordinates rather than replaces `KnowledgeRepository` / `KnowledgeService`, caller-owned SQLite transaction management, audit persistence, document-version state, and assertion lineage.

Submission validates and persists a non-authoritative `PENDING` proposal and a self-contained submission audit event. Approval is one all-or-nothing transaction: validate the proposal, expected current target, and every pair; move document currentness; transition predecessor and successor assertions; create assertion lineage and document-version supersession; record review; set proposal status; create the knowledge update; and write audit events. Any failure rolls back every effect. Rejection records its review, terminal status, and audit facts coherently without changing knowledge authority.

### 16.6 Audit, temporal, reasoning, rendering, and persistence boundaries

The submission audit is self-contained enough to preserve the proposal and complete replacement facts at submission time, including proposal/feedback identities, actor/role, rationale, versions, values, scope, effective facts, evidence memberships, and timestamp. Approval, rejection, stale-target, and future-effective events preserve their decision facts, including actor/role, action, decision/rationale, target/proposed versions, affected assertion IDs, before/after state, lineage/review/update identities where created, failure reason, and timestamp. Historical decision meaning therefore does not depend only on later-mutable rows. This is inspectable prototype audit history: it is **not compliance-grade**, **not tamper-evident**, and **not a production security or audit certification**.

M7 preserves the v1.5 temporal meanings:

- **CURRENT** is explicit current authority.
- **AS_OF** is effective-interval applicability.
- **Historical interaction** is the stored execution snapshot.
- `recorded_at` may explain freshness but is not temporal authority.
- approval time is not effective time.

The future-effective rule does not redefine AS_OF. Effective time, approval time, `recorded_at`, and currentness remain separate concepts. Document-version supersession and assertion replacement lineage remain separate records. Bounded relational lineage validation and one-hop live reads are sufficient for M7; generalized traversal remains deferred.

M7 changes what may become governed knowledge. It does not redesign `ReasoningInput`, `SourceObservation`, `GovernedBaselineAssertion`, comparison, or `CONF-PA-SYN-V2`. Reasoning continues to consume governed results through the existing Knowledge boundary. The answer renderer remains intentionally bounded: removing fixed governance IDs will not make arbitrary corrected predicates live in answers. The supported prior-authorization replacement may remain the demonstration. Generalized governance is not generalized reasoning or rendering.

Schema v7 explicitly persists the pre-approval mapping:

```text
proposal -> predecessor assertion -> proposed successor assertion
```

`correction_proposal` stores proposal and terminal-decision facts; `correction_proposal_replacement_item` stores normalized immutable pair facts; and predecessor/successor evidence tables preserve ordered proposal-time evidence membership. The required foundation baseline is `foundation-empty-v7`. Schema v6 is rejected. No migration framework is claimed; reset/rebuild remains the prototype upgrade mechanism.

### 16.7 Public API compatibility

Existing `POST /api/v1/feedback` submission and `POST /api/v1/feedback/{feedback_id}/approve` approval remain compatible. Submission may additionally provide `actor_role`, `target_version_id`, `proposed_version_id`, and explicit predecessor/successor `replacement_items`; omitting the generalized fields retains the bounded legacy inference used by the demo. `POST /api/v1/feedback/{feedback_id}/reject` is additive. Public routes continue to use `feedback_id`; `proposal_id` remains the canonical internal governance identity. No other governance endpoints are implemented.

### 16.8 M7 non-goals

M7 does not add arbitrary uploads; production authentication or authorization; verified identity; RBAC; separation-of-duties enforcement; destructive rollback; withdrawal; generic merges; a graph database; RDF or ontology; generalized graph traversal; independent replay; new reasoning dimensions; generalized answer generation; LLM reasoning; embeddings or vector search; probabilistic arbitration; production integrations; compliance controls; tamper evidence; or certification claims.

### 16.9 Completed M7 delivery

1. **M7.0 — Architecture/product decision:** froze the lifecycle boundary and invariants.
2. **M7.1 — Characterization:** captured the prior governance lifecycle in tests.
3. **M7.2 — Contracts:** added immutable correction and governance contracts.
4. **M7.3 — Persistence:** added explicit assertion replacement mappings and proposal-time snapshots.
5. **M7.4 — Governance service:** implemented deterministic submission, approval, rejection, and transition policy.
6. **M7.5 — Integration:** connected the live API/demo, preserved feedback compatibility, and removed fixed governance IDs from submission/application behavior.
7. **M7.6 — Hardening:** covered rejection, stale targets, competing proposals, future-effective attempts, guarded concurrent approval, branch/cycle protection, rollback, audit completeness, and snapshot immutability. The M7.6d release gate passed.
8. **M7.7 — Documentation/release:** reconciles documentation and release evidence with the completed implementation.

## 17. Future Production Aspirations — Not MVP Commitments

Potential later work may include governed production integrations, enterprise identity and authorization, formal privacy and security controls, clinical validation, terminology normalization, scalable knowledge governance, model and retrieval evaluation, calibrated confidence, monitoring, retention policies, and resilient infrastructure.

Each production capability requires separate discovery, risk assessment, clinical governance, compliance review, acceptance criteria, and architecture decisions. The hackathon prototype must not imply that these capabilities already exist.
