"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
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
    # supports the department summary: employees who joined on or before a date, per department
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
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


def derive_fields(status: str, pin: Optional[datetime], pout: Optional[datetime], emp: dict, d: str) -> dict:
    """The four derived fields, recomputed from the final status and punch times (R2-R5)."""
    if status in ("ABSENT", "LEAVE") or pin is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, emp["shift_start"], d)
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, emp["shift_start"], emp["shift_end"], d),
        "half_day": wh < 4.5,
    }


PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str) -> tuple:
    """'2026-07' -> (date(2026,7,1), date(2026,7,31)). 422 for impossible years like 0000."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    except ValueError:
        raise HTTPException(422, f"invalid month: {month}")


def count_weekdays(start: date, end: date) -> int:
    """Number of Mon-Fri days from start to end, both included (R7: no holiday calendar)."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=i)).weekday() < 5:
            count += 1
    return count


def weekday_cond(date_path: str) -> dict:
    """True when the 'YYYY-MM-DD' string at date_path is Mon-Fri ($isoDayOfWeek: 1=Mon ... 7=Sun)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_path}}}, 5]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """One group over the employee's records of the month. MongoDB does all the counting."""
    present_weight = {
        "$cond": [
            {"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                weekday_cond("$date"),  # R7: only Mon-Fri records count as present days
            ]},
            # R5: a half day counts as 0.5. Missing half_day (legacy record) means false.
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_weight},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    """
    Starts from `employees`, so people with zero logs still count in headcount (R9) and
    people who join after the month do not. Each employee is joined with their logs of
    the month ($lookup), the logs are unwound (an employee with no logs is kept), and
    everything is grouped by department.
    """
    _, last = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department

    # after $unwind the log fields are at "$logs.<field>"; for an employee without logs they are missing
    present = {"$in": ["$logs.status", PRESENT_STATUSES]}
    has_hours = {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}
    late = {"$ifNull": ["$logs.late_minutes", 0]}
    count_if = lambda cond: {"$sum": {"$cond": [cond, 1, 0]}}

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},  # distinct employees = headcount (R9)
            # R5-R7: Mon-Fri presence records; a half day counts 0.5
            "present_days": {"$sum": {"$cond": [
                {"$and": [present, weekday_cond("$logs.date")]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": count_if({"$gt": [late, 0]}),
            "total_late_minutes": {"$sum": late},
            "leave_count": count_if({"$eq": ["$logs.status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$logs.status", "ON_DUTY"]}),
            # work hours as whole hundredths, so the average is exact (R8 half-up is done later)
            "work_cents": {"$sum": {"$cond": [
                {"$and": [present, has_hours]},
                {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]},
                0,
            ]}},
            "work_n": count_if({"$and": [present, has_hours]}),
        }},
        {"$project": {
            "headcount": {"$size": "$emps"},
            "present_days": 1, "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1, "work_cents": 1, "work_n": 1,
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    """
    Total late minutes per employee for the month, ranked with standard competition
    ranking (1, 2, 2, 4). The ranking is done by MongoDB with $setWindowFields / $rank.
    """
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline: list = [
        # all records of the month, weekends included (R7)
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},  # only employees with a total > 0
        # name and department come from employees; $unwind drops logs of unknown employees
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # ranking happens inside the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties can exceed it
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(codes: list, department: str, date_from: date, date_to: date) -> list:
    """
    One row per calendar day from date_from to date_to (both included), built in MongoDB:
    $group the logs per day, $densify to fill the missing days, $lookup the headcount of
    each day, then $setWindowFields for the 7-day moving average.
    Rates are kept in whole units of 0.0001 so rounding is exactly half-up (floor(x + 0.5)).
    """
    d0, d1 = date_from.isoformat(), date_to.isoformat()
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
    end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc) + timedelta(days=1)  # upper bound is exclusive
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        # logs of this department's employees in the range (uses the emp_code + date index)
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d0, "$lte": d1}}},
        # one harmless zero row for the first day, so $densify always has something to fill from
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"emp_code": codes[0]}},
            {"$replaceWith": {"date": d0, "status": "NONE", "late_minutes": 0}},
        ]}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},  # R5: half day = 0.5
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: a row for every day, weekends and empty days included
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},  # Mon-Fri
        }},
        # R9: employees of the department who joined on or before that day
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # attendance_rate in units of 0.0001, half-up; null unless a working day with headcount > 0
        {"$set": {"rate_units": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
            None,
        ]}}},
        # mean of the non-null rates in this row and up to 6 rows before it (inside the range only)
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"avg_units": {"$avg": "$rate_units", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_units", None]}, None, {"$divide": ["$rate_units", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$avg_units", None]}, None,
                {"$divide": [{"$floor": {"$add": ["$avg_units", 0.5]}}, 10000]},
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


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


class RegularizeIn(BaseModel):
    # derived fields (work_hours, late_minutes, ...) are not declared, so they are ignored
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    body: RegularizeIn,
    att_date: str = Path(alias="date", pattern=DATE_PATTERN),
):
    check_date(att_date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # state before the change (legacy records may miss some fields)
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    # state after the change: omitted fields stay as they are
    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not send them")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in).replace(microsecond=0) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out).replace(microsecond=0) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise HTTPException(422, "this request changes nothing")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive_fields(new_status, new_in, new_out, emp, att_date))

    # history lists only the fields that actually changed
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    now = datetime.now(timezone.utc)
    entry = {
        "at": now.replace(microsecond=now.microsecond // 1000 * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic lock: history never shrinks, so "no element at index n" means nobody
    # else corrected this record since we read it. If someone did, we lose with 409.
    n = len(rec.get("history", []))
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], f"history.{n}": {"$exists": False}, "punch_out": old["punch_out"]},
        {"$set": new, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was changed by someone else, please retry")

    rec.update(new)
    rec["history"] = rec.get("history", []) + [entry]
    return attendance_out(rec)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # R7: working days are counted from joined_on for someone who joined mid-month
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(start, last)

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = Decimal(str(r.get("present_days", 0)))

    pct = None
    if working_days > 0:  # R8: 2 decimals, half-up
        pct = float(round_half_up(present / Decimal(working_days) * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(round_half_up(present, 2)),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    month_bounds(month)  # 422 for impossible months such as 0000-05
    rows = db.employees.aggregate(department_summary_pipeline(month, department))
    items = []
    for r in rows:
        avg = None
        if r["work_n"] > 0:  # R8: 2 decimals half-up; work_cents is an exact integer
            avg = float(round_half_up(Decimal(round(r["work_cents"])) / Decimal(r["work_n"]) / 100, 2))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(round_half_up(Decimal(str(r["present_days"])), 2)),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)  # 422 for impossible months such as 0000-05
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=DATE_PATTERN),
    date_to: str = Query(..., alias="to", pattern=DATE_PATTERN),
):
    d0 = date.fromisoformat(check_date(date_from))
    d1 = date.fromisoformat(check_date(date_to))
    if d1 < d0:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d1 - d0).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "department not found")

    rows = db.attendance_logs.aggregate(department_trend_pipeline(codes, department, d0, d1))
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": r["attendance_rate"],
            "moving_avg_7d": r["moving_avg_7d"],
        }
        for r in rows
    ]
    return {"department": department, "items": items}


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
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #