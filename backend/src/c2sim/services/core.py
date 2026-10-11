"""Six fixed coordination services. Mutations are invoked only on coordinator candidates."""

from copy import deepcopy
from c2sim.domain.records import TASK_TERMINAL, MISSION_TERMINAL, event
from c2sim.domain.predicates import GuardResult, evaluate
from c2sim.services.tasking import create_task, TASKING

MISSION_EDGES = {
    "ORGANIZING": ("REVIEWING_SITUATION",),
    "REVIEWING_SITUATION": ("PLANNING_TASKS",),
    "PLANNING_TASKS": ("DISPATCHING_DOMAIN_TASKS",),
    "DISPATCHING_DOMAIN_TASKS": ("EXECUTING",),
    "EXECUTING": ("COMMANDER_REVIEW", "PAUSED"),
    "COMMANDER_REVIEW": (
        "EXECUTING",
        "PLANNING_TASKS",
        "REVIEWING_SITUATION",
        "PAUSED",
        "COMPLETED",
    ),
    "PAUSED": ("EXECUTING", "PLANNING_TASKS"),
}


class ForceOrganization:
    name = "Force Organization"

    def reserve(self, state, task, unit):
        existing = state["reservations"].get(unit["id"])
        if existing and existing != task["key"]:
            return False
        state["reservations"][unit["id"]] = task["key"]
        return True

    def release(self, state, unit_id, key):
        if state["reservations"].get(unit_id) == key:
            del state["reservations"][unit_id]


class PositionTracking:
    name = "Position Tracking"

    def report(self, state, report):
        if report["kind"] != "position":
            return
        uid = report["source"]
        old = state["positions"].get(uid)
        if old is None or (report["observed_ms"], report["id"]) > (old["observed_ms"], old["id"]):
            state["positions"][uid] = deepcopy(report)

    def age(self, state):
        for report in state["positions"].values():
            report["stale"] = (
                state["time_ms"] - report["observed_ms"]
                > state["scenario"]["timings"]["stale_after_ms"]
            )


class TacticalReporting:
    name = "Tactical Reporting"

    def report(self, state, report):
        if any(r["id"] == report["id"] for r in state["reports"]):
            return False
        state["reports"].append(report)
        event(state, "ReportDelivered", report_id=report["id"], report_kind=report["kind"])
        return True


class TerrainMobility:
    name = "Terrain and Mobility"

    def report(self, state, report):
        if report["kind"] != "route_condition" or not report["confirmed"]:
            return
        rid = report["payload"]["route_id"]
        old = state["mobility"].get(rid)
        if old is None or report["observed_ms"] > old["observed_ms"]:
            state["mobility"][rid] = deepcopy(report)
            state["mission"]["review_due"] = True


class TaskPlanning:
    name = "Task Planning"

    def candidate(self, state):
        approved = state["mission"]["approved_plan"]
        candidates = [p for p in state["scenario"]["plans"] if p["version"] > approved]
        for plan in candidates:
            trigger = plan["trigger_report"]
            if not trigger or evaluate({"op": "report_confirmed", "ref": trigger}, state).allowed:
                return plan
        return next((p for p in state["scenario"]["plans"] if p["version"] == approved), None)

    def validate(self, state, plan):
        if plan is None:
            return GuardResult(False, ("NO_PLAN",))
        reasons = []
        # Competing tasks on one unit require a dependency path; reservations additionally
        # serialize old/new versions until the actual old activity has stopped.
        specs = {t["id"]: t for t in plan["tasks"]}

        def ancestors(tid):
            return {dep for d in specs[tid]["dependencies"] for dep in ({d} | ancestors(d))}

        tasks = list(specs.values())
        for i, task in enumerate(tasks):
            for other in tasks[i + 1 :]:
                if (
                    task["unit_id"] == other["unit_id"]
                    and task["id"] not in ancestors(other["id"])
                    and other["id"] not in ancestors(task["id"])
                ):
                    reasons.append("UNIT_ASSIGNMENT_CONFLICT")
            route = state["mobility"].get(task["route_id"])
            if task["activity"] != "inspect" and route and route["payload"]["blocked"]:
                reasons.append("KNOWN_BLOCKED_ROUTE")
            if task["supply_amount"]:
                resources = state["normalized"]["units"][task["unit_id"]]["resources"]["supplies"]
                if (
                    resources.get("supply", {}).get(
                        "onHand", resources.get("supply", {}).get("count", 0)
                    )
                    < task["supply_amount"]
                ):
                    # Incoming deliveries may supply a dependent transfer.
                    if not any(
                        t["transfer_to"] == task["unit_id"] and t["id"] in ancestors(task["id"])
                        for t in tasks
                    ):
                        reasons.append("INSUFFICIENT_PLANNED_SUPPLY")
        evidence = tuple(sorted(r["id"] for r in state["mobility"].values()))
        return GuardResult(not reasons, tuple(sorted(set(reasons))), evidence)

    def approve(self, state, plan, evidence_id):
        version = str(plan["version"])
        if version not in state["plans"]:
            state["plans"][version] = deepcopy(plan)
        state["mission"]["approved_plan"] = plan["version"]
        state["mission"]["approvals"].append(
            {"plan_version": plan["version"], "evidence_id": evidence_id}
        )
        for spec in plan["tasks"]:
            create_task(state, deepcopy(spec))
            TASKING[spec["domain"]].issue(state, state["tasks"][spec["id"]])
        event(state, "PlanApproved", plan_version=plan["version"])


