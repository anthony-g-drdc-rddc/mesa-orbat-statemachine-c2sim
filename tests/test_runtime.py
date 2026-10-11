from copy import deepcopy
import json
from pathlib import Path
import pytest
from c2sim.contracts.scenario import ControlInput
from c2sim.domain.records import Conflict, canonical, schedule
from c2sim.domain.predicates import evaluate
from c2sim.runtime.coordinator import Coordinator
from c2sim.runtime.replay import verify_history
from c2sim.runtime.run_logging import RunLogs
from c2sim.persistence.repository import Repository
from c2sim.services.core import POSITIONS, REPORTING
from c2sim.services.tasking import create_task, request_disposition, transition_task
from c2sim.simulation.model import C2SimulationModel, deliver_disposition


def start(coordinator, run_id):
    return coordinator.control(
        run_id, ControlInput(command_id="start", action="start", expected_control_revision=0)
    )


def finish(coordinator, run_id):
    for _ in range(250):
        state = coordinator.tick(run_id)
        if state["lifecycle"] == "COMPLETED":
            return state
    pytest.fail("Run did not settle within 250 steps")


def test_reference_mission(repository, inputs):
    coordinator = Coordinator(repository)
    initial = coordinator.create(*inputs)
    start(coordinator, initial["run_id"])
    final = finish(coordinator, initial["run_id"])
    expected = json.loads(
        (Path(__file__).parents[1] / "scenarios/coastal-resupply/expected.json").read_text()
    )
    assert final["mission"]["state"] == "COMPLETED"
    assert final["mission"]["approved_plan"] == 2
    assert [
        e["target_state"] for e in final["events"] if e["kind"] == "MissionTransition"
    ] == expected["mission_transitions"]
    assert final["time_ms"] == expected["simulation_time_ms"]
    assert evaluate(final["scenario"]["criteria"], final).allowed
    assert final["tasks"]["ground-delivery"]["version"] == 2
    assert final["archived_tasks"]["ground-delivery@1"]["state"] == "CANCELLED"
    assert final["tasks"]["sea-delivery"]["version"] == 1
    assert final["units"]["POST-1"]["resources"]["supplies"]["supply"]["onHand"] == 50
    assert len(final["transfers"]) == 3
    assert len(final["services"]) == 10
    assert verify_history(repository.history(initial["run_id"]))["deterministic"]
    for snapshot in repository.history(initial["run_id"]):
        amount = sum(
            u["resources"]["supplies"]["supply"].get(
                "onHand", u["resources"]["supplies"]["supply"]["count"]
            )
            for u in snapshot["units"].values()
        )
        assert amount == 50
    for decision in final["decisions"].values():
        assert all(g["allowed"] for g in decision["guard_results"])
        assert "units" not in decision["input"]  # no physical truth leaks through the port


def test_per_run_log_records_committed_state_changes_and_movement(repository, inputs, tmp_path):
    run_logs = RunLogs(tmp_path / "logs")
    try:
        coordinator = Coordinator(repository, run_logs=run_logs)
        initial = coordinator.create(*inputs)
        start(coordinator, initial["run_id"])
        finish(coordinator, initial["run_id"])
        content = run_logs.path(initial["run_id"]).read_text()
    finally:
        run_logs.close()

    assert "RunCreated" in content
    assert '"to_state": "RUNNING"' in content
    assert "DecisionApplied" in content
    assert "MissionTransition" in content
    assert "TaskTransition" in content
    assert "UnitMoved" in content
    assert '"unit_id": "GROUND-1"' in content
    assert '"displacement_m":' in content
    assert "RunCompleted" in content


def test_process_replacement_and_restore_preserve_physical_future(tmp_path, inputs):
    path = tmp_path / "recovery.sqlite"
    repository = Repository(path)
    coordinator = Coordinator(repository)
    original = coordinator.create(*inputs)
    run_id = original["run_id"]
    start(coordinator, run_id)
    for _ in range(20):
        coordinator.tick(run_id)
    checkpoint = repository.load(run_id)
    assert checkpoint["messages"]
    repository.close()
    repository = Repository(path)
    try:
        coordinator = Coordinator(repository)
        assert repository.load(run_id) == checkpoint
        child = coordinator.restore(run_id, checkpoint["commit_seq"])
        assert child["decision_rng"] == checkpoint["decision_rng"]
        assert child["messages"] == checkpoint["messages"]
        assert child["reservations"] == checkpoint["reservations"]
        start(coordinator, child["run_id"])
        parent_final = finish(coordinator, run_id)
        child_final = finish(coordinator, child["run_id"])
        for key in (
            "units",
            "tasks",
            "archived_tasks",
            "transfers",
            "messages",
            "decision_rng",
            "mesa_rng",
            "time_ms",
        ):
            assert canonical(child_final[key]) == canonical(parent_final[key]), key
        assert child_final["parent_run_id"] == run_id
    finally:
        repository.close()


