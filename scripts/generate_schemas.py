"""Regenerate public JSON Schema and OpenAPI from the runtime contracts."""

import json
from pathlib import Path
from pydantic import TypeAdapter
from c2sim.api.app import create_app
from c2sim.contracts.scenario import (
    ScenarioSpec,
    RunCreateInput,
    RunCreateOutput,
    DecisionChooseInput,
    DecisionAppliedOutput,
    ControlInput,
)
from c2sim.decisions.provider import DecisionInput, DecisionOutput

out = Path(__file__).resolve().parents[1] / "schemas"
for model in (
    ScenarioSpec,
    RunCreateInput,
    RunCreateOutput,
    DecisionInput,
    DecisionOutput,
    DecisionChooseInput,
    DecisionAppliedOutput,
    ControlInput,
):
    (out / f"{model.__name__}.json").write_text(
        json.dumps(TypeAdapter(model).json_schema(), indent=2) + "\n"
    )
(out / "openapi.json").write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
