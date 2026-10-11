from dataclasses import replace
import random
import pytest
from c2sim.decisions.provider import RandomDecisionProvider, decision_from_dict
from c2sim.domain.records import Conflict, canonical
from c2sim.runtime.coordinator import Coordinator, recheck
from c2sim.services.core import MISSION


def test_uniform_provider_sorts_and_matches_python_rng(state):
    decision = decision_from_dict(state["pending_decision"])
    rng = random.Random(125)
    a, b = RandomDecisionProvider(125), RandomDecisionProvider(125)
    for _ in range(100):
        first = a.choose(decision)
        second = b.choose(replace(decision, options=tuple(reversed(decision.options))))
        assert first == second
        assert first.random_draw_index == rng.randrange(len(decision.options))
    with pytest.raises(ValueError):
        a.choose(replace(decision, options=()))


def test_retry_does_not_consume_randomness(repository, state):
    repository.create(state)
    coordinator = Coordinator(repository)
    pending = state["pending_decision"]
    result = coordinator.choose(state["run_id"], pending["decision_id"], 0)
    committed = repository.load(state["run_id"])
    assert coordinator.choose(state["run_id"], pending["decision_id"], 0) == result
    assert repository.load(state["run_id"]) == committed
    with pytest.raises(Conflict):
        coordinator.choose(state["run_id"], pending["decision_id"], 1)


def test_stale_and_invalid_outputs_rejected_atomically(state):
    decision = decision_from_dict(state["pending_decision"])
    provider = RandomDecisionProvider(0)
    output = provider.choose(decision)
    initial = canonical(state)
    for changed in (
        replace(output, decision_id="bad"),
        replace(output, provider_id="bad"),
        replace(output, selected_option_id="bad"),
        replace(output, random_draw_index=999),
    ):
        with pytest.raises(Conflict):
            recheck(state, decision, changed)
        assert canonical(state) == initial
    state["mission"]["revision"] += 1
    with pytest.raises(Conflict):
        recheck(state, decision, output)


def test_completion_requires_receipt_and_accountability(state):
    state["mission"]["state"] = "COMMANDER_REVIEW"
    result = MISSION.guard(state, "COMPLETED")
    assert not result.allowed
    assert any("SUPPLY" in reason for reason in result.reasons)
    assert any("ACCOUNTED" in reason for reason in result.reasons)
    assert not MISSION.guard(state, "MADE_UP").allowed


def test_linked_alignment_stale_intent_leaves_all_machines_unchanged(state):
    from c2sim.runtime.coordinator import refresh_decision
    from c2sim.services.tasking import create_task
    from c2sim.decisions.provider import DecisionOutput

    state["mission"]["state"] = "PAUSED"
    state["mission"]["approved_plan"] = 1
    for spec in state["scenario"]["plans"][0]["tasks"][:2]:
        create_task(state, spec)
        state["tasks"][spec["id"]]["state"] = "ACCEPTED"
        state["tasks"][spec["id"]]["disposition"] = "pause"
    refresh_decision(state)
    decision = decision_from_dict(state["pending_decision"])
    options = sorted(decision.options, key=lambda o: o.option_id)
    index, option = next(
        (i, o) for i, o in enumerate(options) if o.intents[0].target_state == "EXECUTING"
    )
    assert len(option.intents) == 3
    state["tasks"]["air-delivery"]["revision"] += 1
    before = canonical(state)
    output = DecisionOutput(decision.decision_id, "random", "1.0.0", option.option_id, index)
    with pytest.raises(Conflict):
        recheck(state, decision, output)
    assert canonical(state) == before
