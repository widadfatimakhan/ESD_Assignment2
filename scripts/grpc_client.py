"""Command-line client for the StudySlot gRPC service (Assignment 2, Part B).

Calls BookingService.CreateBooking directly over gRPC, through the stub that
grpc_tools generated from studyslot/protos/booking.proto. No browser, no HTTP.

Trace correlation: the client starts a W3C trace and sends it as "traceparent"
metadata (plus an "x-request-id"). The server logs the same trace.id, so the
printed trace ID can be searched for in Kibana.

Examples (from the repo root, with the stack running):
  .venv\\Scripts\\python scripts/grpc_client.py --room R4 --slot 10:00 --students s1 s2 s3
  .venv\\Scripts\\python scripts/grpc_client.py --room R4 --slot 10:00 --students s1 s2 s3 --key my-key-0001
  .venv\\Scripts\\python scripts/grpc_client.py --room R9 --slot 10:00 --students s1          (INVALID_ARGUMENT)
  .venv\\Scripts\\python scripts/grpc_client.py --room R4 --slot 10:30 --students s1 s2 s3 --deadline 0.001   (DEADLINE_EXCEEDED)
"""
import argparse
import random
import re
import sys
import uuid
from pathlib import Path

import grpc

# The generated code lives in studyslot/ next to the server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "studyslot"))
import booking_pb2        # noqa: E402  generated message classes
import booking_pb2_grpc   # noqa: E402  generated client stub


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Book a StudySlot room over gRPC.")
    parser.add_argument("--room", required=True, help="R1 to R5")
    parser.add_argument("--slot", required=True, help="09:00 to 16:30")
    parser.add_argument("--students", required=True, nargs="+", help="student IDs, separated by spaces")
    parser.add_argument("--key", default="", help="optional idempotency key (8-64 chars) for safe retries")
    parser.add_argument("--deadline", type=float, default=2.0,
                        help="seconds to wait before giving up (default 2.0)")
    parser.add_argument("--trace-id", default=None,
                        help="32 hex characters to use as the W3C trace-id (default: a new random one)")
    parser.add_argument("--target", default="localhost:50051", help="server address (default localhost:50051)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.trace_id and not re.fullmatch(r"[0-9a-f]{32}", args.trace_id):
        sys.exit("--trace-id must be exactly 32 lowercase hex characters (0-9, a-f)")
    trace_id = args.trace_id or f"{random.getrandbits(128):032x}"
    client_span_id = f"{random.getrandbits(64):016x}"
    traceparent = f"00-{trace_id}-{client_span_id}-01"
    request_id = uuid.uuid4().hex[:16]
    request = booking_pb2.CreateBookingRequest(
        room=args.room,
        slot=args.slot,
        student_ids=args.students,
        idempotency_key=args.key,
    )
    print(f"-> CreateBooking room={args.room} slot={args.slot} students={args.students}"
          f"{' key=' + args.key if args.key else ''}")
    print(f"   target={args.target}  deadline={args.deadline}s")
    print(f"   traceparent={traceparent}  x-request-id={request_id}")

    with grpc.insecure_channel(args.target) as channel:
        stub = booking_pb2_grpc.BookingServiceStub(channel)
        try:
            booking, call = stub.CreateBooking.with_call(
                request,
                timeout=args.deadline,                       # the deadline
                metadata=[("traceparent", traceparent),      # trace context across the boundary
                          ("x-request-id", request_id)],
            )
        except grpc.RpcError as err:
            trailing = dict(err.trailing_metadata() or ())
            print(f"<- FAILED  status={err.code().name}")
            print(f"   details: {err.details()}")
            if "error-code" in trailing:
                print(f"   error-code: {trailing['error-code']}")
            print(f"   trace.id: {trace_id}  (search Kibana for  trace.id : \"{trace_id}\")")
            return 1

    server_metadata = dict(call.initial_metadata() or ())
    print(f"<- OK  status={call.code().name}{'  (REPLAYED)' if booking.replayed else ''}")
    print(f"   booking id={booking.id} room={booking.room} slot={booking.slot} "
          f"group_size={booking.group_size}")
    print(f"   server traceparent: {server_metadata.get('traceparent')}  (same trace-id, server's own span)")
    print(f"   trace.id: {trace_id}  (search Kibana for  trace.id : \"{trace_id}\")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
