"""StudySlot HTTP API (FastAPI).

This file only deals with HTTP: routes, status codes, metrics and chaos
switches. The booking rules live in booking_core.py and the data in db.py,
so the gRPC service (Part B) can reuse exactly the same rules and data.
"""
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from prometheus_client import (
    Counter, Gauge, Histogram, Summary,
    generate_latest, CONTENT_TYPE_LATEST,
)

import booking_core
import db
from logging_setup import log_event

db.init_db()   # create the database file and table on startup if needed

app = FastAPI(title="StudySlot")

# --- Metrics ---
# DELIBERATELY BAD metric for the A1 cardinality experiment: a per-request-ID label.
CARDINALITY_DEMO = Counter(
    "studyslot_requests_by_id_total",
    "UNSAFE demo: one series per unique request_id",
    ["request_id"],
)
CARDINALITY_BOMB_ON = False

# Counter: successful bookings made through this HTTP API.
BOOKINGS_TOTAL = Counter(
    "studyslot_bookings_total",
    "Total number of successful room bookings",
)

# Gauge: room-slots booked right now. Instead of adding/subtracting in memory,
# it reads the count from the database every time Prometheus scrapes, so it
# stays correct after restarts and when the gRPC service changes bookings too.
ROOMS_OCCUPIED = Gauge(
    "studyslot_rooms_occupied",
    "Number of room-slots currently booked",
)
ROOMS_OCCUPIED.set_function(db.count_bookings)

# Chaos: artificial delay (seconds) injected into the availability check. 0 = off.
INJECTED_DELAY = 0.0

AVAILABILITY_CHECK_SECONDS = Histogram(
    "studyslot_availability_check_seconds",
    "Time spent checking room availability during a booking",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)
AVAILABILITY_CHECK_SUMMARY = Summary(
    "studyslot_availability_check_summary_seconds",
    "Summary of availability-check duration (average via _sum / _count)",
)

# --- Turning booking rule errors into HTTP responses ---
# Which HTTP status each kind of problem becomes. Anything not listed is 400.
HTTP_STATUS_FOR = {
    booking_core.AlreadyBooked: 409,
    booking_core.BookingNotFound: 404,
}


def http_error(err: booking_core.BookingError) -> HTTPException:
    status = HTTP_STATUS_FOR.get(type(err), 400)
    return HTTPException(
        status_code=status,
        detail={"reason": err.reason, "message": err.message, **err.details},
    )


class BookingRequest(BaseModel):
    room: str
    slot: str
    student_ids: list[str]


# --- Routes ---
@app.get("/")
def home():
    return {"service": "StudySlot", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rooms")
def list_rooms():
    """Every room with its size limits and how many slots are free."""
    return booking_core.list_rooms()


@app.get("/availability")
def availability(slot: str, group_size: int | None = None):
    """Free rooms for a slot. Pass group_size to see only rooms that fit."""
    try:
        return booking_core.availability(slot, group_size)
    except booking_core.BookingError as err:
        raise http_error(err)


@app.post("/book")
def book(req: BookingRequest):
    """Book a room for a slot for a group. Enforces size limits and no double-booking."""
    request_id = uuid.uuid4().hex[:8]   # short unique ID for this request, e.g. "a3f9c1d2"
    if CARDINALITY_BOMB_ON:
        CARDINALITY_DEMO.labels(request_id=request_id).inc()

    start = time.perf_counter()        # stopwatch for the availability check
    time.sleep(INJECTED_DELAY)         # chaos: 0 normally; >0 when delay is injected
    try:
        booking = booking_core.create_booking(req.room, req.slot, req.student_ids)
    except booking_core.BookingError as err:
        error = http_error(err)
        log_event("booking rejected", level="WARNING",
                  request_id=request_id, room=req.room, slot=req.slot,
                  group_size=len(req.student_ids), status=error.status_code,
                  reason=err.reason)
        raise error
    finally:
        elapsed = time.perf_counter() - start
        AVAILABILITY_CHECK_SECONDS.observe(elapsed)
        AVAILABILITY_CHECK_SUMMARY.observe(elapsed)

    BOOKINGS_TOTAL.inc()
    log_event("booking confirmed", level="INFO",
              request_id=request_id, booking_id=booking["id"],
              room=req.room, slot=req.slot,
              group_size=len(req.student_ids), status=200)
    return {
        "message": "Booked",
        "booking_id": booking["id"],
        "room": booking["room"],
        "slot": booking["slot"],
        "group_size": len(booking["student_ids"]),
    }


@app.get("/bookings")
def list_bookings():
    """Librarian view: everything currently booked."""
    return booking_core.list_bookings()


@app.delete("/bookings/{room}/{slot}")
def cancel(room: str, slot: str):
    """Cancel a booking, freeing that room+slot."""
    try:
        booking_core.cancel_booking(room, slot)
    except booking_core.BookingError as err:
        raise http_error(err)
    log_event("booking cancelled", level="INFO", room=room, slot=slot, status=200)
    return {"message": "Cancelled", "room": room, "slot": slot}


@app.get("/metrics")
def metrics():
    """The page Prometheus scrapes. Plain text, not JSON."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Chaos switches (from A1) ---
@app.post("/chaos/delay")
def set_delay(seconds: float = 0.0):
    """Turn the artificial availability-check delay on/off. seconds=0 disables it."""
    global INJECTED_DELAY
    INJECTED_DELAY = seconds
    log_event("chaos delay set", level="WARNING", injected_delay_seconds=seconds)
    return {"injected_delay_seconds": INJECTED_DELAY}


@app.get("/chaos/status")
def chaos_status():
    """Show current chaos state."""
    return {"injected_delay_seconds": INJECTED_DELAY}


@app.post("/chaos/cardinality")
def set_cardinality(active: bool = False):
    """Arm/disarm the cardinality bomb (per-request_id metric label)."""
    global CARDINALITY_BOMB_ON
    CARDINALITY_BOMB_ON = active
    log_event("chaos cardinality set", level="WARNING", cardinality_bomb=active)
    return {"cardinality_bomb": CARDINALITY_BOMB_ON}