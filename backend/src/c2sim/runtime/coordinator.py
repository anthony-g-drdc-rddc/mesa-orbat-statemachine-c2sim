"""Single state writer: copy, validate, execute, commit, then publish."""

from copy import deepcopy
from dataclasses import asdict
import uuid
from c2sim.contracts.scenario import RunCreateInput, ScenarioSpec
from c2sim.decisions.provider import (
    DecisionInput,
    DecisionOption,
    DecisionProvider,
    MachineState,
    RandomDecisionProvider,
    TransitionIntent,
    decision_from_dict,
)
from c2sim.domain.records import (
    Conflict,
    MISSION_TERMINAL,
    RUN_TERMINAL,
    TASK_TERMINAL,
    digest,
    event,
)
from c2sim.orbat_io.adapter import import_scenario
from c2sim.services.core import (
    MISSION,
    MISSION_EDGES,
    MOBILITY,
    PLANNING,
    POSITIONS,
    REPORTING,
    SERVICES,
)
from c2sim.services.tasking import TASKING, find_task, request_disposition
from c2sim.simulation.model import (
    C2SimulationModel,
    deliver_acceptance,
    deliver_disposition,
    deliver_instruction,
    update_conditions,
)

CHECKPOINT_VERSION = "1.0"


def new_state(raw, spec: ScenarioSpec, config: RunCreateInput, identity, run_id=None):
    normalized = import_scenario(raw, spec, config.scenario_start_utc)
    units = deepcopy(normalized["units"])
    for unit in units.values():
        unit.update(installed_task=None, activity=None, last_instruction=None)
    provider = RandomDecisionProvider(config.decision_provider.seed)
    state = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "runtime_version": "0.1.0",
        "mesa_version": "3.5.1",
        "provider_version": provider.provider_version,
        "run_id": run_id or str(uuid.uuid4()),
        "parent_run_id": None,
        "parent_checkpoint_seq": None,
        "lifecycle": "READY",
        "control_revision": 0,
        "commit_seq": 0,
        "time_ms": 0,
        "config": config.model_dump(),
        "scenario": spec.model_dump(),
        "normalized": normalized,
        "scenario_hash": normalized["source_hash"],
        "spec_hash": digest(spec.model_dump()),
        "system_identity": identity,
        "units": units,
        "force": deepcopy(normalized["units"]),
        "mission": {
            "id": spec.mission_id,
            "state": "ORGANIZING",
            "revision": 0,
            "objectives": spec.objectives,
            "criteria": spec.criteria.model_dump(),
            "approved_plan": 0,
            "approvals": [],
            "authorizations": [],
            "planning_basis": None,
            "review_due": False,
        },
        "tasks": {},
        "archived_tasks": {},
        "task_specs": {},
        "plans": {},
        "positions": {},
        "reports": [],
        "mobility": {},
        "reservations": {},
        "transfers": {},
        "actual_results": {},
        "actual_route_conditions": {
            rid: rid in spec.actual_blocked_routes for rid in normalized["routes"]
        },
        "messages": [],
        "delivered_messages": [],
        "events": [],
        "decisions": {},
        "pending_decision": None,
        "commands": {},
        "counter": 0,
        "decision_rng": provider.getstate(),
        "mesa_rng": None,
        "mesa_steps": 0,
        "services": [s.name for s in SERVICES],
        "stalled_signature": None,
    }
    refresh_decision(state)
    return state


def disposition_intents(state, target):
    all_tasks = [*state["tasks"].values(), *state["archived_tasks"].values()]
    if target in {"PAUSED", "CANCELLED", "EXECUTING"}:
        disposition = {"PAUSED": "pause", "CANCELLED": "cancel", "EXECUTING": "continue"}[target]
        return [
            TransitionIntent(
                "disposition", task["key"], task["revision"], task["disposition"], disposition
            )
            for task in sorted(all_tasks, key=lambda t: t["key"])
            if task["state"] not in TASK_TERMINAL
            and (target != "EXECUTING" or task["disposition"] == "pause")
        ]
    if target != "DISPATCHING_DOMAIN_TASKS":
        return []
    plan = PLANNING.candidate(state)
    specs = {s["id"]: s for s in plan["tasks"]}
    intents = []
    for task in sorted(state["tasks"].values(), key=lambda t: t["key"]):
        if task["state"] in TASK_TERMINAL or state["task_specs"][task["key"]]["preliminary"]:
            continue
        replacement = specs.get(task["id"])
        disposition = None
        if replacement and replacement["version"] != task["version"]:
            disposition = "cancel"
        elif replacement is None:
            disposition = plan["removed_dispositions"].get(task["id"])
        if disposition and disposition != "complete":
            intents.append(
                TransitionIntent(
                    "disposition", task["key"], task["revision"], task["disposition"], disposition
                )
            )
    return intents


