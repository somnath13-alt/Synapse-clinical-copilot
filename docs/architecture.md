# Synapse — Clinical Knowledge Copilot: Conceptual Architecture

## 1. Scope and Architectural Posture

This document describes the conceptual architecture for the hackathon MVP. It defines responsibilities and boundaries without selecting a framework, database, vector store, agent framework, model vendor, or cloud platform.

The MVP uses only synthetic data and mocked external healthcare systems. “Agent” describes a bounded responsibility; it does not require a particular agent framework or an autonomous process. “Knowledge graph” describes a graph-shaped domain model of evidence-backed entities and assertions; it does not require a graph database.

## 2. Approved MVP Decisions

These approved decisions are architecture constraints for the MVP:

- The primary persona is a care coordinator or nurse navigator; the secondary persona is a clinician. The UI remains generic enough for both.
- The flagship flow answers: **“Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?”**
- The flagship uses synthetic patient, payer-policy, formulary, clinical-guideline, and specialist-note data.
- The system must preserve and explain the distinction between a guideline supporting clinical appropriateness and a payer imposing coverage, prior-authorization, or step-therapy requirements. Neither source may be silently selected over the other.
- A clinician may flag an outdated payer policy and supply a newer version. Only explicit human approval may make the correction current; the graph update preserves provenance and history, and subsequent queries use the approved information.
- User-facing confidence is `HIGH`, `MEDIUM`, or `LOW`, explained through evidence availability, relevance, agreement or conflict, freshness, and completeness of required context. It is not a clinically validated probability; the calculation remains deferred.
- Escalation is required for missing required information, `LOW` confidence, or an unresolved high-severity conflict. The system never guesses when evidence is insufficient.
- Every retrieved source includes at least `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
- An unavailable critical source is never inferred or fabricated. The answer states that it could not be verified, presents other available evidence, and escalates or requests review when appropriate.
- Synthetic source data, knowledge-graph state, questions, answers, feedback, and audit events persist locally. Storage technology remains undecided.

## 3. End-to-End Flow

```text
Care Coordinator / Nurse Navigator (primary) or Clinician (secondary)
  -> Chat UI
  -> Orchestrator
  -> Specialized Retrieval Agents
       -> EHR
       -> Guidelines / Internal Pathways
       -> Payer Policies
       -> Formulary
       -> Specialist Notes
  -> Shared Knowledge Graph
  -> Conflict Detection / Supervisor
  -> Cited + Confidence-Labeled Answer
  -> Human Escalation when needed
  -> Audit Record
  -> Clinician Feedback
  -> Review / Approval
  -> Versioned Knowledge Graph Update
