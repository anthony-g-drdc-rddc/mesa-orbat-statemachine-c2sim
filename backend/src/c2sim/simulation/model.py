"""Mesa owns physical state. Agents propose; the stable resolver applies effects."""

from copy import deepcopy
import math
import mesa
from c2sim.decisions.provider import as_tuple
from c2sim.domain.records import TASK_TERMINAL, event, next_id, schedule
from c2sim.domain.predicates import evaluate
from c2sim.services.core import FORCE
from c2sim.services.tasking import find_task


def report(state, kind, source, payload, confirmed=True):
    item = {
        "id": next_id(state, "report"),
        "kind": kind,
        "source": source,
        "observed_ms": state["time_ms"],
        "sent_ms": state["time_ms"],
        "delivered_ms": None,
        "confirmed": confirmed,
        "payload": deepcopy(payload),
    }
    schedule(state, "report", item)
    event(state, "ReportSent", report_id=item["id"], report_kind=kind)
    return item["id"]


def task_report(state, unit, key, target, **extra):
    return report(state, "task_state", unit["id"], {"task_key": key, "state": target, **extra})


def stop_activity(state, unit, key):
    if unit["installed_task"] == key:
        unit["installed_task"] = None
        unit["activity"] = None
    FORCE.release(state, unit["id"], key)


def deliver_instruction(state, payload):
    key = payload["task_key"]
    task = find_task(state, key)
    if not task or task["state"] in TASK_TERMINAL or task["disposition"] == "cancel":
        return
    spec = state["task_specs"][key]
    unit = state["units"][spec["unit_id"]]
    if unit["installed_task"] not in (None, key) or not FORCE.reserve(state, task, unit):
        schedule(state, "instruction", payload)
        return
    if unit["installed_task"] == key:
        return
    unit["installed_task"] = key
    unit["last_instruction"] = {"task_key": key, "received_ms": state["time_ms"]}
    unit["activity"] = {
        "elapsed_ms": 0,
        "waypoint": 0,
        "started": False,
        "blocked": False,
        "paused": False,
        "accepted": False,
    }
    event(state, "InstructionDelivered", task_key=key)
    task_report(state, unit, key, "ACKNOWLEDGED")
    schedule(state, "accept", {"task_key": key}, 2 * state["config"]["dt_ms"])


def deliver_acceptance(state, payload):
    key = payload["task_key"]
    spec = state["task_specs"][key]
    unit = state["units"][spec["unit_id"]]
    if unit["installed_task"] != key or unit["activity"]["accepted"]:
        return
    unit["activity"]["accepted"] = True
    event(state, "ActualAcceptance", task_key=key)
    task_report(state, unit, key, "ACCEPTED")


def deliver_disposition(state, payload):
    key, disposition = payload["task_key"], payload["disposition"]
    task = find_task(state, key)
    spec = state["task_specs"][key]
    unit = state["units"][spec["unit_id"]]
    # Superseded instructions must not resurrect a pause after a later continuation.
    if task is None or task["disposition"] != disposition:
        return
    if key in state["actual_results"]:
        event(state, "LateDisposition", task_key=key, disposition=disposition)
        return
    if disposition == "cancel":
        state["actual_results"][key] = "CANCELLED"
        stop_activity(state, unit, key)
        event(state, "ActualCancellation", task_key=key)
        task_report(state, unit, key, "CANCELLED", effective_ms=state["time_ms"])
        report(state, "accountability", unit["id"], {"task_key": key})
    elif unit["installed_task"] == key:
        unit["activity"]["paused"] = disposition == "pause"
    report(
        state, "disposition_effective", unit["id"], {**payload, "effective_ms": state["time_ms"]}
    )


