"""Tests for the StudySlot HTTP API (Assignment 2, Part A).

Covers the three cases the assignment asks for (a valid request, invalid
input, and a repeated request) plus idempotency, cancelling, rate limiting,
the shared error format, and that openapi.json still matches the code.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import booking_core
import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def assert_error(response, status: int, code: str) -> dict:
    """Every error must use the same shape and carry the request ID."""
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"]
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


def bookings_total() -> float:
    return main.BOOKINGS_TOTAL._value.get()


# --- 1. Valid request -------------------------------------------------------
def test_valid_booking_returns_201_with_location(client, booking_body):
    response = client.post("/bookings", json=booking_body)

    assert response.status_code == 201
    booking = response.json()
    assert booking["room"] == "R3" and booking["slot"] == "09:00"
    assert booking["group_size"] == 5
    assert response.headers["Location"] == f"/bookings/{booking['id']}"

    # The Location really points at the booking.
    fetched = client.get(response.headers["Location"])
    assert fetched.status_code == 200
    assert fetched.json() == booking


# --- 2. Invalid input -------------------------------------------------------
@pytest.mark.parametrize("change, status, code", [
    ({"slot": "9am"},                                   422, "unknown_slot"),
    ({"room": "R9"},                                    422, "unknown_room"),
    ({"student_ids": ["s1", "s1", "s2", "s3", "s4"]},   422, "invalid_student_ids"),
    ({"student_ids": []},                               422, "validation_error"),
    ({"student_ids": ["s1", "s2"]},                     422, "group_too_small"),
    ({"unexpected_field": True},                        422, "validation_error"),
], ids=["unknown-slot", "unknown-room", "duplicate-ids", "empty-ids", "group-too-small", "extra-field"])
def test_invalid_input_is_rejected(client, booking_body, change, status, code):
    response = client.post("/bookings", json={**booking_body, **change})

    assert_error(response, status, code)
    assert client.get("/bookings").json() == []          # nothing was stored


def test_malformed_json_returns_400(client):
    response = client.post("/bookings", content="{not json",
                           headers={"Content-Type": "application/json"})
    assert_error(response, 400, "malformed_json")


def test_availability_rejects_bad_group_size(client):
    response = client.get("/availability", params={"slot": "09:00", "group_size": 0})
    assert_error(response, 422, "validation_error")


# --- 3. Repeated request ----------------------------------------------------
def test_repeat_without_key_is_409_and_creates_nothing(client, booking_body):
    first = client.post("/bookings", json=booking_body)
    second = client.post("/bookings", json=booking_body)

    assert first.status_code == 201
    assert_error(second, 409, "double_booked")
    assert len(client.get("/bookings").json()) == 1


def test_repeat_with_same_idempotency_key_replays_original(client, booking_body):
    headers = {"Idempotency-Key": "test-key-0001"}
    before = bookings_total()

    first = client.post("/bookings", json=booking_body, headers=headers)
    second = client.post("/bookings", json=booking_body, headers=headers)

    assert first.status_code == second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(client.get("/bookings").json()) == 1
    assert bookings_total() - before == 1                # the replay is not counted


def test_same_key_with_different_body_is_rejected(client, booking_body):
    headers = {"Idempotency-Key": "test-key-0002"}
    client.post("/bookings", json=booking_body, headers=headers)

    response = client.post("/bookings", json={**booking_body, "slot": "09:30"}, headers=headers)

    assert_error(response, 422, "idempotency_key_reused")
    assert len(client.get("/bookings").json()) == 1


def test_simultaneous_retries_create_exactly_one_booking(client):
    """10 threads retry the same booking with the same key at the same moment."""
    start = threading.Barrier(10)

    def attempt(_):
        start.wait()                                     # all threads go together
        return booking_core.create_booking("R4", "10:00", ["a", "b", "c"],
                                           idempotency_key="race-key-0001")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    ids = {booking["id"] for booking, _ in results}
    created = [replayed for _, replayed in results if not replayed]
    assert len(ids) == 1                                 # everyone got the same booking
    assert len(created) == 1                             # only one actually created it
    assert len(client.get("/bookings").json()) == 1


# --- Cancelling ---------------------------------------------------------------
def test_cancel_returns_204_then_404(client, booking_body):
    booking_id = client.post("/bookings", json=booking_body).json()["id"]

    first = client.delete(f"/bookings/{booking_id}")
    second = client.delete(f"/bookings/{booking_id}")

    assert first.status_code == 204 and first.content == b""
    assert_error(second, 404, "not_found")


# --- Rate limiting --------------------------------------------------------------
def test_availability_is_rate_limited(client):
    params = {"slot": "09:00"}
    accepted = [client.get("/availability", params=params) for _ in range(5)]
    refused = client.get("/availability", params=params)

    assert [r.status_code for r in accepted] == [200] * 5
    assert [r.headers["X-RateLimit-Remaining"] for r in accepted] == ["4", "3", "2", "1", "0"]
    assert_error(refused, 429, "rate_limited")
    assert 1 <= int(refused.headers["Retry-After"]) <= 10


# --- Chaos: injected failures (used by the Python caller demo) -------------------
def test_injected_failure_returns_503_then_recovers(client, booking_body):
    client.put("/chaos/failures", json={"count": 1})

    failed = client.post("/bookings", json=booking_body)
    succeeded = client.post("/bookings", json=booking_body)

    assert_error(failed, 503, "service_unavailable")
    assert succeeded.status_code == 201
    assert client.get("/chaos").json()["fail_next_bookings"] == 0


# --- Shared behaviour -----------------------------------------------------------
def test_unknown_path_uses_error_format(client):
    assert_error(client.get("/no-such-path"), 404, "not_found")


def test_request_id_from_caller_is_reused(client):
    response = client.get("/rooms", headers={"X-Request-ID": "my-trace-123"})
    assert response.headers["X-Request-ID"] == "my-trace-123"


def test_traceparent_is_continued(client):
    trace_id, caller_span = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    response = client.get("/rooms", headers={"traceparent": f"00-{trace_id}-{caller_span}-01"})
    version, returned_trace, server_span, flags = response.headers["traceparent"].split("-")
    assert returned_trace == trace_id and server_span != caller_span


def test_new_trace_is_started_when_none_is_sent(client):
    version, trace_id, span_id, flags = client.get("/rooms").headers["traceparent"].split("-")
    assert len(trace_id) == 32 and len(span_id) == 16


def test_committed_openapi_json_matches_the_code():
    """If this fails, the API changed: run  python scripts/export_openapi.py  and commit."""
    committed = json.loads((REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))
    current = json.loads(json.dumps(main.app.openapi()))
    assert committed == current
