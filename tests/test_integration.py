import pytest
from fastapi.testclient import TestClient

from app import cache, db
from app.main import app

client = TestClient(app)


def _services_available():
    try:
        db.query("SELECT 1")
        cache.client().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="PostgreSQL or Redis service is not available (run with db and redis active)",
)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["postgres"] is True
    assert data["redis"] is True


def test_dashboard_ui():
    res = client.get("/")
    assert res.status_code == 200
    assert "Gym Progress Tracker" in res.text
    assert "Log New Set" in res.text
    assert "Recent Sets" in res.text


def test_exercises():
    res = client.get("/exercises")
    assert res.status_code == 200
    data = res.json()
    assert "exercises" in data
    names = [e["name"] for e in data["exercises"]]
    assert "Bench Press" in names
    assert "Back Squat" in names


def test_crud_sets_lifecycle():
    # 1. CREATE set
    payload = {
        "user": "integration_user",
        "exercise": "Bench Press",
        "weight": 80.0,
        "reps": 5,
        "notes": "Testing integration CRUD",
    }
    create_res = client.post("/sets", json=payload)
    assert create_res.status_code == 201
    created = create_res.json()
    assert created["user"] == "integration_user"
    assert created["weight"] == 80.0
    assert created["reps"] == 5
    set_id = created["id"]

    # 2. READ set
    get_res = client.get(f"/sets/{set_id}")
    assert get_res.status_code == 200
    read_data = get_res.json()
    assert read_data["id"] == set_id
    assert read_data["user"] == "integration_user"
    assert read_data["weight"] == 80.0

    # 3. UPDATE set
    update_res = client.put(
        f"/sets/{set_id}",
        json={"weight": 82.5, "reps": 4, "notes": "Updated set"},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["id"] == set_id
    assert updated["weight"] == 82.5
    assert updated["reps"] == 4
    assert updated["notes"] == "Updated set"

    # Verify update in GET
    get_res2 = client.get(f"/sets/{set_id}")
    assert get_res2.json()["weight"] == 82.5

    # 4. DELETE set
    del_res = client.delete(f"/sets/{set_id}")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "deleted"

    # Verify deletion
    get_res3 = client.get(f"/sets/{set_id}")
    assert get_res3.status_code == 404


def test_users_pbs_and_caching():
    # 'asha' is seeded with lifts in 002_seed.sql
    res = client.get("/users/asha/pbs?formula=epley")
    assert res.status_code == 200
    data = res.json()
    assert data["user"] == "asha"
    assert len(data["personal_bests"]) > 0

    # Second call hits Redis cache
    res2 = client.get("/users/asha/pbs?formula=epley")
    assert res2.status_code == 200
    assert res2.json()["user"] == "asha"


def test_progress_and_plateau():
    res = client.get("/users/asha/exercises/Bench%20Press/progress?formula=epley")
    assert res.status_code == 200
    data = res.json()
    assert data["user"] == "asha"
    assert "sessions" in data
    assert "plateau" in data


def test_warmup():
    res = client.get("/users/asha/exercises/Back%20Squat/warmup")
    assert res.status_code == 200
    data = res.json()
    assert data["user"] == "asha"
    assert "ladder" in data
    assert len(data["ladder"]) >= 2


def test_compare_endpoint():
    res = client.post(
        "/compare",
        json={
            "a": {"weight": 100, "reps": 5},
            "b": {"weight": 110, "reps": 3},
            "formula": "epley",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["better"] == "b"
    assert data["formulas_agree"] is True
