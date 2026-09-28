# Synapse — Clinical Knowledge Copilot

Synapse is a synthetic healthcare decision-support prototype for care coordinators, nurse navigators, and clinicians. It reconciles mocked patient, guideline, payer-policy, formulary, and specialist-note evidence; keeps clinical support distinct from coverage requirements; and produces cited answers with deterministic `HIGH`, `MEDIUM`, or `LOW` confidence and explicit escalation when evidence is insufficient.

All data is visibly synthetic. Synapse is not for clinical or coverage decisions and does not claim HIPAA compliance, clinical validation, production readiness, or autonomous decision authority.

## Current release: M6 knowledge-aware reasoning

The local modular monolith uses FastAPI, SQLite, a static browser UI, and five mocked source adapters. Deterministic code performs retrieval, temporal selection, governed-knowledge selection, source/knowledge comparison, reconciliation, confidence, escalation, citation construction, governed feedback approval, and snapshot persistence. No LLM is used.

The question path supports three deliberately separate operations:

- **CURRENT:** “What does Synapse consider current now?” Source versions use `is_current = 1`; knowledge requires an `APPLIED` assertion from a current source-document version.
- **AS_OF:** “What governed knowledge is applicable at time T?” Source and assertion effective intervals must contain the requested UTC instant, and governance eligibility is required. There is no fallback to current and no newest/highest-version or approval-time winner.
- **Historical interaction:** “What did Synapse actually produce earlier?” `GET /api/v1/interactions/{interaction_id}` reads the persisted snapshot without rerunning retrieval, selection, or reasoning.

`POST /api/v1/questions` accepts `question` and optional `as_of` (alongside the existing demo `source_mode`). Omitting `as_of` selects `CURRENT`; a valid explicit UTC timestamp selects `AS_OF`. Malformed, naive, or non-UTC values return HTTP 422. The response remains compatible with the pre-v1.4 shape; `temporal_mode` is not a public request field.

For the prior-authorization path, orchestration builds a dual-input `ReasoningInput`: retrieved conclusions become `SourceObservation` values and selected governed assertions become `GovernedBaselineAssertion` values. Deterministic comparison distinguishes source/knowledge corroboration, source-only and knowledge-only conclusions, compatible cross-dimension constraints, stale-knowledge disagreement, and same-dimension conflict. `CONF-PA-SYN-V2` evaluates this knowledge-aware path without turning confidence into a probability or allowing knowledge to inflate an incomplete source result.

The schema-v5 interaction snapshot records temporal mode, requested as-of time, confidence-policy identity, complete retrieved evidence membership in deterministic order, execution-time citation source/document-version identity, and selected governed-knowledge participation. `interaction_knowledge` preserves each selected assertion's execution-time role, state, value, scope, effective facts, provenance, lineage/correction identities, and deterministic order; `interaction_knowledge_evidence` preserves its evidence membership and order. Later governance changes do not rewrite those facts. Historical display is supported. Independent deterministic replay is not: there is no replay executor, and the snapshot is designed for historical explanation rather than a claim that arbitrary execution can be rerun.

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

The SQLite schema version is **5**. Foundation metadata is `foundation-empty-v5`; the seeded demo baseline remains `synthetic-pa-v1`.

An empty database is initialized and seeded at schema v5. Any other nonzero schema version is rejected, including schema v4. There is no in-place migration framework. Existing local databases must be reset/rebuilt; reset validates a temporary database before atomically replacing the configured demo database.

## Known limitations

The release provides a bounded temporal and knowledge-aware prior-authorization contract, not a generalized temporal framework or generalized multi-version renderer. Agreeing overlapping payer versions still fail safely with HTTP 409 when the answer representation would otherwise require an invented winner. Approval immediately makes V2 current even when it is future-effective; `AS_OF` still honors intervals, but there is no distinct approved-but-not-yet-current state.

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
