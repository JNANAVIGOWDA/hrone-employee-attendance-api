"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000
EpochMillisInput = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

def create_indexes():
    """Idempotently create required indexes at startup."""
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("status", 1), ("date", 1)])

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ERROR_RESPONSE = {
    "description": "Error",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["detail"],
                "properties": {"detail": {"type": "string"}},
            }
        }
    },
}



# --------------------------------------------------------------------------- #
# Helpers & Time Conversion Utilities
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime microsecond component to whole seconds."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime (truncated to seconds)."""
    if not isinstance(ms, int) or isinstance(ms, bool) or ms < MIN_EPOCH_MS or ms > MAX_EPOCH_MS:
        raise ValueError(f"Invalid epoch milliseconds timestamp: {ms}")
    dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    return truncate_to_seconds(dt)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime has tzinfo=UTC (handling naive PyMongo datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime object to integer epoch milliseconds."""
    if dt is None:
        return None
    dt = ensure_utc(dt)
    return int(dt.timestamp() * 1000)



def format_bson_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB document into clean API response format (stripping _id and converting datetimes)."""
    if not doc:
        return doc
    doc.pop("_id", None)
    if "date" in doc and "status" in doc:
        doc.setdefault("punch_in", None)
        doc.setdefault("punch_out", None)
        doc.setdefault("work_hours", None)
        doc.setdefault("history", [])
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        doc.setdefault("half_day", False)
    for field in ("created_at", "punch_in", "punch_out"):
        if field in doc and isinstance(doc[field], datetime):
            doc[field] = datetime_to_epoch_ms(doc[field])
    if "history" in doc and isinstance(doc["history"], list):
        formatted_history = []
        for entry in doc["history"]:
            h_entry = dict(entry)
            if "at" in h_entry and isinstance(h_entry["at"], datetime):
                h_entry["at"] = datetime_to_epoch_ms(h_entry["at"])
            if "changes" in h_entry and isinstance(h_entry["changes"], dict):
                changes = {}
                for k, v in h_entry["changes"].items():
                    c_item = dict(v)
                    if k in ("punch_in", "punch_out"):
                        if isinstance(c_item.get("from"), datetime):
                            c_item["from"] = datetime_to_epoch_ms(c_item["from"])
                        if isinstance(c_item.get("to"), datetime):
                            c_item["to"] = datetime_to_epoch_ms(c_item["to"])
                    changes[k] = c_item
                h_entry["changes"] = changes
            formatted_history.append(h_entry)
        doc["history"] = formatted_history
    return doc


def parse_time_str(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute)."""
    h, m = map(int, time_str.split(":"))
    return h, m


def is_valid_calendar_date(value: str) -> bool:
    """Return whether value is a real date in YYYY-MM-DD form."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == value


def require_calendar_date(value: str, field_name: str) -> None:
    if not is_valid_calendar_date(value):
        raise HTTPException(422, f"{field_name} must be a valid calendar date")


def is_valid_calendar_month(value: str) -> bool:
    year, month = map(int, value.split("-"))
    return 1 <= year <= 9999 and 1 <= month <= 12


def require_calendar_month(value: str, field_name: str = "month") -> None:
    if not is_valid_calendar_month(value):
        raise HTTPException(422, f"{field_name} must be a valid calendar month")


def densify_exclusive_upper_bound(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 59, 59, 999000, tzinfo=UTC)


def determine_attendance_date(punch_in_ist: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    Determine the IST attendance date string (YYYY-MM-DD) per Rule R1.
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end (IST)
    belongs to the shift that started the previous day.
    """
    start_h, start_m = parse_time_str(shift_start_str)
    end_h, end_m = parse_time_str(shift_end_str)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    punch_time = punch_in_ist.time()
    end_time = (datetime.min + timedelta(hours=end_h, minutes=end_m)).time()

    if is_overnight and punch_time < end_time:
        attendance_dt = punch_in_ist.date() - timedelta(days=1)
    else:
        attendance_dt = punch_in_ist.date()

    return attendance_dt.isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start_str: str, shift_end_str: str, date_str: str) -> int:
    """
    Compute late minutes per Rule R2.
    Grace period is 10 minutes. Late only if punch-in is strictly > 10:00 after shift_start.
    When late, late_minutes = floor(minutes since shift_start).
    """
    start_h, start_m = parse_time_str(shift_start_str)
    att_date = datetime.fromisoformat(date_str).date()
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, tzinfo=IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:  # strictly more than 10 minutes (600 seconds)
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Compute work hours per Rule R4 (rounded 2 decimals half-up)."""
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(seconds) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Determine half_day status per Rule R5."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(punch_out_ist: datetime, shift_start_str: str, shift_end_str: str, date_str: str) -> int:
    """
    Compute overtime minutes per Rule R3.
    Whole minutes (floor) between shift_end and punch_out, counted only if >= 30; otherwise 0.
    """
    start_h, start_m = parse_time_str(shift_start_str)
    end_h, end_m = parse_time_str(shift_end_str)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    att_date = datetime.fromisoformat(date_str).date()
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, tzinfo=IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Validation & Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    )

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not is_valid_calendar_date(v):
            raise ValueError("joined_on must be a valid calendar date")
        return v


def reject_explicit_nulls(values: Any, field_names: tuple[str, ...]) -> Any:
    if isinstance(values, dict):
        null_fields = [name for name in field_names if name in values and values[name] is None]
        if null_fields:
            raise ValueError(f"{', '.join(null_fields)} cannot be null")
    return values


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisInput] = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def validate_optional_timestamp(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("punched_at",))

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
                raise ValueError("Invalid punched_at timestamp")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisInput] = None

    @model_validator(mode="before")
    @classmethod
    def validate_optional_timestamp(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("punched_at",))

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
                raise ValueError("Invalid punched_at timestamp")
        return v


