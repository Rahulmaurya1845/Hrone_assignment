"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# tz_aware=True: datetimes read from MongoDB come back as UTC-aware, not naive.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Valid range for epoch milliseconds (see EpochMillis in openapi.yaml)
MS_MIN = 100000000000
MS_MAX = 4102444800000


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Indexes are created at startup. create_index is idempotent.
    db.employees.create_index("emp_code", unique=True)
    # one record per (emp_code, date): this is what makes double punch-in safe
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # supports GET /attendance: filter by date range, sort by date desc then emp_code asc
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # supports punch-out: latest record of an employee with punch_in <= a given time
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: datetime) -> int:
    """Aware datetime -> epoch milliseconds. Integer maths, so no float rounding."""
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime."""
    return EPOCH + timedelta(milliseconds=ms)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to."""
    ist = punch_in.astimezone(IST)
    overnight = shift_end <= shift_start  # "HH:MM" strings compare correctly
    if overnight and ist.strftime("%H:%M") < shift_end:
        ist = ist - timedelta(days=1)  # belongs to the shift that started yesterday
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; floor minutes since start."""
    h, m = map(int, shift_start.split(":"))
    d = date.fromisoformat(date_str)
    start = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    seconds = (punch_in - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def round_half_up(x: Decimal, places: int) -> Decimal:
    """R4/R8: classic rounding (4.495 -> 4.50). Python's round() does not do this."""
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes after shift end, counted only when >= 30. Overnight shifts end next day."""
    h, m = map(int, shift_end.split(":"))
    d = date.fromisoformat(date_str)
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def attendance_out(doc: dict) -> dict:
    """Turn a MongoDB attendance document into the API shape (no _id, times in ms)."""
    history = []
    for h in doc.get("history", []):
        changes = {}
        for field, ch in h.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                ch = {k: (to_ms(v) if isinstance(v, datetime) else v) for k, v in ch.items()}
            changes[field] = ch
        history.append(
            {"at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def check_date(value: Optional[str]) -> Optional[str]:
    """Reject impossible dates such as 2026-02-31 with a 422."""
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"invalid date: {value}")
    return value


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# strict=True: floats and strings are rejected; seconds-looking numbers fail ge=MS_MIN
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_real_date(cls, v):
        date.fromisoformat(v)  # raises an error for things like 2026-02-31
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = datetime.now(timezone.utc)
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)  # MongoDB keeps milliseconds
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)  # the unique index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for it in items:
        it["created_at"] = to_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)  # R1: truncate to whole seconds

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)  # R1: whole seconds

    # most recent record whose punch_in is at or before the punch-out time
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    work_hours = compute_work_hours(pin, ts)
    fields = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"]),
        "half_day": work_hours < 4.5,  # R5: rounded work_hours < 4.50
    }
    # only update if still open: with two simultaneous requests, one gets matched_count 0
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": fields})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(fields)
    return attendance_out(rec)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = check_date(date_from)
    date_to = check_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])  # newest first, then emp_code A-Z
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [attendance_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #