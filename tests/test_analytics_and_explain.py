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

def insert_analytics_data(employees, logs):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    if employees:
        db.employees.insert_many(employees)
    if logs:
        db.attendance_logs.insert_many(logs)

def employee(emp_code, department="Engineering", joined_on="2026-01-01"):
    return {
        "emp_code": emp_code,
        "name": emp_code,
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
    }

def attendance(emp_code, date, status="PRESENT", **fields):
    return {
        "emp_code": emp_code,
        "date": date,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        **fields,
    }

def test_employee_monthly_analytics():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] >= 1.0

def test_attendance_response_fills_optional_seeded_fields():
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-09",
        "status": "ABSENT",
    })

    res = client.get("/attendance?emp_code=EMP0002")

    assert res.status_code == 200
    record = res.json()["items"][0]
    assert record["history"] == []
    assert record["half_day"] is False
    assert record["late_minutes"] == 0
    assert record["overtime_minutes"] == 0

    absent = client.get("/attendance?emp_code=EMP0001").json()["items"][0]
    assert absent["punch_in"] is None
    assert absent["punch_out"] is None
    assert absent["work_hours"] is None
    assert absent["history"] == []

def test_employee_monthly_unknown():
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404


def test_employee_monthly_accepts_last_supported_month():
    res = client.get("/analytics/employees/EMP0001/monthly?month=9999-12")
    summary = client.get("/analytics/departments/summary?month=9999-12")

    assert res.status_code == 200
    assert res.json()["month"] == "9999-12"
    assert summary.status_code == 200
    assert summary.json()["month"] == "9999-12"


@pytest.mark.parametrize("url", [
    "/analytics/employees/EMP0001/monthly?month=0000-01",
    "/analytics/departments/summary?month=0000-01",
    "/analytics/leaderboard/late?month=0000-01",
])
def test_analytics_rejects_year_zero_month(url):
    res = client.get(url)
    assert res.status_code == 422

def test_department_summary_analytics():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) > 0
    dept_names = [item["department"] for item in data["items"]]
    assert "Engineering" in dept_names

def test_late_leaderboard_analytics():
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)
    if data["items"]:
        assert data["items"][0]["rank"] == 1
        assert "total_late_minutes" in data["items"][0]

def test_department_trend_analytics():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]

def test_department_trend_unknown():
    res = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 404

def test_department_trend_rejects_impossible_calendar_date():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-02-31&to=2026-03-01")
    assert res.status_code == 422


def test_department_trend_accepts_last_supported_calendar_date():
    res = client.get(
        "/analytics/departments/Engineering/trend"
        "?from=9999-12-31&to=9999-12-31"
    )
    explain = client.get(
        "/admin/explain/department_trend"
        "?department=Engineering&from=9999-12-31&to=9999-12-31"
    )

    assert res.status_code == 200
    assert [item["date"] for item in res.json()["items"]] == ["9999-12-31"]
    assert explain.status_code == 200


def test_employee_monthly_counts_weekdays_from_join_date():
    insert_analytics_data(
        [employee("EMP1001", joined_on="2026-01-07")],
        [
            attendance("EMP1001", "2026-01-03", late_minutes=20, overtime_minutes=35),
            attendance("EMP1001", "2026-01-07", status="WFH", half_day=True, late_minutes=0),
            attendance("EMP1001", "2026-01-08", late_minutes=15, overtime_minutes=40),
            attendance("EMP1001", "2026-01-09", status="LEAVE"),
        ],
    )

    res = client.get("/analytics/employees/EMP1001/monthly?month=2026-01")

    assert res.status_code == 200
    assert res.json() == {
        "emp_code": "EMP1001",
        "month": "2026-01",
        "working_days": 18,
        "present_days": 1.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 35,
        "total_overtime_minutes": 75,
        "attendance_pct": 8.33,
    }

