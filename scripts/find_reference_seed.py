"""Offline fixture search only. Never called by the runtime or decision provider."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import warnings
from c2sim.contracts.scenario import RunCreateInput, ScenarioSpec
from c2sim.decisions.provider import RandomDecisionProvider
from c2sim.runtime.coordinator import new_state, boundary, finalize
from c2sim.domain.predicates import evaluate

parser = argparse.ArgumentParser()
parser.add_argument("--limit", type=int, default=10000)
parser.add_argument("--start", type=int, default=0)
args = parser.parse_args()
warnings.filterwarnings("ignore", category=FutureWarning, module="mesa")
p = Path("scenarios/coastal-resupply")
spec = ScenarioSpec.model_validate_json((p / "mission.json").read_text())
config = RunCreateInput.model_validate_json((p / "run-config.json").read_text())
base = new_state(
    (p / "source.orbat.json").read_bytes(), spec, config, "headless-system", run_id="fixture-search"
)
for seed in range(args.start, args.limit):
    s = deepcopy(base)
    s["config"]["decision_provider"]["seed"] = seed
    s["decision_rng"] = RandomDecisionProvider(seed).getstate()
    for tick in range(250):
        boundary(s)
        finalize(s)
        if s["mission"]["state"] == "CANCELLED" or s["lifecycle"] == "COMPLETED":
            break
    transitions = [e["target_state"] for e in s["events"] if e["kind"] == "MissionTransition"]
    if (
        s["mission"]["state"] == "COMPLETED"
        and s["mission"]["approved_plan"] == 2
        and any(
            transitions[i : i + 3] == ["EXECUTING", "COMMANDER_REVIEW", "PLANNING_TASKS"]
            for i in range(len(transitions) - 2)
        )
        and evaluate(s["scenario"]["criteria"], s).allowed
    ):
        config.decision_provider.seed = seed
        (p / "run-config.json").write_text(config.model_dump_json(indent=2) + "\n")
        (p / "expected.json").write_text(
            json.dumps(
                {
                    "decision_seed": seed,
                    "mesa_seed": config.mesa_seed,
                    "mission_transitions": transitions,
                    "simulation_time_ms": s["time_ms"],
                    "supply_received": 50,
                    "approved_plan_version": 2,
                    "invariants": [
                        "all guards pass",
                        "unaffected task versions retained",
                        "supply conserved",
                        "unique transfers",
                        "receipt and accountability evidence before completion",
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        print(
            json.dumps({"seed": seed, "time_ms": s["time_ms"], "transitions": transitions}),
            flush=True,
        )
        break
    if seed % 100 == 0:
        print(f"checked {seed}", flush=True)
else:
    raise SystemExit("No reference seed found in range")