def alignment_guard(state, target):
    guard = MISSION.guard(state, target)
    if not guard.allowed or target != "DISPATCHING_DOMAIN_TASKS":
        return guard
    from c2sim.domain.predicates import GuardResult

    plan = PLANNING.candidate(state)
    for tid, disposition in plan["removed_dispositions"].items():
        if disposition == "complete" and state["tasks"].get(tid, {}).get("state") != "COMPLETED":
            return GuardResult(False, ("REMOVED_TASK_NOT_COMPLETED",))
    return guard


def refresh_decision(state):
    state["pending_decision"] = None
    mission = state["mission"]
    current = mission["state"]
    if current in MISSION_TERMINAL or state["lifecycle"] in RUN_TERMINAL:
        return
    # A choice is requested at workflow gates or delivered decision points. Steady
    # execution/waiting is not an invitation to draw again on every simulated tick.
    targets = MISSION_EDGES[current]
    guards = {target: alignment_guard(state, target) for target in targets}
    if current == "EXECUTING":
        actionable = guards["COMMANDER_REVIEW"].allowed
    else:
        actionable = any(g.allowed for t, g in guards.items() if t != "PAUSED")
    actionable = actionable or any(t["state"] == "FAILED" for t in state["tasks"].values())
    if not actionable:
        reasons = sorted({r for g in guards.values() for r in g.reasons})
        signature = digest([current, reasons])
        if signature != state["stalled_signature"]:
            event(state, "DecisionStalled", machine_id=mission["id"], failed_guards=reasons)
            state["stalled_signature"] = signature
        return
    state["stalled_signature"] = None
    guards["CANCELLED"] = MISSION.guard(state, "CANCELLED")
    options = []
    machines = {
        ("mission", mission["id"]): MachineState(
            "mission", mission["id"], current, mission["revision"]
        )
    }
    for target, guard in guards.items():
        if not guard.allowed:
            continue
        intents = (
            TransitionIntent("mission", mission["id"], mission["revision"], current, target),
            *disposition_intents(state, target),
        )
        for intent in intents[1:]:
            machines[(intent.machine_type, intent.machine_id)] = MachineState(
                intent.machine_type, intent.machine_id, intent.from_state, intent.expected_revision
            )
        options.append(
            DecisionOption(digest([asdict(i) for i in intents])[:24], intents, guard.evidence_ids)
        )
    decision = DecisionInput(
        f"{state['run_id']}:decision:{state['commit_seq']}:{digest([asdict(o) for o in options])[:12]}",
        state["run_id"],
        state["commit_seq"],
        state["time_ms"],
        tuple(machines[k] for k in sorted(machines)),
        tuple(sorted(options, key=lambda o: o.option_id)),
    )
    state["pending_decision"] = asdict(decision)


def recheck(state, decision, output):
    if decision.commit_seq != state["commit_seq"] or decision.run_id != state["run_id"]:
        raise Conflict("Stale decision input")
    if (
        output.decision_id != decision.decision_id
        or output.provider_id != "random"
        or output.provider_version != "1.0.0"
    ):
        raise Conflict("Unexpected decision or provider identity")
    option = next((o for o in decision.options if o.option_id == output.selected_option_id), None)
    if option is None:
        raise Conflict("Selected option is absent from decision")
    ordered = sorted(decision.options, key=lambda o: o.option_id)
    if (
        not 0 <= output.random_draw_index < len(ordered)
        or ordered[output.random_draw_index] != option
    ):
        raise Conflict("Draw index does not match selection")
    results = []
    for intent in option.intents:
        if intent.machine_type == "mission":
            machine = state["mission"]
            if machine["id"] != intent.machine_id:
                raise Conflict("Unknown mission")
            guard = alignment_guard(state, intent.target_state)
            results.append(asdict(guard))
            if not guard.allowed:
                raise Conflict(f"Guard rejected: {guard.reasons}")
            current = machine["state"]
        elif intent.machine_type == "disposition":
            machine = find_task(state, intent.machine_id)
            if (
                not machine
                or machine["state"] in TASK_TERMINAL
                or intent.target_state not in {"continue", "pause", "cancel"}
            ):
                raise Conflict("Invalid task disposition")
            current = machine["disposition"]
            results.append({"allowed": True, "reasons": [], "evidence_ids": []})
        else:
            raise Conflict("Unknown machine type")
        if machine["revision"] != intent.expected_revision or current != intent.from_state:
            raise Conflict("Stale machine revision")
    return option, results


