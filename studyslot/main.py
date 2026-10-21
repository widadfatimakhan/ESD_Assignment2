"""StudySlot HTTP API (FastAPI).

This file only deals with HTTP: routes, status codes, the error format,
metrics and chaos switches. The booking rules live in booking_core.py and the
data in db.py, so the gRPC service (Part B) reuses exactly the same rules and data.
"""
import re
import time
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import (
    Counter, Gauge, Histogram, Summary,
    generate_latest, CONTENT_TYPE_LATEST,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

import booking_core
import db
from logging_setup import log_event, request_id_var
from rate_limit import RateLimitExceeded, SlidingWindowRateLimiter

db.init_db()   # create the database file and table on startup if needed

app = FastAPI(
    title="StudySlot",
    version="2.0.0",
    description=(
        "Campus study-room booking service (ESD Assignment 2).\n\n"
        "- Every response carries an `X-Request-ID` header. Send your own to correlate calls; "
        "otherwise one is generated. The same ID appears in the logs and in error bodies.\n"
        "- Every error uses the `ErrorResponse` shape: `{\"error\": {\"code\", \"message\", "
        "\"request_id\", \"details\"}}`.\n"
        "- `POST /bookings` accepts an `Idempotency-Key` header so retries are safe.\n"
        "- `GET /availability` is rate limited to 5 calls per 10 seconds per client IP."
    ),
    redoc_url=None,   # replaced by the /redoc route below (FastAPI's default ReDoc link is broken)
)

# ---------------------------------------------------------------------------
# Metrics (same as A1)
# ---------------------------------------------------------------------------
# DELIBERATELY BAD metric for the A1 cardinality experiment: a per-request-ID label.
CARDINALITY_DEMO = Counter(
    "studyslot_requests_by_id_total",
    "UNSAFE demo: one series per unique request_id",
    ["request_id"],
)
CARDINALITY_BOMB_ON = False

BOOKINGS_TOTAL = Counter(
    "studyslot_bookings_total",
    "Total number of successful room bookings",
)

# Reads the count from the database at every scrape, so it stays correct after
# restarts and when the gRPC service changes bookings too.
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


# ---------------------------------------------------------------------------
# Request IDs: every request gets one, sent back in the X-Request-ID header,
# included in every log line and in every error body.
# ---------------------------------------------------------------------------
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@app.middleware("http")
async def assign_request_id(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID", "")
    # Reuse the caller's ID if it looks safe, otherwise make a new one.
    request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# One error format for every error response:
#   {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}
# ---------------------------------------------------------------------------
class ErrorBody(BaseModel):
    code: str = Field(description="Short machine-readable error code", examples=["double_booked"])
    message: str = Field(description="Human-readable explanation")
    request_id: str | None = Field(None, description="Matches the X-Request-ID header and the logs")
    details: dict | None = Field(None, description="Extra information, when there is any")


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_response(request: Request, status: int, code: str, message: str,
                   details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"code": code, "message": message,
            "request_id": getattr(request.state, "request_id", None)}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


# Which HTTP status each kind of booking-rule problem becomes.
# Anything not listed is 422: the request was readable but broke a rule.
HTTP_STATUS_FOR = {
    booking_core.AlreadyBooked: 409,
    booking_core.BookingNotFound: 404,
}


def status_for(err: booking_core.BookingError) -> int:
    return HTTP_STATUS_FOR.get(type(err), 422)


@app.exception_handler(booking_core.BookingError)
async def handle_booking_error(request: Request, err: booking_core.BookingError):
    return error_response(request, status_for(err), err.reason, err.message, err.details)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if any(e["type"] == "json_invalid" for e in errors):
        status, code, message, details = 400, "malformed_json", "The request body is not valid JSON.", None
    else:
        problems = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in errors]
        status, code, message, details = 422, "validation_error", "Some fields are missing or invalid.", {"problems": problems}
    log_event("request rejected", level="WARNING", method=request.method,
              path=request.url.path, status=status, reason=code)
    return error_response(request, status, code, message, details)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    # e.g. 404 for an unknown path, 405 for a wrong method
    code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
    return error_response(request, exc.status_code, code, str(exc.detail), headers=exc.headers)


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limited(request: Request, exc: RateLimitExceeded):
    log_event("rate limited", level="WARNING", method=request.method, path=request.url.path,
              client=exc.client, status=429, retry_after_seconds=exc.retry_after)
    return error_response(
        request, 429, "rate_limited",
        f"Too many requests: at most {exc.limit} calls per {exc.window_seconds} seconds "
        f"per client. Try again in {exc.retry_after} seconds.",
        details={"limit": exc.limit, "window_seconds": exc.window_seconds,
                 "retry_after_seconds": exc.retry_after, "client": exc.client},
        headers={"Retry-After": str(exc.retry_after),
                 "X-RateLimit-Limit": str(exc.limit),
                 "X-RateLimit-Remaining": "0"},
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    log_event("unhandled error", level="ERROR", path=request.url.path, error=repr(exc))
    return error_response(request, 500, "internal_error", "Something went wrong on our side.")


def header_doc(description: str, type_: str = "string") -> dict:
    """OpenAPI helper: describe one response header."""
    return {"description": description, "schema": {"type": type_}}


RATE_LIMIT_HEADERS = {
    "X-RateLimit-Limit": header_doc("Calls allowed per window", "integer"),
    "X-RateLimit-Remaining": header_doc("Calls left in the current window", "integer"),
}


def errors(*codes: int) -> dict:
    """OpenAPI helper: document that these status codes return ErrorResponse."""
    descriptions = {400: "Malformed request", 404: "Not found", 409: "Conflict",
                    422: "Invalid input or broken booking rule", 429: "Too many requests"}
    docs = {c: {"model": ErrorResponse, "description": descriptions.get(c, "Error")} for c in codes}
    if 429 in docs:
        docs[429]["headers"] = {
            "Retry-After": header_doc("Seconds to wait before calling again", "integer"),
            **RATE_LIMIT_HEADERS,
        }
    return docs


# ---------------------------------------------------------------------------
# Request and response shapes
# ---------------------------------------------------------------------------
StudentId = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


class BookingRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",   # unknown fields are rejected instead of silently ignored
        json_schema_extra={"examples": [
            {"room": "R3", "slot": "09:00", "student_ids": ["s1", "s2", "s3", "s4", "s5"]}
        ]},
    )
    room: str = Field(description="Room code, R1 to R5")
    slot: str = Field(description="Start time of a 30-minute slot, 09:00 to 16:30")
    student_ids: list[StudentId] = Field(min_length=1, max_length=50,
                                         description="Every student in the group, each listed once")


class Booking(BaseModel):
    id: str
    room: str
    slot: str
    student_ids: list[str]
    group_size: int
    created_at: str


class RoomInfo(BaseModel):
    room: str
    capacity: int
    min_group: int
    booked_slots: list[str]
    free_slot_count: int


class FreeRoom(BaseModel):
    room: str
    capacity: int
    min_group: int


class Availability(BaseModel):
    slot: str
    group_size: int | None
    free_rooms: list[FreeRoom]


class DelaySetting(BaseModel):
    seconds: float = Field(ge=0, le=10, description="Delay added to each booking's availability check; 0 turns it off")


class CardinalitySetting(BaseModel):
    active: bool


class ChaosState(BaseModel):
    delay_seconds: float
    cardinality_bomb: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", tags=["service"])
def home():
    return {"service": "StudySlot", "status": "running"}


@app.get("/health", tags=["service"])
def health():
    return {"status": "ok"}


@app.get("/rooms", response_model=list[RoomInfo], tags=["rooms"])
def list_rooms():
    """Every room with its size limits and which slots are booked."""
    return booking_core.list_rooms()


# ---------------------------------------------------------------------------
# Rate limiting for GET /availability
# Students refresh availability to hunt for a free room, so this is the
# endpoint most likely to be hammered. Each client (identified by its IP
# address) gets 5 calls in any 10-second period.
# ---------------------------------------------------------------------------
AVAILABILITY_LIMIT = 5
AVAILABILITY_WINDOW_SECONDS = 10
availability_limiter = SlidingWindowRateLimiter(AVAILABILITY_LIMIT, AVAILABILITY_WINDOW_SECONDS)


def limit_availability_calls(request: Request, response: Response) -> None:
    """Dependency: runs before /availability. Raises RateLimitExceeded when over the limit."""
    client = request.client.host if request.client else "unknown"
    remaining = availability_limiter.check(client)
    response.headers["X-RateLimit-Limit"] = str(AVAILABILITY_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)


@app.get("/availability", response_model=Availability,
         responses={200: {"headers": RATE_LIMIT_HEADERS}, **errors(422, 429)},
         dependencies=[Depends(limit_availability_calls)], tags=["rooms"])
def availability(
    slot: str = Query(description="Slot start time, e.g. 09:00"),
    group_size: int | None = Query(None, ge=1, le=50, description="Only show rooms that fit this many students"),
):
    """Free rooms for a slot. Pass group_size to see only rooms that fit.

    Rate limited: 5 calls per 10 seconds per client IP. Over the limit you get
    429 with a Retry-After header saying how many seconds to wait.
    """
    return booking_core.availability(slot, group_size)


@app.post("/bookings", status_code=201, response_model=Booking, tags=["bookings"],
          responses={
              201: {"description": "Booking created, or replayed for a repeated Idempotency-Key",
                    "headers": {
                        "Location": header_doc("URL of the booking, e.g. /bookings/{id}"),
                        "Idempotent-Replayed": header_doc("'true' when this is a replay of an earlier request"),
                    }},
              **errors(400, 409, 422),
          })
def create_booking(
    req: BookingRequest,
    response: Response,
    idempotency_key: str | None = Header(
        None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Optional but recommended. A unique value (e.g. a UUID) chosen by the client for "
            "this booking. Retrying with the same key and body returns the original booking "
            "instead of creating a new one or failing with 409."
        ),
    ),
):
    """Book a room for a slot. Returns 201 with a Location header pointing at the new booking.

    A replayed request (same Idempotency-Key, same body) returns the original 201
    response with the header `Idempotent-Replayed: true`.
    """
    if CARDINALITY_BOMB_ON:
        CARDINALITY_DEMO.labels(request_id=request_id_var.get()).inc()

    start = time.perf_counter()        # stopwatch for the availability check
    time.sleep(INJECTED_DELAY)         # chaos: 0 normally; >0 when delay is injected
    try:
        booking, replayed = booking_core.create_booking(
            req.room, req.slot, req.student_ids, idempotency_key=idempotency_key
        )
    except booking_core.BookingError as err:
        log_event("booking rejected", level="WARNING", room=req.room, slot=req.slot,
                  group_size=len(req.student_ids), status=status_for(err), reason=err.reason)
        raise                          # handle_booking_error turns it into the error response
    finally:
        elapsed = time.perf_counter() - start
        AVAILABILITY_CHECK_SECONDS.observe(elapsed)
        AVAILABILITY_CHECK_SUMMARY.observe(elapsed)

    response.headers["Location"] = f"/bookings/{booking['id']}"
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
        log_event("booking replayed", level="INFO", booking_id=booking["id"], room=booking["room"],
                  slot=booking["slot"], status=201, idempotent_replay=True)
    else:
        BOOKINGS_TOTAL.inc()           # count only bookings that were really created
        log_event("booking confirmed", level="INFO", booking_id=booking["id"], room=booking["room"],
                  slot=booking["slot"], group_size=booking["group_size"], status=201)
    return booking


