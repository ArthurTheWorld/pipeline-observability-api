import pytest
from fastapi.testclient import TestClient
from helpers import load, now_after

from obs_api.config import ApiKey, Settings
from obs_api.main import create_app
from obs_api.security import hash_key

KEYS = {"viewer": "obs_viewer_key_for_tests", "admin": "obs_admin_key_for_tests", "viewer2": "obs_second_viewer_key"}
VIEWER = {"X-API-Key": KEYS["viewer"]}
ADMIN = {"X-API-Key": KEYS["admin"]}


def make_settings(tmp_path, **overrides):
    keys = {
        "grafana": ApiKey("grafana", hash_key(KEYS["viewer"]), "viewer"),
        "airflow": ApiKey("airflow", hash_key(KEYS["admin"]), "admin"),
        "analyst": ApiKey("analyst", hash_key(KEYS["viewer2"]), "viewer"),
    }
    base = dict(db_path=str(tmp_path / "obs.db"), api_keys=keys, rate_limit_per_minute=10_000)
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def fixed_now():
    return now_after(load("run_results.json"), hours=1)


@pytest.fixture
def make_client(tmp_path, fixed_now):
    def factory(**overrides):
        clock_value = overrides.pop("now", fixed_now)
        app = create_app(make_settings(tmp_path, **overrides), clock=lambda: clock_value)
        return TestClient(app)

    return factory


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def loaded(client):
    """Cliente com as fixtures reais (run_results + sources) já ingeridas."""
    assert client.post("/v1/ingest/run-results", json=load("run_results.json"), headers=ADMIN).status_code == 200
    assert client.post("/v1/ingest/sources", json=load("sources.json"), headers=ADMIN).status_code == 200
    return client
