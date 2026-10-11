from copy import deepcopy
import json
import pytest
from pydantic import ValidationError
from c2sim.contracts.scenario import Predicate, RunCreateInput, ScenarioSpec
from c2sim.domain.records import validate_graph
from c2sim.orbat_io.adapter import import_scenario, export_orbat, walk_units


def test_native_roundtrip_preserves_identity_resources_and_geometry(state, inputs):
    raw, spec, config = inputs
    exported = export_orbat(state, [state])
    normalized = import_scenario(json.dumps(exported).encode(), spec, config.scenario_start_utc)
    for uid, unit in normalized["units"].items():
        original = deepcopy(state["normalized"]["units"][uid])
        assert unit.pop("position") == pytest.approx(original.pop("position"), abs=1e-5)
        assert unit == original
    assert normalized["routes"] == state["normalized"]["routes"]
    assert state["normalized"]["source_text"].encode() == raw
    assert normalized["unmapped_fields"]


@pytest.mark.parametrize("mutation", ["duplicate", "negative", "outside", "ambiguous", "parent"])
def test_invalid_native_input(inputs, mutation):
    raw, spec, config = inputs
    source = json.loads(raw)
    unit = next(walk_units(source))[0]
    if mutation == "duplicate":
        unit["subUnits"][0]["id"] = unit["id"]
    elif mutation == "negative":
        unit["supplies"][0]["count"] = -1
    elif mutation == "outside":
        unit["location"] = [180, 90]
    elif mutation == "parent":
        spec.unit_config["missing"] = spec.unit_config["HQ-1"]
    else:
        unit["state"] = [
            {"id": "a", "t": config.scenario_start_utc},
            {"id": "b", "t": config.scenario_start_utc},
        ]
    with pytest.raises(ValueError):
        import_scenario(json.dumps(source).encode(), spec, config.scenario_start_utc)


def test_timed_resources_use_updates_and_deltas(inputs):
    raw, spec, config = inputs
    source = json.loads(raw)
    unit = next(walk_units(source))[0]
    unit["state"] = [
        {
            "id": "a",
            "t": "2026-10-10T23:59:58Z",
            "update": {"supplies": [{"name": "fuel", "onHand": 50}]},
        },
        {
            "id": "b",
            "t": "2026-10-10T23:59:59Z",
            "diff": {"supplies": [{"name": "fuel", "onHand": -5}]},
        },
    ]
    result = import_scenario(json.dumps(source).encode(), spec, config.scenario_start_utc)
    assert result["units"]["HQ-1"]["resources"]["supplies"]["fuel"]["onHand"] == 45


def test_unsafe_predicates_and_dependency_cycles_rejected(inputs):
    with pytest.raises(ValidationError):
        Predicate.model_validate({"op": "eval", "ref": '__import__("os")'})
    with pytest.raises(ValueError, match="cycle"):
        validate_graph({"a": ["b"], "b": ["a"]})
    with pytest.raises(ValueError, match="Missing"):
        validate_graph({"a": ["missing"]})
    data = inputs[1].model_dump()
    data["plans"][1]["tasks"][3]["version"] = 1
    with pytest.raises(ValidationError, match="Material"):
        ScenarioSpec.model_validate(data)


def test_strict_provider_and_fixed_tick(inputs):
    config = inputs[2].model_dump()
    config["decision_provider"]["type"] = "smart"
    with pytest.raises(ValidationError):
        RunCreateInput.model_validate(config)
    config = inputs[2].model_dump()
    config["dt_ms"] = 0
    with pytest.raises(ValidationError):
        RunCreateInput.model_validate(config)
