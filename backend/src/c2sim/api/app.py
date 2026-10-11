"""Machine-oriented API and a single authoritative simulation loop."""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from c2sim.contracts.scenario import (
    ControlInput,
    DecisionChooseInput,
    RestoreInput,
    RunCreateInput,
    RunCreateOutput,
    ScenarioSpec,
    DecisionAppliedOutput,
)
from c2sim.domain.records import Conflict, event
from c2sim.persistence.repository import Repository
from c2sim.runtime.coordinator import Coordinator, finalize, summary
from c2sim.runtime.exports import export_run

logger = logging.getLogger("c2sim")


def create_app(database=None, export_root=None, continuous=True):
    database = database or os.getenv("C2SIM_DATABASE", ".data/c2sim.sqlite")
    export_root = Path(export_root or os.getenv("C2SIM_EXPORT_ROOT", ".data/exports"))

    @asynccontextmanager
    async def lifespan(app):
        repository = Repository(database)
        coordinator = Coordinator(repository, os.getenv("C2SIM_SYSTEM_IDENTITY", "headless-system"))
        app.state.repository, app.state.coordinator = repository, coordinator

        async def loop():
            while True:
                for run_id in repository.run_ids():
                    try:
                        await asyncio.to_thread(coordinator.tick, run_id)
                    except Exception:
                        logger.exception("Simulation interval failed", extra={"run_id": run_id})
                        # Recover the last committed boundary, then record a separate failure.
                        try:
                            with repository.mutex:
                                last = repository.load(run_id)
                                seq = last["commit_seq"]
                                last["lifecycle"] = "FAILED"
                                last["control_revision"] += 1
                                event(last, "RunFailed", reason="INTERVAL_ERROR")
                                finalize(last)
                                repository.commit(last, seq)
                        except Exception:
                            logger.exception("Could not persist run failure")
                await asyncio.sleep(0.05)

        worker = asyncio.create_task(loop()) if continuous else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            # Wait for any to_thread interval still holding the writer lock.
            with repository.mutex:
                repository.close()

    app = FastAPI(title="Mesa C2 Simulation", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/runs", response_model=RunCreateOutput, status_code=201)
    async def create_run(
        scenario: UploadFile = File(...), config: str = Form(...), mission: str = Form(...)
    ):
        try:
            parsed_config = RunCreateInput.model_validate_json(config)
            spec = ScenarioSpec.model_validate_json(mission)
            raw = await scenario.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise HTTPException(413, "Scenario exceeds 10 MiB")
            state = app.state.coordinator.create(raw, spec, parsed_config)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            **{k: state[k] for k in ("run_id", "lifecycle", "control_revision", "scenario_hash")},
            "provider_id": "random",
            "provider_version": state["provider_version"],
        }

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        return summary(app.state.repository.load(run_id))

    @app.post("/api/v1/runs/{run_id}/controls")
    def control(run_id: str, command: ControlInput):
        return app.state.coordinator.control(run_id, command)

    @app.get("/api/v1/runs/{run_id}/decisions/current")
    def current(run_id: str):
        decision = app.state.repository.load(run_id)["pending_decision"]
        return decision if decision else Response(status_code=204)

    @app.post("/api/v1/runs/{run_id}/decisions/choose", response_model=DecisionAppliedOutput)
    def choose(run_id: str, command: DecisionChooseInput):
        return app.state.coordinator.choose(
            run_id, command.decision_id, command.expected_commit_seq
        )

    @app.get("/api/v1/runs/{run_id}/decisions/{decision_id}")
    def decision(run_id: str, decision_id: str):
        return app.state.repository.load(run_id)["decisions"][decision_id]

    @app.get("/api/v1/runs/{run_id}/state")
    def state(run_id: str):
        return app.state.repository.load(run_id)

    @app.get("/api/v1/runs/{run_id}/checkpoints")
    def checkpoints(run_id: str):
        app.state.repository.load(run_id)
        return [summary(s) for s in app.state.repository.history(run_id)]

    @app.get("/api/v1/runs/{run_id}/checkpoints/{seq}")
    def playback(run_id: str, seq: int):
        return app.state.repository.load(run_id, seq)

    @app.post("/api/v1/runs/{run_id}/exports")
    def exports(run_id: str):
        directory, files = export_run(app.state.repository, run_id, export_root)
        return {name: f"/api/v1/runs/{run_id}/exports/{directory.name}/{name}" for name in files}

    @app.get("/api/v1/runs/{run_id}/exports/{seq}/{name}")
    def download(run_id: str, seq: int, name: str):
        app.state.repository.load(run_id, seq)
        if name not in {
            "scenario.orbat.json",
            "overlays.geojson",
            "evidence.json",
            "source.orbat.json",
        }:
            raise HTTPException(404)
        path = export_root / run_id / str(seq) / name
        if not path.is_file():
            raise HTTPException(404, "Create this export first")
        return FileResponse(path, filename=name, media_type="application/json")

    @app.post("/api/v1/runs/{run_id}/restores", status_code=201)
    def restore(run_id: str, command: RestoreInput):
        return summary(app.state.coordinator.restore(run_id, command.checkpoint_seq))

    return app