def test_run_pause_freezes_everything_and_commands_are_idempotent(repository, inputs):
    coordinator = Coordinator(repository)
    state = coordinator.create(*inputs)
    run_id = state["run_id"]
    started = start(coordinator, run_id)
    paused = coordinator.control(
        run_id,
        ControlInput(
            command_id="pause",
            action="pause",
            expected_control_revision=started["control_revision"],
        ),
    )
    frozen = repository.load(run_id)
    for _ in range(3):
        assert coordinator.tick(run_id) == frozen
    with pytest.raises(Conflict):
        coordinator.control(
            run_id, ControlInput(command_id="bad", action="resume", expected_control_revision=0)
        )
    cmd = ControlInput(
        command_id="step", action="step", expected_control_revision=paused["control_revision"]
    )
    result = coordinator.control(run_id, cmd)
    assert coordinator.control(run_id, cmd) == result
    assert repository.load(run_id)["time_ms"] == 1000


def test_interval_failure_does_not_publish_partial_state(repository, inputs, monkeypatch, tmp_path):
    run_logs = RunLogs(tmp_path / "logs")
    coordinator = Coordinator(repository, run_logs=run_logs)
    state = coordinator.create(*inputs)
    start(coordinator, state["run_id"])
    committed = repository.load(state["run_id"])

    def fail(self, state):
        state["units"]["POST-1"]["resources"]["supplies"]["supply"]["onHand"] = 999
        raise RuntimeError("Injected resolver failure")

    monkeypatch.setattr(C2SimulationModel, "resolve", fail)
    try:
        with pytest.raises(RuntimeError):
            coordinator.tick(state["run_id"])
        assert repository.load(state["run_id"]) == committed
        content = run_logs.path(state["run_id"]).read_text()
        assert "BoundaryFailed" in content
        assert "discarded_candidate" in content
        assert "UnitResourcesChanged" not in content
    finally:
        run_logs.close()


def test_database_failure_does_not_publish_choice(repository, inputs, monkeypatch):
    coordinator = Coordinator(repository)
    state = coordinator.create(*inputs)

    def fail(*args):
        raise RuntimeError("Injected disk failure")

    monkeypatch.setattr(repository, "commit", fail)
    with pytest.raises(RuntimeError):
        coordinator.choose(state["run_id"], state["pending_decision"]["decision_id"], 0)
    assert canonical(repository.load(state["run_id"])) == canonical(state)


def test_second_writer_refused(repository):
    with pytest.raises(RuntimeError, match="writer"):
        Repository(repository.path)


def test_out_of_order_reports_remain_in_history(state):
    newest = {
        "id": "new",
        "kind": "position",
        "source": "SEA-1",
        "observed_ms": 5000,
        "delivered_ms": 6000,
        "confirmed": True,
        "payload": {"position": [1, 2]},
    }
    old = {**deepcopy(newest), "id": "old", "observed_ms": 1000, "delivered_ms": 7000}
    for report in (newest, old):
        REPORTING.report(state, report)
        POSITIONS.report(state, report)
    assert state["positions"]["SEA-1"]["id"] == "new"
    assert len(state["reports"]) == 2


def test_zero_delay_waits_until_next_phase(state):
    schedule(state, "instruction", {"task_key": "not-yet"}, delay_ms=0)
    assert state["messages"][0]["due_ms"] == state["time_ms"] + 1000


def test_late_cancel_cannot_erase_actual_completion(state):
    spec = state["scenario"]["plans"][0]["tasks"][0]
    create_task(state, spec, authorized=True)
    task = state["tasks"][spec["id"]]
    key = task["key"]
    task["state"] = "IN_PROGRESS"
    state["actual_results"][key] = "COMPLETED"
    request_disposition(state, task, "cancel")
    deliver_disposition(
        state, {"task_key": key, "disposition": "cancel", "revision": task["revision"]}
    )
    transition_task(state, task, "COMPLETED", "delivered-completion")
    assert task["state"] == "COMPLETED"
    assert state["actual_results"][key] == "COMPLETED"


