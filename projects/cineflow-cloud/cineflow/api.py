from __future__ import annotations

import json
import os

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse

from .config import get_settings
from .models import AdmissionResult, JobRecord, JobRequest
from .orchestrator import JobStore, PipelineOrchestrator
from .providers import DemoProviders, ProductionProviders
from .sla import AdmissionController, AdmissionRejected

settings = get_settings()
providers = DemoProviders() if settings.mode == "demo" else ProductionProviders(settings)
store = JobStore()
admission = AdmissionController(settings)
orchestrator = PipelineOrchestrator(providers, admission, store)

app = FastAPI(
    title="CineFlow Cloud",
    version="0.2.0",
    description=(
        "Standalone cloud video translation. Inputs are already uploaded to OSS by an "
        "external client. Five minutes is an optimization target, not a hard timeout."
    ),
)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    provider_health = await providers.health()
    return {
        "ok": all(item.healthy for item in provider_health),
        "mode": settings.mode,
        "translation_provider": settings.translation_provider,
        "target_processing_seconds": settings.target_processing_seconds,
        "capacity": await admission.capacity(),
        "providers": [item.model_dump() for item in provider_health],
    }


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    provider_health = await providers.health()
    capacity = await admission.capacity()
    # A busy service is still ready: valid jobs wait instead of being rejected.
    ready = all(item.healthy for item in provider_health)
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={
                "ready": False,
                "capacity": capacity,
                "providers": [item.model_dump() for item in provider_health],
            },
        )
    return {
        "ready": True,
        "capacity": capacity,
        "providers": [item.model_dump() for item in provider_health],
    }


@app.post("/v1/admission", response_model=AdmissionResult)
async def check_admission(request: JobRequest) -> AdmissionResult:
    target = settings.target_processing_seconds
    try:
        predicted, cost = admission.quote(request)
        warnings = admission.validate_provider_health(request, await providers.health())
        likely = predicted <= target
        if not likely:
            warnings.append(
                f"predicted p95 is {predicted:.1f}s; the task is still accepted and will "
                f"continue beyond the {target}s target if needed"
            )
        return AdmissionResult(
            accepted=True,
            predicted_seconds=round(predicted, 2),
            target_seconds=target,
            likely_within_target=likely,
            estimated_cost_cny=cost.total_cny,
            cost_breakdown=cost.breakdown,
            warnings=warnings,
        )
    except AdmissionRejected as exc:
        return AdmissionResult(
            accepted=False,
            predicted_seconds=0.0,
            target_seconds=target,
            likely_within_target=False,
            reason=str(exc),
        )


@app.get("/v1/capacity")
async def get_capacity() -> dict[str, int]:
    return await admission.capacity()


@app.post("/v1/jobs", response_model=JobRecord, status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: JobRequest) -> JobRecord:
    try:
        return await orchestrator.submit(request)
    except AdmissionRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    record = await store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return record


@app.get("/v1/jobs/{job_id}/events")
async def stream_job_events(job_id: str) -> StreamingResponse:
    if await store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def stream():
        async for event in orchestrator.events.subscribe(job_id):
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            yield f"event: {event.event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def run() -> None:
    uvicorn.run(
        "cineflow.api:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )
