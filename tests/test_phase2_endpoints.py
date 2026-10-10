from concurrent.futures import ThreadPoolExecutor

import pytest
from datetime import datetime, timezone
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

def setup_employee_and_punch_in():
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
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783309080000,
        "status": "PRESENT"
    })

def test_punch_out_success():
    setup_employee_and_punch_in()

    # Punch out 2026-07-06 18:35:00 IST (13:05:00 UTC = 1783341900000 ms)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783341900000
    })
    assert res.status_code == 200
    data = res.json()
    assert data["punch_out"] == 1783341900000
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

def test_punch_out_duplicate():
    setup_employee_and_punch_in()
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783341900000
    })
    res_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783341900000
    })
    assert res_dup.status_code == 409

def test_concurrent_punch_outs_close_record_once():
    setup_employee_and_punch_in()
    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783341900000,
    }

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(
            lambda _: client.post("/attendance/punch-out", json=payload),
            range(8),
        ))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7
    record = db.attendance_logs.find_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
    })
    assert record["punch_out"] is not None
    assert record["work_hours"] == 9.12

def test_punch_out_before_punch_in():
    setup_employee_and_punch_in()
    # Punch out earlier than punch in (1783300000000 ms)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783300000000
    })
    assert res.status_code == 422

def test_punch_out_rejects_explicit_null_timestamp():
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": None
    })
    assert res.status_code == 422

def test_punch_out_ignores_punch_ins_after_requested_time():
    setup_employee_and_punch_in()
    next_day_punch_in = 1783309080000 + 24 * 60 * 60 * 1000
    next_day = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": next_day_punch_in
    })
    assert next_day.status_code == 201

    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783341900000
    })

    assert res.status_code == 200
    assert res.json()["date"] == "2026-07-06"
    assert res.json()["work_hours"] == 9.12
    future_record = db.attendance_logs.find_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07"
    })
    assert future_record["punch_out"] is None

def test_regularize_success_and_history():
    setup_employee_and_punch_in()

    # Punch out first
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783341900000
    })

    # HR regularizes punch-in from 09:28 IST to 09:30 IST (04:00:00 UTC = 1783309200000 ms)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": 1783309200000,
        "reason": "biometric correction",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["punch_in"] == 1783309200000
    assert len(data["history"]) == 1
    h_entry = data["history"][0]
    assert h_entry["by"] == "hr.admin"
    assert h_entry["reason"] == "biometric correction"
    assert "punch_in" in h_entry["changes"]
    assert h_entry["changes"]["punch_in"]["from"] == 1783309080000
    assert h_entry["changes"]["punch_in"]["to"] == 1783309200000

def test_regularize_to_absent_clears_times():
    setup_employee_and_punch_in()

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Marked absent by manager",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["late_minutes"] == 0
    assert data["work_hours"] is None
    assert data["half_day"] is False

def test_regularize_absent_with_punch_in_rejected():
    setup_employee_and_punch_in()
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": 1783309200000,
        "reason": "Invalid combination",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_regularize_rejects_null_punch_fields_when_setting_absent():
    setup_employee_and_punch_in()
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": None,
        "reason": "Null punch field",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_regularize_rejects_explicit_null_status():
    setup_employee_and_punch_in()
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": None,
        "punch_in": 1783309200000,
        "reason": "Correct punch in",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_regularize_no_change_rejected():
    setup_employee_and_punch_in()
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",  # Already PRESENT
        "reason": "No changes made",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_regularize_rejects_impossible_calendar_date():
    setup_employee_and_punch_in()
    res = client.patch("/attendance/EMP0001/2026-02-31", json={
        "status": "ABSENT",
        "reason": "Invalid record date",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_concurrent_regularization_does_not_overwrite_history(monkeypatch):
    setup_employee_and_punch_in()
    record = db.attendance_logs.find_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06"
    })
    collection_type = type(db.attendance_logs)
    original_update = collection_type.find_one_and_update
    injected_competing_update = False

    def update_after_competing_correction(collection, filter_query, update, **kwargs):
        nonlocal injected_competing_update
        if collection.name == "attendance_logs" and not injected_competing_update:
            injected_competing_update = True
            original_update(
                collection,
                {"_id": record["_id"]},
                {
                    "$set": {"status": "WFH"},
                    "$push": {
                        "history": {
                            "at": datetime.now(timezone.utc),
                            "by": "other.admin",
                            "reason": "parallel status correction",
                            "changes": {"status": {"from": "PRESENT", "to": "WFH"}},
                        }
                    },
                },
                return_document=True,
            )
        return original_update(collection, filter_query, update, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", update_after_competing_correction)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ON_DUTY",
        "reason": "another status correction",
        "regularized_by": "hr.admin"
    })

    assert res.status_code == 409
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    assert updated["status"] == "WFH"
    assert len(updated["history"]) == 1

def test_simultaneous_regularizations_preserve_every_successful_audit_entry():
    setup_employee_and_punch_in()
    requests = [
        {
            "status": "WFH",
            "reason": "first concurrent correction",
            "regularized_by": "hr.first",
        },
        {
            "status": "ON_DUTY",
            "reason": "second concurrent correction",
            "regularized_by": "hr.second",
        },
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(
            lambda body: client.patch("/attendance/EMP0001/2026-07-06", json=body),
            requests,
        ))

    successful = [response for response in responses if response.status_code == 200]
    conflicts = [response for response in responses if response.status_code == 409]
    assert len(successful) + len(conflicts) == 2
    assert len(successful) >= 1
    record = db.attendance_logs.find_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
    })
    assert len(record["history"]) == len(successful)
    assert {entry["by"] for entry in record["history"]} == {
        response.json()["history"][-1]["by"] for response in successful
    }
