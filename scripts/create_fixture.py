"""Build the authored native ORBAT fixture and declarative C2 scenario (no runtime policy)."""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "scenarios/coastal-resupply"


def write(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + "\n")


locations = {
    "HQ-1": [-1.1, 50.7],
    "INT-1": [-1.1, 50.7],
    "AIR-REC-1": [-1.1, 50.7],
    "AIR-LIFT-1": [-1.1, 50.7],
    "SEA-1": [-1.1, 50.7],
    "ENG-1": [-1.099, 50.7],
    "GROUND-1": [-1.099, 50.7],
    "POST-1": [-1.098, 50.7],
}
domains = {
    "HQ-1": "organization",
    "INT-1": "intelligence",
    "AIR-REC-1": "air",
    "AIR-LIFT-1": "air",
    "SEA-1": "navy",
    "ENG-1": "army",
    "GROUND-1": "army",
    "POST-1": "army",
}
units = []
for uid, location in locations.items():
    supplies = [
        {"name": "fuel", "count": 1000},
        {"name": "supply", "count": 10 if uid == "AIR-LIFT-1" else 40 if uid == "SEA-1" else 0},
    ]
    units.append(
        {
            "id": uid,
            "name": uid,
            "sidc": "10031000001211000000",
            "location": location,
            "supplies": supplies,
            "subUnits": [],
            "state": [],
        }
    )
units[0]["subUnits"] = units[1:]
routes = {
    "air-route": [locations["AIR-LIFT-1"], locations["POST-1"]],
    "sea-route": [locations["SEA-1"], locations["GROUND-1"]],
    "crossing": [locations["GROUND-1"], locations["POST-1"]],
    "detour": [locations["GROUND-1"], [-1.099, 50.7005], [-1.098, 50.7005], locations["POST-1"]],
}
write(
    "source.orbat.json",
    {
        "id": "coastal-resupply",
        "type": "ORBAT-mapper",
        "version": "3.7.0",
        "name": "Coastal resupply",
        "startTime": "2026-10-11T00:00:00Z",
        "timeZone": "UTC",
        "symbologyStandard": "2525d",
        "sides": [
            {
                "id": "blue",
                "name": "Blue",
                "standardIdentity": "3",
                "groups": [{"id": "joint-force", "name": "Joint force", "subUnits": [units[0]]}],
                "subUnits": [],
            }
        ],
        "events": [],
        "layerStack": [
            {
                "id": "routes",
                "name": "Routes",
                "kind": "overlay",
                "items": [
                    {
                        "id": rid,
                        "kind": "geometry",
                        "name": rid,
                        "geometry": {"type": "LineString", "coordinates": points},
                        "geometryMeta": {"type": "LineString"},
                        "style": {},
                    }
                    for rid, points in routes.items()
                ],
            }
        ],
        "supplyCategories": [{"name": "fuel"}, {"name": "supply"}],
    },
)


def task(tid, unit, activity, **kw):
    return {
        "id": tid,
        "version": 1,
        "domain": domains[unit],
        "unit_id": unit,
        "activity": activity,
        "duration_ms": 1000,
        **kw,
    }


pre = [
    task(
        "int-collect",
        "INT-1",
        "collect",
        preliminary=True,
        finding="coastal-access",
        duration_ms=2000,
    ),
    task(
        "air-collect",
        "AIR-REC-1",
        "collect",
        preliminary=True,
        finding="landing-zone",
        duration_ms=2000,
    ),
]
tasks = [
    task(
        "sea-delivery",
        "SEA-1",
        "unload",
        route_id="sea-route",
        supply_amount=40,
        transfer_to="GROUND-1",
    ),
    task(
        "air-delivery",
        "AIR-LIFT-1",
        "deliver",
        route_id="air-route",
        supply_amount=10,
        transfer_to="POST-1",
    ),
    task("engineering", "ENG-1", "inspect", route_id="crossing", duration_ms=2000),
    task(
        "ground-delivery",
        "GROUND-1",
        "deliver",
        route_id="crossing",
        supply_amount=40,
        transfer_to="POST-1",
        dependencies=["sea-delivery"],
    ),
    task(
        "post-accountability",
        "POST-1",
        "wait",
        duration_ms=1000,
        dependencies=["air-delivery", "ground-delivery"],
    ),
]
revised = [dict(t) for t in tasks]
revised[3] = {**revised[3], "version": 2, "route_id": "detour"}
criteria = {
    "op": "all_of",
    "args": [
        {"op": "supply_received", "ref": "POST-1", "amount": 50},
        *[{"op": "element_accounted_for", "ref": uid} for uid in locations if uid != "HQ-1"],
    ],
}
write(
    "mission.json",
    {
        "mission_id": "coastal-resupply",
        "objectives": ["Deliver fifty supply units and account for all physical elements"],
        "criteria": criteria,
        "projected_crs": "EPSG:32630",
        "area_wgs84": [-1.2, 50.6, -1.0, 50.8],
        "unit_config": {
            uid: {
                "domain": domain,
                "speed_mps": 100 if domain == "air" else 30,
                "fuel_per_second": 0 if uid == "HQ-1" else 0.1,
            }
            for uid, domain in domains.items()
        },
        "preliminary_tasks": pre,
        "plans": [
            {"version": 1, "tasks": tasks},
            {"version": 2, "tasks": revised, "trigger_report": "crossing:unavailable"},
        ],
        "actual_blocked_routes": ["crossing"],
        "accepted_information_gaps": ["Crossing requires engineering inspection"],
        "timings": {
            "communication_delay_ms": 2000,
            "position_period_ms": 5000,
            "stale_after_ms": 15000,
        },
    },
)
write(
    "run-config.json",
    {
        "scenario_start_utc": "2026-10-11T00:00:00Z",
        "mesa_seed": 42,
        "dt_ms": 1000,
        "decision_provider": {"type": "random", "seed": 0},
    },
)
