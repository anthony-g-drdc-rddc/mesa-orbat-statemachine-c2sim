from pathlib import Path
import pytest
from c2sim.contracts.scenario import RunCreateInput, ScenarioSpec
from c2sim.runtime.coordinator import new_state
from c2sim.persistence.repository import Repository

FIXTURE = Path(__file__).resolve().parents[1] / "scenarios/coastal-resupply"


@pytest.fixture
def inputs():
    return (
        (FIXTURE / "source.orbat.json").read_bytes(),
        ScenarioSpec.model_validate_json((FIXTURE / "mission.json").read_text()),
        RunCreateInput.model_validate_json((FIXTURE / "run-config.json").read_text()),
    )


@pytest.fixture
def state(inputs):
    return new_state(*inputs, "test-system", run_id="test-run")


@pytest.fixture
def repository(tmp_path):
    repo = Repository(tmp_path / "test.sqlite")
    yield repo
    repo.close()
