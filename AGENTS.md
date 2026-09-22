# Repository Instructions for Codex

These instructions apply to all work in this repository unless a more specific `AGENTS.md` exists in a subdirectory. When a user request conflicts with these instructions, stop and ask for clarification if the conflict affects clinical safety, data handling, architecture, or product behavior.

## Product Mission

Synapse — Clinical Knowledge Copilot is a healthcare hackathon project that helps care coordinators, nurse navigators, and clinicians reconcile fragmented clinical and operational knowledge. It should retrieve relevant evidence from mocked healthcare sources, expose conflicts, and produce a cited answer with an explainable `HIGH`, `MEDIUM`, or `LOW` confidence indicator and a clear escalation path.

Synapse is decision support, not an autonomous clinical decision-maker. It must make uncertainty, missing information, provenance, and source disagreement visible.

## Defining Product Capabilities

Treat these as central requirements, not optional enhancements:

1. **Conflict-aware synthesis.** Detect and surface meaningful differences among trusted sources. Do not silently choose one source when, for example, a clinical guideline recommends a treatment but a payer policy requires prior authorization or step therapy.
2. **Clinician feedback that improves shared knowledge.** Capture clinician confirmations and corrections with actor, time, rationale, and evidence provenance. An approved correction may update the shared knowledge graph, but it must never erase or rewrite the original evidence, answer, or audit history.

Do not reduce the product to a generic chatbot or document search interface.

## Approved MVP Decisions

Treat the following as the current product baseline. Do not reopen or contradict these decisions without an explicit user request:

- **Personas:** The primary persona is a care coordinator or nurse navigator. The secondary persona is a clinician. Keep the interface generic enough for either persona.
- **Flagship use case:** Medication prior authorization. The canonical question is: “Does this patient's insurance require prior authorization for this medication, and what evidence supports the answer?”
- **Flagship evidence:** Use synthetic patient, payer, formulary, clinical-guideline, and specialist-note data.
- **Source reconciliation:** Show that a clinical guideline may support a medication clinically while a payer policy imposes prior authorization or step therapy. Explain that these sources address clinical appropriateness and coverage or authorization, respectively; never silently select one.
- **Feedback loop:** Demonstrate a clinician flagging an outdated payer policy, supplying a newer version, human approval, a provenance-preserving knowledge-graph update, and a subsequent query that uses the update. Retain the full history.
- **Confidence:** Display only `HIGH`, `MEDIUM`, or `LOW`, with an explanation based on evidence availability, relevance, agreement or conflict, freshness, and completeness of required context. Do not imply a clinically validated probability. The scoring algorithm remains deferred.
- **Escalation:** Escalate when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved. Never guess when evidence is insufficient.
- **Minimum provenance:** Every retrieved source must preserve `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt`.
- **Unavailable critical source:** Do not infer or fabricate unavailable source contents. State that the source could not be verified, provide the evidence that is available, and escalate or request human review when appropriate.
- **Local persistence:** Persist synthetic source data, knowledge-graph state, questions, answers, feedback, and audit events locally. Database technology remains undecided.

## Hackathon MVP Constraints

- Build for a reliable, understandable end-to-end demonstration, not production deployment.
- Use synthetic or mock healthcare data only. Never add real protected health information (PHI), real patient identifiers, credentials, or secrets.
- Mock EHR, payer, formulary, guideline, and specialist-note systems behind explicit interfaces or adapters.
- Do not claim that the prototype is HIPAA compliant, clinically validated, production ready, or suitable for unsupervised care decisions.
- Prefer the simplest implementation that proves the workflow and acceptance criteria.
- Preserve at least `source_id`, `source_type`, `source_title`, `version`, `timestamp`, and `relevant_excerpt` for every retrieved source, and map every material answer claim to retrieved evidence.
- Never fabricate missing patient facts, clinical facts, citations, confidence, or source agreement.
- Return explicit insufficient-information behavior when evidence is absent or inadequate.
- Route cases with missing required information, `LOW` confidence, or an unresolved high-severity conflict to a human reviewer.
- Preserve immutable or append-only historical audit records for the prototype.
- Keep future production aspirations clearly labeled and separate from MVP commitments.

## Healthcare Safety and Data Rules