class UnitAgent(mesa.Agent):
    def __init__(self, model, unit_id):
        super().__init__(model)
        self.unit_id = unit_id

    def propose(self):
        state = self.model.snapshot
        unit = state["units"][self.unit_id]
        key = unit["installed_task"]
        if not key:
            return None
        task, activity = find_task(state, key), unit["activity"]
        spec = state["task_specs"][key]
        if task["state"] in TASK_TERMINAL or activity["paused"] or activity["blocked"]:
            return None
        if not activity["started"]:
            if (
                task["state"] != "ACCEPTED"
                or not task["authorized"]
                or task["disposition"] != "continue"
                or state["mission"]["state"] in {"PAUSED", "CANCELLED", "COMPLETED"}
            ):
                return None
            if not all(
                evaluate({"op": "task_completed", "ref": dep}, state).allowed
                for dep in spec["dependencies"]
            ):
                return None
        dt = state["config"]["dt_ms"]
        fuel = unit["fuel_per_second"] * dt / 1000
        stock = unit["resources"]["supplies"].get("fuel", {})
        if stock.get("onHand", stock.get("count", 0)) < fuel:
            return {"unit_id": self.unit_id, "key": key, "failed": "FUEL_EXHAUSTED"}
        if spec["deadline_ms"] is not None and state["time_ms"] >= spec["deadline_ms"]:
            return {"unit_id": self.unit_id, "key": key, "failed": "DEADLINE_EXCEEDED"}
        proposal = {
            "unit_id": self.unit_id,
            "key": key,
            "fuel": fuel,
            "position": list(unit["position"]),
            "waypoint": activity["waypoint"],
            "elapsed_ms": activity["elapsed_ms"] + dt,
            "blocked": False,
        }
        if spec["route_id"] and spec["activity"] != "inspect":
            points = state["normalized"]["routes"][spec["route_id"]]["points"]
            budget = unit["speed_mps"] * dt / 1000
            i = activity["waypoint"]
            while i < len(points):
                # A closed crossing stops traversal at the first route segment, regardless
                # of whether headquarters has received its report yet.
                if i >= 1 and state["actual_route_conditions"].get(spec["route_id"], False):
                    proposal["blocked"] = True
                    break
                target = points[i]
                distance = math.dist(proposal["position"], target)
                if distance <= budget:
                    proposal["position"] = list(target)
                    budget -= distance
                    i += 1
                else:
                    if distance:
                        proposal["position"] = [
                            p + (q - p) * budget / distance
                            for p, q in zip(proposal["position"], target)
                        ]
                    break
            proposal["waypoint"] = i
            proposal["done"] = i == len(points) and proposal["elapsed_ms"] >= spec["duration_ms"]
        else:
            proposal["done"] = proposal["elapsed_ms"] >= spec["duration_ms"]
        return proposal


