# Release Notes

## v1.4.0-temporal

V1.4 implements the bounded temporal-querying contract while preserving v1.3 CURRENT behavior and the governed correction history.

- Added explicit `CURRENT` and `AS_OF` operations. CURRENT uses source `is_current` plus `APPLIED` assertions from current source versions. AS_OF requires governance eligibility and closed UTC effective-interval containment, with null `effective_to` open-ended.
- Added governed effective-interval source selection. All applicable versions are returned deterministically; AS_OF never falls back to current or ranks by newest/highest version, `recorded_at`, or approval time.
- Added knowledge assertion temporal selection across `APPLIED` and historically applicable `SUPERSEDED` assertions, requiring both assertion and source-document interval containment. `CANDIDATE` assertions remain excluded.
- Added optional public `as_of` on `POST /api/v1/questions`. Omission means CURRENT; a valid explicit UTC timestamp means AS_OF; malformed, naive, or non-UTC values return HTTP 422. No public `temporal_mode` field was added and the response shape remains compatible.
- Aligned assertion-level applicability with reasoning. Retrieval preserves applicable document evidence, Knowledge selects applicable persisted assertions, and orchestration builds a reasoning-only applicable evidence view. Raw retrieved evidence remains in the snapshot even when a narrower assertion does not contribute to the answer.
- Added no-winner overlap behavior. Opposing same-scope/same-dimension conclusions preserve both sides, remain unresolved, produce `LOW`, and require escalation. Multiple applicable payer versions that cannot be represented safely fail with HTTP 409 and create no fabricated interaction.
- Added the schema-v4 self-contained temporal interaction snapshot identity: `temporal_mode`, `requested_as_of`, confidence-policy identity, the complete retrieved evidence universe with deterministic zero-based ordinals, and execution-time citation source/document-version identity.
- Preserved historical snapshot immutability across later approval and governance changes. Historical GET reads persisted output without rerunning retrieval, knowledge selection, reasoning, or temporal selection.
- Defined retroactive semantics: an approved retroactive correction may affect new AS_OF queries for its covered period, but never rewrites old interactions. Approved overlaps remain visible without an invented winner.
- Advanced the required SQLite schema to **4**, foundation baseline to `foundation-empty-v4`, and retained demo baseline `synthetic-pa-v1`. Schema-v3 databases are rejected; reset/rebuild remains the supported local MVP upgrade path because no in-place migration framework exists.

Historical display is supported; independent deterministic replay is **not implemented**. Schema v4 preserves identities useful to a future replay design, but does not copy every evidence payload and provides no replay executor.

Known limitation: approval immediately makes V2 current even if it is future-effective, while AS_OF still honors effective intervals. There is no distinct approved-but-not-yet-current governance state.

### v1.4 release gate

- [x] CURRENT remains compatible with v1.3.
- [x] AS_OF source selection is implemented.
- [x] AS_OF knowledge selection is implemented.
- [x] Assertion applicability is aligned with reasoning.
- [x] Pending and approved V2 behavior is tested.
- [x] No silent temporal winner is selected.
- [x] Malformed temporal data fails safely.
- [x] Public `as_of` is validated.
- [x] Temporal snapshots are immutable.
- [x] Schema-v4 reset and integrity checks are clean.
- [x] Full suite is green.

Latest implementation validation before this documentation release commit: **299 passed**; `PRAGMA foreign_key_check`: clean; `PRAGMA integrity_check`: `ok`.

This remains a bounded synthetic MVP: no independent replay engine, generalized temporal framework, approved-but-not-yet-current state, generalized multi-version payer rendering, graph database, ontology/RDF, LLM reasoning, arbitrary external healthcare integrations, or production authentication/compliance controls.

## v1.3.0-knowledge

- Added first-class persisted knowledge assertions with normalized scope, decision dimensions, effective fields, and governed `CANDIDATE`, `APPLIED`, and `SUPERSEDED` states.
- Added foreign-key-backed, unique assertion-to-evidence links and provenance reads through evidence, document version, and source document metadata.
- Added assertion lineage alongside the existing, separate document-version lineage.
- Added `KnowledgeRepository` and `KnowledgeService` contracts for assertion, provenance, current-applied, and direct predecessor/successor reads.
- Enforced currentness integrity: current applied knowledge requires both an `APPLIED` assertion and a current source document version.
- Routed governed assertion state changes and assertion-lineage creation through the knowledge service while preserving caller-owned transaction control.
- Kept correction approval atomic across document currentness, assertion states, review, lineage, knowledge update, and audit records; failures roll back the full transition.

This release remains a deterministic local synthetic MVP. The knowledge layer is not yet part of the live question path and is not a generalized graph or temporal-reasoning engine.
