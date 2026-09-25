# Synapse — Clinical Knowledge Copilot

Synapse is a synthetic healthcare decision-support prototype for care coordinators, nurse navigators, and clinicians. It reconciles mocked patient, guideline, payer-policy, formulary, and specialist-note evidence; keeps clinical support distinct from coverage requirements; and produces cited answers with deterministic `HIGH`, `MEDIUM`, or `LOW` confidence and explicit escalation when evidence is insufficient.

All data is visibly synthetic. Synapse is not for clinical or coverage decisions and does not claim HIPAA compliance, clinical validation, production readiness, or autonomous decision authority.

## Current architecture

The executable demo is a local modular monolith:

- FastAPI serves the JSON API and static HTML/CSS/JavaScript UI.
- SQLite stores source documents and versions, evidence, knowledge assertions, interactions, claims, citations, feedback, reviews, lineage, updates, and audit events.
- Five local mocked adapters retrieve evidence in a fixed order: EHR, guideline, payer policy, formulary, and specialist note.
- Deterministic reasoning classifies compatible constraints and same-dimension conflicts, assigns categorical confidence, and applies escalation rules. No LLM is used.
- The v1.3 knowledge boundary is an evidence-backed assertion and lineage layer persisted in SQLite. It is the foundation for the product's knowledge graph concept, not a generalized graph engine.

The live question path is separate from the knowledge service:

```text
Question -> RetrievalService -> EvidenceBundle -> deterministic reasoning -> cited answer
```

`KnowledgeRepository` and `KnowledgeService` provide typed assertion, provenance, current-applied, and one-hop lineage access. The approval workflow also uses them to enforce assertion state transitions and create assertion lineage. They are not yet inputs to the live question path.

## Run the synthetic demo on Windows PowerShell

From the repository root, using the existing project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. If port 8000 is occupied, choose another local port, such as `--port 8010`.

The flagship flow is:

1. Select **Reset demo** to restore the `synthetic-pa-v1` baseline with Payer Policy V1 current.
2. Ask the prefilled prior-authorization question and inspect the five-source trace, claims, citations, reconciliation, and confidence rationale.
3. Submit the pre-seeded V2 correction. It remains `PENDING`; V1 stays current and its assertions remain `APPLIED`.
4. Approve the correction. The document-version switch, assertion state transitions, review, document-version lineage, assertion lineage, knowledge update, and audit records commit atomically.
5. Ask again. Retrieval uses current Payer Policy V2, while reopening the first interaction still returns its V1 answer, claims, and citations.

The UI also exposes synthetic missing-payer and true-conflict scenarios. Both produce `LOW` confidence and mandatory human escalation without inventing evidence.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Schema and reset behavior

The current SQLite schema version is **3**. The foundation metadata baseline is `foundation-empty-v3`; the seeded demo baseline is `synthetic-pa-v1`.

On startup, an empty database is initialized at schema v3 and seeded. A database with another nonzero schema version is rejected. In particular, schema-v2 databases are not migrated automatically: no in-place migration framework exists. For this local MVP, use the UI's **Reset demo** action to rebuild the configured database from the schema and version-controlled synthetic fixtures. Reset constructs and validates a temporary database, then atomically replaces the current demo database; a failed reset preserves the prior database.

## V1.3 knowledge capabilities

- First-class persisted `KnowledgeAssertion` records with subject, predicate, object/value, decision dimension, normalized scope, effective fields, and `CANDIDATE`, `APPLIED`, or `SUPERSEDED` state.
- Foreign-key-backed, unique `assertion_evidence` links and typed provenance reads through source document, version, and evidence metadata.
- Separate assertion lineage and document-version lineage retained after approval.
- Current-applied knowledge defined exactly as an `APPLIED` assertion whose source document version has `is_current = 1`.
- Governed knowledge mutations routed through `KnowledgeService` during approval, with caller-owned SQLite transaction scope.
- Immutable historical interaction snapshots: later current knowledge does not rewrite earlier answers or citations.
- Deterministic local synthetic fixtures and reset behavior.

`effective_from` and `effective_to` are persisted assertion data, but generalized temporal or as-of authority selection is not implemented in the knowledge layer.

## Limitations and deferred work

The current release does not provide generalized as-of selection, temporal authority, generalized multi-hop lineage traversal, generic cycle detection, arbitrary correction workflows, a graph database, ontology/RDF support, vector retrieval, LLM reasoning, arbitrary document uploads, external healthcare integrations, authentication, or production compliance controls. Audit history is application-preserved but not tamper-evident.

## Repository structure

```text
backend/                 FastAPI app, SQLite lifecycle, retrieval, reasoning, and knowledge modules
data/fixtures/           Version-controlled synthetic source fixtures
frontend/                Static demo UI
tests/                   Database, retrieval, reasoning, knowledge, API, and vertical-slice tests
docs/                    Product, architecture, domain-design, and release documentation
```

See [product specification](docs/product-spec.md), [conceptual architecture](docs/architecture.md), [technical architecture](docs/technical-architecture.md), and [release notes](docs/release-notes.md).
