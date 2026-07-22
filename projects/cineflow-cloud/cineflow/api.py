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
from .sla import AdmissionController, AdmissionRejected, CapacityUnavailable

settings = get_settings()
providers = DemoProviders() if settings.mode == "demo" else ProductionProviders(settings)
store = JobStore()
admission = AdmissionController(settings)
orchestrator = PipelineOrchestrator(providers, admission, store)

app = FastAPI(
    title="CineFlow Cloud",
    version="0.1.0",
    description=(
        "Deadline-aware cloud video translation. The 300-second SLA starts only after "
        "the input object is already uploaded and the job passes admission control."
    ),
)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    provider_health = await providers.health()
    return {
        "ok": all(item.healthy for item in provider_health),
        "mode": settings.mode,
        "hard_sla_seconds": settings.hard_sla_seconds,
        "capacity": await admission.capacity(),
        "providers": [item.model_dump() for item in provider_health],
    }


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    provider_health = await providers.health()
    try:
        # A representative strict request is not needed here; readiness only
        # exposes provider warmth and immediate slot availability.
        capacity = await admission.capacity()
        ready = all(item.healthy and item.warm for item in provider_health)
        ready = ready and capacity["available"] > 0
    except Exception:
        ready = False
        capacity = await admission.capacity()
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
    try:
        predicted, cost = admission.quote(request)
        admission.validate_provider_health(request, await providers.health())
        return AdmissionResult(
            accepted=True,
            predicted_seconds=round(predicted, 2),
            estimated_cost_cny=cost.total_cny,
            cost_breakdown=cost.breakdown,
            hard_sla_seconds=settings.hard_sla_seconds,
            reserve_seconds=settings.sla_reserve_seconds,
        )
    except AdmissionRejected as exc:
        return AdmissionResult(
            accepted=False,
            predicted_seconds=0.0,
            hard_sla_seconds=settings.hard_sla_seconds,
            reserve_seconds=settings.sla_reserve_seconds,
            reason=str(exc),
        )


@app.get("/v1/capacity")
async def get_capacity() -> dict[str, int]:
    return await admission.capacity()


@app.post("/v1/jobs", response_model=JobRecord, status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: JobRequest) -> JobRecord:
    try:
        return await orchestrator.submit(request)
    except CapacityUnavailable as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
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
