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

Make a booking: on `/docs`, `POST /book` with e.g.
`{ "room": "R3", "slot": "09:00", "student_ids": ["s1","s2","s3","s4","s5"] }`.

## Test / reproduce the experiments
- **Injected latency (Part E1):** `POST /chaos/delay?seconds=0.5`, make some
  bookings, watch Grafana's p95 panel spike; `POST /chaos/delay?seconds=0` to
  recover.
- **Cardinality (Part E2):** `POST /chaos/cardinality?active=true`, make ~15–20
  requests, run `count(studyslot_requests_by_id_total)` in Prometheus and watch
  series grow; `POST /chaos/cardinality?active=false` to stop.
- Check current chaos state: `GET /chaos/status`.

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