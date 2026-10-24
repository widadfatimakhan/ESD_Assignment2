"""Repeatable traffic for the Part C dashboards: HTTP and gRPC, success and failure.

Two phases:
  1. steady (--seconds, default 60): a few workers per API, pausing --pause
     seconds between rounds - normal load, so latency stays low.
  2. burst (--burst-seconds, default 30): more workers, no pause - many calls
     overlap, so the in-flight gauges and p95 latency visibly rise.
Several worker threads per API each own one
room+slot, so workers never collide by accident. Every round a worker does:

    create a booking (with an idempotency key)   -> success (201 / OK)
    repeat it with the same key                  -> success, replayed
    every 4th round also:
      create the same slot again without a key   -> failure (409 / ALREADY_EXISTS)
      send an invalid room                       -> failure (422 / INVALID_ARGUMENT)
    cancel the booking (HTTP DELETE)             -> 204, frees the slot again

So the expected mix for POST /bookings and CreateBooking is ~80% success and
~20% client errors, plus steady replays. Run it again any time: it removes
its own leftovers first.

Run from the repo root with the stack up:
    .venv\\Scripts\\python scripts/generate_traffic.py
    .venv\\Scripts\\python scripts/generate_traffic.py --seconds 30 --burst-seconds 0   (steady only)
"""
import argparse
import random
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import grpc
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "studyslot"))
import booking_pb2        # noqa: E402
import booking_pb2_grpc   # noqa: E402

HTTP_URL = "http://localhost:8000"
GRPC_TARGET = "localhost:50051"
SLOTS = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
         "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30"]
STUDENTS = ["trf-1", "trf-2", "trf-3"]                 # marks this script's bookings

results: Counter = Counter()
results_lock = threading.Lock()


def record(api: str, outcome: str) -> None:
    with results_lock:
        results[(api, outcome)] += 1


def traceparent() -> str:
    return f"00-{random.getrandbits(128):032x}-{random.getrandbits(64):016x}-01"


PAUSE = 0.2   # seconds between rounds; set from --pause


def http_worker(room: str, slot: str, stop_at: float) -> None:
    http = httpx.Client(base_url=HTTP_URL, timeout=5)
    body = {"room": room, "slot": slot, "student_ids": STUDENTS}
    round_no = 0
    while time.monotonic() < stop_at:
        round_no += 1
        try:
            http_round(http, body, round_no)
        except httpx.HTTPError:                # server down or restarting: note it, back off
            record("HTTP", "connection error")
            time.sleep(1)
        time.sleep(PAUSE)


def http_round(http: httpx.Client, body: dict, round_no: int) -> None:
    """One round: book, replay, (every 4th round) two failures, then cancel."""
    key = uuid.uuid4().hex
    tp = {"traceparent": traceparent()}
    r = http.post("/bookings", json=body, headers={**tp, "Idempotency-Key": key})
    record("HTTP", str(r.status_code))
    booking_id = r.json().get("id") if r.status_code == 201 else None
    r = http.post("/bookings", json=body, headers={**tp, "Idempotency-Key": key})
    record("HTTP", f"{r.status_code} replay" if r.headers.get("Idempotent-Replayed") else str(r.status_code))
    if round_no % 4 == 0:
        record("HTTP", str(http.post("/bookings", json=body, headers=tp).status_code))
        record("HTTP", str(http.post("/bookings", json={**body, "room": "R9"}, headers=tp).status_code))
    if booking_id:
        http.delete(f"/bookings/{booking_id}", headers=tp)


def grpc_worker(room: str, slot: str, stop_at: float) -> None:
    http = httpx.Client(base_url=HTTP_URL, timeout=5)      # only for cancelling
    with grpc.insecure_channel(GRPC_TARGET) as channel:
        stub = booking_pb2_grpc.BookingServiceStub(channel)

        def call(request) -> tuple[str, str | None]:
            try:
                booking = stub.CreateBooking(request, timeout=5, metadata=[("traceparent", traceparent())])
                return ("OK replay" if booking.replayed else "OK"), booking.id
            except grpc.RpcError as err:
                if err.code() == grpc.StatusCode.UNAVAILABLE:   # server down: back off
                    time.sleep(1)
                return err.code().name, None

        round_no = 0
        while time.monotonic() < stop_at:
            round_no += 1
            key = uuid.uuid4().hex
            request = booking_pb2.CreateBookingRequest(room=room, slot=slot, student_ids=STUDENTS,
                                                       idempotency_key=key)
            outcome, booking_id = call(request)
            record("gRPC", outcome)
            record("gRPC", call(request)[0])
            if round_no % 4 == 0:
                no_key = booking_pb2.CreateBookingRequest(room=room, slot=slot, student_ids=STUDENTS)
                bad = booking_pb2.CreateBookingRequest(room="R9", slot=slot, student_ids=STUDENTS)
                record("gRPC", call(no_key)[0])
                record("gRPC", call(bad)[0])
            if booking_id:
                try:
                    http.delete(f"/bookings/{booking_id}")
                except httpx.HTTPError:
                    pass                           # HTTP API down; the next run cleans up
            time.sleep(PAUSE)


def run_phase(name: str, workers: int, pause: float, seconds: int) -> None:
    """Start `workers` threads per API (HTTP on R4, gRPC on R5, one slot each) for `seconds`."""
    global PAUSE
    PAUSE = pause
    stop_at = time.monotonic() + seconds
    threads = [threading.Thread(target=http_worker, args=("R4", SLOTS[i], stop_at)) for i in range(workers)]
    threads += [threading.Thread(target=grpc_worker, args=("R5", SLOTS[i], stop_at)) for i in range(workers)]
    print(f"[{time.strftime('%H:%M:%S')}] {name}: {workers} workers per API, "
          f"{pause}s pause, {seconds}s...")
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HTTP + gRPC booking traffic.")
    parser.add_argument("--seconds", type=int, default=60, help="length of the steady phase")
    parser.add_argument("--workers", type=int, default=4, help="workers per API in the steady phase")
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between rounds (steady phase)")
    parser.add_argument("--burst-seconds", type=int, default=30, help="length of the burst phase (0 = skip)")
    parser.add_argument("--burst-workers", type=int, default=12, help="workers per API in the burst phase")
    args = parser.parse_args()

    http = httpx.Client(base_url=HTTP_URL, timeout=5)
    try:
        bookings = http.get("/bookings").json()
    except httpx.HTTPError:
        sys.exit(f"Cannot reach {HTTP_URL}. Is the stack running? Try: docker compose up -d")
    for booking in bookings:                                   # leftovers from an earlier run
        if booking["student_ids"] == STUDENTS:
            http.delete(f"/bookings/{booking['id']}")

    run_phase("steady", max(1, min(args.workers, 16)), max(0.0, args.pause), args.seconds)
    if args.burst_seconds > 0:
        run_phase("burst", max(1, min(args.burst_workers, 16)), 0.0, args.burst_seconds)
    print(f"[{time.strftime('%H:%M:%S')}] done")

    print("\nResults (what the dashboards should show):")
    for api in ("HTTP", "gRPC"):
        total = sum(n for (a, _), n in results.items() if a == api)
        print(f"  {api}: {total} calls")
        for (a, outcome), n in sorted(results.items()):
            if a == api:
                print(f"    {outcome:<22} {n:>6}  ({100 * n / total:4.1f}%)")


if __name__ == "__main__":
    main()
