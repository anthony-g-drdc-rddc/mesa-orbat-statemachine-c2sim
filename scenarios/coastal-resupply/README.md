# Coastal resupply reference fixture

`source.orbat.json` is the authored, standard ORBAT Mapper scenario. It contains eight named elements, the hierarchy, resources and four route geometries. The runtime retains the source bytes and hash unchanged.

`mission.json` contains the C2 task specifications, mission criteria, communications timings, simulation area and physical conditions. Plan 1 uses a closed crossing; plan 2 changes only the ground-delivery task to use the detour. Forty supply units go by sea and ground; ten go directly by air. POST-1 must receive all fifty, and all seven physical elements must be accounted for.

`normalized.json` is a generated example of the adapter output for this input. Its original-source and provenance fields demonstrate what is retained at run creation. `run-config.json` freezes separate Mesa and decision seeds. `expected.json` records the successful reference path and invariants. `reference-decisions.json` retains the exact reference inputs, outputs, guard rechecks and final decision RNG state.

Reproduce from the repository root with `uv run c2sim demo`. The provider is unmodified and chooses uniformly. The seed was found with the separate offline command `uv run python scripts/find_reference_seed.py`; never run that search inside a simulation.

The reference path must enter COMMANDER_REVIEW after initial execution and select PLANNING_TASKS before approving the revised plan. The successful final state is supported by delivered receipts and accountability reports. Cancellation and incomplete runs are legitimate outcomes for other decision seeds.
