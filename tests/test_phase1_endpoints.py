from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    from app.main import create_indexes
    create_indexes()
    yield

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_employee_success():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert "_id" not in data

def test_create_employee_duplicate():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201
    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409

def test_concurrent_employee_creates_enforce_unique_code():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(
            lambda _: client.post("/employees", json=payload),
            range(8),
        ))

    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1

def test_create_employee_invalid_shift():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",  # Same start & end
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422

def test_create_employee_invalid_calendar_date():
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-02-31"
    })
    assert res.status_code == 422

def test_list_employees_pagination_and_sorting():
    for i in (3, 1, 2):
        client.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering" if i != 3 else "HR",
            "joined_on": "2026-01-05"
        })

    # Page 1, size 2
    res = client.get("/employees?page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0002"

    # Filter department HR
    res_hr = client.get("/employees?department=HR")
    assert res_hr.status_code == 200
    data_hr = res_hr.json()
    assert data_hr["total"] == 1
    assert data_hr["items"][0]["emp_code"] == "EMP0003"

def test_punch_in_success_and_duplicate():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in 2026-07-06 09:28:00 IST (03:58:00 UTC = 1783309080000 ms)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783309080000,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_in"] == 1783309080000

    # Duplicate punch in for same date
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783309080000,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

def test_concurrent_punch_ins_create_exactly_one_record():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })
    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783309080000,
    }

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(
            lambda _: client.post("/attendance/punch-in", json=payload),
            range(8),
        ))

    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(409) == 7
    assert db.attendance_logs.count_documents({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
    }) == 1


def test_punch_in_unknown_employee():
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

@pytest.mark.parametrize("punched_at", ["1783309080000", 1783309080000.0, 1783309080000.5, None])
def test_punch_in_rejects_non_integer_epoch_milliseconds(punched_at):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": punched_at
    })

    assert res.status_code == 422

def test_list_attendance_date_validation():
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422

def test_list_attendance_rejects_impossible_calendar_date():
    res = client.get("/attendance?date_from=2026-02-31")
    assert res.status_code == 422
