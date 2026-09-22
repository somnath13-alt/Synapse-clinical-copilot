"""FastAPI entry point for the Milestone 1A application foundation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from backend import database
from backend import demo
from backend.config import Settings


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    source_mode: str = "BASELINE"


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    actor: str = "Synthetic Care Coordinator"
    message: str = "Payer policy V1 is outdated; use V2."


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = "Synthetic Knowledge Reviewer"
    rationale: str = "Verified the pre-seeded V2 provenance and effective date."


def create_app(settings: Settings | None = None) -> FastAPI:
    configured_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        demo.initialize_demo(configured_settings)
        yield

    application = FastAPI(title="Synapse", lifespan=lifespan)
    application.state.settings = configured_settings

    if configured_settings.frontend_directory.exists():
        application.mount(
            "/static",
            StaticFiles(directory=configured_settings.frontend_directory),
            name="static",
        )

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        index_path = configured_settings.frontend_directory / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Frontend is unavailable.")
        return FileResponse(index_path)

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/preflight")
    def preflight(response: Response) -> dict[str, object]:
        readiness, checks = database.preflight_checks(configured_settings)
        if readiness == "not_ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": readiness, "checks": checks}

    @application.post("/api/demo/reset")
    def demo_reset(request: ResetRequest) -> dict[str, object]:
        if not configured_settings.demo_mode:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Demo reset is disabled.",
            )
        if request.confirmation != configured_settings.demo_reset_confirmation:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Demo reset confirmation was rejected.",
            )
        try:
            database.reset_demo_database(configured_settings)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Demo reset failed; the existing database was preserved.",
            ) from exc
        return {
            "status": "reset",
            "baseline": database.DEMO_BASELINE,
            "schema_version": database.EXPECTED_SCHEMA_VERSION,
        }

    @application.post("/api/v1/questions")
    def ask(request: QuestionRequest) -> dict[str, object]:
        allowed_modes = {"BASELINE", "PAYER_POLICY_UNAVAILABLE", "TEST_ONLY_FORMULARY_SUBSTITUTION"}
        if request.source_mode not in allowed_modes:
            raise HTTPException(status_code=422, detail="Unsupported source mode.")
        return demo.ask_question(
            configured_settings, request.question.strip(), source_mode=request.source_mode
        )

    @application.post("/api/v1/feedback")
    def feedback(request: FeedbackRequest) -> dict[str, object]:
        try:
            return demo.submit_feedback(
                configured_settings, request.interaction_id, request.actor, request.message
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Interaction not found.") from exc

    @application.post("/api/v1/feedback/{feedback_id}/approve")
    def approve(feedback_id: str, request: ApprovalRequest) -> dict[str, object]:
        try:
            return demo.approve_feedback(
                configured_settings, feedback_id, request.reviewer, request.rationale
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Feedback not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/v1/interactions/{interaction_id}")
    def interaction(interaction_id: str) -> dict[str, object]:
        payload = demo.get_interaction(configured_settings, interaction_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Interaction not found.")
        return payload

    @application.get("/api/v1/audit/{interaction_id}")
    def audit(interaction_id: str) -> dict[str, object]:
        return demo.get_audit(configured_settings, interaction_id)

    return application


app = create_app()