def apply_choice(state, provider: DecisionProvider):
    decision = decision_from_dict(state["pending_decision"])
    output = provider.choose(decision)
    option, guards = recheck(state, decision, output)
    # Recheck every intent before applying any. Old task dispositions precede plan replacement.
    eid = event(state, "DecisionApplied", decision_id=decision.decision_id, output=asdict(output))
    for intent in option.intents:
        if intent.machine_type == "disposition":
            request_disposition(state, find_task(state, intent.machine_id), intent.target_state)
    for intent in option.intents:
        if intent.machine_type == "mission":
            MISSION.apply(state, intent.target_state, eid)
    result = {
        "decision": asdict(output),
        "applied_intents": [asdict(i) for i in option.intents],
        "outcome": "APPLIED",
        "resulting_commit_seq": state["commit_seq"] + 1,
    }
    state["decisions"][decision.decision_id] = {
        "input": asdict(decision),
        "output": asdict(output),
        "guard_results": guards,
        "result": result,
    }
    return result


def deliver_due(state):
    due = sorted(
        (m for m in state["messages"] if m["due_ms"] <= state["time_ms"]),
        key=lambda m: (m["due_ms"], m["id"]),
    )
    due_ids = {m["id"] for m in due}
    state["messages"] = [m for m in state["messages"] if m["id"] not in due_ids]
    for message in due:
        if message["id"] in state["delivered_messages"]:
            continue
        state["delivered_messages"].append(message["id"])
        payload = message["payload"]
        kind = message["kind"]
        if kind == "instruction":
            deliver_instruction(state, payload)
        elif kind == "accept":
            deliver_acceptance(state, payload)
        elif kind == "disposition":
            deliver_disposition(state, payload)
        elif kind == "report":
            payload["delivered_ms"] = state["time_ms"]
            if REPORTING.report(state, payload):
                POSITIONS.report(state, payload)
                MOBILITY.report(state, payload)
                for service in TASKING.values():
                    service.report(state, payload)
                if payload["kind"] == "task_state" and payload["payload"]["state"] in {
                    "BLOCKED",
                    "FAILED",
                }:
                    state["mission"]["review_due"] = True
        else:
            raise ValueError(f"Unknown message kind: {kind}")


def boundary(state, provider=None):
    provider = provider or RandomDecisionProvider(state["config"]["decision_provider"]["seed"])
    provider.setstate(state["decision_rng"])
    # Apply the delivery phase first, then choose using the latest delivered evidence.
    # Everything remains private to this candidate until its one boundary commit.
    deliver_due(state)
    refresh_decision(state)
    if state["pending_decision"]:
        apply_choice(state, provider)
    update_conditions(state)
    model = C2SimulationModel(state)
    model.step()
    model.resolve(state)
    state["time_ms"] += state["config"]["dt_ms"]
    POSITIONS.age(state)
    state["decision_rng"] = provider.getstate()
    # Terminal mission records still drain outstanding confirmations before ending a run.
    if state["mission"]["state"] in MISSION_TERMINAL:
        unsettled = any(
            t["state"] not in TASK_TERMINAL
            for t in [*state["tasks"].values(), *state["archived_tasks"].values()]
        )
        operational_messages = any(
            m["kind"] != "report" or m["payload"]["kind"] != "position" for m in state["messages"]
        )
        if not unsettled and not operational_messages:
            state["lifecycle"] = "COMPLETED"
            state["control_revision"] += 1
            event(state, "RunCompleted", mission_state=state["mission"]["state"])


