# Synapse — Clinical Knowledge Copilot: Architecture

## 1. Scope and current posture

Synapse v1.4 is a local, deterministic, synthetic healthcare decision-support MVP. It demonstrates conflict-aware prior-authorization synthesis and a governed correction loop without real PHI, external clinical systems, an LLM, or production compliance claims.

The application is a modular monolith: FastAPI serves the API and static UI, SQLite stores source, knowledge, interaction, governance, and audit records, and mocked adapters represent five healthcare source types.

## 2. Responsibility boundaries

| Boundary | Responsibility |
|---|---|
| Retrieval | Which source evidence is applicable? Select source-document versions for `CURRENT` or `AS_OF` and return normalized evidence or explicit failure. |
| Knowledge | Which persisted assertions are applicable? Select current-applied or governed historical assertions and validate assertion/source intervals. |
| Reasoning | What does the applicable context imply? Reconcile the reasoning-only evidence view, create supported claims and citations, classify conflicts, confidence, and escalation. It does not decide temporal authority. |
| Governance | Which corrections become eligible/applied? Require human approval and atomically update document/assertion state while preserving history. |
| Interaction snapshot | What execution context and result were actually produced? Persist temporal identity, retrieved evidence membership/order, claims, citations, answer, confidence, and escalation. |
| Historical GET | What did Synapse say then? Read the persisted interaction without rerunning selection or reasoning. |

Retrieval and Knowledge do not call one another. Application orchestration invokes them independently and combines their results for reasoning.

## 3. Question paths and three temporal operations

```text
CURRENT question
  -> current source selection
  -> deterministic reasoning
  -> persisted snapshot

AS_OF question
  -> governed source interval selection + governed assertion interval selection
  -> orchestration builds applicable reasoning view
  -> deterministic reasoning
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

The live CURRENT question path reasons over current retrieved source evidence. The separate current-applied knowledge operation defines the persisted knowledge authority boundary and remains available to knowledge/governance consumers; assertion-level evidence composition is performed for AS_OF queries.

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

Approval changes V1 assertions from `APPLIED` to `SUPERSEDED`, V2 assertions from `CANDIDATE` to `APPLIED`, and source currentness in one caller-owned transaction. Lineage, review, knowledge update, and audit records are additive.

An approved retroactive correction may affect new AS_OF queries for the covered historical period. It cannot rewrite old snapshots. Approved overlapping intervals remain jointly applicable; no temporal winner is invented.

Future-effective approval is unresolved product behavior. Current approval immediately makes V2 current; AS_OF continues to honor effective intervals. There is no separate approved-but-not-yet-current state.

## 7. Temporal interaction snapshots

Schema v4 stores:

- on `interaction`: `temporal_mode`, `requested_as_of`, and `confidence_policy_id`;
- on `interaction_evidence`: the complete retrieved evidence universe, each `evidence_id`, and deterministic zero-based `ordinal`; and
- on `citation`: execution-time `source_id`, `document_version_id`, `evidence_id`, and existing display provenance.

These sets have different meanings:

```text
retrieved evidence: complete adapter output preserved for the interaction
claim evidence: retrieved evidence materially supporting a claim
citation evidence: retrieved evidence linked to a rendered claim citation
```

Claim and citation evidence are subsets of retrieved evidence. Evidence can be retained in the snapshot without materially contributing to the answer.

Later governance changes do not mutate an interaction's temporal mode, requested as-of time, confidence-policy identity, evidence membership/order, claims, citations, citation source/document-version identity, answer, confidence, or escalation.

Historical display is supported. Independent deterministic replay is not. Schema v4 preserves minimum immutable execution identities useful for future replay, but it does not store immutable copies of every evidence payload and provides no replay executor.

## 8. Confidence, escalation, and safe failure

Temporal queries use the existing deterministic confidence policy. Missing sources do not all have equal weight:

- unavailable applicable payer evidence produces `LOW` and escalation;
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

SQLite schema version **4** is required. Foundation metadata is `foundation-empty-v4`; the deterministic seeded demo baseline is `synthetic-pa-v1`. Any other nonzero schema version is rejected, explicitly including schema v3. No in-place migration framework exists; reset/rebuild is the supported local MVP upgrade path.

Implemented in v1.4: bounded CURRENT/AS_OF source and knowledge selection, public optional `as_of`, assertion-level applicability alignment, overlap safe failure, and temporal snapshot identity.

Deferred: independent replay, a generalized temporal framework, a distinct approved-but-not-yet-current state, generalized multi-version answer rendering, generalized graph traversal/cycle detection, arbitrary correction and upload workflows, a graph database, ontology/RDF, vector retrieval, LLM reasoning, arbitrary external healthcare integrations, production authentication/authorization, and production compliance controls.

The v1.3 knowledge release remains historical context: it introduced the persisted assertion, provenance, lineage, and current-applied boundaries upon which v1.4 builds.
