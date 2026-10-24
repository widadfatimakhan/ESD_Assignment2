"""Tests for the Part C metrics: correct values, and labels that stay bounded."""
import grpc
from prometheus_client import REGISTRY

import booking_pb2
import booking_pb2_grpc
import grpc_server

SERVICE = "studyslot.v1.BookingService"


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- HTTP ------------------------------------------------------------------------
def test_http_requests_are_counted_by_route_template(client, booking_body):
    labels = {"method": "POST", "route": "/bookings"}
    created_before = sample("http_requests_total", **labels, status="201")
    conflict_before = sample("http_requests_total", **labels, status="409")
    timed_before = sample("http_request_duration_seconds_count", **labels)

    client.post("/bookings", json=booking_body)
    client.post("/bookings", json=booking_body)          # 409

    assert sample("http_requests_total", **labels, status="201") - created_before == 1
    assert sample("http_requests_total", **labels, status="409") - conflict_before == 1
    assert sample("http_request_duration_seconds_count", **labels) - timed_before == 2


def test_ids_in_paths_do_not_become_labels(client, booking_body):
    booking_id = client.post("/bookings", json=booking_body).json()["id"]
    before = sample("http_requests_total", method="GET", route="/bookings/{booking_id}", status="200")

    client.get(f"/bookings/{booking_id}")

    after = sample("http_requests_total", method="GET", route="/bookings/{booking_id}", status="200")
    assert after - before == 1
    assert booking_id not in client.get("/metrics").text  # the real ID never appears


def test_unknown_paths_share_one_label(client):
    before = sample("http_requests_total", method="GET", route="unmatched", status="404")
    client.get("/random-1")
    client.get("/random-2")
    assert sample("http_requests_total", method="GET", route="unmatched", status="404") - before == 2


def test_scrapes_are_not_counted(client):
    client.get("/metrics")
    assert 'route="/metrics"' not in client.get("/metrics").text


def test_http_in_flight_returns_to_zero(client, booking_body):
    client.post("/bookings", json=booking_body)
    assert sample("http_requests_in_flight", method="POST", route="/bookings") == 0


def test_replays_are_counted_separately_from_bookings(client, booking_body):
    headers = {"Idempotency-Key": "metrics-key-01"}
    bookings_before = sample("studyslot_bookings_total")
    replays_before = sample("studyslot_idempotent_replays_total")

    client.post("/bookings", json=booking_body, headers=headers)
    client.post("/bookings", json=booking_body, headers=headers)

    assert sample("studyslot_bookings_total") - bookings_before == 1
    assert sample("studyslot_idempotent_replays_total") - replays_before == 1


# --- gRPC ------------------------------------------------------------------------
def test_grpc_calls_are_counted_by_status_code(client):
    server, port = grpc_server.create_server("127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = booking_pb2_grpc.BookingServiceStub(channel)
            labels = {"grpc_service": SERVICE, "grpc_method": "CreateBooking"}
            ok_before = sample("grpc_server_handled_total", **labels, grpc_code="OK")
            exists_before = sample("grpc_server_handled_total", **labels, grpc_code="ALREADY_EXISTS")
            invalid_before = sample("grpc_server_handled_total", **labels, grpc_code="INVALID_ARGUMENT")
            timed_before = sample("grpc_server_handling_seconds_count", **labels)

            good = booking_pb2.CreateBookingRequest(room="R4", slot="10:00", student_ids=["a", "b", "c"])
            bad = booking_pb2.CreateBookingRequest(room="R9", slot="10:00", student_ids=["a"])
            stub.CreateBooking(good, timeout=2)
            for request in (good, bad):                    # ALREADY_EXISTS, INVALID_ARGUMENT
                try:
                    stub.CreateBooking(request, timeout=2)
                except grpc.RpcError:
                    pass

            assert sample("grpc_server_handled_total", **labels, grpc_code="OK") - ok_before == 1
            assert sample("grpc_server_handled_total", **labels, grpc_code="ALREADY_EXISTS") - exists_before == 1
            assert sample("grpc_server_handled_total", **labels, grpc_code="INVALID_ARGUMENT") - invalid_before == 1
            assert sample("grpc_server_handling_seconds_count", **labels) - timed_before == 3
            assert sample("grpc_server_in_flight_requests", **labels) == 0
    finally:
        server.stop(grace=None)