def test_department_summary_counts_employees_without_logs_and_averages_records():
    insert_analytics_data(
        [
            employee("EMP1001"),
            employee("EMP1002"),
            employee("EMP1003", joined_on="2026-02-01"),
        ],
        [
            attendance("EMP1001", "2026-01-05", status="WFH", half_day=True, work_hours=4.5),
            attendance("EMP1001", "2026-01-06", work_hours=None),
            attendance("EMP1001", "2026-01-07", status="LEAVE"),
        ],
    )

    res = client.get("/analytics/departments/summary?month=2026-01&department=Engineering")

    assert res.status_code == 200
    assert res.json() == {
        "month": "2026-01",
        "items": [{
            "department": "Engineering",
            "headcount": 2,
            "present_days": 1.5,
            "avg_work_hours": 4.5,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 1,
            "on_duty_count": 0,
        }],
    }

def test_late_leaderboard_includes_ties_at_limit_and_filters_department():
    employees = [
        employee("EMP1001"),
        employee("EMP1002"),
        employee("EMP1003"),
        employee("EMP1004", department="Sales"),
    ]
    logs = [
        attendance("EMP1001", "2026-01-05", late_minutes=50),
        attendance("EMP1002", "2026-01-05", late_minutes=30),
        attendance("EMP1003", "2026-01-06", late_minutes=30),
        attendance("EMP1004", "2026-01-05", late_minutes=100),
        attendance("EMP9999", "2026-01-05", late_minutes=200),
        attendance("EMP1001", "2026-01-07", late_minutes=10),
    ]
    insert_analytics_data(employees, logs)

    res = client.get("/analytics/leaderboard/late?month=2026-01&limit=2&department=Engineering")

    assert res.status_code == 200
    assert res.json()["items"] == [
        {
            "rank": 1,
            "emp_code": "EMP1001",
            "name": "EMP1001",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP1002",
            "name": "EMP1002",
            "department": "Engineering",
            "total_late_minutes": 30,
            "late_count": 1,
        },
        {
            "rank": 2,
            "emp_code": "EMP1003",
            "name": "EMP1003",
            "department": "Engineering",
            "total_late_minutes": 30,
            "late_count": 1,
        },
    ]

def test_department_trend_fills_days_and_averages_only_non_null_rates():
    insert_analytics_data(
        [
            employee("EMP1001", joined_on="2026-01-05"),
            employee("EMP1002", joined_on="2026-01-07"),
        ],
        [
            attendance("EMP1001", "2026-01-05"),
            attendance("EMP1001", "2026-01-07", half_day=True),
            attendance("EMP1002", "2026-01-07"),
            attendance("EMP1001", "2026-01-10"),
        ],
    )

    res = client.get("/analytics/departments/Engineering/trend?from=2026-01-05&to=2026-01-11")

    assert res.status_code == 200
    items = res.json()["items"]
    assert [item["date"] for item in items] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
        "2026-01-10",
        "2026-01-11",
    ]
    assert [item["headcount"] for item in items] == [1, 1, 2, 2, 2, 2, 2]
    assert [item["present_count"] for item in items] == [1.0, 0.0, 1.5, 0.0, 0.0, 1.0, 0.0]
    assert [item["attendance_rate"] for item in items] == [1.0, 0.0, 0.75, 0.0, 0.0, None, None]
    assert [item["moving_avg_7d"] for item in items] == [1.0, 0.5, 0.5833, 0.4375, 0.35, 0.35, 0.35]
    assert [item["is_working_day"] for item in items] == [True, True, True, True, True, False, False]

def test_department_trend_moving_average_uses_rounded_daily_rates():
    employees = [employee("EMP1001", joined_on="2026-01-05")]
    employees.extend(
        employee(f"EMP{number:04d}", joined_on="2026-01-06")
        for number in range(1002, 1008)
    )
    insert_analytics_data(
        employees,
        [
            attendance("EMP1001", "2026-01-06"),
            attendance("EMP1002", "2026-01-06", half_day=True),
        ],
    )

    res = client.get("/analytics/departments/Engineering/trend?from=2026-01-05&to=2026-01-06")

    assert res.status_code == 200
    assert [item["attendance_rate"] for item in res.json()["items"]] == [0.0, 0.2143]
    assert [item["moving_avg_7d"] for item in res.json()["items"]] == [0.0, 0.1072]

def test_explain_endpoints():
    endpoints = [
        "/admin/explain/attendance_list",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07"
    ]
    for url in endpoints:
        res = client.get(url)
        assert res.status_code == 200
        data = res.json()
        assert "explain" in data
        assert "collection" in data