def finalize(state):
    state["commit_seq"] += 1
    refresh_decision(state)


class Coordinator:
    def __init__(
        self, repository, system_identity="headless-system", provider_factory=RandomDecisionProvider
    ):
        self.repository = repository
        self.system_identity = system_identity
        self.provider_factory = provider_factory

    def create(self, raw, spec, config):
        state = new_state(raw, spec, config, self.system_identity)
        self.repository.create(state)
        return state

    def choose(self, run_id, decision_id, expected_seq):
        with self.repository.mutex:
            state = self.repository.load(run_id)
            previous = state["decisions"].get(decision_id)
            if previous:
                if previous["input"]["commit_seq"] != expected_seq:
                    raise Conflict("Mismatched choose retry")
                return previous["result"]
            pending = state["pending_decision"]
            if (
                not pending
                or pending["decision_id"] != decision_id
                or state["commit_seq"] != expected_seq
            ):
                raise Conflict("Stale decision")
            provider = self.provider_factory(state["config"]["decision_provider"]["seed"])
            provider.setstate(state["decision_rng"])
            result = apply_choice(state, provider)
            state["decision_rng"] = provider.getstate()
            finalize(state)
            self.repository.commit(state, expected_seq)
            return result

    def control(self, run_id, command):
        with self.repository.mutex:
            state = self.repository.load(run_id)
            previous = state["commands"].get(command.command_id)
            if previous:
                if previous["input"] != command.model_dump():
                    raise Conflict("Command ID reused with different content")
                return previous["result"]
            if command.expected_control_revision != state["control_revision"]:
                raise Conflict("Stale control revision")
            action, lifecycle = command.action, state["lifecycle"]
            allowed = {
                "start": {"READY"},
                "step": {"READY", "PAUSED", "RUNNING"},
                "pause": {"RUNNING"},
                "resume": {"PAUSED"},
                "stop": {"READY", "RUNNING", "PAUSED"},
            }
            if lifecycle not in allowed[action]:
                raise Conflict(f"Cannot {action} a {lifecycle} run")
            expected = state["commit_seq"]
            if action == "step":
                boundary(state, self.provider_factory(state["config"]["decision_provider"]["seed"]))
            else:
                state["lifecycle"] = {
                    "start": "RUNNING",
                    "pause": "PAUSED",
                    "resume": "RUNNING",
                    "stop": "STOPPED",
                }[action]
            state["control_revision"] += 1
            event(state, "RunControl", command_id=command.command_id, action=action)
            finalize(state)
            result = summary(state)
            state["commands"][command.command_id] = {
                "input": command.model_dump(),
                "result": result,
            }
            self.repository.commit(state, expected)
            return result

    def tick(self, run_id):
        with self.repository.mutex:
            state = self.repository.load(run_id)
            if state["lifecycle"] != "RUNNING":
                return state
            expected = state["commit_seq"]
            try:
                boundary(state, self.provider_factory(state["config"]["decision_provider"]["seed"]))
                finalize(state)
                self.repository.commit(state, expected)
            except Exception:
                # The candidate is discarded; no partial messages, effects or RNG are published.
                raise
            return state

    def restore(self, run_id, seq):
        with self.repository.mutex:
            state = self.repository.load(run_id, seq)
            if (
                state["checkpoint_version"] != CHECKPOINT_VERSION
                or state["runtime_version"] != "0.1.0"
                or state["mesa_version"] != "3.5.1"
                or state["provider_version"] != "1.0.0"
            ):
                raise Conflict("Incompatible checkpoint")
            state["parent_run_id"], state["parent_checkpoint_seq"] = run_id, seq
            state["run_id"] = str(uuid.uuid4())
            # Preserve exact simulation/RNG state; branch-local command/decision IDs are new.
            state["lifecycle"] = "READY"
            state["control_revision"] = 0
            state["commit_seq"] = 0
            state["commands"] = {}
            refresh_decision(state)
            self.repository.create(state)
            return state


def summary(state):
    return {
        key: state[key]
        for key in (
            "run_id",
            "lifecycle",
            "control_revision",
            "commit_seq",
            "time_ms",
            "scenario_hash",
        )
    }
