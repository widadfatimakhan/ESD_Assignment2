"""A resilient caller for POST /bookings (Assignment 2, Part A).

It plays the part of another service that books rooms through the StudySlot
HTTP API. Every call is protected by three things:

  1. TIMEOUT      - never wait more than TIMEOUT_SECONDS for one attempt.
  2. RETRY        - a transient failure (timeout, connection error, 5xx) is
                    retried up to MAX_ATTEMPTS in total, waiting longer each
                    time (exponential backoff with a little random jitter).
                    Retrying a POST is only safe because every booking carries
                    an Idempotency-Key: a retry can never create a second booking.
                    Client errors (4xx, e.g. 409 room taken) are NOT retried,
                    because sending the same request again would fail the same way.
  3. CIRCUIT BREAKER - after BREAKER_FAILURE_THRESHOLD failed attempts in a row
                    the breaker OPENS and calls are refused immediately, without
                    touching the network, for BREAKER_RECOVERY_SECONDS. Then it
                    lets ONE trial call through (HALF-OPEN): success closes it,
                    failure opens it again.

Running this file runs a demo that uses the API's chaos switches to cause
failures on purpose, so you can see each behaviour in the output.

Run from the repo root while the stack is up:
    .venv\\Scripts\\python scripts/booking_caller.py
"""
import random
import sys
import time
import uuid

import httpx

BASE_URL = "http://localhost:8000"

# ---- Settings (quote these in the report) ----------------------------------
TIMEOUT_SECONDS = 1.0             # per attempt
MAX_ATTEMPTS = 3                  # 1 try + up to 2 retries
BACKOFF_BASE_SECONDS = 0.25       # waits ~0.25s, then ~0.5s (doubling), +/-20% jitter
BREAKER_FAILURE_THRESHOLD = 3     # consecutive failed attempts that open the breaker
BREAKER_RECOVERY_SECONDS = 5.0    # how long it stays open before a trial call

START = time.monotonic()


def log(message: str) -> None:
    print(f"[t={time.monotonic() - START:6.2f}s] {message}")


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class CircuitOpenError(Exception):
    """Raised instead of making a call while the breaker is open."""


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF-OPEN"

    def __init__(self, failure_threshold: int, recovery_seconds: float):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.opened_at = 0.0

    def before_call(self) -> None:
        """Raise CircuitOpenError if calls are currently blocked."""
        if self.state == self.OPEN:
            waited = time.monotonic() - self.opened_at
            if waited < self.recovery_seconds:
                raise CircuitOpenError(
                    f"circuit OPEN - call blocked without contacting the server "
                    f"(trial allowed in {self.recovery_seconds - waited:.1f}s)"
                )
            self._move_to(self.HALF_OPEN, "recovery period over, letting one trial call through")

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.state != self.CLOSED:
            self._move_to(self.CLOSED, "trial call succeeded")

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == self.HALF_OPEN:
            self._open("trial call failed")
        elif self.state == self.CLOSED and self.consecutive_failures >= self.failure_threshold:
            self._open(f"{self.consecutive_failures} failures in a row")

    def _open(self, reason: str) -> None:
        self.opened_at = time.monotonic()
        self._move_to(self.OPEN, f"{reason}; blocking calls for {self.recovery_seconds:.0f}s")

    def _move_to(self, new_state: str, reason: str) -> None:
        log(f"   breaker: {self.state} -> {new_state} ({reason})")
        self.state = new_state


# ---------------------------------------------------------------------------
# The caller
# ---------------------------------------------------------------------------
class TransientError(Exception):
    """A failure worth retrying: timeout, connection problem or 5xx."""


class BookingFailed(Exception):
    """The booking did not happen and retrying will not help."""


class BookingCaller:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS)
        self.breaker = CircuitBreaker(BREAKER_FAILURE_THRESHOLD, BREAKER_RECOVERY_SECONDS)

    def book(self, room: str, slot: str, student_ids: list[str],
             idempotency_key: str | None = None) -> dict:
        """Create a booking, retrying transient failures. Returns the booking."""
        # One key for the whole logical booking, reused by every retry.
        key = idempotency_key or str(uuid.uuid4())
        body = {"room": room, "slot": slot, "student_ids": student_ids}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.breaker.before_call()                 # may raise CircuitOpenError
            try:
                booking = self._attempt(body, key, attempt)
            except TransientError as err:
                self.breaker.record_failure()
                if attempt == MAX_ATTEMPTS:
                    raise BookingFailed(f"gave up after {MAX_ATTEMPTS} attempts ({err})") from None
                if self.breaker.state == CircuitBreaker.OPEN:
                    raise BookingFailed("stopped retrying because the breaker opened") from None
                wait = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1) * random.uniform(0.8, 1.2)
                log(f"   retrying in {wait:.2f}s (same Idempotency-Key, so no duplicate is possible)")
                time.sleep(wait)
            else:
                self.breaker.record_success()
                return booking
        raise AssertionError("unreachable")

    def _attempt(self, body: dict, key: str, attempt: int) -> dict:
        label = f"   attempt {attempt}/{MAX_ATTEMPTS}:"
        try:
            response = self.http.post("/bookings", json=body, headers={"Idempotency-Key": key})
        except httpx.TimeoutException:
            log(f"{label} no answer within {TIMEOUT_SECONDS}s (timeout)")
            raise TransientError("timeout") from None
        except httpx.TransportError as err:
            log(f"{label} connection problem: {err!r}")
            raise TransientError("connection error") from None

        if response.status_code >= 500:
            log(f"{label} {response.status_code} {response.json()['error']['code']}")
            raise TransientError(f"HTTP {response.status_code}")
        if response.status_code >= 400:                # the server answered: it is healthy
            error = response.json()["error"]
            log(f"{label} {response.status_code} {error['code']} - not retrying a client error")
            self.breaker.record_success()
            raise BookingFailed(f"{response.status_code} {error['code']}: {error['message']}")

        booking = response.json()
        replayed = response.headers.get("Idempotent-Replayed") == "true"
        note = " (REPLAYED: this booking was already made by an earlier attempt)" if replayed else ""
        log(f"{label} {response.status_code} booked {booking['room']} {booking['slot']}, "
            f"id {booking['id'][:8]}{note}")
        return booking


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
DEMO_STUDENTS = ["demo-a", "demo-b", "demo-c"]      # lets the demo find and remove its own bookings
ROOM = "R5"


def chaos(admin: httpx.Client, path: str, payload: dict) -> None:
    """Flip a chaos switch on the server (demo setup, not part of the caller)."""
    admin.put(f"/chaos/{path}", json=payload).raise_for_status()
    log(f"[demo] PUT /chaos/{path} {payload}")


def try_book(caller: BookingCaller, slot: str, key: str | None = None) -> None:
    log(f"BOOK {ROOM} {slot}")
    try:
        caller.book(ROOM, slot, DEMO_STUDENTS, idempotency_key=key)
    except CircuitOpenError as err:
        log(f"   BLOCKED: {err}")
    except BookingFailed as err:
        log(f"   FAILED: {err}")


def main() -> None:
    admin = httpx.Client(base_url=BASE_URL, timeout=5)
    try:
        admin.get("/health").raise_for_status()
    except httpx.HTTPError as err:
        sys.exit(f"Cannot reach {BASE_URL} ({err!r}). Is the stack running? Try: docker compose up -d")

    print("Caller settings:")
    print(f"  timeout per attempt : {TIMEOUT_SECONDS}s")
    print(f"  attempts            : {MAX_ATTEMPTS} (1 + {MAX_ATTEMPTS - 1} retries), "
          f"backoff {BACKOFF_BASE_SECONDS}s doubling, +/-20% jitter")
    print(f"  retried             : timeouts, connection errors, 5xx (never 4xx)")
    print(f"  circuit breaker     : opens after {BREAKER_FAILURE_THRESHOLD} failed attempts in a row, "
          f"trial call after {BREAKER_RECOVERY_SECONDS:.0f}s\n")

    # Clean slate: remove bookings left by an earlier demo run, switch chaos off.
    for booking in admin.get("/bookings").json():
        if booking["student_ids"] == DEMO_STUDENTS:
            admin.delete(f"/bookings/{booking['id']}")
    chaos(admin, "delay", {"seconds": 0})
    chaos(admin, "failures", {"count": 0})
    caller = BookingCaller(BASE_URL)

    print("\n=== 1. Healthy service: calls succeed first time ===")
    try_book(caller, "13:00")
    try_book(caller, "13:30")

    print("\n=== 2. One transient failure: the retry succeeds ===")
    chaos(admin, "failures", {"count": 1})
    try_book(caller, "14:00")

    print("\n=== 3. Server too slow (2s delay > 1s timeout): retries time out, breaker opens ===")
    chaos(admin, "delay", {"seconds": 2})
    slow_key = str(uuid.uuid4())                     # kept so we can retry this booking later
    try_book(caller, "14:30", key=slow_key)
    try_book(caller, "15:00")
    try_book(caller, "15:30")

    print(f"\n=== 4. Recovery: fix the server, wait {BREAKER_RECOVERY_SECONDS:.0f}s, breaker lets a trial through ===")
    chaos(admin, "delay", {"seconds": 0})
    wait = BREAKER_RECOVERY_SECONDS - (time.monotonic() - caller.breaker.opened_at) + 0.2
    log(f"[demo] waiting {max(wait, 0):.1f}s for the recovery period to pass")
    time.sleep(max(wait, 0))
    try_book(caller, "15:00")
    try_book(caller, "15:30")

    print("\n=== 5. Retry the booking that 'failed' in step 3, with the SAME key ===")
    log("   (the server kept working after we timed out, so it may already exist)")
    try_book(caller, "14:30", key=slow_key)

    booked = sorted(b["slot"] for b in admin.get("/bookings").json() if b["student_ids"] == DEMO_STUDENTS)
    print(f"\nDemo bookings on the server: {booked} - each slot exactly once, no duplicates.")


if __name__ == "__main__":
    main()