"""Same operation, two APIs: create bookings over HTTP and gRPC side by side.

Shows that both versions use the same business rules and the same source of
truth: a slot booked over HTTP is refused over gRPC (and vice versa), and the
same invalid input is rejected by both. Prints the HTTP status and the gRPC
status for each case.

All eight calls belong to ONE W3C trace (one trace-id, a new span per call),
so searching Kibana for that trace.id shows log lines from both services.

Run from the repo root with the stack running:
    .venv\\Scripts\\python scripts/compare_http_grpc.py
"""
import random
import sys
from pathlib import Path

import grpc
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "studyslot"))
import booking_pb2        # noqa: E402
import booking_pb2_grpc   # noqa: E402

HTTP_URL = "http://localhost:8000"
GRPC_TARGET = "localhost:50051"
STUDENTS = ["cmp-1", "cmp-2", "cmp-3", "cmp-4", "cmp-5", "cmp-6"]   # R2 needs 6-12 students
TRACE_ID = f"{random.getrandbits(128):032x}"                         # one trace for the whole run


def traceparent() -> str:
    """Same trace-id every time, new span-id for each call."""
    return f"00-{TRACE_ID}-{random.getrandbits(64):016x}-01"


def via_http(http: httpx.Client, room: str, slot: str, students: list[str]) -> str:
    r = http.post("/bookings", json={"room": room, "slot": slot, "student_ids": students},
                  headers={"traceparent": traceparent()})
    detail = f"id {r.json()['id'][:8]}" if r.status_code == 201 else r.json()["error"]["code"]
    return f"HTTP {r.status_code} {detail}"


def via_grpc(stub, room: str, slot: str, students: list[str]) -> str:
    request = booking_pb2.CreateBookingRequest(room=room, slot=slot, student_ids=students)
    try:
        booking = stub.CreateBooking(request, timeout=2.0, metadata=[("traceparent", traceparent())])
        return f"gRPC OK  id {booking.id[:8]}"
    except grpc.RpcError as err:
        reason = dict(err.trailing_metadata() or ()).get("error-code", "")
        return f"gRPC {err.code().name} ({reason})"


def main() -> None:
    http = httpx.Client(base_url=HTTP_URL, timeout=5)
    try:
        http.get("/health").raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"Cannot reach {HTTP_URL}. Is the stack running? Try: docker compose up -d")

    # Clean slate: remove bookings left by an earlier run.
    for booking in http.get("/bookings").json():
        if booking["student_ids"] == STUDENTS:
            http.delete(f"/bookings/{booking['id']}")

    with grpc.insecure_channel(GRPC_TARGET) as channel:
        stub = booking_pb2_grpc.BookingServiceStub(channel)
        cases = [
            ("1. Book R2 12:00 over HTTP (success)",               lambda: via_http(http, "R2", "12:00", STUDENTS)),
            ("2. Same slot over gRPC (already booked by HTTP)",    lambda: via_grpc(stub, "R2", "12:00", STUDENTS)),
            ("3. Book R2 12:30 over gRPC (success)",               lambda: via_grpc(stub, "R2", "12:30", STUDENTS)),
            ("4. Same slot over HTTP (already booked by gRPC)",    lambda: via_http(http, "R2", "12:30", STUDENTS)),
            ("5. Unknown room R9 over HTTP",                       lambda: via_http(http, "R9", "13:00", STUDENTS)),
            ("6. Unknown room R9 over gRPC",                       lambda: via_grpc(stub, "R9", "13:00", STUDENTS)),
            ("7. Group too small for R2 over HTTP",                lambda: via_http(http, "R2", "13:00", STUDENTS[:2])),
            ("8. Group too small for R2 over gRPC",                lambda: via_grpc(stub, "R2", "13:00", STUDENTS[:2])),
        ]
        for title, run in cases:
            print(f"{title:<52} -> {run()}")
    print(f"\nAll calls share trace.id {TRACE_ID}")
    print(f'Kibana: search  trace.id : "{TRACE_ID}"  to see the logs from both services.')


if __name__ == "__main__":
    main()
