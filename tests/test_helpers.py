import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import app.main as main
from app.main import db
from app.main import (
    IST,
    truncate_to_seconds,
    epoch_ms_to_datetime,
    datetime_to_epoch_ms,
    determine_attendance_date,
    is_valid_calendar_month,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
)

def test_truncate_to_seconds():
    dt = datetime(2026, 7, 6, 3, 58, 12, 900000, tzinfo=timezone.utc)
    truncated = truncate_to_seconds(dt)
    assert truncated == datetime(2026, 7, 6, 3, 58, 12, tzinfo=timezone.utc)

def test_epoch_ms_conversions():
    # 2026-07-06 09:28:00 IST = 2026-07-06 03:58:00 UTC = 1783312680000 ms
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ms = datetime_to_epoch_ms(dt_utc)
    res_dt = epoch_ms_to_datetime(ms)
    assert res_dt == dt_utc

def test_epoch_ms_invalid():
    with pytest.raises(ValueError):
        epoch_ms_to_datetime(100)  # less than 100000000000

def test_calendar_month_validation_rejects_year_zero():
    assert is_valid_calendar_month("2026-07")
    assert not is_valid_calendar_month("0000-01")

def test_determine_attendance_date_normal_shift():
    # 09:30 - 18:30 shift
    # Punch in at 09:28 IST on 2026-07-06 -> 2026-07-06
    punch_in_ist = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    date_str = determine_attendance_date(punch_in_ist, "09:30", "18:30")
    assert date_str == "2026-07-06"

def test_determine_attendance_date_overnight_shift():
    # 22:00 - 06:00 shift
    # Punch in at 21:55 IST on 2026-07-06 -> 2026-07-06
    punch_1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    assert determine_attendance_date(punch_1, "22:00", "06:00") == "2026-07-06"

    # Punch in at 01:15 IST on 2026-07-07 -> 2026-07-06 (belongs to shift starting previous day)
    punch_2 = datetime(2026, 7, 7, 1, 15, 0, tzinfo=IST)
    assert determine_attendance_date(punch_2, "22:00", "06:00") == "2026-07-06"

    # Punch in at 06:00 IST on 2026-07-07 -> 2026-07-07 (not earlier than shift_end 06:00)
    punch_3 = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert determine_attendance_date(punch_3, "22:00", "06:00") == "2026-07-07"

def test_compute_late_minutes():
    # Shift 09:30
    # 09:40:00 -> 0
    p1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(p1, "09:30", "18:30", "2026-07-06") == 0

    # 09:40:01 -> 10
    p2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(p2, "09:30", "18:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    p3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(p3, "09:30", "18:30", "2026-07-06") == 45

def test_compute_work_hours():
    p_in = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    p_out = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    # 9h 7m = 32820 seconds / 3600 = 9.11666... -> half-up 9.12
    assert compute_work_hours(p_in, p_out) == 9.12

def test_compute_overtime():
    # Shift 09:30 - 18:30
    # Out at 18:35 -> 5 min overtime (< 30) -> 0
    p_out1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(p_out1, "09:30", "18:30", "2026-07-06") == 0

    # Out at 19:10 -> 40 min overtime (>= 30) -> 40
    p_out2 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(p_out2, "09:30", "18:30", "2026-07-06") == 40

def test_compute_overtime_overnight():
    # Shift 22:00 - 06:00, date 2026-07-06
    # Punch out at 06:40 IST on 2026-07-07 -> 40 min overtime
    p_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(p_out, "22:00", "06:00", "2026-07-06") == 40

def test_compute_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.50) is False

def test_create_indexes_propagates_index_creation_failure(monkeypatch):
    class FailingCollection:
        def create_index(self, *args, **kwargs):
            raise RuntimeError("index creation failed")

    class FakeDatabase:
        employees = FailingCollection()
        attendance_logs = FailingCollection()

    monkeypatch.setattr(main, "db", FakeDatabase())

    with pytest.raises(RuntimeError, match="index creation failed"):
        main.create_indexes()

def test_app_startup_creates_indexes(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "create_indexes", lambda: calls.append("created"))

    with TestClient(main.app):
        pass

    assert calls == ["created"]

def test_employee_indexes_include_join_date_for_monthly_headcount():
    main.create_indexes()
    index_info = db.employees.index_information()
    assert any(
        index.get("key") == [("joined_on", 1)]
        for index in index_info.values()
    )
