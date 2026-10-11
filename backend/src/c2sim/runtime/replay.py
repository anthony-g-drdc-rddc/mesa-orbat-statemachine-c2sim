"""Recorded playback is repository-only; this verifier separately re-executes boundaries."""

from copy import deepcopy
import threading
from c2sim.contracts.scenario import ControlInput
from c2sim.domain.records import canonical
from c2sim.runtime.coordinator import Coordinator


class ReplayRepository:
    """Ephemeral recorder used only for offline deterministic verification."""

    def __init__(self, initial):
        self.state = deepcopy(initial)
        self.mutex = threading.RLock()

    def load(self, run_id):
        if run_id != self.state["run_id"]:
            raise KeyError(run_id)
        return deepcopy(self.state)

    def commit(self, state, expected_seq):
        if expected_seq != self.state["commit_seq"]:
            raise ValueError("Replay sequence mismatch")
        self.state = deepcopy(state)


def verify_history(history):
    if not history:
        raise ValueError("No recorded states")
    repository = ReplayRepository(history[0])
    coordinator = Coordinator(repository, history[0]["system_identity"])
    run_id = history[0]["run_id"]
    for expected in history[1:]:
        previous = repository.state
        events = expected["events"][len(previous["events"]) :]
        control = next((e for e in events if e["kind"] == "RunControl"), None)
        if control:
            command = expected["commands"][control["command_id"]]["input"]
            coordinator.control(run_id, ControlInput.model_validate(command))
        elif expected["time_ms"] == previous["time_ms"]:
            decision = next((e for e in events if e["kind"] == "DecisionApplied"), None)
            if decision is None:
                raise ValueError(
                    "Boundary cannot be re-executed (for example, an infrastructure failure)"
                )
            recorded = expected["decisions"][decision["decision_id"]]
            if canonical(previous["pending_decision"]) != canonical(recorded["input"]):
                raise ValueError("Recorded decision input mismatch")
            coordinator.choose(run_id, decision["decision_id"], previous["commit_seq"])
        else:
            coordinator.tick(run_id)
        # Includes inputs, output indices, RNG, evidence, queue and complete physical state.
        if canonical(repository.state) != canonical(expected):
            differing = [
                k for k in expected if canonical(repository.state.get(k)) != canonical(expected[k])
            ]
            raise ValueError(f"Replay diverged at commit {expected['commit_seq']}: {differing}")
    return {"verified_boundaries": len(history), "run_id": run_id, "deterministic": True}
