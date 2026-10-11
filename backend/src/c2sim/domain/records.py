"""Framework-independent records, validation helpers, and audit primitives."""

from enum import StrEnum
import hashlib
import json


class RunState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class MissionState(StrEnum):
    ORGANIZING = "ORGANIZING"
    REVIEWING_SITUATION = "REVIEWING_SITUATION"
    PLANNING_TASKS = "PLANNING_TASKS"
    DISPATCHING_DOMAIN_TASKS = "DISPATCHING_DOMAIN_TASKS"
    EXECUTING = "EXECUTING"
    COMMANDER_REVIEW = "COMMANDER_REVIEW"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TaskState(StrEnum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TASK_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
MISSION_TERMINAL = {"COMPLETED", "CANCELLED"}
RUN_TERMINAL = {"COMPLETED", "STOPPED", "FAILED"}


class Conflict(ValueError):
    """A stale command or an invalid transition (HTTP 409)."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def next_id(state, prefix):
    state["counter"] += 1
    return f"{prefix}-{state['counter']:08d}"


def event(state, kind, **data):
    item = {
        "id": f"control:{data['command_id']}" if kind == "RunControl" else next_id(state, "event"),
        "kind": kind,
        "time_ms": state["time_ms"],
        "commit_seq": state["commit_seq"] + 1,
        "identity": state["system_identity"],
        **data,
    }
    state["events"].append(item)
    return item["id"]


def schedule(state, kind, payload, delay_ms=None):
    delay = state["scenario"]["timings"]["communication_delay_ms"] if delay_ms is None else delay_ms
    message = {
        "id": next_id(state, "message"),
        "kind": kind,
        "payload": payload,
        "sent_ms": state["time_ms"],
        "due_ms": state["time_ms"] + max(state["config"]["dt_ms"], delay),
    }
    state["messages"].append(message)
    return message["id"]


def validate_graph(graph: dict[str, list[str]]):
    visiting, visited = set(), set()

    def visit(node):
        if node not in graph:
            raise ValueError(f"Missing reference: {node}")
        if node in visiting:
            raise ValueError(f"Dependency/hierarchy cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