class RegularizeIn(BaseModel):
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[EpochMillisInput] = None
    punch_out: Optional[EpochMillisInput] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def validate_optional_changes(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("status", "punch_in", "punch_out"))

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
                raise ValueError("Invalid epoch timestamp")
        return v


class HealthResponse(BaseModel):
    status: Literal["ok"]


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str = Field(json_schema_extra={"format": "date"})
    created_at: int


class EmployeePageResponse(BaseModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int


class HistoryEntryResponse(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceResponse(BaseModel):
    emp_code: str
    date: str = Field(json_schema_extra={"format": "date"})
    status: AttendanceStatus
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryResponse]


class AttendancePageResponse(BaseModel):
    items: list[AttendanceResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItemResponse(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentSummaryItemResponse]


class LeaderboardItemResponse(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItemResponse]


class TrendItemResponse(BaseModel):
    date: str = Field(json_schema_extra={"format": "date"})
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendResponse(BaseModel):
    department: str
    items: list[TrendItemResponse]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get(
    "/health",
    operation_id="health",
    response_model=HealthResponse,
    responses={503: ERROR_RESPONSE},
)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post(
    "/employees",
    status_code=201,
    operation_id="createEmployee",
    response_model=EmployeeResponse,
    responses={409: ERROR_RESPONSE},
)
def create_employee(body: EmployeeIn):
    now_utc = truncate_to_seconds(datetime.now(tz=UTC))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_bson_doc(doc)


@app.get("/employees", operation_id="listEmployees", response_model=EmployeePageResponse)
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for item in items:
        format_bson_doc(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=201,
    operation_id="punchIn",
    response_model=AttendanceResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        try:
            punch_in_dt = epoch_ms_to_datetime(body.punched_at)
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(tz=UTC))

    punch_in_ist = punch_in_dt.astimezone(IST)
    attendance_date = determine_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_ist, emp["shift_start"], emp["shift_end"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_bson_doc(doc)


@app.post(
    "/attendance/punch-out",
    operation_id="punchOut",
    response_model=AttendanceResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        try:
            punch_out_dt = epoch_ms_to_datetime(body.punched_at)
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(tz=UTC))

    # Find the employee's most recent record with a punch-in
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
        },
        sort=[("punch_in", -1)]
    )

    if not rec:
        latest_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", -1)]
        )
        if latest_rec and punch_out_dt <= ensure_utc(latest_rec["punch_in"]):
            raise HTTPException(422, "punch_out must be strictly after punch_in")
        raise HTTPException(404, "No matching punch-in record found for punch-out")

    punch_in_dt = ensure_utc(rec["punch_in"])

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    if punch_out_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    punch_out_ist = punch_out_dt.astimezone(IST)
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(punch_out_ist, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day_val = compute_half_day(work_hrs)

    # Atomic update to avoid punch-out race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_mins,
                "half_day": half_day_val,
            }
        },
        return_document=True
    )

    if not updated_doc:
        raise HTTPException(409, "Record was punched out by a concurrent request")

    return format_bson_doc(updated_doc)


