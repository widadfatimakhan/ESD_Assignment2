# StudySlot (ESD Assignments)

A campus study-room booking service (Python + FastAPI) with a gRPC version of
its booking operation, instrumented end to end with Prometheus + Grafana
(metrics) and Filebeat + Elasticsearch + Kibana (logs), all run with Docker Compose.

- **Assignment 2** (HTTP APIs, gRPC, metrics): [REPORT.md](REPORT.md)
- **Assignment 1** (observability): [docs/a1/REPORT.md](docs/a1/REPORT.md)

## What it does
Students book a limited group study room for a time slot, listing their group's
student IDs. The service enforces room size limits and prevents double-booking,
and librarians can see live occupancy. Bookings can be made over HTTP
(`POST /bookings`) or gRPC (`BookingService.CreateBooking`). Both use the same
rules (`studyslot/booking_core.py`) and the same SQLite database (Docker volume
`studyslot-data`).

| Container | Port(s) | Role |
|---|---|---|
| `studyslot-app` | 8000 | HTTP API (FastAPI), its `/metrics` |
| `studyslot-grpc` | 50051 (gRPC), 9101 (`/metrics`) | gRPC `CreateBooking` |
| `studyslot-prometheus` | 9090 | Scrapes both services and node-exporter |
| `studyslot-grafana` | 3000 | Dashboards (admin / admin) |
| `studyslot-elasticsearch`, `studyslot-kibana`, `studyslot-filebeat` | 9200, 5601 | Logs |
| `studyslot-node-exporter` | 9100 | Machine metrics |

## Requirements
- Docker Desktop with Docker Compose v2 (`docker compose version`)
- About 6 GB free RAM (Elasticsearch is the heavy component)
- Python 3.13, for the tests and the client scripts

## Start
From the repo root:
```powershell
docker compose up -d --build
docker compose ps
```
All 8 services should be up. If Grafana exited on first start (a known
Grafana 10.2 start-up race), run `docker compose start grafana`.

One-time setup for the tests and client scripts (Windows; on macOS/Linux use
`.venv/bin/python`):
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

## Use it
| URL | What |
|-----|------|
| http://localhost:8000/docs | Swagger UI: book, cancel, check availability |
| http://localhost:8000/redoc | API reference (ReDoc), generated from the OpenAPI spec |
| http://localhost:8000/metrics | HTTP service metrics |
| http://localhost:9101/metrics | gRPC service metrics |
| http://localhost:9090/targets | Prometheus targets (`studyslot`, `studyslot-grpc`, `node-exporter` should be UP) |
| http://localhost:3000 | Grafana: **StudySlot A2: HTTP vs gRPC (create booking)** and **StudySlot Overview** |
| http://localhost:5601/app/discover | Kibana: data view `studyslot-logs-*`, timestamp `@timestamp` |

Make a booking: on `/docs`, `POST /bookings` with
`{ "room": "R3", "slot": "09:00", "student_ids": ["s1","s2","s3","s4","s5"] }`.
It returns `201 Created` with the booking (including its `id`) and a `Location`
header. Cancel it with `DELETE /bookings/{id}`. Send an `Idempotency-Key`
header to make retries safe.

## Test
```powershell
.venv\Scripts\python -m pytest -v
```
The tests run the real app in-process against a throwaway database, so Docker
does not need to be running.

## Demos (stack running)
| Command | Shows |
|---|---|
| `python scripts/rate_limit_demo.py` | `GET /availability`: 5 accepted calls, then `429` + `Retry-After`, then accepted again |
| `.venv\Scripts\python scripts/booking_caller.py` | Resilient caller: timeout, retry with backoff, circuit breaker opening and recovering |
| `.venv\Scripts\python scripts/grpc_client.py --room R4 --slot 10:00 --students s1 s2 s3` | gRPC call (options `--key`, `--deadline`, `--trace-id`) |
| `.venv\Scripts\python scripts/compare_http_grpc.py` | Same operation over HTTP and gRPC, one shared trace |
| `.venv\Scripts\python scripts/generate_traffic.py` | 60 s steady + 30 s burst of HTTP and gRPC traffic for the dashboards |
| `python scripts/export_openapi.py` | Re-export `openapi.json` after changing the API |

Chaos switches (from Swagger UI): `PUT /chaos/delay {"seconds": 0.5}`,
`PUT /chaos/failures {"count": 3}` (the next 3 bookings fail with 503),
`PUT /chaos/cardinality {"active": true}`, and `GET /chaos` for the current state.

## gRPC
- Contract: `studyslot/protos/booking.proto`
- Generated code: `studyslot/booking_pb2.py`, `booking_pb2.pyi`, `booking_pb2_grpc.py` (committed; do not edit)
- Regenerate after changing the `.proto`:
  ```powershell
  .venv\Scripts\python -m grpc_tools.protoc -I studyslot/protos --python_out=studyslot --pyi_out=studyslot --grpc_python_out=studyslot studyslot/protos/booking.proto
  ```
- Call paths: [docs/a2/call-paths.md](docs/a2/call-paths.md)

## Tracing
Both services accept a W3C `traceparent` (HTTP header / gRPC metadata), keep its
trace-id, start their own span, and log `trace.id`, `span.id` and
`span.parent_id`. Search Kibana for `trace.id : "<id>"` to see one operation
across both services.

## Stop and clean up
- Pause (keeps everything): `docker compose stop`
- Remove containers, keep the bookings volume: `docker compose down`
- Remove everything, **including stored bookings**: `docker compose down -v`

## Metrics summary
| Metric | Type | Labels |
|---|---|---|
| `http_requests_total` | Counter | `method`, `route`, `status` |
| `http_request_duration_seconds` | Histogram | `method`, `route` |
| `http_requests_in_flight` | Gauge | `method`, `route` |
| `grpc_server_handled_total` | Counter | `grpc_service`, `grpc_method`, `grpc_code` |
| `grpc_server_handling_seconds` | Histogram | `grpc_service`, `grpc_method` |
| `grpc_server_in_flight_requests` | Gauge | `grpc_service`, `grpc_method` |
| `studyslot_bookings_total` | Counter | none (per job) |
| `studyslot_idempotent_replays_total` | Counter | none (per job) |
| `studyslot_rooms_occupied` | Gauge | none |
| `studyslot_availability_check_seconds` | Histogram | none (A1) |
| `node_*` | Node Exporter | machine CPU/memory/disk/network |

## Credits
Built following the conventions of the course Week 1 lab (Midnight Launch): the
same tool stack, JSON logging style, W3C `traceparent` propagation, and
Prometheus/Grafana provisioning approach.