def test_unaccepted_unauthorized_or_paused_task_cannot_start(state):
    spec = state["scenario"]["plans"][0]["tasks"][0]
    create_task(state, spec)
    task = state["tasks"][spec["id"]]
    unit = state["units"][spec["unit_id"]]
    unit["installed_task"] = task["key"]
    unit["activity"] = {
        "elapsed_ms": 0,
        "waypoint": 0,
        "started": False,
        "blocked": False,
        "paused": False,
        "accepted": True,
    }
    for reported, authority, mission in [
        ("ACKNOWLEDGED", True, "EXECUTING"),
        ("ACCEPTED", False, "EXECUTING"),
        ("ACCEPTED", True, "PAUSED"),
    ]:
        task["state"], task["authorized"], state["mission"]["state"] = reported, authority, mission
        model = C2SimulationModel(state)
        model.step()
        assert model.proposals == []


def test_crossing_blocks_truth_before_report_and_clearance_restores_lifecycle(state):
    from c2sim.runtime.coordinator import deliver_due
    from c2sim.simulation.model import update_conditions

    spec = deepcopy(state["scenario"]["plans"][0]["tasks"][3])
    spec["dependencies"] = []
    create_task(state, spec, authorized=True)
    task = state["tasks"][spec["id"]]
    task["state"] = "ACCEPTED"
    unit = state["units"][spec["unit_id"]]
    unit["resources"]["supplies"]["supply"]["onHand"] = 40
    unit["installed_task"] = task["key"]
    unit["activity"] = {
        "elapsed_ms": 0,
        "waypoint": 0,
        "started": False,
        "blocked": False,
        "paused": False,
        "accepted": True,
    }
    state["mission"]["state"] = "EXECUTING"
    initial = list(unit["position"])
    model = C2SimulationModel(state)
    model.step()
    model.resolve(state)
    assert unit["position"] == initial
    assert unit["activity"]["blocked"]
    assert state["mobility"] == {}
    assert task["state"] == "ACCEPTED"
    state["time_ms"] = 2000
    deliver_due(state)
    assert task["state"] == "BLOCKED"
    assert state["mobility"]["crossing"]["payload"]["blocked"]
    state["actual_route_conditions"]["crossing"] = False
    update_conditions(state)
    assert not unit["activity"]["blocked"]
    assert task["state"] == "BLOCKED"  # clearance report is still in transit
    state["time_ms"] = 4000
    deliver_due(state)
    assert task["state"] == "IN_PROGRESS"


def test_material_change_keeps_old_reports_off_new_version(state):
    from c2sim.services.tasking import TASKING

    old = deepcopy(state["scenario"]["plans"][0]["tasks"][3])
    create_task(state, old)
    state["tasks"][old["id"]]["state"] = "IN_PROGRESS"
    replacement = {**deepcopy(old), "version": 2, "route_id": "detour"}
    create_task(state, replacement)
    report = {
        "id": "late",
        "kind": "task_state",
        "payload": {"task_key": "ground-delivery@1", "state": "COMPLETED"},
    }
    TASKING["army"].report(state, report)
    assert state["archived_tasks"]["ground-delivery@1"]["state"] == "COMPLETED"
    assert state["tasks"]["ground-delivery"]["state"] == "DRAFT"


def test_safety_across_random_seeds(inputs):
    from c2sim.decisions.provider import RandomDecisionProvider
    from c2sim.runtime.coordinator import new_state, boundary, finalize

    base = new_state(*inputs, "seed-test", run_id="seed-test")
    for seed in range(64):
        state = deepcopy(base)
        state["decision_rng"] = RandomDecisionProvider(seed).getstate()
        state["config"]["decision_provider"]["seed"] = seed
        for _ in range(100):
            boundary(state)
            finalize(state)
            for unit in state["units"].values():
                for resource in unit["resources"]["supplies"].values():
                    assert resource.get("onHand", resource["count"]) >= 0
            total = sum(
                u["resources"]["supplies"]["supply"].get(
                    "onHand", u["resources"]["supplies"]["supply"]["count"]
                )
                for u in state["units"].values()
            )
            assert total == 50
            if state["mission"]["state"] == "COMPLETED":
                assert evaluate(state["scenario"]["criteria"], state).allowed
            if state["lifecycle"] == "COMPLETED":
                break
        for decision in state["decisions"].values():
            assert all(g["allowed"] for g in decision["guard_results"])
