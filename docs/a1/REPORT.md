# Enterprise Software Development
# Assignment 1: Observability
**StudySlot: a campus study-room booking service**
Widad Fatima Khan · Habib University

---

## Part A — The project

### Problem
In the campus library, group study rooms are booked by physically asking a
librarian, who then walks over to check whether a room is free. If none are
free, groups simply wait. This is slow for students and tedious for staff.

### Intended users
- **Students** — book a group room for a 30- or 60-minute slot in advance,
  listing the student IDs of everyone in the group.
- **Librarians** — see at a glance which rooms are occupied at any moment,
  without walking the floors.

### Solution
A small web service (Python + FastAPI). A student requests a room for a slot
and supplies the group's student IDs. The service checks that the room+slot is
free and that the group fits the room's size limits, books it if so, and
rejects it otherwise — so there is no double-booking. Bookings can be cancelled,
which frees the room. The service exposes live metrics (bookings made, rooms
occupied, availability-check time) and structured logs, which are collected and
visualised by the monitoring stack described below.

### Rooms and rules
| Room | Capacity (max) | Min group |
|------|----------------|-----------|
| R1 | 20 | 10 |
| R2 | 12 | 6  |
| R3 | 10 | 5  |
| R4 | 8  | 3  |
| R5 | 8  | 3  |

- A `(room, slot)` pair can be booked only once → prevents double-booking.
- A group larger than a room's capacity, or smaller than its minimum, is
  rejected with a machine-readable reason (`group_too_large`,
  `group_too_small`, `double_booked`) and a list of suggested rooms that fit.

### How to run it
The whole system runs with one command from the repo root:
`docker compose up -d --build`. See `README.md` for URLs and usage. The API and
its interactive docs are at `http://localhost:8000/docs`.

---

## Part B — Metrics and dashboards

### Stack
- The app exposes a `/metrics` endpoint using the `prometheus-client` library.
- **Prometheus** (in Docker) scrapes `/metrics` every 5 seconds and stores the
  values as time series.
- **Grafana** (in Docker) reads from Prometheus and draws live dashboards. The
  Prometheus data source and the dashboard are auto-provisioned on startup.
- **Node Exporter** (in Docker) exposes machine metrics (CPU, memory, disk,
  network) which Prometheus also scrapes. The machine measured is the Docker
  Desktop Linux VM that hosts the containers.

### The four metric types

| Metric name | Type | Purpose | Unit | Labels | Where recorded in code |
|-------------|------|---------|------|--------|------------------------|
| `studyslot_bookings_total` | Counter | Count successful bookings (only ever rises) | bookings | none | `book()`, on success: `BOOKINGS_TOTAL.inc()` |
| `studyslot_rooms_occupied` | Gauge | Room-slots booked right now (rises on book, falls on cancel) | room-slots | none | `book()`: `.inc()`; `cancel()`: `.dec()` |
| `studyslot_availability_check_seconds` | Histogram | Time for the availability check, bucketed for p95/p99 | seconds | none (buckets) | `book()`, `record_check_time()`: `.observe(elapsed)` |
| `studyslot_availability_check_summary_seconds` | Summary | Same timing as average (`_sum`/`_count`); Python summaries have no percentiles | seconds | none | `book()`, `record_check_time()`: `.observe(elapsed)` |

Machine metrics come from Node Exporter (`node_cpu_seconds_total`,
`node_memory_MemAvailable_bytes`, etc.) and are shown as CPU % and memory %
panels.

### Dashboard panels (PromQL)

| Panel | Query | What it shows |
|-------|-------|---------------|
| Booking rate | `rate(studyslot_bookings_total[1m])` | bookings per second, recent |
| Rooms occupied now | `studyslot_rooms_occupied` | current occupancy (Stat panel) |
| Availability p95 | `histogram_quantile(0.95, rate(studyslot_availability_check_seconds_bucket[5m]))` | 95th-percentile check time; flat near-zero at baseline |
| Availability average | `rate(...summary_seconds_sum[5m]) / rate(...summary_seconds_count[5m])` | mean check time, recent |
| CPU usage % | `100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))` | machine CPU load |
| Memory usage % | `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` | machine memory in use |

**On percentiles and the time window.** The histogram exposes cumulative
`_bucket{le="..."}` counts; `histogram_quantile(0.95, rate(...[5m]))` estimates
the 95th percentile over a 5-minute sliding window — i.e. "95% of availability
checks in the last 5 minutes were faster than this value." The Summary cannot
produce percentiles in the Python client, so it is reported as an average
(`_sum / _count`); the histogram provides p95/p99.

Screenshots of the dashboard, the Prometheus targets page (both targets UP), and
the CPU/memory panels are in `report-images/`.

---

## Part C — Logging pipeline

### Pipeline overview
StudySlot's logs follow the container-native path:

The app writes one JSON line per event to its stdout
→ Docker captures the container's stdout to a log file
→ Filebeat (in Docker) reads and parses that log
→ Elasticsearch (in Docker) stores each line as a searchable document
→ Kibana (in Docker) is used to search and inspect them.

