"""Per-run diagnostics derived from committed checkpoints, never candidate effects."""

from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sys
import uuid

from loguru import logger


# Loguru ships with a catch-all stderr sink. Replace it once so detailed run records
# stay in their run file while ordinary application diagnostics remain on stderr.
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    filter=lambda record: not record["extra"].get("run_file_only", False),
    colorize=True,
)


class RunLogs:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.owner = uuid.uuid4().hex
        self.handlers = {}

    def path(self, run_id):
        # Run IDs are generated internally; also protect callers handling an unknown ID.
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("Invalid run ID")
        return self.root / f"{run_id}.log"

    def write(self, state, kind, *, level="INFO", exception=None, **details):
        """Logging failures must not turn a successful database commit into a failed turn."""
        try:
            run_id = state["run_id"]
            if run_id not in self.handlers:
                self.root.mkdir(parents=True, exist_ok=True)
                self.handlers[run_id] = logger.add(
                    self.path(run_id),
                    mode="a",
                    encoding="utf-8",
                    level="DEBUG",
                    filter=lambda record, rid=run_id: (
                        record["extra"].get("log_owner") == self.owner
                        and record["extra"].get("run_id") == rid
                    ),
                    format=(
                        "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level:<8} | "
                        "run={extra[run_id]} | commit={extra[commit_seq]} | "
                        "sim_ms={extra[time_ms]} | scenario={extra[scenario_time]} | {message}"
                    ),
                    backtrace=False,
                    diagnose=False,
                    enqueue=False,
                    catch=True,
                )
            start = datetime.fromisoformat(state["config"]["scenario_start_utc"])
            bound = logger.bind(
                run_file_only=True,
                log_owner=self.owner,
                run_id=run_id,
                commit_seq=state["commit_seq"],
                time_ms=state["time_ms"],
                scenario_time=(start + timedelta(milliseconds=state["time_ms"])).isoformat(),
            )
            bound.opt(exception=exception).log(
                level, "{} {}", kind, json.dumps(details, sort_keys=True, ensure_ascii=True)
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Could not write diagnostics for run {}", state["run_id"]
            )

    def committed(self, previous, state):
        try:
            self._committed(previous, state)
        except Exception:
            # Diagnostics are secondary to the authoritative SQLite checkpoint.
            logger.opt(exception=True).warning(
                "Could not describe commit for run {}", state["run_id"]
            )

    def _committed(self, previous, state):
        if previous is None:
            self.write(
                state,
                "RunRestored" if state["parent_run_id"] else "RunCreated",
                lifecycle=state["lifecycle"],
                mission_state=state["mission"]["state"],
                parent_run_id=state["parent_run_id"],
                parent_checkpoint_seq=state["parent_checkpoint_seq"],
                config=state["config"],
                services=state["services"],
                scenario_hash=state["scenario_hash"],
                spec_hash=state["spec_hash"],
                unit_count=len(state["units"]),
            )
            for uid, unit in sorted(state["units"].items()):
                self.write(state, "UnitInitialState", level="DEBUG", unit_id=uid, unit=unit)
        else:
            self.write(
                state,
                "BoundaryCommitted",
                level="DEBUG",
                previous_commit_seq=previous["commit_seq"],
                previous_time_ms=previous["time_ms"],
                mesa_steps=state["mesa_steps"],
            )
            if state["lifecycle"] != previous["lifecycle"]:
                self.write(
                    state,
                    "RunStateChanged",
                    from_state=previous["lifecycle"],
                    to_state=state["lifecycle"],
                )

        # A restored branch retains historical evidence, but must not replay its parent's log.
        event_offset = (
            len(previous["events"])
            if previous
            else (len(state["events"]) if state["parent_run_id"] else 0)
        )
        for item in state["events"][event_offset:]:
            self.write(state, item["kind"], event=item)
        prior_decisions = previous["decisions"] if previous else state["decisions"]
        for did, decision in state["decisions"].items():
            if did not in prior_decisions:
                self.write(state, "DecisionResolved", **decision)
        if state["pending_decision"] and (
            previous is None or previous["pending_decision"] != state["pending_decision"]
        ):
            self.write(
                state, "DecisionAvailable", level="DEBUG", decision=state["pending_decision"]
            )

        if previous is None:
            return
        before_messages = {m["id"]: m for m in previous["messages"]}
        for message in state["messages"]:
            if message["id"] not in before_messages:
                self.write(state, "MessageScheduled", level="DEBUG", message=message)
        for mid in state["delivered_messages"][len(previous["delivered_messages"]) :]:
            self.write(
                state,
                "MessageDelivered",
                level="DEBUG",
                message=before_messages.get(mid, {"id": mid}),
            )
        for report in state["reports"][len(previous["reports"]) :]:
            self.write(state, "ReportReceived", level="DEBUG", report=report)
        for uid, unit in sorted(state["units"].items()):
            old = previous["units"][uid]
            key = unit["installed_task"] or old["installed_task"]
            spec = state["task_specs"].get(key, {})
            if unit["position"] != old["position"]:
                self.write(
                    state,
                    "UnitMoved",
                    unit_id=uid,
                    task_key=key,
                    route_id=spec.get("route_id"),
                    from_position_m=old["position"],
                    to_position_m=unit["position"],
                    displacement_m=math.dist(old["position"], unit["position"]),
                    speed_mps=unit["speed_mps"],
                    interval_start_ms=previous["time_ms"],
                    interval_end_ms=state["time_ms"],
                )
            if unit["resources"] != old["resources"]:
                self.write(
                    state,
                    "UnitResourcesChanged",
                    level="DEBUG",
                    unit_id=uid,
                    task_key=key,
                    before=old["resources"],
                    after=unit["resources"],
                )
            if (
                unit["activity"] != old["activity"]
                or unit["installed_task"] != old["installed_task"]
            ):
                self.write(
                    state,
                    "UnitActivityChanged",
                    level="DEBUG",
                    unit_id=uid,
                    previous_task=old["installed_task"],
                    task_key=unit["installed_task"],
                    before=old["activity"],
                    after=unit["activity"],
                )

    def close(self):
        for handler in self.handlers.values():
            logger.remove(handler)
        self.handlers.clear()
