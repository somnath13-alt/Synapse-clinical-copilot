# Synapse — Clinical Knowledge Copilot: Technical Architecture

## 1. Runtime and component boundary

Synapse v1.4 is a local modular monolith. FastAPI serves a framework-free static UI and JSON API; SQLite stores synthetic source documents, evidence, knowledge assertions, interactions, feedback, reviews, lineage, updates, and audit events. All connections enable foreign keys, and `managed_connection` commits on success or rolls back on exception.

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

## 5. Assertion-level applicability and reasoning

For AS_OF, application orchestration first retains the complete retrieval bundle. It separately asks Knowledge for matching assertion families and their applicable assertions. It then builds a reasoning-only bundle that keeps evidence not governed by those families and only evidence linked to applicable governed assertions within a governed family.

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

## 8. Schema-v4 interaction snapshot

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

`GET /api/v1/interactions/{interaction_id}` reads the stored interaction, claims, and citations. It does not rerun retrieval, knowledge selection, reasoning, or temporal selection.

Historical display is supported. Independent deterministic replay is **not implemented**: schema v4 stores minimum immutable execution identities useful to a future replay design, but it does not store immutable copies of every evidence payload and no replay executor exists.

## 9. Confidence and failure mapping

Temporal questions use the existing deterministic policy (`CONF-PA-SYN-V1`), not a probability. Evidence criticality matters:

- unavailable applicable payer evidence: `LOW` and escalation;
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
| Schema version | `4` |
| Foundation baseline | `foundation-empty-v4` |
| Demo baseline | `synthetic-pa-v1` |
| SQLite `user_version` | `4` |

Startup initializes an empty database from `backend/schema.sql`, applies `backend/demo_schema.sql`, seeds version-controlled synthetic fixtures, and validates metadata. Any other nonzero version is rejected, including schema v3. There is no in-place migration framework.

Reset/rebuild is the supported local MVP upgrade path. Reset builds and validates a temporary schema-v4 database, including foreign-key and SQLite integrity, then atomically replaces the configured database. Failure preserves the prior database.

## 11. Implemented and deferred boundary

Implemented in v1.4:

- CURRENT and AS_OF source selection;
- current and AS_OF knowledge selection;
- public optional `as_of` validation;
- assertion-level applicability composition;
- overlap/no-winner behavior;
- deterministic confidence and escalation; and
- schema-v4 temporal snapshot identities and historical display.

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

## 12. Future M6 dual-input technical boundary

M6.0 is documentation only. The current implementation continues to pass an `EvidenceBundle` to Reasoning. For AS_OF only, application orchestration currently uses applicable knowledge assertions to compose a narrower reasoning evidence bundle; CURRENT reasoning does not yet consume selected governed assertions as a peer input. No `ReasoningInput`, assertion-origin enum, comparison-outcome enum, confidence policy v2, or knowledge-participation snapshot exists yet.

The future internal flow is:

```text
Retrieval -> SourceObservation -----------+
                                            +-> orchestration-owned ReasoningInput
Knowledge -> GovernedBaselineAssertion ---+     -> deterministic comparison/findings
                                                  -> confidence/escalation
                                                  -> answer composition
```

`SourceObservation` means a conclusion observed from source evidence selected by Retrieval. `GovernedBaselineAssertion` means a reviewed/governed persisted assertion selected by Knowledge; baseline is a comparison point and confers no precedence. The conceptual orchestration-owned `ReasoningInput` will carry temporal context, retrieved evidence, selected governed assertions, resolved assertion provenance, and deterministic origin/selection metadata.

Future normalized origins are `SOURCE`, `KNOWLEDGE`, and `CORROBORATED`. Corroboration requires equivalent same-scope support from both inputs and must retain both provenance chains. Comparison contracts will represent `AGREEMENT / CORROBORATION`, `SOURCE_ONLY`, `KNOWLEDGE_ONLY`, `STALE_KNOWLEDGE_DISAGREEMENT`, `SAME_DIMENSION_CONFLICT`, `COMPATIBLE_CROSS_DIMENSION_CONSTRAINT`, `MISSING_SOURCE_CHANNEL`, `MALFORMED_KNOWLEDGE_CHANNEL`, and `GOVERNANCE_PENDING`. These are frozen semantics, not current code declarations.

Selection remains outside Reasoning. CURRENT source and assertion selection and AS_OF source/assertion interval selection retain the rules in sections 3 and 4. `CANDIDATE` is never an authoritative input; `SUPERSEDED` is excluded from CURRENT but can be historically eligible in AS_OF. Retrieval and Reasoning never write Knowledge, and source evidence cannot bypass the caller-owned correction/approval transaction.

The implementation must compare only compatible scopes and dimensions. Clinical appropriateness and payer authorization are distinct dimensions, so recency across them creates no override. Opposing same-scope, same-dimension conclusions retain both inputs and provenance with no winner. Critical payer source unavailability cannot be repaired by knowledge: a last governed payer baseline may be displayed as unverified, but confidence remains `LOW` and escalation remains mandatory.

`CONF-PA-SYN-V1` remains active until a separately versioned knowledge-aware policy is designed and validated. Before that future behavior can materially change rendered answers, the snapshot design must persist sufficient immutable execution facts for selected assertions: identity, deterministic order, origin/role, execution-time state, value, scope, and effective facts. An assertion foreign key alone is not a historical snapshot.

The existing relational `knowledge_assertion`, `assertion_evidence`, and `assertion_lineage` tables are sufficient for M6. No schema change is approved in M6.0, and graph databases, RDF/ontology, generalized traversal/cycle detection, embeddings, vector retrieval, LLM reasoning, and probabilistic arbitration remain deferred.