```

The audit trail spans the full flow rather than existing only at the end. Feedback updates create new linked assertions; they do not rewrite prior evidence or interaction records.

For the flagship scenario, the flow reconciles clinical appropriateness with coverage and authorization requirements; those dimensions may constrain one another without being logically contradictory.

## 4. Conceptual Data Contracts

These are technology-neutral concepts, not finalized schemas.

### Question context

- question text;
- canonical-question marker when the medication prior-authorization flagship is used;
- synthetic patient or case reference;
- requesting synthetic user or role;
- request timestamp;
- optional interaction identifier;
- explicit known and missing context.

### Evidence item

- `source_id`;
- `source_type`;
- `source_title`;
- `version`;
- `timestamp`;
- `relevant_excerpt`;
- source document or record reference;
- retrieval status and staleness metadata;
- synthetic-data marker.

The first six fields are mandatory for every retrieved source. Additional fields may supplement them but cannot replace them.

### Assertion

- subject, relationship or predicate, and value or object;
- supporting evidence references;
- source-specific effective time where applicable;
- status such as current, disputed, pending, or superseded;
- link to a prior assertion when revised;
- creation and approval provenance.

### Conflict

- assertions and sources involved;
- conflict type, such as contradiction, eligibility mismatch, coverage constraint, recency mismatch, or missing prerequisite;
- explanation and effect on the answer;
- resolution status, which may remain unresolved.

### Answer result

- answer sections separating clinical guidance, operational or coverage constraints, missing information, and next steps;
- claim-to-evidence citation mapping;
- conflicts;
- confidence category (`HIGH`, `MEDIUM`, or `LOW`) and rationale;
- escalation status and reason;
- interaction and audit references.

### Feedback record

- target answer or assertion;
- confirmation or proposed correction;
- rationale and supporting evidence references;
- synthetic author or role and timestamp;
- pending, approved, or rejected review status;
- reviewer provenance and resulting assertion reference, if approved.

## 5. Major Components

### 5.1 Care Coordinator, Nurse Navigator, or Clinician

**Responsibility:** Ask a question, inspect evidence and conflicts, act on limitations or escalation, and provide confirmation or correction.

**Inputs:** Synthetic patient context, clinical or operational question, displayed answer, citations, and feedback controls.

**Outputs:** Question, optional context, feedback, correction rationale, and human decisions.

**Boundary:** The user remains the decision-maker. Synapse does not diagnose, prescribe, approve coverage, or alter a real clinical record.

**Hackathon MVP:** A care coordinator or nurse navigator is the primary demo persona; a clinician is secondary. Both operate entirely on synthetic cases.

**Mocked:** Real identity, role verification, clinical privileges, and production workflow integration.

### 5.2 Chat UI

**Responsibility:** Capture questions and context; present answers, citations, conflicts, confidence rationale, missing information, escalation status, audit history, and feedback state.

**Inputs:** User questions and actions; answer, evidence, audit, escalation, and feedback data from application services.

**Outputs:** Structured requests, feedback commands, and human-readable views.

**Boundary:** Presentation and input validation only. It must not invent evidence, calculate authoritative confidence, resolve conflicts, or directly mutate the knowledge graph.

**Hackathon MVP:** A role-neutral interface sufficient for the golden scenarios, including visible synthetic-data and prototype notices, usable by either approved persona.

**Mocked:** Enterprise authentication, accessibility certification, production notification channels, and integration into clinical desktops.

### 5.3 Orchestrator

**Responsibility:** Interpret the question, identify required source types, create a retrieval plan, invoke the relevant retrieval agents, and assemble their results for downstream evaluation.

**Inputs:** Question context and available source capability descriptions.

**Outputs:** Recorded retrieval plan, normalized evidence results, and explicit source errors or omissions.

**Boundary:** It coordinates retrieval but does not silently decide clinical truth, hide source failures, or produce uncited final claims.

**Hackathon MVP:** Bounded, explainable routing for the canonical medication prior-authorization question and supporting scenarios. The flagship plan includes synthetic patient, payer-policy, formulary, clinical-guideline, and specialist-note sources.

**Mocked:** Broad intent coverage, dynamic discovery of enterprise sources, and production workload management.

### 5.4 Specialized Retrieval Agents and Source Adapters

Each retrieval agent translates a source-specific response into normalized evidence. The adapter boundary keeps mock sources replaceable and prevents source details from leaking into synthesis logic.

Every successful retrieval must return the six mandatory provenance fields. Failures return an explicit status rather than fabricated evidence.

#### EHR Agent

**Responsibility:** Retrieve relevant synthetic demographics, conditions, medications, allergies, labs, encounters, or other mock chart facts.

**Inputs:** Synthetic case reference and requested fact categories.

**Outputs:** Normalized evidence items or explicit missing-data and retrieval statuses.

**Boundary:** Read-only retrieval; no chart writes and no inference of absent facts.

**MVP versus mocked:** Retrieval logic and normalization are real; the EHR and patient data are locally persisted synthetic fixtures.

#### Guideline Agent

**Responsibility:** Retrieve relevant synthetic clinical guidelines and internal pathways, including version and effective-date metadata.

**Inputs:** Question concepts, patient-context criteria, and requested topics.

**Outputs:** Relevant recommendations, applicability criteria, and provenance as evidence items.

**Boundary:** Retrieves and maps guidance but does not determine final patient-specific action.

**MVP versus mocked:** Search over a curated synthetic corpus is real; external guideline services and production internal repositories are mocked.

#### Payer Policy Agent

**Responsibility:** Retrieve synthetic coverage rules, prior-authorization criteria, step-therapy requirements, and documentation prerequisites.

**Inputs:** Synthetic payer or plan, intervention or medication, and relevant patient-context facts.

**Outputs:** Policy constraints and provenance, plus explicit unknown or unavailable status.

**Boundary:** Does not promise coverage, submit authorization, or treat payer rules as clinical recommendations.

**MVP versus mocked:** Policy matching and normalization are real; payer systems, plan data, and policies are mocked.

#### Formulary Agent

**Responsibility:** Retrieve synthetic tier, restriction, preferred-alternative, and formulary-status information.

**Inputs:** Synthetic plan reference and medication concepts.

**Outputs:** Formulary evidence and restrictions with provenance.

**Boundary:** Does not determine medical appropriateness, price, or actual dispensing eligibility.

**MVP versus mocked:** Relevant lookup behavior is real; formulary service and data are mocked.

#### Specialist Notes Agent

**Responsibility:** Retrieve relevant statements from synthetic specialist consultation notes.

**Inputs:** Synthetic case reference, specialty, date range, and question concepts.

**Outputs:** Cited excerpts or structured assertions with author role, note date, and note reference.

**Boundary:** Does not reinterpret absent statements as findings or treat an old note as current without exposing its date.

**MVP versus mocked:** Note retrieval and provenance extraction are real; the note repository and all note content are synthetic fixtures.

### 5.5 Shared Knowledge Graph

**Responsibility:** Provide a shared representation of entities, evidence-backed assertions, relationships, provenance, state, and version history across sources.

**Inputs:** Normalized evidence, derived assertions, conflict links, and approved feedback updates.

**Outputs:** Relevant subgraphs or assertion sets for supervision, citations, audit, and later questions.

**Boundary:** Stores coexistence and lineage; it does not decide which assertion is clinically authoritative. Unapproved feedback cannot change current knowledge.

**Hackathon MVP:** A small graph-like domain model whose state persists locally using an implementation selected later.

**Mocked:** Enterprise-scale persistence, terminology services, access control, knowledge governance, replication, and graph infrastructure.

### 5.6 Conflict Detection

**Responsibility:** Compare assertions addressing the same decision or fact and classify disagreement, coverage constraints, missing prerequisites, temporal mismatch, and compatible differences.

**Inputs:** Evidence-backed assertions and their provenance, versions, and effective dates.

**Outputs:** Explicit conflict records with involved sources, explanation, severity or relevance, and unresolved status.

**Boundary:** Detection does not silently discard a source or resolve a clinically meaningful conflict. Any precedence rule must be explicit, documented, and visible in the result.

**Hackathon MVP:** Deterministic handling for the flagship distinction: a clinical guideline may support the medication while payer policy imposes prior authorization or step therapy. The result explains clinical appropriateness separately from coverage and authorization rather than silently selecting a source.

**Mocked:** Comprehensive medical reasoning, ontology-wide contradiction detection, and production clinical governance.

### 5.7 Supervisor and Answer Composer

**Responsibility:** Evaluate relevance, provenance, completeness, recency, and conflicts; produce one structured answer with citations, confidence, limitations, and next steps.

**Inputs:** Question context, retrieval results, relevant knowledge subgraph, and conflict records.

**Outputs:** Answer result, claim-to-evidence map, confidence rationale, missing-information statement, and escalation recommendation.

**Boundary:** It cannot create evidence, conceal conflicts, or make unsupported claims. Clinical guidance and operational or coverage constraints remain distinct.

**Hackathon MVP:** Explainable synthesis for curated scenarios, with deterministic checks guarding citation and escalation behavior.

**Mocked:** Clinically validated generalized reasoning, calibrated risk prediction, and autonomous decision authority.

### 5.8 Confidence Evaluation

**Responsibility:** Produce a `HIGH`, `MEDIUM`, or `LOW` confidence category and explanation using evidence availability, relevance, agreement or conflict, freshness, and completeness of required context.

**Inputs:** Source availability, evidence relevance and completeness, agreement or conflict, recency metadata, and required-context status.

**Outputs:** One approved confidence category, contributing factors, and applicable escalation signal.

**Boundary:** Confidence is a transparent prototype indicator, not a clinically validated probability. Do not expose a numeric probability. `LOW` confidence always triggers escalation.

**Hackathon MVP:** The three labels and input factors are fixed. The exact mapping or scoring algorithm remains an implementation decision.

**Mocked:** Statistical calibration, continuous monitoring, and clinical validation.

### 5.9 Human Escalation

**Responsibility:** Create and display a review request when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved.

**Inputs:** Answer result, confidence category, missing-information status, conflict severity, and conflict resolution status.

**Outputs:** Escalation record containing reason, required reviewer or expertise, requested information, status, and timestamps.

**Boundary:** Escalation does not imply that a reviewer has acted. The system must not fabricate a resolution.

**Hackathon MVP:** An in-application escalation record and demonstrable review state.

**Mocked:** Live paging, inboxes, service-level agreements, staffing workflows, and ticket-system integration.

### 5.10 Audit Service / Audit View

**Responsibility:** Preserve an inspectable history of questions, retrieval plans, evidence, conflicts, answers, citations, confidence, escalation, actions, feedback, reviews, and graph updates.

**Inputs:** Events emitted throughout the workflow.

**Outputs:** Chronological, linked records for a demo interaction and its later corrections.

**Boundary:** Historical events are append-only in product semantics. Updates may add status events but must not erase the original record.

**Hackathon MVP:** A locally persisted event history sufficient to prove lineage across questions, answers, feedback, approvals, graph updates, and subsequent queries. Persistence technology is selected later.

**Mocked:** Tamper-evident storage, enterprise retention, legal holds, compliance reporting, and security monitoring.

### 5.11 Clinician Feedback and Review

**Responsibility:** Capture confirmations and proposed corrections, attach provenance, manage pending or approved or rejected review state, and request an approved knowledge update.

**Inputs:** Target answer or assertion, feedback type, correction, rationale, evidence references, synthetic actor, and reviewer decision.

**Outputs:** Feedback and review records; an approved update command referencing both new and superseded assertions.

**Boundary:** Submission alone never changes current shared knowledge. Review decisions are explicit and auditable.

**Hackathon MVP:** A clinician flags a payer policy as outdated, supplies the newer policy version, and creates a pending record. A separate explicit human action approves or rejects it.

**Mocked:** Enterprise identity, role authorization, multi-party governance, notifications, and adjudication workflows.

### 5.12 Knowledge Graph Update

**Responsibility:** Validate an approved correction and append a new versioned assertion with links to its provenance and predecessor.

**Inputs:** Approved feedback record, reviewer provenance, supporting evidence, and target assertion.

**Outputs:** New current assertion, prior assertion marked superseded through history-preserving state, and audit event.

**Boundary:** Never delete or rewrite original evidence, prior assertions, answers, or feedback. Reject invalid or unapproved update commands explicitly.

**Hackathon MVP:** A deterministic append-and-link update for the approved payer-policy correction. A subsequent canonical query uses the new current policy assertion.

**Mocked:** Production knowledge stewardship, rollback governance, distributed transactions, and enterprise publishing.

## 6. Cross-Cutting Workflow

1. The role-neutral UI sends the canonical medication prior-authorization question and explicit synthetic context to the orchestrator.
2. The orchestrator records a plan and requests synthetic patient, payer-policy, formulary, clinical-guideline, and specialist-note evidence.
3. Adapters return normalized evidence with the six mandatory provenance fields, or explicit failure and missing-data results.
4. Evidence-backed assertions are assembled into the shared graph-shaped model; no absent fact is inferred.
5. Conflict detection compares related assertions and makes the clinical-appropriateness versus coverage-or-authorization distinction visible.
6. The supervisor composes an answer whose material claims map to evidence and never silently selects the guideline or payer policy over the other.
7. Confidence evaluation emits `HIGH`, `MEDIUM`, or `LOW` with its factors. Missing required information, `LOW` confidence, or an unresolved high-severity conflict triggers escalation.
8. Local persistence preserves synthetic sources, knowledge-graph state, questions, answers, feedback, and audit events.
9. The audit trail preserves inputs, intermediate decisions, outputs, and actions.
10. Clinician feedback that supplies a newer payer-policy version is recorded as pending. Approval or rejection is a separate human action.
11. An approved correction appends a linked assertion and preserves the superseded assertion and original interaction history.
12. A subsequent canonical query uses the approved current policy assertion.

## 7. Error and Safety Boundaries

- **Unavailable critical source:** explicitly state that the source could not be verified, do not infer or fabricate its contents, present the evidence that is available, and escalate or request human review when appropriate.
- **Missing patient fact:** identify the exact missing fact; never fill it from general knowledge.
- **Stale or unversioned evidence:** surface the metadata limitation and reduce confidence as defined by policy.
- **Malformed adapter response:** exclude it from supported claims, record the failure, and continue only if the remaining evidence is sufficient.
- **Material source conflict:** display both positions and their provenance; do not silently resolve it.
- **Uncited generated claim:** block or remove the material claim rather than inventing a citation.
- **Required escalation:** create a human escalation with a clear reason when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved.
- **Unapproved correction:** retain it as pending and leave current knowledge unchanged.
- **Failed approved update:** preserve the approval record, record the failure, and do not partially overwrite knowledge history.

## 8. MVP Reality Versus Mocked Environment

The following behaviors should be real and demonstrable in the MVP:

- question intake and source planning;
- invocation of source-specific adapters;
- normalized evidence and provenance;
- shared graph-shaped assertions and lineage;
- explicit conflict detection for golden scenarios;
- claim-level citations;
- documented confidence logic and insufficient-information behavior;
- escalation creation;
- append-only audit semantics;
- feedback review and approved, versioned knowledge update;
- local persistence of synthetic sources, knowledge-graph state, questions, answers, feedback, and audit events.

The following remain mocked or simplified:

- all patient, clinical, payer, formulary, guideline, and note data;
- external healthcare systems and write-backs;
- user identity, authorization, and reviewer credentials;
- live human routing and notification delivery;
- production security, privacy, compliance, resilience, scale, and governance;
- comprehensive clinical reasoning and confidence calibration.

## 9. Implementation Decisions Intentionally Deferred

Later implementation planning must choose only what the accepted demo requires. No choice is made here for:

- frontend or backend framework;
- programming language or repository layout beyond the current skeleton;
- model or model provider;
- retrieval technique, embeddings, or vector store;
- graph representation or database;
- agent orchestration library;
- audit persistence mechanism;
- local persistence technology and data format;
- hosting, cloud, container, or deployment platform;
- authentication and authorization approach.

Options may be evaluated against demo reliability, implementation time, testability, explainability, offline behavior, data handling, and team familiarity.

The requirement for local persistence is approved; only its implementation is deferred. Likewise, the confidence labels and their input factors are approved, while the category-mapping algorithm remains deferred.

## 10. Future Production Architecture — Not Part of the MVP

A production system would require separate design and validation for real-system integrations, identity and least-privilege authorization, consent and data segmentation, encryption and key management, comprehensive audit controls, security monitoring, retention, high availability, disaster recovery, terminology normalization, knowledge governance, model evaluation, clinical safety management, regulatory obligations, and ongoing human oversight.

The hackathon architecture is not evidence of HIPAA compliance, clinical validation, or production readiness. Moving toward production requires explicit organizational, clinical, security, privacy, legal, and compliance decisions.

## 11. Remaining Open Architecture Decisions

### Product and workflow details

- Exact synthetic medication, indication, patient facts, payer, formulary status, guideline excerpt, specialist-note statement, and policy criteria.
- Definition and assignment of “high severity” for unresolved conflicts.
- Human role authorized to approve payer-policy corrections and whether the submitting clinician may also approve.
- Rule for optional versus mandatory review when a critical source is unavailable beyond the approved escalation triggers.
- Exact prototype and synthetic-data disclaimer language.

### Implementation details

- Confidence-factor mapping that produces `HIGH`, `MEDIUM`, or `LOW`.
- Local persistence technology, data format, lifecycle, and reset mechanism.
- Frontend and backend frameworks or languages.
- Model provider, retrieval technique, graph representation, and orchestration approach.
- Audit-view placement and detail level.
- Source `timestamp` semantics and format.
- Hosting, packaging, and deployment approach for the hackathon environment.
- Numeric latency target.
