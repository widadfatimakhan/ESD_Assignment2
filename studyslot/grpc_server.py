"""StudySlot gRPC server (Assignment 2, Part B).

Runs in its own container (studyslot-grpc) on port 50051. It serves ONE
operation, CreateBooking, defined in protos/booking.proto.

The handler calls booking_core.create_booking(), exactly like POST /bookings
in main.py, and that writes to the same SQLite file through the shared Docker
volume. So the gRPC and HTTP versions share their business rules and their
source of truth; this is NOT a wrapper that forwards calls to the HTTP API.

Generated code used here (from booking.proto, see the generation command there):
  booking_pb2.py       - the message classes (CreateBookingRequest, Booking)
  booking_pb2_grpc.py  - BookingServiceServicer (base class for our handler)
                         and add_BookingServiceServicer_to_server()
"""
import os
import re
import signal
import uuid
from concurrent import futures

import grpc

import booking_core
import booking_pb2
import booking_pb2_grpc
import db
from logging_setup import log_event, request_id_var, trace_context_var
from tracing import child_span

GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))

# Which gRPC status each kind of booking-rule problem becomes.
# Anything not listed is INVALID_ARGUMENT (the gRPC equivalent of HTTP 422).
GRPC_STATUS_FOR = {
    booking_core.AlreadyBooked: grpc.StatusCode.ALREADY_EXISTS,      # HTTP 409
    booking_core.BookingNotFound: grpc.StatusCode.NOT_FOUND,         # HTTP 404
}


def grpc_status_for(err: booking_core.BookingError) -> grpc.StatusCode:
    return GRPC_STATUS_FOR.get(type(err), grpc.StatusCode.INVALID_ARGUMENT)


# ---------------------------------------------------------------------------
# The handler: implements the service defined in booking.proto
# ---------------------------------------------------------------------------
class BookingService(booking_pb2_grpc.BookingServiceServicer):

    def CreateBooking(self, request: booking_pb2.CreateBookingRequest, context: grpc.ServicerContext):
        student_ids = list(request.student_ids)
        try:
            booking, replayed = booking_core.create_booking(
                request.room, request.slot, student_ids,
                idempotency_key=request.idempotency_key or None,   # "" means "no key"
            )
        except booking_core.BookingError as err:
            code = grpc_status_for(err)
            log_event("booking rejected", level="WARNING", rpc="CreateBooking",
                      room=request.room, slot=request.slot, group_size=len(student_ids),
                      grpc_code=code.name, reason=err.reason)
            # The machine-readable reason travels in trailing metadata, the
            # human-readable message as the status details.
            context.set_trailing_metadata((("error-code", err.reason),))
            context.abort(code, err.message)
        except Exception as exc:                       # anything unexpected
            log_event("unhandled error", level="ERROR", rpc="CreateBooking", error=repr(exc))
            context.abort(grpc.StatusCode.INTERNAL, "Something went wrong on our side.")

        log_event("booking replayed" if replayed else "booking confirmed", level="INFO",
                  rpc="CreateBooking", booking_id=booking["id"], room=booking["room"],
                  slot=booking["slot"], group_size=booking["group_size"], grpc_code="OK",
                  idempotent_replay=replayed)
        return booking_pb2.Booking(
            id=booking["id"],
            room=booking["room"],
            slot=booking["slot"],
            student_ids=booking["student_ids"],
            group_size=booking["group_size"],
            created_at=booking["created_at"],
            replayed=replayed,
        )


# ---------------------------------------------------------------------------
# Trace correlation across the gRPC boundary
# gRPC has no HTTP headers of its own; the equivalent is call *metadata*.
# The client sends "traceparent" (W3C Trace Context, as in the course lab) and
# "x-request-id" there. This interceptor runs before the handler: it continues
# the caller's trace with a new span, puts both IDs in the logging context so
# every log line for this call carries them, and sends them back to the client
# in the response metadata.
# ---------------------------------------------------------------------------
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class TracingInterceptor(grpc.ServerInterceptor):

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler

        metadata = dict(handler_call_details.invocation_metadata or ())
        incoming_request_id = metadata.get("x-request-id", "")
        incoming_traceparent = metadata.get("traceparent")
        original = handler.unary_unary

        def traced(request, context):
            request_id = (incoming_request_id if _SAFE_REQUEST_ID.match(incoming_request_id)
                          else uuid.uuid4().hex[:16])
            span = child_span(incoming_traceparent)
            context.send_initial_metadata((
                ("x-request-id", request_id),
                ("traceparent", span["traceparent"]),
            ))
            request_token = request_id_var.set(request_id)
            trace_token = trace_context_var.set(span)
            try:
                return original(request, context)
            finally:
                request_id_var.reset(request_token)
                trace_context_var.reset(trace_token)

        return grpc.unary_unary_rpc_method_handler(
            traced,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


# ---------------------------------------------------------------------------
# Server start-up
# ---------------------------------------------------------------------------
def create_server(address: str = f"[::]:{GRPC_PORT}") -> tuple[grpc.Server, int]:
    """Build the server (not started yet). Returns it and the port it bound."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[TracingInterceptor()],
    )
    booking_pb2_grpc.add_BookingServiceServicer_to_server(BookingService(), server)
    port = server.add_insecure_port(address)
    return server, port


def serve() -> None:
    db.init_db()
    server, port = create_server()
    server.start()
    log_event("grpc server started", level="INFO", port=port)

    # "docker compose stop" sends SIGTERM: finish in-flight calls (up to 5 s), then exit.
    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