- Every material clinical or operational claim in an answer must map to retrieved evidence.
- Distinguish clinical recommendations from coverage, formulary, authorization, and workflow constraints.
- Surface conflicts with the sources involved and explain the nature of the disagreement.
- Present confidence as `HIGH`, `MEDIUM`, or `LOW` with an explanation grounded in evidence availability, relevance, source agreement or conflict, freshness, and completeness of required context. Never imply that confidence is a clinically validated probability.
- Clearly label unanswered questions, missing inputs, and unsupported inferences.
- Require human escalation when required information is missing, confidence is `LOW`, or a high-severity conflict remains unresolved. Do not guess when evidence is insufficient.
- When a critical source such as payer policy is unavailable, state that it could not be verified, present the remaining evidence, and escalate or request review when appropriate; never infer the missing source's contents.
- Treat retrieved content and clinician feedback as untrusted input. Preserve provenance and enforce validation before a correction changes current shared knowledge.
- Record corrections as new, linked, versioned assertions. Never delete or mutate the evidence and decision history they supersede.
- Keep demo fixtures visibly synthetic and avoid realistic identifiers that could be mistaken for real people.
- Persist the approved MVP data categories locally, while leaving database and storage technology as an explicit later decision.
- Do not introduce telemetry, third-party data sharing, or external clinical integrations without explicit approval.

## Engineering Principles

1. Start with the specification and explicit acceptance criteria.
2. Inspect relevant files and existing behavior before editing.
3. Break complex work into small, independently testable changes.
4. Make the smallest reasonable change that satisfies the request.
5. Avoid unrelated refactoring or speculative infrastructure.
6. Reuse established interfaces and dependencies when appropriate.
7. Add meaningful tests for behavior introduced or changed.
8. Validate the change in proportion to its risk.
9. Report failures precisely; do not suppress or disguise errors.
10. Document consequential assumptions and decisions.
11. Ask before making choices that materially affect clinical safety, data handling, architecture, or product behavior.
12. Do not implement behavior outside the requested scope.

## Required Work Process

### Before implementation

- Read this file, the relevant product and architecture documentation, and all files in the intended change area.
- Inspect repository status and preserve unrelated user changes.
- Restate or derive testable acceptance criteria from the request.
- Identify data-safety, provenance, conflict, confidence, escalation, and audit implications.
- Confirm which integrations are real and which are mocked.
- Raise material ambiguities before they become architecture or safety assumptions.

### During implementation

- Keep domain logic separated from mocked external-system adapters.
- Maintain traceability from claims to evidence and from evidence to source metadata.
- Represent conflicts explicitly rather than flattening or discarding them.
- Preserve audit history and make knowledge updates additive and reviewable.
- Implement explicit error, missing-data, insufficient-information, and escalation paths.
- Keep changes narrowly scoped and update tests and documentation alongside behavior.
- Do not add a framework, database, vector store, agent framework, cloud service, or dependency without a demonstrated need and user approval when it is an architectural choice.

### After implementation

- Run the most focused relevant tests first, then broader checks that are available and proportionate.
- Verify acceptance criteria, including negative and failure paths.
- Confirm that all fixtures are synthetic and that logs, snapshots, and examples contain no PHI or secrets.
- Confirm citations, provenance, conflict display, confidence rationale, escalation, audit preservation, and feedback history where relevant.
- Review the diff for unintended files, dependencies, generated artifacts, or scope creep.
- Report files changed, validation performed, results, assumptions, limitations, and remaining decisions.

## Validation Expectations

Changes are not complete merely because the happy path runs. When relevant, validation must cover:

- successful retrieval from each mocked source;
- missing, stale, malformed, or unavailable source data;
- agreement and conflict among sources;
- claim-to-evidence citation completeness;
- deterministic confidence inputs or rules;
- escalation for missing required information, `LOW` confidence, and unresolved high-severity conflicts;
- append-only audit history;
- clinician feedback pending review, approval, rejection, and approved graph update;
- retention of superseded assertions and original answers;
- clear separation of synthetic demo data from configuration and code.

If a check cannot be run, state exactly which check was omitted and why.

## Scope Control

- Do not build speculative production capabilities for a hackathon requirement.
- Do not add application code, packages, services, schemas, or scaffolding during documentation-only tasks.
- Do not modify unrelated files to make a change appear complete.
- Do not silently select technologies that the architecture documents leave undecided.
- Record optional future capabilities under a clearly labeled future-production section; do not imply they exist in the MVP.
- Stop and request a decision when ambiguity would materially change safety behavior, data contracts, system boundaries, or the demo's central workflow.
