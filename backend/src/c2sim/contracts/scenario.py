"""Strict, non-executable scenario and API input schemas."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from c2sim.domain.records import validate_graph


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Predicate(StrictModel):
    op: Literal[
        "all_of",
        "any_of",
        "task_completed",
        "report_confirmed",
        "supply_received",
        "element_accounted_for",
    ]
    args: list["Predicate"] = Field(default_factory=list)
    ref: str | None = None
    amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def shape(self):
        if self.op in {"all_of", "any_of"}:
            if not self.args or self.ref is not None or self.amount is not None:
                raise ValueError("Boolean predicates require nonempty args only")
        elif not self.ref or self.args:
            raise ValueError("Leaf predicates require ref and no args")
        elif self.op == "supply_received" and self.amount is None:
            raise ValueError("supply_received requires amount")
        elif self.op != "supply_received" and self.amount is not None:
            raise ValueError("Only supply_received accepts amount")
        return self


class UnitConfig(StrictModel):
    domain: Literal["organization", "intelligence", "air", "navy", "army"]
    speed_mps: float = Field(default=0, ge=0)
    fuel_per_second: float = Field(default=0, ge=0)


class TaskSpec(StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    domain: Literal["intelligence", "air", "navy", "army"]
    unit_id: str
    activity: Literal["move", "wait", "inspect", "collect", "load", "unload", "deliver"]
    route_id: str | None = None
    duration_ms: int = Field(default=1000, ge=0)
    dependencies: list[str] = Field(default_factory=list)
    preliminary: bool = False
    initial: bool = True
    supply_amount: float = Field(default=0, ge=0)
    transfer_to: str | None = None
    finding: str | None = None
    priority: int = Field(default=1, ge=1)
    deadline_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def activity_fields(self):
        if self.activity == "move" and not self.route_id:
            raise ValueError("move requires route_id")
        if self.supply_amount and (
            self.activity not in {"load", "unload", "deliver"} or not self.transfer_to
        ):
            raise ValueError("Supply transfer requires load/unload/deliver and transfer_to")
        if self.activity == "inspect" and not self.route_id:
            raise ValueError("inspect requires route_id")
        return self


class PlanSpec(StrictModel):
    version: int = Field(ge=1)
    tasks: list[TaskSpec]
    trigger_report: str | None = None
    removed_dispositions: dict[str, Literal["continue", "pause", "cancel", "complete"]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def graph(self):
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task identifiers")
        validate_graph({task.id: task.dependencies for task in self.tasks})
        return self


class Timings(StrictModel):
    communication_delay_ms: int = Field(default=1000, ge=0)
    position_period_ms: int = Field(default=5000, gt=0)
    stale_after_ms: int = Field(default=15000, gt=0)


class RouteConditionChange(StrictModel):
    time_ms: int = Field(ge=0)
    route_id: str
    blocked: bool


class ScenarioSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: str
    objectives: list[str] = Field(min_length=1)
    criteria: Predicate
    projected_crs: str
    area_wgs84: tuple[float, float, float, float]
    unit_config: dict[str, UnitConfig]
    preliminary_tasks: list[TaskSpec] = Field(default_factory=list)
    plans: list[PlanSpec] = Field(min_length=1)
    actual_blocked_routes: list[str] = Field(default_factory=list)
    route_condition_changes: list[RouteConditionChange] = Field(default_factory=list)
    accepted_information_gaps: list[str] = Field(default_factory=list)
    timings: Timings = Field(default_factory=Timings)

    @model_validator(mode="after")
    def validate_plans(self):
        west, south, east, north = self.area_wgs84
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("Invalid simulation area")
        changes = [(c.time_ms, c.route_id) for c in self.route_condition_changes]
        if len(changes) != len(set(changes)) or any(
            c.time_ms % 1000 for c in self.route_condition_changes
        ):
            raise ValueError("Route condition changes must be unique and on a 1000 ms boundary")
        versions = [p.version for p in self.plans]
        if versions != sorted(set(versions)):
            raise ValueError("Plan versions must be unique and increasing")
        pre = [t.id for t in self.preliminary_tasks]
        if len(pre) != len(set(pre)):
            raise ValueError("Duplicate preliminary task")
        validate_graph({t.id: t.dependencies for t in self.preliminary_tasks})
        contents = {}
        previous = None
        for plan in self.plans:
            for task in plan.tasks:
                if task.id in pre or task.preliminary:
                    raise ValueError("Main and preliminary tasks must be separate")
                key = (task.id, task.version)
                if key in contents and contents[key] != task:
                    raise ValueError("Material task changes require a new version")
                contents[key] = task
            if previous:
                current = {t.id: t for t in plan.tasks}
                for old in previous.tasks:
                    if old.id not in current and old.id not in plan.removed_dispositions:
                        raise ValueError("Removed tasks require explicit dispositions")
                    if old.id in current and current[old.id].version < old.version:
                        raise ValueError("Task versions cannot go backwards")
            previous = plan
        for task in self.preliminary_tasks:
            if not task.preliminary:
                raise ValueError("Preliminary tasks must explicitly declare preliminary=true")
        return self


class RandomProviderConfig(StrictModel):
    type: Literal["random"]
    seed: int


class RunCreateInput(StrictModel):
    scenario_start_utc: str
    mesa_seed: int
    dt_ms: int = Field(default=1000, gt=0)
    decision_provider: RandomProviderConfig

    @field_validator("scenario_start_utc")
    @classmethod
    def utc(cls, value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
            raise ValueError("scenario_start_utc must be timezone-aware UTC")
        return value

    @field_validator("dt_ms")
    @classmethod
    def fixed_interval(cls, value):
        if value != 1000:
            raise ValueError("This release requires dt_ms=1000")
        return value


class RunCreateOutput(StrictModel):
    run_id: str
    lifecycle: Literal["READY"]
    control_revision: int
    scenario_hash: str
    provider_id: str
    provider_version: str


class ControlInput(StrictModel):
    command_id: str = Field(min_length=1)
    action: Literal["start", "step", "pause", "resume", "stop"]
    expected_control_revision: int = Field(ge=0)


class DecisionChooseInput(StrictModel):
    decision_id: str
    expected_commit_seq: int = Field(ge=0)


class RestoreInput(StrictModel):
    checkpoint_seq: int = Field(ge=0)


class DecisionAppliedOutput(StrictModel):
    decision: "DecisionOutput"
    applied_intents: tuple["TransitionIntent", ...]
    outcome: Literal["APPLIED", "STALE", "REJECTED"]
    resulting_commit_seq: int | None


from c2sim.decisions.provider import DecisionOutput, TransitionIntent  # noqa: E402

DecisionAppliedOutput.model_rebuild()