@app.get("/attendance", operation_id="listAttendance", response_model=AttendancePageResponse)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    date_to: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    status: Optional[AttendanceStatus] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        require_calendar_date(date_from, "date_from")
    if date_to:
        require_calendar_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    for doc in docs:
        format_bson_doc(doc)

    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    operation_id="regularizeAttendance",
    response_model=AttendanceResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    body: RegularizeIn = ...,
):
    require_calendar_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found for this employee and date")

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    else: # Presence status
        if body.punch_in is not None:
            try:
                new_punch_in = epoch_ms_to_datetime(body.punch_in)
            except ValueError as e:
                raise HTTPException(422, str(e))
        else:
            new_punch_in = ensure_utc(rec.get("punch_in"))

        if new_punch_in is None:
            raise HTTPException(422, "A presence status requires a punch_in timestamp")

        # Verify punch_in remains on attendance date per R1
        punch_in_ist = new_punch_in.astimezone(IST)
        calc_date = determine_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(422, "punch_in does not belong to this attendance date")

        if body.punch_out is not None:
            try:
                new_punch_out = epoch_ms_to_datetime(body.punch_out)
            except ValueError as e:
                raise HTTPException(422, str(e))
        else:
            new_punch_out = ensure_utc(rec.get("punch_out"))

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late_mins = 0
        new_work_hrs = None
        new_overtime_mins = 0
        new_half_day = False
    else:
        new_late_mins = compute_late_minutes(new_punch_in.astimezone(IST), emp["shift_start"], emp["shift_end"], date)
        if new_punch_out is not None:
            new_work_hrs = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_mins = compute_overtime(new_punch_out.astimezone(IST), emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hrs)
        else:
            new_work_hrs = None
            new_overtime_mins = 0
            new_half_day = False

    # Compare changes
    target_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hrs,
        "late_minutes": new_late_mins,
        "overtime_minutes": new_overtime_mins,
        "half_day": new_half_day,
    }

    changes_dict = {}
    for f, new_val in target_fields.items():
        old_val = ensure_utc(rec.get(f)) if f in ("punch_in", "punch_out") else rec.get(f)
        if old_val != new_val:
            changes_dict[f] = {"from": old_val, "to": new_val}

    if not changes_dict:
        raise HTTPException(422, "Request modifies no fields")

    now_utc = truncate_to_seconds(datetime.now(tz=UTC))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_dict,
    }

    update_filter = {"_id": rec["_id"]}
    for field in target_fields:
        update_filter[field] = rec[field] if field in rec else {"$exists": False}
    update_filter["history"] = rec["history"] if "history" in rec else {"$exists": False}

    updated_doc = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": target_fields,
            "$push": {"history": history_entry},
        },
        return_document=True
    )

    if not updated_doc:
        raise HTTPException(409, "Attendance record changed during regularization")

    return format_bson_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    """Helper for half-up rounding of floats."""
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def round_ratio_half_up(numerator: str, denominator: str, decimals: int) -> Dict[str, Any]:
    """Build a MongoDB expression that rounds a nonnegative ratio half-up."""
    scale = 10 ** decimals
    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {"$multiply": [{"$divide": [numerator, denominator]}, scale]},
                        0.5,
                    ]
                }
            },
            scale,
        ]
    }


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    operation_id="employeeMonthly",
    response_model=EmployeeMonthlyResponse,
    responses={404: ERROR_RESPONSE},
)
def get_employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    require_calendar_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    month_start = datetime(year, m, 1).date()
    month_end = date(year, m, calendar.monthrange(year, m)[1])

    joined_on = datetime.fromisoformat(emp["joined_on"]).date()
    effective_start = max(month_start, joined_on)

    # Calculate working days (Mon-Fri) per R7
    working_days = 0
    if effective_start <= month_end:
        curr = effective_start
        while curr <= month_end:
            if curr.weekday() < 5:  # Mon-Fri
                working_days += 1
            if curr == month_end:
                break
            curr += timedelta(days=1)

    # Aggregation for employee's monthly records
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    # Mon-Fri check using $dayOfWeek (1=Sun, 7=Sat, 2-6=Mon-Fri)
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                }
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = float(stats.get("present_days", 0.0))
        leave_days = int(stats.get("leave_days", 0))
        late_count = int(stats.get("late_count", 0))
        total_late = int(stats.get("total_late_minutes", 0))
        total_ot = int(stats.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct_raw = (present_days / working_days) * 100
        attendance_pct = round_half_up(pct_raw, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get(
    "/analytics/departments/summary",
    operation_id="departmentSummary",
    response_model=DepartmentSummaryResponse,
)
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    require_calendar_month(month)
    year, m = map(int, month.split("-"))
    month_end_str = date(year, m, calendar.monthrange(year, m)[1]).isoformat()

    emp_match = {"joined_on": {"$lte": month_end_str}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        headcount = len(r["employees_set"])
        if headcount == 0:
            continue

        wh_list = r.get("work_hours_list", [])
        if wh_list:
            avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2)
        else:
            avg_wh = None

        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    operation_id="lateLeaderboard",
    response_model=LeaderboardResponse,
)
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    require_calendar_month(month)
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "partitionBy": None,
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["_id"],
            "name": r["emp"]["name"],
            "department": r["emp"]["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    operation_id="departmentTrend",
    response_model=TrendResponse,
    responses={404: ERROR_RESPONSE},
)
def get_department_trend(
    department: str = Path(...),
    from_date: str = Query(
        ...,
        alias="from",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    to_date: str = Query(
        ...,
        alias="to",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
):
    require_calendar_date(from_date, "from")
    require_calendar_date(to_date, "to")
    if from_date > to_date:
        raise HTTPException(422, "'from' date must be <= 'to' date")

    d_from = datetime.fromisoformat(from_date).date()
    d_to = datetime.fromisoformat(to_date).date()
    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    d_from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    d_to_next_dt = densify_exclusive_upper_bound(d_to)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date": d_from_dt}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from_dt, d_to_next_dt]
                }
            }
        },
        {
            "$project": {
                "date_str": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date"}
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc_res"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d_str": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d_str"]}
                                ]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}}
                ],
                "as": "day_logs"
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_res.count", 0]}, 0]},
                "day_logs": "$day_logs"
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": 1,
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        round_ratio_half_up("$present_count", "$headcount", 4),
                        "$$REMOVE"
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": None,
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_raw")

        rate_fmt = round_half_up(rate, 4) if rate is not None else None
        m_avg_fmt = round_half_up(m_avg, 4) if m_avg is not None else None

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate_fmt,
            "moving_avg_7d": m_avg_fmt,
        })

    return {"department": department, "items": items}




# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get(
    "/admin/explain/{endpoint}",
    operation_id="explainEndpoint",
    response_model=ExplainResponse,
)
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    date_to: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    status: Optional[AttendanceStatus] = Query(None),
    from_date: Optional[str] = Query(
        None,
        alias="from",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    to_date: Optional[str] = Query(
        None,
        alias="to",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        json_schema_extra={"format": "date"},
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if month:
        require_calendar_month(month)

    if endpoint == "attendance_list":
        if date_from:
            require_calendar_date(date_from, "date_from")
        if date_to:
            require_calendar_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        collection_name = "attendance_logs"
        cmd = {
            "find": collection_name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_doc = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        collection_name = "attendance_logs"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
            {
                "$group": {
                    "_id": None,
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            }
        ]
        cmd = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
        explain_doc = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, m = map(int, month.split("-"))
        month_end_str = date(year, m, calendar.monthrange(year, m)[1]).isoformat()

        emp_match = {"joined_on": {"$lte": month_end_str}}
        if department:
            emp_match["department"] = department

        collection_name = "employees"
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "employees_set": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                                    ]
                                },
                                {"$cond": ["$logs.half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "work_hours_list": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]}
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE"
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
                }
            },
            {"$sort": {"_id": 1}}
        ]
        cmd = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
        explain_doc = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        collection_name = "attendance_logs"
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {
                "$setWindowFields": {
                    "partitionBy": None,
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    }
                }
            },
            {"$match": {"rank": {"$lte": limit or 10}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}}
        ])
        cmd = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
        explain_doc = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        require_calendar_date(from_date, "from")
        require_calendar_date(to_date, "to")
        if from_date > to_date:
            raise HTTPException(422, "'from' date must be <= 'to' date")

        d_from = datetime.fromisoformat(from_date).date()
        d_to = datetime.fromisoformat(to_date).date()
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        collection_name = "employees"

        d_from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
        d_to_next_dt = densify_exclusive_upper_bound(d_to)

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "date": d_from_dt}},

            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [d_from_dt, d_to_next_dt]
                    }
                }
            },

            {
                "$project": {
                    "date_str": {
                        "$dateToString": {"format": "%Y-%m-%d", "date": "$date"}
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"d_str": "$date_str"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$d_str"]}
                                    ]
                                }
                            }
                        },
                        {"$count": "count"}
                    ],
                    "as": "hc_res"
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"d_str": "$date_str"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$d_str"]}
                                    ]
                                }
                            }
                        },
                        {
                            "$lookup": {
                                "from": "employees",
                                "localField": "emp_code",
                                "foreignField": "emp_code",
                                "as": "emp"
                            }
                        },
                        {"$unwind": "$emp"},
                        {"$match": {"emp.department": department}}
                    ],
                    "as": "day_logs"
                }
            },
            {
                "$project": {
                    "date": "$date_str",
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_res.count", 0]}, 0]},
                    "day_logs": "$day_logs"
                }
            },
            {
                "$project": {
                    "date": 1,
                    "headcount": 1,
                    "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": "$day_logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            }
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$day_logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                            }
                        }
                    }
                }
            },
            {
                "$project": {
                    "date": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$gt": ["$headcount", 0]}
                                ]
                            },
                            round_ratio_half_up("$present_count", "$headcount", 4),
                            "$$REMOVE"
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "partitionBy": None,
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_raw": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, 0]
                            }
                        }
                    }
                }
            },
            {"$sort": {"date": 1}}
        ]
        cmd = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
        explain_doc = db.command("explain", cmd, verbosity="executionStats")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_doc,
    }
