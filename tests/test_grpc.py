"""Tests for the gRPC service (Assignment 2, Part B).

Starts the real gRPC server in-process on a free port, using the same test
database as the HTTP tests, and calls it through the generated stub.
"""
import io
import logging

import grpc
import pytest

import booking_pb2
import booking_pb2_grpc
import grpc_server
import logging_setup


@pytest.fixture
def stub(client):                      # `client` empties the database first
    server, port = grpc_server.create_server("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield booking_pb2_grpc.BookingServiceStub(channel)
    channel.close()
    server.stop(grace=None)


def request(room="R3", slot="09:00", students=("s1", "s2", "s3", "s4", "s5"), key=""):
    return booking_pb2.CreateBookingRequest(room=room, slot=slot, student_ids=list(students),
                                            idempotency_key=key)


def rpc_error(call) -> grpc.RpcError:
    with pytest.raises(grpc.RpcError) as info:
        call()
    return info.value


def test_create_booking_ok(stub):
    booking = stub.CreateBooking(request(), timeout=2)
    assert booking.room == "R3" and booking.slot == "09:00"
    assert booking.group_size == 5 and booking.id and not booking.replayed


def test_double_booking_is_already_exists(stub):
    stub.CreateBooking(request(), timeout=2)
    err = rpc_error(lambda: stub.CreateBooking(request(), timeout=2))
    assert err.code() == grpc.StatusCode.ALREADY_EXISTS
    assert dict(err.trailing_metadata())["error-code"] == "double_booked"


@pytest.mark.parametrize("bad, reason", [
    ({"room": "R9"},                                   "unknown_room"),
    ({"slot": "9am"},                                  "unknown_slot"),
    ({"students": ()},                                 "no_student_ids"),
    ({"students": ("s1", "s1", "s2", "s3", "s4")},     "invalid_student_ids"),
    ({"students": ("s1", "has space", "s3", "s4", "s5")}, "invalid_student_ids"),
    ({"students": ("s1", "s2")},                       "group_too_small"),
    ({"key": "short"},                                 "invalid_idempotency_key"),
], ids=["unknown-room", "unknown-slot", "no-students", "duplicate-ids", "malformed-id",
        "group-too-small", "bad-key"])
def test_invalid_input_is_invalid_argument(stub, bad, reason):
    err = rpc_error(lambda: stub.CreateBooking(request(**bad), timeout=2))
    assert err.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert dict(err.trailing_metadata())["error-code"] == reason


def test_idempotent_retry_replays(stub):
    first = stub.CreateBooking(request(key="grpc-key-0001"), timeout=2)
    second = stub.CreateBooking(request(key="grpc-key-0001"), timeout=2)
    assert second.id == first.id and second.replayed and not first.replayed


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CLIENT_SPAN = "00f067aa0ba902b7"


def test_trace_context_crosses_the_boundary(stub):
    _, call = stub.CreateBooking.with_call(
        request(), timeout=2,
        metadata=[("traceparent", f"00-{TRACE_ID}-{CLIENT_SPAN}-01"), ("x-request-id", "req-abc-123")],
    )
    echoed = dict(call.initial_metadata())
    _, trace_id, server_span, _ = echoed["traceparent"].split("-")
    assert trace_id == TRACE_ID                     # same trace...
    assert server_span != CLIENT_SPAN               # ...but the server's own span
    assert echoed["x-request-id"] == "req-abc-123"


def test_server_logs_carry_the_callers_trace_id(stub):
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)          # listen to the JSON log lines
    logging_setup._logger.addHandler(handler)
    try:
        stub.CreateBooking(request(), timeout=2,
                           metadata=[("traceparent", f"00-{TRACE_ID}-{CLIENT_SPAN}-01")])
    finally:
        logging_setup._logger.removeHandler(handler)
    logs = captured.getvalue()
    assert f'"trace":{{"id":"{TRACE_ID}"}}' in logs
    assert f'"parent_id":"{CLIENT_SPAN}"' in logs


# --- Same source of truth as the HTTP API -----------------------------------
def test_grpc_booking_is_visible_over_http(stub, client):
    booking = stub.CreateBooking(request(), timeout=2)
    assert client.get(f"/bookings/{booking.id}").status_code == 200


def test_http_booking_blocks_grpc_booking(stub, client, booking_body):
    assert client.post("/bookings", json=booking_body).status_code == 201
    err = rpc_error(lambda: stub.CreateBooking(request(), timeout=2))
    assert err.code() == grpc.StatusCode.ALREADY_EXISTS


def test_idempotency_key_is_shared_between_apis(stub, client, booking_body):
    http = client.post("/bookings", json=booking_body, headers={"Idempotency-Key": "shared-key-01"})
    grpc_booking = stub.CreateBooking(request(key="shared-key-01"), timeout=2)
    assert grpc_booking.replayed and grpc_booking.id == http.json()["id"]
