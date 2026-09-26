# Synapse — Clinical Knowledge Copilot: Product Specification

## 1. Purpose and Status

This document defines the product intent and implemented hackathon minimum viable product (MVP) for Synapse — Clinical Knowledge Copilot. The repository contains an executable, deterministic synthetic prior-authorization demo. This specification separates the implemented v1.4 temporal capability from deferred or possible production work.

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

### Implemented v1.4 capability

V1.4 retains the deterministic local synthetic workflow, first-class evidence-backed assertions, provenance, lineage, governed correction persistence, and currentness integrity introduced in v1.3. It adds bounded temporal querying and schema-v4 interaction identity. The persisted knowledge layer remains the foundation for the product's knowledge graph concept, not a generalized graph engine.

Three product operations are implemented and must not be conflated:

1. A **CURRENT query** asks what Synapse considers current now. Source authority is `source_document_version.is_current = 1`; knowledge authority is an `APPLIED` assertion whose source-document version is current.
2. An **AS_OF query** asks what governance-eligible source or knowledge was applicable at time `T`. It requires governance eligibility and closed UTC effective-interval containment (`effective_from <= as_of <= effective_to`), with null `effective_to` open-ended.
3. A **historical interaction** asks what Synapse actually produced in an earlier execution. It loads the persisted snapshot and does not rerun retrieval, knowledge selection, reasoning, or temporal selection.

`POST /api/v1/questions` accepts the existing `question` plus optional `as_of`. Omitted `as_of` means `CURRENT`; a valid explicit UTC value means `AS_OF`. Malformed, naive, or non-UTC values return HTTP 422. The public request does not expose `temporal_mode`, and the response shape remains compatible.

AS_OF selection never falls back to current and never chooses the newest version, highest version, `recorded_at`, or approval time as temporal precedence. Retrieval returns every governed `APPLIED` source-document version whose document interval contains `as_of`, in deterministic order. Knowledge returns every `APPLIED` or `SUPERSEDED` assertion whose own interval and governance-eligible source-document interval contain `as_of`; `CANDIDATE` assertions are excluded. `SUPERSEDED` means not current, but a previously governed assertion may remain historically applicable.

Retrieval selects applicable source/document evidence. Knowledge selects applicable persisted assertions. Application orchestration combines those results into a reasoning-only applicable evidence view; reasoning does not decide temporal authority. Raw retrieved evidence remains in the interaction snapshot even when a narrower assertion interval makes that evidence immaterial to the answer.

Overlaps do not create a silent winner. Opposing same-scope, same-dimension conclusions remain an unresolved high-severity conflict, preserve both sides, produce `LOW`, and require escalation. Multiple applicable payer versions that cannot be represented safely without inventing a temporal winner fail with HTTP 409 and do not create an interaction. Generalized multi-version answer rendering is not implemented.

The canonical pending/approved matrix is:

| Governance state | CURRENT | AS_OF June 15 | AS_OF July 3 |
|---|---|---|---|
| V2 pending | V1 | V1 | No applicable payer version; safe `LOW`-confidence path |
| V2 approved | V2 | V1 | V2 |

An approved retroactive correction can affect a **new** AS_OF query for the covered historical period. It never rewrites an old interaction snapshot. If approved intervals overlap, all applicable governed versions remain visible and no temporal winner is invented.

Future-effective approval remains a known product limitation: approval currently makes V2 current immediately, while AS_OF still honors effective intervals. The system has no distinct “approved but not yet current” state.

Schema v4 snapshots persist `temporal_mode`, `requested_as_of`, `confidence_policy_id`, the complete retrieved evidence universe as `interaction_evidence` rows with deterministic zero-based ordinals, and execution-time citation `source_id`, `document_version_id`, and evidence identity alongside display provenance. Retrieved evidence, claim evidence, and citation evidence are different sets: claim and citation evidence are subsets of the complete retrieved universe.

Historical display is implemented. Independent deterministic replay is not: schema v4 stores the minimum immutable execution identities useful for future replay, but not immutable copies of every evidence payload, and no replay executor exists.

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
- Record the feedback author or synthetic role, timestamp, target assertion or answer, and review status.
- Require an explicit approval step before a proposed correction changes current shared knowledge.
- Apply an approved correction as a new, versioned assertion linked to the feedback and the superseded assertion.
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
3. Which human role may approve a payer-policy correction, and may the clinician who submitted it also approve it?
4. When a critical source is unavailable, what rule determines whether review is optional versus mandatory beyond the already-required missing-information, `LOW`-confidence, and high-severity-conflict triggers?
5. What exact wording should identify the prototype as synthetic decision support and not for clinical use?

