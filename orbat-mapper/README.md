# Run the ORBAT Mapper interface

This directory runs the **unchanged official ORBAT Mapper v1.15.0 release**. Its ZIP is downloaded from the upstream GitHub release and checked against the pinned SHA-256 in `release.json`. The compiled application is downloaded into `dist/`, which is not committed. The upstream MIT license is included.

## Start the interface and backend

From the repository root:

```bash
docker compose -f deploy/compose.yaml up --build -d
```

Open:

- **ORBAT Mapper:** http://localhost:6159/
- **Simulation API documentation:** http://localhost:6158/docs

To start only ORBAT Mapper:

```bash
docker compose -f deploy/compose.yaml up --build -d orbat-mapper
```

The first build requires internet access to download the official release. The application stores scenarios in your browser, so use the same browser and origin, and export important scenarios to files. Default basemap tiles and place-name search also require internet access; upstream supports local PMTiles for offline maps.

## Open the example

1. Open http://localhost:6159/ and create an empty scenario if prompted.
2. Choose **File → Load scenario... → Local file / URL** and select `scenarios/coastal-resupply/source.orbat.json`. This loads the complete scenario, including its routes and units.
3. Alternatively, drag the file onto the map and select **Open as new scenario** in the import dialog.
4. Inspect the units and map. The simulation controls remain in the backend API.

ORBAT Mapper and Mesa exchange files. Starting both services does not establish live synchronization or add a commander interface.

## Run a simulation and view the result

With the backend already running in Docker, open http://localhost:6158/docs:

1. Expand **POST /api/v1/runs** and select **Try it out**. Upload `scenarios/coastal-resupply/source.orbat.json` as `scenario`. Paste the contents of `run-config.json` into `config` and `mission.json` into `mission`. Execute the request and copy the returned `run_id`.
2. Call **POST /api/v1/runs/{run_id}/controls** with `{"command_id":"start-1","action":"start","expected_control_revision":0}`.
3. Call **GET /api/v1/runs/{run_id}** until `lifecycle` is `COMPLETED`.
4. Call **POST /api/v1/runs/{run_id}/exports**. It returns download paths. Open the `scenario.orbat.json` path under `http://localhost:6158` and save the file.
5. Open the downloaded file with **File → Load scenario... → Local file / URL** to load the complete result. Use the timeline to inspect the simulated movement. To update an existing scenario instead, use **File → Import data** and choose **Units with state**. Import `overlays.geojson` separately for tracks and observations. `evidence.json` contains the C2 records.

Download files through the browser or API before importing them. Direct URL imports from port 6159 to port 6158 require cross-origin access, which this backend does not enable.

If you run Python locally instead of the Docker backend, the shorter demonstration command is:

```bash
uv sync --locked
uv run c2sim demo
```

The command prints the location of `scenario.orbat.json`. Open that file in the interface. This local command uses its own `.data/` database, separate from the Docker volume.

## Run without Docker

Python 3.12 is sufficient; no Node.js installation is required:

```bash
python orbat-mapper/download.py
python orbat-mapper/serve.py
```

Then open http://localhost:6159/. The backend can be started separately with `uv run c2sim serve`.

For an offline installation, download the official ZIP on another machine and run:

```bash
python orbat-mapper/download.py --archive /path/to/orbat-mapper-v1.15.0.zip
```

The same pinned checksum is enforced. To update versions, update `release.json` using the upstream release's published checksum and remove the generated `dist/` directory before reinstalling.

Upstream: https://github.com/orbat-mapper/orbat-mapper/releases/tag/v1.15.0