class MissionControl:
    name = "Mission Control"

    def guard(self, state, target):
        mission = state["mission"]
        current = mission["state"]
        if current in MISSION_TERMINAL:
            return GuardResult(False, ("TERMINAL_MISSION",))
        if target == "CANCELLED":
            return GuardResult(True)
        if target not in MISSION_EDGES.get(current, ()):
            return GuardResult(False, ("INVALID_TARGET",))
        if target == "COMPLETED":
            result = evaluate(state["scenario"]["criteria"], state)
            if any(
                t["state"] not in TASK_TERMINAL
                for t in [*state["tasks"].values(), *state["archived_tasks"].values()]
            ):
                return GuardResult(False, ("OUTSTANDING_TASK_DISPOSITIONS",))
            return result
        if current == "ORGANIZING":
            return GuardResult(
                bool(state["force"] and mission["objectives"]),
                ("FORCE_AND_CRITERIA_REQUIRED",) if not state["force"] else (),
                ("scenario:mission",),
            )
        if current == "REVIEWING_SITUATION":
            pre = [
                state["tasks"][t["id"]]
                for t in state["scenario"]["preliminary_tasks"]
                if t["id"] in state["tasks"]
            ]
            ok = len(pre) == len(state["scenario"]["preliminary_tasks"]) and all(
                t["state"] == "COMPLETED" for t in pre
            )
            return GuardResult(
                ok,
                () if ok else ("PRELIMINARY_FINDINGS_PENDING",),
                tuple(e for t in pre for e in t["evidence_ids"]),
            )
        if current == "PLANNING_TASKS":
            return PLANNING.validate(state, PLANNING.candidate(state))
        if current == "DISPATCHING_DOMAIN_TASKS" and target == "EXECUTING":
            plan = state["plans"][str(mission["approved_plan"])]
            tasks = [state["tasks"][t["id"]] for t in plan["tasks"] if t["initial"]]
            ok = all(t["state"] in {"ACCEPTED", "IN_PROGRESS", "COMPLETED"} for t in tasks)
            return GuardResult(
                ok,
                () if ok else ("INITIAL_ACCEPTANCE_PENDING",),
                tuple(e for t in tasks for e in t["evidence_ids"]),
            )
        if current == "EXECUTING" and target == "COMMANDER_REVIEW":
            ok = mission["review_due"] or evaluate(state["scenario"]["criteria"], state).allowed
            return GuardResult(
                ok,
                () if ok else ("NO_DECISION_POINT",),
                tuple(
                    r["id"]
                    for r in state["reports"]
                    if r["kind"] in {"route_condition", "receipt", "accountability"}
                ),
            )
        if target == "EXECUTING":
            ok = mission["approved_plan"] > 0
            return GuardResult(ok, () if ok else ("NO_APPROVED_PLAN",))
        return GuardResult(True)

    def apply(self, state, target, evidence_id):
        mission = state["mission"]
        previous = mission["state"]
        mission["state"] = target
        mission["revision"] += 1
        event(
            state,
            "MissionTransition",
            from_state=previous,
            target_state=target,
            evidence_id=evidence_id,
        )
        if target == "REVIEWING_SITUATION":
            mission["planning_basis"] = {
                "accepted_information_gaps": state["scenario"]["accepted_information_gaps"],
                "report_ids": [r["id"] for r in state["reports"]],
            }
            for spec in state["scenario"]["preliminary_tasks"]:
                create_task(state, deepcopy(spec), authorized=True)
                TASKING[spec["domain"]].issue(state, state["tasks"][spec["id"]])
            event(state, "PreliminaryCollectionAuthorized")
        if target == "DISPATCHING_DOMAIN_TASKS":
            PLANNING.approve(state, PLANNING.candidate(state), evidence_id)
        if target == "EXECUTING":
            mission["review_due"] = False
            mission["authorizations"].append(
                {"plan_version": mission["approved_plan"], "evidence_id": evidence_id}
            )
            plan = state["plans"][str(mission["approved_plan"])]
            for spec in plan["tasks"]:
                task = state["tasks"][spec["id"]]
                task["authorized"] = True


MISSION = MissionControl()
FORCE = ForceOrganization()
POSITIONS = PositionTracking()
REPORTING = TacticalReporting()
MOBILITY = TerrainMobility()
PLANNING = TaskPlanning()
SERVICES = (MISSION, FORCE, POSITIONS, REPORTING, MOBILITY, PLANNING, *TASKING.values())
