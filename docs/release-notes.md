# Release Notes

## v1.3.0-knowledge

- Added first-class persisted knowledge assertions with normalized scope, decision dimensions, effective fields, and governed `CANDIDATE`, `APPLIED`, and `SUPERSEDED` states.
- Added foreign-key-backed, unique assertion-to-evidence links and provenance reads through evidence, document version, and source document metadata.
- Added assertion lineage alongside the existing, separate document-version lineage.
- Added `KnowledgeRepository` and `KnowledgeService` contracts for assertion, provenance, current-applied, and direct predecessor/successor reads.
- Enforced currentness integrity: current applied knowledge requires both an `APPLIED` assertion and a current source document version.
- Routed governed assertion state changes and assertion-lineage creation through the knowledge service while preserving caller-owned transaction control.
- Kept correction approval atomic across document currentness, assertion states, review, lineage, knowledge update, and audit records; failures roll back the full transition.

This release remains a deterministic local synthetic MVP. The knowledge layer is not yet part of the live question path and is not a generalized graph or temporal-reasoning engine.