@app.get("/bookings", response_model=list[Booking], tags=["bookings"])
def list_bookings():
    """Librarian view: everything currently booked."""
    return booking_core.list_bookings()


@app.get("/bookings/{booking_id}", response_model=Booking, responses=errors(404, 422), tags=["bookings"])
def get_booking(booking_id: uuid.UUID):
    """One booking by its ID."""
    return booking_core.get_booking(str(booking_id))


@app.delete("/bookings/{booking_id}", status_code=204, responses=errors(404, 422), tags=["bookings"])
def cancel_booking(booking_id: uuid.UUID):
    """Cancel a booking. Returns 204 with no body."""
    booking = booking_core.cancel_booking(str(booking_id))
    log_event("booking cancelled", level="INFO", booking_id=booking["id"],
              room=booking["room"], slot=booking["slot"], status=204)
    return Response(status_code=204)


@app.get("/redoc", include_in_schema=False)
def redoc():
    """API reference page drawn from the OpenAPI spec.

    FastAPI 0.109 loads ReDoc from the CDN tag "redoc@next", which no longer
    renders (blank page), so we point it at the stable 2.x release instead.
    """
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title="StudySlot - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js",
    )


@app.get("/metrics", response_class=PlainTextResponse, tags=["service"],
         responses={200: {"description": "Prometheus text exposition format"}})
def metrics():
    """The page Prometheus scrapes. Plain text, not JSON."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Chaos switches (from A1) ---
def chaos_state() -> dict:
    return {"delay_seconds": INJECTED_DELAY, "cardinality_bomb": CARDINALITY_BOMB_ON}


@app.get("/chaos", response_model=ChaosState, tags=["chaos"])
def get_chaos():
    """Current chaos settings."""
    return chaos_state()


@app.put("/chaos/delay", response_model=ChaosState, responses=errors(422), tags=["chaos"])
def set_delay(setting: DelaySetting):
    """Set the artificial availability-check delay. 0 turns it off."""
    global INJECTED_DELAY
    INJECTED_DELAY = setting.seconds
    log_event("chaos delay set", level="WARNING", injected_delay_seconds=setting.seconds)
    return chaos_state()


@app.put("/chaos/cardinality", response_model=ChaosState, responses=errors(422), tags=["chaos"])
def set_cardinality(setting: CardinalitySetting):
    """Arm or disarm the cardinality bomb (per-request_id metric label)."""
    global CARDINALITY_BOMB_ON
    CARDINALITY_BOMB_ON = setting.active
    log_event("chaos cardinality set", level="WARNING", cardinality_bomb=setting.active)
    return chaos_state()