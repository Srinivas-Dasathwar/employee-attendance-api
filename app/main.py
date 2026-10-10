import json
import re
from bson import json_util
import os
import calendar
from datetime import date as date_type
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo import MongoClient, ReturnDocument

load_dotenv()  # real environment variables win over .env

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def lifespan(_app):
    # idempotent: create_index does nothing if the index already exists
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime from MongoDB -> epoch ms for the API."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm_to_min(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of the punch-in, or the previous day for an overnight shift
    when the punch-in is earlier than shift_end."""
    local = punch_in.astimezone(IST)
    s, e = hhmm_to_min(shift_start), hhmm_to_min(shift_end)
    if e <= s and (local.hour * 60 + local.minute) < e:
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Start and end of the shift (IST-aware) for an attendance date."""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    s, e = hhmm_to_min(shift_start), hhmm_to_min(shift_end)
    start = day + timedelta(minutes=s)
    end = day + timedelta(minutes=e)
    if e <= s:
        end += timedelta(days=1)
    return start, end


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding (Python's round() is not half-up)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 600 seconds after shift start."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if at least 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def employee_out(doc: dict) -> dict:
    """Employee document -> API shape (no _id, created_at in epoch ms)."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = dt_to_ms(out.get("created_at"))
    return out

def parse_date(value: str, name: str = "date") -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    return value


def record_out(doc: dict) -> dict:
    """Attendance document -> API shape (no _id, instants as epoch ms, defaults filled)."""
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for field, ch in h.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
            else:
                changes[field] = ch
        history.append({"at": dt_to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def month_range(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31')"""
    y, m = int(month[:4]), int(month[5:])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on if that is later."""
    first, last = month_range(month)
    start = max(date_type.fromisoformat(first), date_type.fromisoformat(joined_on))
    end = date_type.fromisoformat(last)
    if start > end:
        return 0
    days = (end - start).days + 1
    return sum(1 for i in range(days) if (start.toordinal() + i) % 7 not in (6, 0))  # Sat/Sun

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v):
        datetime.strptime(v, "%Y-%m-%d")  # raises ValueError -> 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
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
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    # TODO (yours): three fixes
    #   1. skip = (page - 1) * page_size
    #   2. total = count_documents with the filter q
    #   3. sort by emp_code ascending before skip/limit
    skip = page * page_size
    total = db.employees.count_documents({})
    cursor = db.employees.find(q).skip(skip).limit(page_size)
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q

ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()

    # most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(rec["punch_in"], ts)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < 4.50,
    }
    # atomic: only succeeds if the record is still open
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    status = body.status or rec["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # recompute derived fields from the final values (R2-R5)
    if status in PRESENCE:
        late = compute_late_minutes(new_in, date, emp["shift_start"], emp["shift_end"])
        if new_out is not None:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, date, emp["shift_start"], emp["shift_end"])
            half_day = work_hours < 4.50
        else:
            work_hours, overtime, half_day = None, 0, False
    else:
        late, work_hours, overtime, half_day = 0, None, 0, False

    new = {
        "status": status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # optimistic lock: only succeeds if nobody changed the record or appended history since we read it
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            f"history.{n}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    is_weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
    is_present = {"$in": ["$status", list(PRESENCE)]}
    day_value = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [is_present, is_weekday]}, day_value, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    working = count_working_days(month, emp["joined_on"])
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present / working * 100, 2) if working else None,
    }


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    first, last = month_range(month)
    is_weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
    is_present = {"$in": ["$status", list(PRESENCE)]}
    day_value = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    has_hours = {"$and": [is_present, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    late = {"$ifNull": ["$late_minutes", 0]}

    emp_match = {"joined_on": {"$lte": last}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [is_present, is_weekday]}, day_value, 0]}},
                    "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
                    "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": late},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$stats.hours_sum", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$stats.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}

def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    first, last = month_range(month)
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are ignored
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},  # standard competition ranking: 1, 2, 2, 4
        }},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def build_trend(department: str, date_from: str, date_to: str):
    """Validates input and returns (collection, pipeline). Used by the endpoint and by explain."""
    parse_date(date_from, "from")
    parse_date(date_to, "to")
    d1, d2 = date_type.fromisoformat(date_from), date_type.fromisoformat(date_to)
    if d2 < d1:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d2 - d1).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")

    lower = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
    upper = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)  # bounds: upper is exclusive
    is_present = {"$in": ["$status", list(PRESENCE)]}
    is_half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    window = {"documents": [-6, 0]}

    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        # one row per day that has records; p2 = present_count * 2 (keeps half days as integers)
        {"$group": {
            "_id": "$date",
            "p2": {"$sum": {"$cond": [is_present, {"$cond": [is_half, 1, 2]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one row for every calendar day in the range
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$sort": {"d": 1}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "p2": {"$ifNull": ["$p2", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},  # 1 = Sunday
        }},
        # headcount (R9): department employees who joined on or before that day
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # rate in units of 0.0001, rounded half-up with integer maths: floor((p2*10000 + h) / (2h))
        {"$addFields": {"rate4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]}},
            None,
        ]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}}},
        # moving window: this row and up to 6 before it, only inside the requested range
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {
                "s": {"$sum": {"$ifNull": ["$rate4", 0]}, "window": window},
                "k": {"$sum": "$has_rate", "window": window},
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate4", None]}, None, {"$divide": ["$rate4", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$k", 0]},
                None,
                {"$divide": [
                    {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$s"]}, "$k"]}, {"$multiply": [2, "$k"]}]}},
                    10000,
                ]},
            ]},
        }},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    pipeline = build_trend(department, date_from, date_to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


def need(value, name: str):
    if value is None or value == "":
        raise HTTPException(422, f"{name} is required for this endpoint")
    return value


def need_month(month):
    need(month, "month")
    if not re.fullmatch(MONTH_PATTERN, month):
        raise HTTPException(422, "month must be YYYY-MM")
    return month


def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # BSON types -> plain JSON


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = build_attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size, "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = employee_monthly_pipeline(need(emp_code, "emp_code"), need_month(month))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = department_summary_pipeline(need_month(month), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(need_month(month), limit, department)
        else:  # department_trend
            collection = "attendance_logs"
            pipeline = build_trend(need(department, "department"), need(from_, "from"), need(to, "to"))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}