(An earlier version of the app also wrote the same JSON to a file,
`studyslot/logs/app.log`, which Filebeat read directly with a `filestream`
input and `ndjson` parser. That approach is preserved in the project's git
history. The final, submitted version uses container autodiscovery, described
next, so everything runs from a single `docker compose up`.)

### What we log, why, and where in the code
Every booking attempt and cancellation writes exactly one structured JSON
event via the `log_event()` helper in `studyslot/main.py`. We log at the
decision points inside `book()` (each rejection branch and the success path)
and in `cancel()`. This captures *what happened to each request* without
logging noise on every unrelated call.

Each event carries: `@timestamp` (UTC, ISO-8601), `service.name`, `log.level`
(`info` for success/cancel, `warning` for rejections), `message`, a per-request
`request_id` (an 8-char ID generated at the top of `book()`), and context
fields `room`, `slot`, `group_size`, `status`, and `reason` (for rejections:
`group_too_large`, `group_too_small`, `double_booked`, etc.). We never log
student IDs beyond counting them, and no secrets or personal data are logged.

### How Filebeat collects and parses the logs
Filebeat (`monitoring/filebeat/filebeat.yml`) uses Docker autodiscover, scoped
by a condition to containers whose image name contains `studyslot` — so it
collects only the app's logs, not the whole stack. It reads the app container's
log file under `/var/lib/docker/containers/.../*.log`, and its inline JSON
parsing (`json.keys_under_root: true`) lifts every key of each JSON line to a
top-level field, so `room`, `status`, `request_id`, etc. become individual
searchable fields in Elasticsearch rather than one opaque text blob. Filebeat
also adds metadata (`agent.*`, `host.name`, `container.*`, `log.file.path`).

Note: Uvicorn's own startup lines are plain text (not JSON); they appear as raw
`message` strings. All application events are queried by filtering
`service.name : "studyslot"`, which selects only the structured logs.

### Where logs live, what survives restarts, retention
Logs are shipped into Elasticsearch under daily indices named
`studyslot-logs-YYYY.MM.DD`. Elasticsearch stores its data inside the container,
so `docker compose down -v` (which removes volumes) clears the index; the app's
own stdout is regenerated on each run. No automatic deletion (ILM) is
configured; for this classroom scale, old daily indices can be deleted manually
(e.g. `DELETE /_data_stream/studyslot-logs-*`).

### Searching in Kibana
A data view `StudySlot Logs` over pattern `studyslot-logs-*` (timestamp field
`@timestamp`) is used in **Discover**. Example searches (KQL):
- `service.name : "studyslot"` — only the app's structured events.
- `reason : "group_too_small"` — all rejections of that kind (see screenshot).
- `status : 200` — all confirmed bookings.
- `request_id : "<id>"` — trace one specific request end to end.
- `request_id : a*` — wildcard/pattern match: every request whose ID begins
  with `a`.

A sample stored event (fields after Filebeat parsing):
`@timestamp`, `service.name=studyslot`, `log.level=warning`,
`message="booking rejected"`, `request_id`, `room`, `slot`, `group_size`,
`status=400`, `reason=group_too_small`, plus Filebeat's `agent.*`,
`host.name`, `container.name=studyslot-app`.

Screenshot: Kibana Discover with the `reason : "group_too_small"` search
returning one document, expanded to show its parsed fields
(`report-images/Screenshot 2026-09-23 022755.png`).

---

## Part D — System design

### 1. Architecture diagram
See `report-images/architecture-diagram.drawio.png`. The system has two
observability flows, both fed by the StudySlot app and all running as Docker
Compose containers on one private network:

- **Metrics path:** `studyslot-app` exposes `/metrics`; Prometheus scrapes it
  every 5s and stores the values; Grafana queries Prometheus and draws
  dashboards. `node-exporter` also exposes machine metrics that Prometheus
  scrapes.
- **Logs path:** the app writes one JSON line per event to stdout; Docker
  captures it; Filebeat reads and parses it; Elasticsearch stores each line as
  a searchable document; Kibana is the search UI.

**What each component does / how they communicate.** Containers talk over the
Docker network by service name (e.g. Grafana → `http://prometheus:9090`,
Prometheus → `studyslot-app:8000`, Filebeat → `http://elasticsearch:9200`).
Prometheus uses a *pull* model (it fetches `/metrics`); the log path is *push*
(Filebeat ships lines onward).

**Where data is stored and why.** Metrics live in Prometheus's time-series
database (efficient for numeric series over time). Logs live in Elasticsearch
(built for full-text search over structured documents). Grafana and Kibana
store no primary data — they only read and display. This split matches the
two questions being asked: "how is the system behaving over time?" (metrics)
vs "what exactly happened on this request?" (logs).

**What happens if a component stops.**
- If Prometheus stops: Grafana panels go blank (no data source), but the app
  keeps serving bookings — monitoring is observational, not in the request
  path.
- If Grafana stops: metrics are still collected by Prometheus; only the
  visualization is lost.
