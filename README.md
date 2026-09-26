# Synapse — Clinical Knowledge Copilot

Synapse is a synthetic healthcare decision-support prototype for care coordinators, nurse navigators, and clinicians. It reconciles mocked patient, guideline, payer-policy, formulary, and specialist-note evidence; keeps clinical support distinct from coverage requirements; and produces cited answers with deterministic `HIGH`, `MEDIUM`, or `LOW` confidence and explicit escalation when evidence is insufficient.

All data is visibly synthetic. Synapse is not for clinical or coverage decisions and does not claim HIPAA compliance, clinical validation, production readiness, or autonomous decision authority.

## Current release: v1.4.0-temporal

The local modular monolith uses FastAPI, SQLite, a static browser UI, and five mocked source adapters. Deterministic code performs retrieval, temporal selection, assertion applicability, reconciliation, confidence, escalation, citation construction, governed feedback approval, and snapshot persistence. No LLM is used.

The question path supports three deliberately separate operations:

- **CURRENT:** “What does Synapse consider current now?” Source versions use `is_current = 1`; knowledge requires an `APPLIED` assertion from a current source-document version.
- **AS_OF:** “What governed knowledge is applicable at time T?” Source and assertion effective intervals must contain the requested UTC instant, and governance eligibility is required. There is no fallback to current and no newest/highest-version or approval-time winner.
- **Historical interaction:** “What did Synapse actually produce earlier?” `GET /api/v1/interactions/{interaction_id}` reads the persisted snapshot without rerunning retrieval, selection, or reasoning.

`POST /api/v1/questions` accepts `question` and optional `as_of` (alongside the existing demo `source_mode`). Omitting `as_of` selects `CURRENT`; a valid explicit UTC timestamp selects `AS_OF`. Malformed, naive, or non-UTC values return HTTP 422. The response remains compatible with the pre-v1.4 shape; `temporal_mode` is not a public request field.

The schema-v4 interaction snapshot records temporal mode, requested as-of time, confidence-policy identity, the complete retrieved evidence membership in deterministic order, and execution-time citation source/document-version identity. Historical display is supported. Independent deterministic replay is not: snapshots do not copy every evidence payload and there is no replay executor.

## Run the synthetic demo

From the repository root in Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The flagship flow asks the prior-authorization question, submits the pre-seeded Payer Policy V2 correction, approves it, asks again, and reopens the immutable earlier V1 interaction. Missing-payer and true-conflict scenarios demonstrate `LOW` confidence and mandatory escalation without invented evidence.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Schema and reset behavior

The SQLite schema version is **4**. Foundation metadata is `foundation-empty-v4`; the seeded demo baseline is `synthetic-pa-v1`.

An empty database is initialized and seeded at schema v4. Any other nonzero schema version is rejected, including schema v3. There is no in-place migration framework. For this local MVP, reset/rebuild is the supported upgrade path; reset validates a temporary database before atomically replacing the configured demo database.

## Known limitations

The release provides a bounded temporal contract, not a generalized temporal framework or generalized multi-version renderer. Agreeing overlapping payer versions fail safely with HTTP 409 when the answer representation would otherwise require an invented winner. Approval immediately makes V2 current even when it is future-effective; `AS_OF` still honors intervals, but there is no distinct approved-but-not-yet-current state.

There is no independent replay engine, graph database, ontology/RDF layer, LLM reasoning, arbitrary external healthcare integration, production authentication/compliance control, or tamper-evident audit store.

## Repository structure

```text
backend/                 FastAPI app, SQLite lifecycle, retrieval, reasoning, and knowledge modules
data/fixtures/           Version-controlled synthetic source fixtures
frontend/                Static demo UI
tests/                   Database, retrieval, reasoning, knowledge, API, and vertical-slice tests
docs/                    Product, architecture, domain-design, and release documentation
```

See the [product specification](docs/product-spec.md), [conceptual architecture](docs/architecture.md), [technical architecture](docs/technical-architecture.md), and [release notes](docs/release-notes.md).
