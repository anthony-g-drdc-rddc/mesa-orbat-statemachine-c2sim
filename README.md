# Mesa C2 simulation

A headless, deterministic implementation of the [C2 state-machine plan](plans/c2-state-machine-implementation-plan.md). Python 3.12, Mesa 3.5.1, ten fixed C2 services, a uniformly random decision provider, SQLite checkpoints, and native ORBAT Mapper file exchange.

## Run the demonstration

Install [uv](https://docs.astral.sh/uv/), then run from this repository:

```bash
uv sync --locked
uv run c2sim demo
```

The included seed completes the coastal-resupply mission: preliminary collection, main plan approval, the unavailable crossing, commander review, plan version 2, delivery of fifty supply units, and completion with receipt and accountability evidence. The CLI prints the run ID and paths to the exported files. A different seed can legitimately cancel or pause the mission:

```bash
uv run c2sim --database .data/experiment.sqlite demo --seed 42
```

All runs persist in `.data/c2sim.sqlite` by default. Full dependency versions, including transitive packages, are recorded in `uv.lock`.

## Run the API

```bash
uv run c2sim serve
```

The machine API is at `http://127.0.0.1:6158`; its OpenAPI documentation is at `/docs`. There is no commander application. Start and resume enable continuous execution at an accelerated wall-clock rate; each interval always represents 1000 simulated milliseconds. Step advances exactly one interval. Run pause freezes time and delivery; explicit step is still available while paused.

Create a run with three multipart fields. The native ORBAT document supplies organization and geography; `mission` supplies the declarative C2 objectives, tasks, timings and simulation parameters that ORBAT does not represent:

```bash
curl -X POST http://127.0.0.1:6158/api/v1/runs \
  -F 'scenario=@scenarios/coastal-resupply/source.orbat.json;type=application/json' \
  -F 'config=<scenarios/coastal-resupply/run-config.json' \
  -F 'mission=<scenarios/coastal-resupply/mission.json'
```

Use the returned run ID and current control revision:

```bash
curl -X POST http://127.0.0.1:6158/api/v1/runs/RUN_ID/controls \
  -H 'Content-Type: application/json' \
  -d '{"command_id":"start-1","action":"start","expected_control_revision":0}'
```

Commands require a unique `command_id`. An identical retry returns its original result; reusing an ID with different content fails. Controls use `expected_control_revision`; decision choices use `decision_id` and `expected_commit_seq`. A stale request returns HTTP 409. Callers cannot supply an option ID or install a decision provider.

The API supports run summaries, full committed state, current and recorded decisions, controls, exports, checkpoint playback, and child-run restores. All routes are described in [the generated OpenAPI schema](schemas/openapi.json).

A `READY` or run-`PAUSED` execution can accept a decision through `/decisions/choose` without advancing simulation time. Decisions only occur at workflow gates and delivered decision points; the runtime does not repeatedly draw while waiting for a report. The random provider selects uniformly from every permitted command-level option at each such point, including cancellation. A mission pause sends explicit delayed dispositions and prevents new starts; it is distinct from freezing the whole run.

## Use ORBAT Mapper unchanged

Run `docker compose -f deploy/compose.yaml up --build -d` and open **http://localhost:6159/**. The unchanged official release is packaged by [`orbat-mapper/`](orbat-mapper/README.md), which includes detailed installation and import/export instructions. The backend API documentation remains at **http://localhost:6158/docs**.

1. Open `scenarios/coastal-resupply/source.orbat.json` in standard ORBAT Mapper, or author another native 3.x scenario.
2. Supply that file and a matching `mission.json` to the runtime.
3. Run the simulation and export the committed result.
4. Open `scenario.orbat.json` using ORBAT Mapper's normal scenario import. Import `overlays.geojson` as geographical data if desired.
5. Examine `evidence.json` for C2 details and `source.orbat.json` for the exact original source.

The adapter preserves identifiers, nested unit hierarchy, symbols, native equipment/personnel/supply categories, and timed resource updates. Initial locations and routes are projected into the declared metre-based CRS; exported coordinates use WGS 84 longitude/latitude. Timed unit states in the result contain committed physical truth; GeoJSON separately labels physical tracks and delivered observations. C2 knowledge and simulator truth are kept separate internally.

Routes are native LineString features in `layers[].features` or `layerStack[].items`. Every unmapped field is listed in the normalized import report and retained in the original source. Unknown C2 fields and invalid references are rejected. Timed hierarchy changes at initialization and ambiguous interpolation between authored locations are rejected: export a flattened hierarchy with an explicit location at the requested start time. Organizational nodes need no physical position.

The format reference is ORBAT Mapper commit `d7d3a5cde9996549a18cff0fc8a1cc10421f6a62`. See [integration notes](docs/architecture.md) for validation scope and implementation boundaries.

## Recovery and reproducibility

Every boundary stores a complete checkpoint: service records, task versions, agents, queue, reservations, transfers, decisions, counters and all random-generator states. SQLite uses foreign keys, WAL and a 5-second busy timeout. Alembic applies schema migrations on startup. An exclusive OS lock refuses a second writer against the same database. Run exactly one server worker on Linux, macOS or WSL; Docker is supported on other hosts.

Restart with the same database to recover the last committed state. Running simulations continue automatically when the API starts. A failed candidate is discarded before a separate failure record is committed. If the database itself is unavailable, the last successful commit remains authoritative.

```bash
# Read a retained boundary without running Mesa.
uv run c2sim playback RUN_ID --checkpoint 20

# Re-execute the recorded controls and decision stream and compare every checkpoint.
uv run c2sim verify RUN_ID

# Create a new READY child run from a checkpoint. Resume via the API.
uv run c2sim restore RUN_ID 20
```

Restore retains the parent run and checkpoint references. Branch-local run and decision identities change. Checkpoint compatibility is explicit and checked before restoration. The release uses full snapshots, so storage grows with the run duration; this is intended for small research scenarios.

## Container

```bash
docker compose -f deploy/compose.yaml up --build
```

Compose starts the ORBAT Mapper interface at **http://localhost:6159/** alongside the backend. Use `docker compose -f deploy/compose.yaml up --build c2sim` if you only want the backend.

The named `c2sim-data` volume retains the database and exports across container replacement. The service runs as an unprivileged user and binds its host port to loopback. The configured system identity is written to audit records. Authentication is outside this initial headless implementation.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/generate_schemas.py
```

The scenario files are ordinary editable JSON, validated with the schemas in `schemas/`. `scripts/find_reference_seed.py` is a separate offline fixture-generation tool. It is never invoked by the runtime, does not modify available options, and does not redraw within a run.

The latest implementation plan controls the scope where it differs from older design documents in `plans/`.
