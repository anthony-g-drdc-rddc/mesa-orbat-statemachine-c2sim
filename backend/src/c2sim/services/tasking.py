"""Shared task lifecycle with fixed ownership policies for services 7–10."""

from c2sim.domain.records import TASK_TERMINAL, Conflict, event, schedule

TASK_EDGES = {
    "DRAFT": {"ISSUED", "FAILED", "CANCELLED"},
    "ISSUED": {"ACKNOWLEDGED", "FAILED", "CANCELLED"},
    "ACKNOWLEDGED": {"ACCEPTED", "BLOCKED", "FAILED", "CANCELLED"},
    "ACCEPTED": {"IN_PROGRESS", "BLOCKED", "FAILED", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"},
    "BLOCKED": {"ACKNOWLEDGED", "ACCEPTED", "IN_PROGRESS", "FAILED", "CANCELLED"},
}


def task_key(spec):
    return f"{spec['id']}@{spec['version']}"


def find_task(state, key):
    return next(
        (t for t in state["tasks"].values() if t["key"] == key), state["archived_tasks"].get(key)
    )


def create_task(state, spec, authorized=False):
    key = task_key(spec)
    if key in state["task_specs"]:
        return
    state["task_specs"][key] = spec
    old = state["tasks"].get(spec["id"])
    if old:
        state["archived_tasks"][old["key"]] = old
    state["tasks"][spec["id"]] = {
        "key": key,
        "id": spec["id"],
        "version": spec["version"],
        "domain": spec["domain"],
        "state": "DRAFT",
        "revision": 0,
        "authorized": authorized,
        "disposition": "continue",
        "disposition_requested_ms": None,
        "disposition_effective_ms": None,
        "evidence_ids": [],
        "blocked_from": None,
    }
    event(state, "TaskCreated", task_key=key)


def transition_task(state, task, target, evidence_id):
    if target == task["state"] or task["state"] in TASK_TERMINAL:
        return False
    if target not in TASK_EDGES.get(task["state"], set()):
        raise Conflict(f"Invalid task transition {task['state']} -> {target}")
    if target == "BLOCKED":
        task["blocked_from"] = task["state"]
    if (
        task["state"] == "BLOCKED"
        and target not in TASK_TERMINAL
        and target != task["blocked_from"]
    ):
        raise Conflict("Block can clear only to the previous lifecycle state")
    previous = task["state"]
    task["state"] = target
    task["revision"] += 1
    task["evidence_ids"].append(evidence_id)
    event(
        state,
        "TaskTransition",
        task_key=task["key"],
        from_state=previous,
        target_state=target,
        evidence_ids=[evidence_id],
    )
    return True


def request_disposition(state, task, disposition):
    if task["state"] in TASK_TERMINAL:
        return
    task["disposition"] = disposition
    task["disposition_requested_ms"] = state["time_ms"]
    task["disposition_effective_ms"] = None
    task["revision"] += 1
    event(state, "DispositionRequested", task_key=task["key"], disposition=disposition)
    if task["state"] == "DRAFT" and disposition == "cancel":
        eid = event(state, "WithdrawalProven", task_key=task["key"])
        transition_task(state, task, "CANCELLED", eid)
        task["disposition_effective_ms"] = state["time_ms"]
    else:
        schedule(
            state,
            "disposition",
            {"task_key": task["key"], "disposition": disposition, "revision": task["revision"]},
        )


class TaskingService:
    domain = ""
    name = ""

    def issue(self, state, task):
        if task["domain"] != self.domain:
            raise Conflict("Wrong task service")
        if task["state"] != "DRAFT":
            return
        eid = event(state, "InstructionAdmitted", task_key=task["key"])
        transition_task(state, task, "ISSUED", eid)
        schedule(state, "instruction", {"task_key": task["key"]})

    def report(self, state, report):
        payload = report["payload"]
        task = find_task(state, payload.get("task_key"))
        if task is None or task["domain"] != self.domain:
            return
        # The exact version is updated; old-version evidence cannot advance its replacement.
        if report["kind"] == "task_state":
            target = payload["state"]
            if target == "CANCELLED" and payload.get("effective_ms") is None:
                raise Conflict("Cancellation requires effective evidence")
            transition_task(state, task, target, report["id"])
        elif report["kind"] == "disposition_effective":
            if payload["revision"] == task["revision"]:
                task["disposition_effective_ms"] = payload["effective_ms"]


class IntelligenceTasking(TaskingService):
    domain, name = "intelligence", "Intelligence Tasking"


class AirForceTasking(TaskingService):
    domain, name = "air", "Air Force Tasking"


class NavyTasking(TaskingService):
    domain, name = "navy", "Navy Tasking"


class ArmyTasking(TaskingService):
    domain, name = "army", "Army Tasking"


TASKING = {
    service.domain: service
    for service in (IntelligenceTasking(), AirForceTasking(), NavyTasking(), ArmyTasking())
}
