# StudySlot (ESD Assignments)

A campus study-room booking service (Python + FastAPI) instrumented end to end
with Prometheus + Grafana (metrics) and Filebeat + Elasticsearch + Kibana
(logs), all run with Docker Compose.

- **Assignment 2** (HTTP APIs, gRPC, metrics): report in `REPORT.md` (in progress)
- **Assignment 1** (observability): [docs/a1/REPORT.md](docs/a1/REPORT.md)

## What it does
Students book a limited group study room for a time slot, listing their group's
student IDs; the service enforces room capacity limits and prevents
double-booking, and lets librarians see live occupancy. Every booking is
measured (metrics) and logged (structured JSON).

## Requirements
- Docker Desktop with Docker Compose v2 (`docker compose version`)
- ~6 GB free RAM (Elasticsearch is the heavy component)

## Start
From the repo root:
docker compose up -d --build
Check everything is up:
docker compose ps

## Use it
| URL | What |
|-----|------|
| http://localhost:8000/docs | StudySlot API (Swagger UI) — book rooms, cancel, view availability |
| http://localhost:8000/metrics | Raw Prometheus metrics |
| http://localhost:9090/targets | Prometheus scrape targets (should be UP) |
| http://localhost:3000 | Grafana dashboards (login admin / admin) → dashboard "StudySlot Overview" |
| http://localhost:5601/app/discover | Kibana — search logs (data view `studyslot-logs-*`) |
| http://localhost:8000/redoc | API reference (ReDoc), generated from the OpenAPI spec |
| `openapi.json` | Exported OpenAPI 3.1 spec. Regenerate: `python scripts/export_openapi.py` |

Make a booking: on `/docs`, `POST /bookings` with e.g.
`{ "room": "R3", "slot": "09:00", "student_ids": ["s1","s2","s3","s4","s5"] }`.
It returns `201 Created` with the booking (including its `id`) and a `Location`
header. Cancel it with `DELETE /bookings/{id}`.

## Test / reproduce the experiments
- **Injected latency (A1 Part E1):** `PUT /chaos/delay` with `{"seconds": 0.5}`,
  make some bookings, watch Grafana's p95 panel spike; `PUT /chaos/delay` with
  `{"seconds": 0}` to recover.
- **Cardinality (A1 Part E2):** `PUT /chaos/cardinality` with `{"active": true}`,
  make ~15–20 bookings, run `count(studyslot_requests_by_id_total)` in Prometheus
  and watch series grow; `PUT /chaos/cardinality` with `{"active": false}` to stop.
- Check current chaos state: `GET /chaos`.
- **Injected outage (A2):** `PUT /chaos/failures` with `{"count": 3}` makes the next 3
  `POST /bookings` calls return 503; `{"count": 0}` turns it off.
- **Python caller demo (A2):** `.venv\Scripts\python scripts/booking_caller.py`
  shows timeout, retry with backoff, and the circuit breaker opening and recovering.
- **Rate-limit demo (A2):** `python scripts/rate_limit_demo.py`
- **Tests:** `.venv\Scripts\python -m pytest -v`

## gRPC service (Assignment 2, Part B)
`studyslot-grpc` serves `BookingService.CreateBooking` on port 50051, using the
same rules (`booking_core.py`) and database as the HTTP API.

- Contract: `studyslot/protos/booking.proto`
- Generate code (after changing the .proto):
  `.venv\Scripts\python -m grpc_tools.protoc -I studyslot/protos --python_out=studyslot --pyi_out=studyslot --grpc_python_out=studyslot studyslot/protos/booking.proto`
- Call it: `.venv\Scripts\python scripts/grpc_client.py --room R4 --slot 10:00 --students s1 s2 s3`
  (options: `--key`, `--deadline`, `--trace-id`)
- HTTP vs gRPC side by side, one shared trace: `.venv\Scripts\python scripts/compare_http_grpc.py`
- Both servers accept W3C `traceparent` (header / metadata) and log `trace.id`.
- Call paths: `docs/a2/call-paths.md`

## Metrics (Assignment 2, Part C)
- Prometheus scrapes `studyslot` (:8000/metrics), `studyslot-grpc` (:9101/metrics) and node-exporter.
- Grafana dashboard **StudySlot A2: HTTP vs gRPC (create booking)** shows calls and error
  ratio by status, p50/p95 latency (server side, 1-minute window), duration heatmaps,
  in-flight requests, bookings and idempotent replays, HTTP beside gRPC.
- Populate it: `.venv\Scripts\python scripts/generate_traffic.py` (60 s steady + 30 s burst).

## Stop and clean up
- Pause (keeps data): `docker compose stop`
- Stop and remove containers (keeps named volumes): `docker compose down`
- Remove everything including stored data: `docker compose down -v`

## Metrics summary
| Metric | Type |
|--------|------|
| `studyslot_bookings_total` | Counter |
| `studyslot_rooms_occupied` | Gauge |
| `studyslot_availability_check_seconds` | Histogram |
| `studyslot_availability_check_summary_seconds` | Summary |
| `node_*` (via Node Exporter) | machine CPU/memory/disk/network |

## Credits
Built following the conventions of the course Lab 1 (Midnight Launch) — same
tool stack, JSON logging style, and Prometheus/Grafana provisioning approach.