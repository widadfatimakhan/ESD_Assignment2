"""W3C Trace Context ("traceparent") helpers, shared by the HTTP and gRPC servers.

Same approach as the course's Week 1 lab: a caller sends
    traceparent: 00-<trace-id: 32 hex>-<parent span-id: 16 hex>-<flags: 2 hex>
Each service keeps the trace-id, starts its own span (a new span-id), logs
both, and passes "00-<trace-id>-<its span-id>-<flags>" onwards / back. Every
log line from every service that touched one operation then shares the same
trace.id, which is what lets Kibana show the whole journey.
"""
import random
import re

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def new_trace_id() -> str:
    return f"{random.getrandbits(128):032x}"


def new_span_id() -> str:
    return f"{random.getrandbits(64):016x}"


def child_span(incoming: str | None) -> dict:
    """Continue the caller's trace if it sent a valid traceparent, else start a new one."""
    match = _TRACEPARENT.match((incoming or "").strip().lower())
    if match and match.group(1) != "0" * 32:
        trace_id, parent_span_id, flags = match.groups()
    else:
        trace_id, parent_span_id, flags = new_trace_id(), None, "01"
    span_id = new_span_id()
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "traceparent": f"00-{trace_id}-{span_id}-{flags}",
    }
