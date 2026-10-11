from fastapi.testclient import TestClient
from c2sim.api.app import create_app


def test_api_create_choose_retry_control_export_restore(tmp_path, inputs):
    raw, spec, config = inputs
    app = create_app(tmp_path / "api.sqlite", tmp_path / "exports", continuous=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            files={"scenario": ("scenario.json", raw, "application/json")},
            data={"config": config.model_dump_json(), "mission": spec.model_dump_json()},
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        base = f"/api/v1/runs/{run_id}"
        pending = client.get(base + "/decisions/current").json()
        body = {"decision_id": pending["decision_id"], "expected_commit_seq": pending["commit_seq"]}
        first = client.post(base + "/decisions/choose", json=body)
        assert first.status_code == 200, first.text
        assert client.post(base + "/decisions/choose", json=body).json() == first.json()
        assert (
            client.post(
                base + "/decisions/choose", json={**body, "option_id": "forbidden"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                base + "/controls",
                json={"command_id": "stale", "action": "start", "expected_control_revision": 20},
            ).status_code
            == 409
        )
        exports = client.post(base + "/exports").json()
        native = client.get(exports["scenario.orbat.json"])
        assert native.status_code == 200
        assert native.json()["type"] == "ORBAT-mapper"
        assert client.get(exports["source.orbat.json"]).content == raw
        restored = client.post(base + "/restores", json={"checkpoint_seq": 0})
        assert restored.status_code == 201
        assert restored.json()["run_id"] != run_id
        assert client.get(base + "/checkpoints/0").json()["time_ms"] == 0
        assert client.get("/api/v1/runs/unknown").status_code == 404