### Deferred capabilities and decisions

The bounded v1.4 `CURRENT` / `AS_OF` contract above is implemented. Broader work remains deferred: independent deterministic replay, a generic temporal framework, a distinct approved-but-not-yet-current governance state, generalized multi-version payer rendering, generalized multi-hop graph traversal, generalized cycle detection, arbitrary correction workflows, a graph database, ontology/RDF, vector retrieval, LLM reasoning, external healthcare integrations, authentication, and production compliance controls. The current MVP also does not accept arbitrary document uploads.

Production storage, deployment, numeric latency targets, broader audit presentation, and source timestamp policy remain future decisions. These do not change the implemented schema-v4 local MVP boundary.

## 15. M6 knowledge-aware reasoning direction — Not yet implemented

M6 will make governed knowledge a peer comparison input to deterministic reasoning while preserving retrieval as an independent input. The product flow will be retrieved source evidence plus selected governed assertions, followed by deterministic comparison/reconciliation, findings, confidence/escalation, and answer composition. Knowledge is a governed baseline for comparison; “baseline” does not confer precedence, replace retrieval, act as a silent fallback, or mean a probabilistic prior.

The future product vocabulary distinguishes a **SourceObservation**, meaning a conclusion observed from source evidence selected by Retrieval, from a **GovernedBaselineAssertion**, meaning a reviewed/governed persisted assertion selected by Knowledge. Equivalent same-scope conclusions may be marked `CORROBORATED`, but both evidence and knowledge provenance chains remain visible. Source-only conclusions do not acquire fabricated governance, and candidate assertions remain `GOVERNANCE_PENDING` rather than authoritative.

Source/knowledge comparisons will distinguish corroboration, source-only, knowledge-only, stale-knowledge disagreement, same-dimension conflict, compatible cross-dimension constraints, a missing source channel, a malformed knowledge channel, and governance pending. A clinical guideline and a payer authorization policy address different dimensions: neither overrides the other merely because it is newer, and both may form compatible constraints. Opposing same-scope, same-dimension conclusions remain an explicit unresolved conflict with no silent winner or automatic knowledge update.

The critical-source safety rule remains strict. If current payer retrieval is unavailable, governed payer knowledge may be shown only as the last governed baseline, explicitly unverified against the current source. It is not current verification or a silent fallback; confidence remains `LOW` and escalation is required. A high-severity payer disagreement between retrieved evidence and governed knowledge likewise remains `LOW` with required escalation.

M6 preserves the v1.4 CURRENT, AS_OF, and historical-interaction semantics. It also preserves the existing human correction and approval boundary: Retrieval and Reasoning never write shared Knowledge, new evidence does not become governed knowledge automatically, and only approved additive corrections may change shared state.

`CONF-PA-SYN-V1` remains active for v1.4. Knowledge-aware confidence requires a separately versioned future policy while keeping the public `HIGH`, `MEDIUM`, and `LOW` labels. Before governed knowledge changes rendered answers, snapshots must preserve execution-time knowledge participation; assertion identity alone is insufficient because state can later change.

The staged delivery plan is M6.0 architecture decision; M6.1 characterization of agreement, disagreement, stale knowledge, source unavailability, and candidate/superseded temporal cases; M6.2 internal dual-input `ReasoningInput` and comparison contracts without public behavior change; M6.3 confidence policy v2 and safe presentation; and M6.4 execution-time knowledge snapshot completeness.

M6 non-goals are knowledge replacing retrieval, automatic source-to-knowledge updates, probabilistic priors or arbitration, LLM arbitration, embeddings or vector search, a graph database, generalized lineage traversal, production healthcare integrations, and production compliance or authentication. The existing relational assertion, evidence, and lineage model remains sufficient unless a demonstrated future requirement proves otherwise.

## 16. Future Production Aspirations — Not MVP Commitments

Potential later work may include governed production integrations, enterprise identity and authorization, formal privacy and security controls, clinical validation, terminology normalization, scalable knowledge governance, model and retrieval evaluation, calibrated confidence, monitoring, retention policies, and resilient infrastructure.

Each production capability requires separate discovery, risk assessment, clinical governance, compliance review, acceptance criteria, and architecture decisions. The hackathon prototype must not imply that these capabilities already exist.
