# Synapse — Clinical Knowledge Copilot

Synapse is a healthcare hackathon project for reconciling fragmented clinical and operational knowledge. The intended prototype will retrieve evidence from mocked healthcare sources, surface conflicts rather than hiding them, and produce a cited, confidence-scored answer with human escalation and an auditable clinician-feedback path into shared knowledge.

## Status

**Executable hackathon vertical slice.** The local FastAPI application serves a deterministic,
fixture-backed prior-authorization demo and a static browser UI. All data is visibly synthetic;
the prototype is not for clinical use and does not claim production readiness or HIPAA compliance.

## Run the demo

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. If port 8000 is already occupied, choose another local port,
for example `--port 8010`.

The flagship flow is:

1. Click **Reset demo** to restore Payer Policy V1 as current.
2. Ask the prefilled canonical prior-authorization question.
3. Inspect the five-source trace, `HIGH` confidence, cross-dimensional reconciliation, and citations.
4. Click **Submit V2 correction**; verify it remains `PENDING` and V1 remains current.
5. Click **Approve correction**; approval and application commit together and V2 becomes current.
6. Ask the same question again; the new answer cites V2 and recognizes the documented Norlaxa history as satisfying its prerequisite.
7. Refresh the audit timeline or retrieve the original interaction to verify its V1 snapshot remains unchanged.

Optional failure paths are available through **Simulate missing payer** and **Show true conflict**.
Both return `LOW` confidence and mandatory human escalation without guessing.

## Validate

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Repository Structure

```text
.
├── AGENTS.md               Repository-level instructions for future Codex work
├── README.md               Project overview and current status
├── backend/                FastAPI API, SQLite lifecycle, deterministic demo services
├── frontend/               Static HTML, CSS, and vanilla JavaScript demo UI
├── tests/                  Foundation, fixture, API, and vertical-slice tests
└── docs/
    ├── architecture.md     Technology-neutral conceptual architecture
    ├── demo-script.md      Reserved for a future demo script; currently empty
    └── product-spec.md     Hackathon MVP product specification
```

## Project Documents

- [Product specification](docs/product-spec.md)
- [Conceptual architecture](docs/architecture.md)

The source integrations are explicit local mocks over the version-controlled synthetic fixtures.
No network healthcare integration, model API, or LLM is used.
