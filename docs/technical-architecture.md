# Synapse — Clinical Knowledge Copilot: Technical Architecture

## 1. Runtime and component boundary

Synapse M6 is a local modular monolith. FastAPI serves a framework-free static UI and JSON API; SQLite stores synthetic source documents, evidence, knowledge assertions, interactions, feedback, reviews, lineage, updates, and audit events. All connections enable foreign keys, and `managed_connection` commits on success or rolls back on exception.

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

The current fixed approval transaction marks V1 noncurrent/SUPERSEDED and V2 current/APPLIED immediately. Therefore approval before V2's `effective_from` would still make V2 current, although AS_OF honors the interval. A distinct approved-but-not-yet-current state is not modeled; this remains an unresolved product decision.

## 8. Schema-v5 interaction snapshot

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

Historical display is supported, and later approval or mutation of live assertion state does not change the execution-time knowledge snapshot. Independent deterministic replay is **not implemented**: there is no replay executor, and schema v5 snapshot completeness supports historical explanation rather than a guarantee that arbitrary executions can be rerun.

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
| Schema version | `5` |
| Foundation baseline | `foundation-empty-v5` |
| Demo baseline | `synthetic-pa-v1` |
| SQLite `user_version` | `5` |

Startup initializes an empty database from `backend/schema.sql`, applies `backend/demo_schema.sql`, seeds version-controlled synthetic fixtures, and validates metadata. Any other nonzero version is rejected, including schema v4. There is no in-place migration framework.

Reset/rebuild is required for existing local databases and is the supported MVP upgrade path. Reset builds and validates a temporary schema-v5 database, including foreign-key and SQLite integrity, then atomically replaces the configured database. Failure preserves the prior database.

## 11. Implemented and deferred boundary

Implemented through M6:

- CURRENT and AS_OF source selection;
- current and AS_OF knowledge selection;
- public optional `as_of` validation;
- assertion-level applicability composition;
- overlap/no-winner behavior;
- deterministic source confidence and escalation;
- live dual-input prior-authorization reasoning with `SourceObservation`, `GovernedBaselineAssertion`, and `ReasoningInput`;
- deterministic source/knowledge corroboration, source-only, knowledge-only, stale-disagreement, conflict, compatible-constraint, missing-source, malformed-knowledge, and governance-pending classifications;
- `CONF-PA-SYN-V2` confidence and escalation for the knowledge-aware PA path;
- schema-v5 temporal plus execution-time knowledge snapshots and historical display; and
- atomic snapshot persistence and historical snapshot immutability.

Deferred:

- independent deterministic replay;
- a generalized temporal framework;
- approved-but-not-yet-current governance state;
- generalized multi-version payer rendering;
- generalized graph traversal/cycle prevention;
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
