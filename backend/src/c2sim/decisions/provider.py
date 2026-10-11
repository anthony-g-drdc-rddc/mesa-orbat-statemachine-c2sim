"""The decision boundary: immutable options in, one uniformly selected option out."""

from dataclasses import dataclass
import random
from typing import Protocol


@dataclass(frozen=True)
class MachineState:
    machine_type: str
    machine_id: str
    current_state: str
    revision: int


@dataclass(frozen=True)
class TransitionIntent:
    machine_type: str
    machine_id: str
    expected_revision: int
    from_state: str
    target_state: str


@dataclass(frozen=True)
class DecisionOption:
    option_id: str
    intents: tuple[TransitionIntent, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecisionInput:
    decision_id: str
    run_id: str
    commit_seq: int
    simulation_time_ms: int
    machines: tuple[MachineState, ...]
    options: tuple[DecisionOption, ...]


@dataclass(frozen=True)
class DecisionOutput:
    decision_id: str
    provider_id: str
    provider_version: str
    selected_option_id: str
    random_draw_index: int


class DecisionProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    def choose(self, decision: DecisionInput) -> DecisionOutput: ...


def as_tuple(value):
    return tuple(as_tuple(item) for item in value) if isinstance(value, list) else value


class RandomDecisionProvider:
    provider_id = "random"
    provider_version = "1.0.0"

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def choose(self, decision: DecisionInput) -> DecisionOutput:
        if not decision.options:
            raise ValueError("DecisionInput.options cannot be empty")
        options = tuple(sorted(decision.options, key=lambda option: option.option_id))
        index = self._rng.randrange(len(options))
        return DecisionOutput(
            decision.decision_id,
            self.provider_id,
            self.provider_version,
            options[index].option_id,
            index,
        )

    def getstate(self):
        return self._rng.getstate()

    def setstate(self, state):
        self._rng.setstate(as_tuple(state))


def decision_from_dict(data: dict) -> DecisionInput:
    return DecisionInput(
        **{k: data[k] for k in ("decision_id", "run_id", "commit_seq", "simulation_time_ms")},
        machines=tuple(MachineState(**m) for m in data["machines"]),
        options=tuple(
            DecisionOption(
                o["option_id"],
                tuple(TransitionIntent(**i) for i in o["intents"]),
                tuple(o["evidence_ids"]),
            )
            for o in data["options"]
        ),
    )
