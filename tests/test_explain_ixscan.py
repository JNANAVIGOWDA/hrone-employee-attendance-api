import pytest
from fastapi.testclient import TestClient
from app.main import app, db
from bson import json_util
import pathlib

client = TestClient(app)

@pytest.fixture(autouse=True)
def seed_sample_data():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    from app.main import create_indexes
    create_indexes()

    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        if docs:
            db[name].insert_many(docs)
    yield

def test_explain_index_scans():
    endpoints = [
        ("/admin/explain/attendance_list?emp_code=EMP0001", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07&department=Engineering", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07&department=Engineering", "attendance_logs"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07", "employees"),
    ]

    for url, coll in endpoints:
        res = client.get(url)
        assert res.status_code == 200
        data = res.json()
        assert data["collection"] == coll
        explain = data["explain"]

        # Convert explain to string to check for IXSCAN
        explain_str = str(explain)
        assert "IXSCAN" in explain_str, f"Explain output for {url} should use IXSCAN"
        assert "COLLSCAN" not in explain_str, f"Explain output for {url} should not use COLLSCAN"

def test_explain_attendance_rejects_invalid_status():
    res = client.get("/admin/explain/attendance_list?status=UNKNOWN")
    assert res.status_code == 422

def test_explain_trend_rejects_ranges_over_92_days():
    res = client.get(
        "/admin/explain/department_trend"
        "?department=Engineering&from=2026-01-01&to=2026-04-03"
    )
    assert res.status_code == 422

def test_explain_rejects_year_zero_month():
    res = client.get(
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=0000-01"
    )
    assert res.status_code == 422
