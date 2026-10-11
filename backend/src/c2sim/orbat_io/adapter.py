"""Native ORBAT Mapper JSON exchange; no changes to the upstream application."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pyproj import CRS, Transformer
from c2sim.contracts.scenario import ScenarioSpec
from c2sim.domain.records import validate_graph

UPSTREAM_COMMIT = "d7d3a5cde9996549a18cff0fc8a1cc10421f6a62"


def timestamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ORBAT timestamps must include a timezone")
    return int(parsed.timestamp() * 1000)


def walk_units(source):
    def walk(units, parent, side):
        for unit in units:
            yield unit, parent, side
            yield from walk(unit.get("subUnits", []), unit["id"], side)

    for side in source["sides"]:
        yield from walk(side.get("subUnits", []), None, side["id"])
        for group in side.get("groups", []):
            yield from walk(group.get("subUnits", []), None, side["id"])


class Projection:
    def __init__(self, crs, area):
        definition = CRS.from_user_input(crs)
        if not definition.is_projected or any(a.unit_name != "metre" for a in definition.axis_info):
            raise ValueError("Simulation CRS must be projected in metres")
        self.forward = Transformer.from_crs("EPSG:4326", definition, always_xy=True)
        self.inverse = Transformer.from_crs(definition, "EPSG:4326", always_xy=True)
        self.area = area

    def project(self, point):
        if len(point) < 2 or not all(math.isfinite(v) for v in point):
            raise ValueError("Invalid coordinates")
        lon, lat = point[:2]
        w, s, e, n = self.area
        if not (w <= lon <= e and s <= lat <= n):
            raise ValueError(f"Position outside simulation area: {point}")
        x, y = self.forward.transform(lon, lat)
        back = self.inverse.transform(x, y)
        if (
            not all(math.isfinite(v) for v in (x, y))
            or max(abs(back[0] - lon), abs(back[1] - lat)) > 1e-8
        ):
            raise ValueError("Coordinate round trip exceeds tolerance")
        return [x, y]

    def unproject(self, point):
        return list(self.inverse.transform(*point))


def resources_at(unit, states):
    resources = {}
    for category in ("supplies", "equipment", "personnel"):
        entries = {}
        for item in unit.get(category, []):
            if item["name"] in entries:
                raise ValueError("Duplicate resource name")
            entries[item["name"]] = deepcopy(item)
        for state in states:
            for mode in ("update", "diff"):
                for item in state.get(mode, {}).get(category, []):
                    name = item["name"]
                    if name not in entries:
                        raise ValueError(
                            f"Timed resource update references missing base resource: {name}"
                        )
                    target = entries[name]
                    for field in ("count", "onHand"):
                        if field in item and (mode != "diff" or field == "onHand"):
                            target[field] = (
                                target.get(field, target.get("count", 0)) + item[field]
                                if mode == "diff"
                                else item[field]
                            )
        for item in entries.values():
            for key in ("count", "onHand"):
                if key in item and (not math.isfinite(item[key]) or item[key] < 0):
                    raise ValueError("Negative or nonfinite resource")
        resources[category] = entries
    return resources


def import_scenario(raw: bytes, spec: ScenarioSpec, start_utc: str):
    def unique_object(pairs):
        value = {}
        for k, v in pairs:
            if k in value:
                raise ValueError(f"Duplicate JSON key: {k}")
            value[k] = v
        return value

    source = json.loads(raw, object_pairs_hook=unique_object)
    if source.get("type") != "ORBAT-mapper" or not source.get("version", "").startswith("3."):
        raise ValueError("Expected a native ORBAT Mapper 3.x scenario")
    projection = Projection(spec.projected_crs, spec.area_wgs84)
    start = timestamp(start_utc)
    if "startTime" in source and timestamp(source["startTime"]) > start:
        raise ValueError("Requested start precedes the ORBAT scenario start")
    units, unmapped, ids = {}, [], set()

    def ignore(obj, mapped, path):
        for key in obj:
            if key not in mapped:
                unmapped.append({"path": f"{path}/{key}", "reason": "preserved_source_only"})

    ignore(source, {"id", "type", "version", "sides", "startTime", "layers", "layerStack"}, "")
    for side in source["sides"]:
        for container in [side, *side.get("groups", [])]:
            if container["id"] in ids:
                raise ValueError("Duplicate ORBAT identifier")
            ids.add(container["id"])
            ignore(container, {"id", "subUnits", "groups"}, f"/containers/{container['id']}")
    for unit, parent, side in walk_units(source):
        uid = unit["id"]
        if uid in ids:
            raise ValueError(f"Duplicate ORBAT identifier: {uid}")
        ids.add(uid)
        ordered = sorted(unit.get("state", []), key=lambda item: timestamp(item["t"]))
        times, state_ids = set(), set()
        for item in ordered:
            t = timestamp(item["t"])
            if t in times or item["id"] in state_ids:
                raise ValueError(f"Ambiguous timed state for {uid}")
            times.add(t)
            state_ids.add(item["id"])
        selected = [s for s in ordered if timestamp(s["t"]) <= start]
        location, sidc = unit.get("location"), unit["sidc"]
        for item in selected:
            if "hierarchy" in item:
                raise ValueError(
                    "Timed hierarchy at initial time is unsupported; flatten hierarchy before export"
                )
            if "location" in item:
                location = item["location"]
            sidc = item.get("sidc", sidc)
        # Between keyframes, upstream may interpolate via intermediate points. Do not guess.
        future = next((s for s in ordered if timestamp(s["t"]) > start and "location" in s), None)
        prior = next((s for s in reversed(selected) if "location" in s), None)
        if (
            future
            and future.get("interpolate", True)
            and (not prior or timestamp(prior["t"]) != start)
        ):
            raise ValueError(
                f"Ambiguous interpolated initial location for {uid}; add a state at start"
            )
        cfg = spec.unit_config.get(uid)
        if cfg is None:
            raise ValueError(f"Missing unit configuration: {uid}")
        if cfg.domain != "organization" and location is None:
            raise ValueError(f"Physical unit requires an initial location: {uid}")
        units[uid] = {
            "id": uid,
            "name": unit["name"],
            "sidc": sidc,
            "parent_id": parent,
            "side_id": side,
            "position": projection.project(location) if location else None,
            "resources": resources_at(unit, selected),
            **cfg.model_dump(),
        }
        ignore(
            unit,
            {
                "id",
                "name",
                "sidc",
                "subUnits",
                "location",
                "state",
                "supplies",
                "equipment",
                "personnel",
            },
            f"/units/{uid}",
        )
        for item in ordered:
            ignore(
                item,
                {"id", "t", "location", "sidc", "update", "diff"},
                f"/units/{uid}/state/{item['id']}",
            )
            if timestamp(item["t"]) > start:
                unmapped.append(
                    {
                        "path": f"/units/{uid}/state/{item['id']}",
                        "reason": "future_authoring_state_not_executed",
                    }
                )
        for category in ("supplies", "equipment", "personnel"):
            for item in unit.get(category, []):
                ignore(item, {"name", "count", "onHand"}, f"/units/{uid}/{category}/{item['name']}")
    if set(spec.unit_config) != set(units):
        raise ValueError("Unit configuration references missing units")
    validate_graph({uid: [u["parent_id"]] if u["parent_id"] else [] for uid, u in units.items()})
    routes = {}
    layers = source.get("layers", []) + source.get("layerStack", [])
    for layer in layers:
        items = layer.get("features", []) + layer.get("items", [])
        ignore(layer, {"id", "items", "features"}, f"/layers/{layer['id']}")
        for feature in items:
            geometry = feature.get("geometry", {})
            if geometry.get("type") == "LineString":
                rid = str(feature["id"])
                if rid in routes or rid in ids:
                    raise ValueError("Duplicate route identifier")
                coords = [projection.project(p) for p in geometry["coordinates"]]
                if len(coords) < 2:
                    raise ValueError("Route must have at least two points")
                routes[rid] = {"id": rid, "points": coords}
                ignore(feature, {"id", "geometry"}, f"/features/{rid}")
            else:
                unmapped.append({"path": f"/features/{feature['id']}", "reason": "not_a_route"})
    for task in [*spec.preliminary_tasks, *(t for p in spec.plans for t in p.tasks)]:
        if task.unit_id not in units or units[task.unit_id]["domain"] != task.domain:
            raise ValueError(f"Task unit/domain mismatch: {task.id}")
        if task.route_id and task.route_id not in routes:
            raise ValueError(f"Missing route for task: {task.id}")
        if task.transfer_to and (task.transfer_to not in units or task.transfer_to == task.unit_id):
            raise ValueError("Invalid transfer recipient")
    if not (
        set(spec.actual_blocked_routes) | {c.route_id for c in spec.route_condition_changes}
    ) <= set(routes):
        raise ValueError("Unknown actual blocked route")
    all_tasks = {t.id for p in spec.plans for t in p.tasks} | {t.id for t in spec.preliminary_tasks}

    def check_predicate(predicate):
        if predicate.op == "task_completed" and predicate.ref not in all_tasks:
            raise ValueError(f"Unknown criterion task: {predicate.ref}")
        if (
            predicate.op in {"element_accounted_for", "supply_received"}
            and predicate.ref not in units
        ):
            raise ValueError(f"Unknown criterion unit: {predicate.ref}")
        for child in predicate.args:
            check_predicate(child)

    check_predicate(spec.criteria)
    return {
        "units": units,
        "routes": routes,
        "unmapped_fields": unmapped,
        "source": source,
        "source_text": raw.decode(),
        "start_ms": start,
        "source_hash": hashlib.sha256(raw).hexdigest(),
        "upstream_commit": UPSTREAM_COMMIT,
    }


def export_orbat(state, history):
    result = deepcopy(state["normalized"]["source"])
    projection = Projection(state["scenario"]["projected_crs"], state["scenario"]["area_wgs84"])
    start = state["normalized"]["start_ms"]
    for unit, _, _ in walk_units(result):
        uid = unit["id"]
        # Keep pre-run authored history and replace future predictions with committed samples.
        unit["state"] = [s for s in unit.get("state", []) if timestamp(s["t"]) < start]
        samples = {s["time_ms"]: s["units"][uid] for s in history}
        for category in ("supplies", "equipment", "personnel"):
            existing = {r["name"] for r in unit.get(category, [])}
            for physical in samples.values():
                for name in physical["resources"][category]:
                    if name not in existing:
                        unit.setdefault(category, []).append({"name": name, "count": 0})
                        existing.add(name)
        for t, physical in sorted(samples.items()):
            sample = {
                "id": f"c2sim-{uid}-{t}",
                "t": start + t,
                "interpolate": False,
                "sidc": physical["sidc"],
                "update": {
                    cat: list(entries.values()) for cat, entries in physical["resources"].items()
                },
            }
            if physical["position"] is not None:
                sample["location"] = projection.unproject(physical["position"])
            unit["state"].append(sample)
    return result


def export_geojson(state, history):
    projection = Projection(state["scenario"]["projected_crs"], state["scenario"]["area_wgs84"])
    features = []
    for uid, unit in sorted(state["units"].items()):
        positions = [
            projection.unproject(s["units"][uid]["position"])
            for s in history
            if s["units"][uid]["position"] is not None
        ]
        if positions:
            features.append(
                {
                    "type": "Feature",
                    "id": uid,
                    "properties": {"unit_id": uid, "kind": "truth_track"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": positions if len(positions) > 1 else positions * 2,
                    },
                }
            )
    for report in state["reports"]:
        if "position" in report["payload"]:
            features.append(
                {
                    "type": "Feature",
                    "id": report["id"],
                    "properties": {
                        "kind": report["kind"],
                        "source": report["source"],
                        "observed_ms": report["observed_ms"],
                        "delivered_ms": report["delivered_ms"],
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": projection.unproject(report["payload"]["position"]),
                    },
                }
            )
    for rid, route in state["normalized"]["routes"].items():
        features.append(
            {
                "type": "Feature",
                "id": rid,
                "properties": {
                    "kind": "route",
                    "known_blocked": state["mobility"]
                    .get(rid, {})
                    .get("payload", {})
                    .get("blocked", False),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [projection.unproject(p) for p in route["points"]],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
