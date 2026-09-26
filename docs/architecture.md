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

## 10. M6 knowledge-aware reasoning decision (in development)

M6 will extend the reasoning boundary without changing the v1.4 implementation in this architecture-decision release. Its approved direction is:

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

### 10.1 Future orchestration-owned input

Application orchestration will eventually construct a conceptual `ReasoningInput` containing:

- temporal context;
- retrieved evidence;
- selected governed assertions;
- resolved assertion provenance; and
- deterministic origin and selection metadata.

`ReasoningInput` is a future internal concept, not an implemented type or public API. Retrieval and Knowledge remain independent selectors. Orchestration owns their composition, while Reasoning owns deterministic comparison, findings, confidence/escalation evaluation, and answer composition. Reasoning does not select temporal authority or write Knowledge.

The two input roles are:

- **SourceObservation:** a conclusion observed from source evidence selected by Retrieval.
- **GovernedBaselineAssertion:** a reviewed and governed persisted assertion selected by Knowledge. “Baseline” identifies its governed comparison role and never gives it automatic priority over a SourceObservation.

### 10.2 Origin and provenance preservation

Future normalized assertions will use these origins:

| Origin | Meaning |
|---|---|
| `SOURCE` | Supported by newly or currently retrieved source evidence. |
| `KNOWLEDGE` | Supported by a selected governed persisted assertion. |
| `CORROBORATED` | An equivalent, same-scope conclusion is independently supported by both inputs. |

`CORROBORATED` must retain both provenance chains; it is not a merged record that erases either source evidence or knowledge lineage. These values are frozen terminology, not an implemented enum.

A future knowledge-aware normalized assertion must retain enough immutable execution context to explain its selection and claims, including:

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

M6 freezes the following semantic outcomes. They are not implemented enums in M6.0.

| Outcome | Semantics |
|---|---|
| `AGREEMENT / CORROBORATION` | Equivalent same-scope, same-dimension conclusions are independently supported by SourceObservation and GovernedBaselineAssertion; preserve both chains. |
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

### 10.6 Confidence and snapshot release gates

`CONF-PA-SYN-V1` remains the active v1.4 confidence policy and is unchanged by M6.0. Knowledge-aware behavior requires a separately versioned future policy. That future policy may distinguish source-backed, knowledge-backed, and corroborated conclusions; a source newer than knowledge; knowledge-only presentation caused by retrieval failure; source/knowledge disagreement; governance pending; and knowledge validation failure. Public confidence labels remain exactly `HIGH`, `MEDIUM`, and `LOW`, and are not clinically validated probabilities.

Before governed knowledge can materially change rendered answers, interaction snapshots must preserve execution-time knowledge participation. The future design must evaluate storing, at minimum:

- selected assertion identity and deterministic order;
- origin or input role;
- assertion state at execution; and
- immutable assertion value, scope, and effective facts needed to explain the result.

An assertion ID alone is insufficient because assertion state and other mutable records can change after execution. M6.0 makes no schema change and does not claim that knowledge participation is currently snapshotted.

### 10.7 Technology boundary and staged delivery

The current relational `knowledge_assertion`, `assertion_evidence`, and `assertion_lineage` model is sufficient for M6. M6 does not require a graph database, RDF or an ontology, generalized graph traversal, or generic cycle detection. LLM reasoning, embeddings, vector retrieval, and probabilistic arbitration are also deferred; M6 comparison and reconciliation remain deterministic.

The staged plan is:

1. **M6.0 — Architecture decision:** freeze the boundary, terminology, authority rules, safety constraints, and release gates; no runtime behavior changes.
2. **M6.1 — Characterization:** cover agreement, disagreement, stale knowledge, unavailable source, and candidate/superseded temporal behavior.
3. **M6.2 — Internal contracts:** introduce the internal dual-input `ReasoningInput` and comparison contracts with no public behavior change.
4. **M6.3 — Policy and presentation:** add a separately versioned confidence policy and safe knowledge-aware presentation.
5. **M6.4 — Snapshot completeness:** persist sufficient immutable execution-time knowledge participation for historical explanation.

Explicit M6 non-goals are knowledge replacing retrieval, automatic knowledge updates from source evidence, probabilistic priors, LLM arbitration, vector search, a graph database, generalized lineage traversal, production healthcare integrations, and production compliance or authentication capabilities.