- If Elasticsearch stops: Filebeat retries and Kibana can't search, but the
  app keeps emitting logs to stdout, so no log data is lost at the source.
- If the app stops: no new metrics or logs, and Prometheus marks the target
  DOWN on its /targets page — which is itself a useful signal.

### 2. Follow a metric and a log

**A metric — `studyslot_availability_check_seconds` (the Part E delay).**
1. *Code updates it:* inside `book()`, `record_check_time()` calls
   `AVAILABILITY_CHECK_SECONDS.observe(elapsed)`, recording how long the
   availability check took.
2. *Prometheus collects and stores it:* every 5s Prometheus scrapes
   `/metrics` and reads the histogram buckets
   (`studyslot_availability_check_seconds_bucket{le="..."}`), storing them as
   time series.
3. *Grafana queries and displays it:* the p95 panel runs
   `histogram_quantile(0.95, rate(studyslot_availability_check_seconds_bucket[5m]))`.
4. *Values observed (Part E):* at baseline p95 ≈ 0.005s; after injecting a
   0.5s delay via `POST /chaos/delay?seconds=0.5`, p95 rose to ≈ 0.95s (the
   0.5s timing lands in the `le="1.0"` bucket, so the quantile estimate
   interpolates high); after disabling the delay it fell back to baseline.
   See `report-images/partE-p95-full.png`.

**A log — a booking event.**
1. *Code writes it:* `book()` calls `log_event("booking confirmed", ...,
   request_id=..., room=..., slot=..., status=200)`, which writes one JSON
   line to stdout.
2. *Filebeat collects and parses it:* Filebeat reads the app container's log
   and its inline JSON parsing lifts each key to a top-level searchable field.
3. *Elasticsearch stores it:* indexed into `studyslot-logs-YYYY.MM.DD`.
4. *Kibana finds it:* in Discover, `request_id : "<id>"` returns that single
   event; `reason : "group_too_small"` returns all rejections of that kind.
   The format changes along the way: a raw one-line JSON string on stdout
   becomes a structured document with fields (`room`, `status`, `reason`,
   plus Filebeat's `agent.*`, `host.name`, `container.*`) in Elasticsearch.

---

## Part E — Experiments

### Experiment 1: Reproduce a problem (injected latency)
**Setup.** A chaos toggle (`POST /chaos/delay?seconds=X`) sets a global
`INJECTED_DELAY` that is applied with `time.sleep()` inside the timed
availability-check section of `book()`. This makes the fault repeatable and
reversible without editing code mid-experiment.

1. **Normal behaviour.** With delay = 0, ~5 bookings were made. The p95 panel
   (`histogram_quantile(0.95, rate(studyslot_availability_check_seconds_bucket[5m]))`)
   sat flat at ≈ 0.005s. See `report-images/partE-p95-before.png`.
2. **Prediction.** Injecting a 0.5s delay should push p95 toward 0.5s and lift
   the average, while bookings still succeed (a latency problem, not an error).
3. **Inject.** `POST /chaos/delay?seconds=0.5`, then ~6–8 bookings. Each
   response visibly took ~0.5s.
4. **Observed.** p95 jumped from ≈ 0.005s to ≈ 0.95s (the 0.5s timing falls in
   the `le="1.0"` bucket, so the quantile estimate interpolates high); the
   average panel rose too; bookings still returned 200. This is the classic
   "slow, not failing" signature. See `report-images/partE-p95-full.png`,
   which shows baseline → plateau → recovery in one view.
5. **Recover.** `POST /chaos/delay?seconds=0`, then more bookings; p95 ramped
   back down to baseline over the 5-minute query window, confirming recovery.

**Effect on users.** Every booking took about half a second longer. The system
kept working but felt sluggish — the kind of degradation metrics catch before
users complain.

### Experiment 2: Cardinality explosion
**Setup.** A deliberately unsafe counter `studyslot_requests_by_id_total` with
a `request_id` label, incremented per request only when armed via
`POST /chaos/cardinality?active=true`.

1. **Baseline.** `count(studyslot_requests_by_id_total)` in Prometheus returned
   an empty result — zero series. See `report-images/cardinality-explosion-before.png`.
2. **Arm and load.** Enabled the bomb and made ~19 requests (mixed successes
   and rejections; each gets a unique `request_id`).
3. **Series growth.** `count(studyslot_requests_by_id_total)` climbed to ≈ 19 —
   one new time series per unique ID. See `report-images/cardinality-explosion-after.png`.
4. **Cost at scale and the fix.** Cardinality is the product of a metric's
   distinct label-value combinations. An unbounded label like `request_id`
   creates a new series per request — at millions of requests, millions of
   series, exhausting Prometheus's memory. The fix is to keep high-cardinality
   identifiers in **logs** (where StudySlot already puts `request_id`, searchable
   in Kibana at zero metric cost), not in metric labels. The bomb was then
   disarmed (`active=false`). Removing a label does not immediately delete
   series already stored; they age out with the index.

---

## Credits and assistance
- Tooling stack, JSON logging style, and Prometheus/Grafana provisioning
  conventions follow the course Lab 1 (Midnight Launch).