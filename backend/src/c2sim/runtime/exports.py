from pathlib import Path
import json
from c2sim.orbat_io.adapter import export_geojson, export_orbat


def export_run(repository, run_id, root):
    with repository.mutex:
        state = repository.load(run_id)
        history = repository.history(run_id)
    directory = Path(root) / run_id / str(state["commit_seq"])
    directory.mkdir(parents=True, exist_ok=True)
    values = {
        "scenario.orbat.json": export_orbat(state, history),
        "overlays.geojson": export_geojson(state, history),
        "evidence.json": {
            key: state[key]
            for key in (
                "run_id",
                "parent_run_id",
                "parent_checkpoint_seq",
                "config",
                "scenario_hash",
                "spec_hash",
                "mission",
                "plans",
                "tasks",
                "archived_tasks",
                "task_specs",
                "reports",
                "events",
                "decisions",
                "decision_rng",
                "mesa_rng",
                "transfers",
                "reservations",
                "provider_version",
                "runtime_version",
            )
        },
    }
    for name, value in values.items():
        temporary = directory / (name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(directory / name)
    (directory / "source.orbat.json").write_text(state["normalized"]["source_text"])
    return directory, [*values, "source.orbat.json"]
