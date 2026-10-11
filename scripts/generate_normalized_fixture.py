"""Regenerate the example normalized input using the production adapter."""

import json
from pathlib import Path
from c2sim.contracts.scenario import RunCreateInput, ScenarioSpec
from c2sim.orbat_io.adapter import import_scenario

path = Path(__file__).resolve().parents[1] / "scenarios/coastal-resupply"
spec = ScenarioSpec.model_validate_json((path / "mission.json").read_text())
config = RunCreateInput.model_validate_json((path / "run-config.json").read_text())
normalized = import_scenario(
    (path / "source.orbat.json").read_bytes(), spec, config.scenario_start_utc
)
(path / "normalized.json").write_text(json.dumps(normalized, indent=2) + "\n")