class C2SimulationModel(mesa.Model):
    def __init__(self, state):
        super().__init__(rng=state["config"]["mesa_seed"])
        self.snapshot = state  # All proposals finish before the resolver mutates this candidate.
        if state["mesa_rng"]:
            self.random.setstate(as_tuple(state["mesa_rng"]["python"]))
            self.rng.bit_generator.state = state["mesa_rng"]["numpy"]
        self.steps = state["mesa_steps"]
        for uid, unit in sorted(state["units"].items()):
            if unit["domain"] != "organization":
                UnitAgent(self, uid)
        self.proposals = []

    def step(self):
        self.proposals = [p for agent in self.agents if (p := agent.propose()) is not None]

    def resolve(self, state):
        for proposal in sorted(self.proposals, key=lambda p: p["unit_id"]):
            unit, key = state["units"][proposal["unit_id"]], proposal["key"]
            spec, activity = state["task_specs"][key], unit["activity"]
            if "failed" in proposal:
                state["actual_results"][key] = "FAILED"
                task_report(state, unit, key, "FAILED", reason=proposal["failed"])
                report(state, "accountability", unit["id"], {"task_key": key})
                stop_activity(state, unit, key)
                continue
            if not activity["started"]:
                activity["started"] = True
                event(state, "ActualStart", task_key=key)
                task_report(state, unit, key, "IN_PROGRESS")
            if proposal["fuel"]:
                fuel = unit["resources"]["supplies"]["fuel"]
                fuel["onHand"] = fuel.get("onHand", fuel["count"]) - proposal["fuel"]
            unit["position"] = proposal["position"]
            activity.update({k: proposal[k] for k in ("elapsed_ms", "waypoint", "blocked")})
            if proposal["blocked"]:
                event(state, "ActualBlock", task_key=key, route_id=spec["route_id"])
                task_report(state, unit, key, "BLOCKED")
                report(
                    state,
                    "route_condition",
                    unit["id"],
                    {
                        "route_id": spec["route_id"],
                        "blocked": True,
                        "finding": f"{spec['route_id']}:unavailable",
                    },
                )
                continue
            if not proposal["done"]:
                report(
                    state,
                    "progress",
                    unit["id"],
                    {"task_key": key, "elapsed_ms": activity["elapsed_ms"]},
                )
                continue
            if spec["supply_amount"]:
                transfer_id = f"transfer:{key}"
                resources = unit["resources"]["supplies"]
                stock = resources.get("supply", {"count": 0})
                available = stock.get("onHand", stock["count"])
                if available < spec["supply_amount"]:
                    activity["blocked"] = True
                    task_report(state, unit, key, "BLOCKED", reason="INSUFFICIENT_SUPPLY")
                    continue
                if transfer_id not in state["transfers"]:
                    recipient = state["units"][spec["transfer_to"]]
                    if math.dist(unit["position"], recipient["position"]) > 1:
                        activity["blocked"] = True
                        task_report(state, unit, key, "BLOCKED", reason="RECIPIENT_NOT_COLOCATED")
                        continue
                    stock["onHand"] = available - spec["supply_amount"]
                    target = recipient["resources"]["supplies"].setdefault(
                        "supply", {"name": "supply", "count": 0}
                    )
                    target["onHand"] = target.get("onHand", target["count"]) + spec["supply_amount"]
                    transfer = {
                        "transfer_id": transfer_id,
                        "recipient": recipient["id"],
                        "amount": spec["supply_amount"],
                    }
                    state["transfers"][transfer_id] = transfer
                    report(state, "receipt", recipient["id"], transfer)
                    event(state, "SupplyTransferred", **transfer)
            if spec["activity"] == "inspect":
                blocked = state["actual_route_conditions"].get(spec["route_id"], False)
                report(
                    state,
                    "route_condition",
                    unit["id"],
                    {
                        "route_id": spec["route_id"],
                        "blocked": blocked,
                        "finding": f"{spec['route_id']}:{'unavailable' if blocked else 'available'}",
                    },
                )
            if spec["activity"] == "collect":
                report(
                    state,
                    "finding",
                    unit["id"],
                    {"finding": spec["finding"], "task_key": key},
                    confirmed=False,
                )
            state["actual_results"][key] = "COMPLETED"
            event(state, "ActualCompletion", task_key=key)
            task_report(state, unit, key, "COMPLETED")
            report(state, "accountability", unit["id"], {"task_key": key})
            stop_activity(state, unit, key)
        # Every physical element reports, including idle supply recipients.
        for uid, unit in sorted(state["units"].items()):
            if (
                unit["domain"] != "organization"
                and state["time_ms"] % state["scenario"]["timings"]["position_period_ms"] == 0
            ):
                report(state, "position", uid, {"position": unit["position"], "accuracy_m": 5})
        state["mesa_rng"] = {
            "python": self.random.getstate(),
            "numpy": self.rng.bit_generator.state,
        }
        state["mesa_steps"] = self.steps


def update_conditions(state):
    for change in state["scenario"]["route_condition_changes"]:
        if change["time_ms"] == state["time_ms"]:
            state["actual_route_conditions"][change["route_id"]] = change["blocked"]
            event(state, "ActualRouteCondition", **change)
    for unit in state["units"].values():
        key = unit["installed_task"]
        if not key or not unit["activity"]["blocked"]:
            continue
        task, spec = find_task(state, key), state["task_specs"][key]
        if (
            task["state"] != "BLOCKED"
            or not task["authorized"]
            or task["disposition"] != "continue"
            or state["mission"]["state"] in {"PAUSED", "CANCELLED", "COMPLETED"}
            or state["actual_route_conditions"].get(spec["route_id"], False)
        ):
            continue
        if not all(
            evaluate({"op": "task_completed", "ref": dep}, state).allowed
            for dep in spec["dependencies"]
        ):
            continue
        stock = unit["resources"]["supplies"].get("supply", {"count": 0})
        if spec["supply_amount"] > stock.get("onHand", stock["count"]):
            continue
        if (
            spec["transfer_to"]
            and math.dist(unit["position"], state["units"][spec["transfer_to"]]["position"]) > 1
        ):
            points = state["normalized"]["routes"].get(spec["route_id"], {}).get("points", [])
            if unit["activity"]["waypoint"] >= len(points):
                continue
        unit["activity"]["blocked"] = False
        task_report(state, unit, key, task["blocked_from"])
        if spec["route_id"]:
            report(
                state,
                "route_condition",
                unit["id"],
                {
                    "route_id": spec["route_id"],
                    "blocked": False,
                    "finding": f"{spec['route_id']}:available",
                },
            )
        event(state, "ActualBlockCleared", task_key=key)
