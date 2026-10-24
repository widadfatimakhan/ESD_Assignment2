"""Storage layer: every booking lives in one SQLite database file.

This file is the single source of truth. The HTTP API and the gRPC service
(Part B) run in separate containers but open the SAME file through a shared
Docker volume, so they always see the same bookings.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# Where the database file lives. Docker sets this to /data/studyslot.db (a
# named volume); when running locally it defaults to data/studyslot.db.
DB_PATH = os.getenv("STUDYSLOT_DB_PATH", "data/studyslot.db")

# How long an Idempotency-Key is remembered. A retry later than this is treated as new.
IDEMPOTENCY_KEY_TTL = timedelta(hours=24)


class DuplicateBooking(Exception):
    """Raised when a room+slot pair is already taken."""


class IdempotencyKeyConflict(Exception):
    """Raised when an Idempotency-Key is reused with a different request body."""


class StorageBusy(Exception):
    """Other writers kept the database locked for longer than the busy timeout.

    Nothing was written, so the request is safe to retry. The HTTP API turns
    this into 503 + Retry-After, the gRPC API into UNAVAILABLE.
    """


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


# SQLite allows one writer at a time. Queue this process's own threads here,
# so only the two processes (HTTP and gRPC containers) compete for SQLite's
# lock. Without this, under heavy load some threads kept losing the race.
_write_lock = threading.Lock()


@contextmanager
def connect():
    """Open a connection, commit if the block succeeds, roll back if it fails, always close."""
    conn = sqlite3.connect(DB_PATH, timeout=5)  # wait up to 5 s if another process is writing
    conn.row_factory = sqlite3.Row             # rows behave like dicts: row["room"]
    try:
        with conn:
            yield conn
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise StorageBusy("database busy") from exc
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction():
    """A transaction that takes the database's write lock straight away.

    Everything inside the block happens as one step: no other process (the HTTP
    API or the gRPC service) can write in between. That makes "check the key,
    then insert the booking" safe even when two retries arrive at the same moment.
    """
    with _write_lock:
        conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise StorageBusy("database busy") from exc
            raise
        finally:
            conn.close()


def init_db() -> None:
    """Create the database file and tables if they don't exist yet."""
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    # WAL (write-ahead log) mode: the right mode when several processes - here
    # the HTTP and gRPC containers - use one SQLite file. Readers never block
    # the writer, and SQLite stops creating and deleting a journal file on every
    # write, which under heavy load from two processes caused "disk I/O error".
    # The setting is stored in the database file, so it only has to be set once.
    raw = sqlite3.connect(DB_PATH, timeout=5)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass        # the other container is switching it at this moment; it ends up WAL either way
    finally:
        raw.close()
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id          TEXT PRIMARY KEY,
                room        TEXT NOT NULL,
                slot        TEXT NOT NULL,
                student_ids TEXT NOT NULL,      -- JSON list, e.g. ["s1","s2"]
                created_at  TEXT NOT NULL,
                UNIQUE (room, slot)             -- the database itself forbids double-booking
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key          TEXT PRIMARY KEY,  -- the client's Idempotency-Key
                request_hash TEXT NOT NULL,     -- fingerprint of the request body
                response     TEXT NOT NULL,     -- the booking we returned, as JSON
                created_at   TEXT NOT NULL
            )
            """
        )


def booked_rooms(slot: str) -> set[str]:
    """Rooms that already have a booking for this slot."""
    with connect() as conn:
        rows = conn.execute("SELECT room FROM bookings WHERE slot = ?", (slot,)).fetchall()
    return {row["room"] for row in rows}


def _row_to_booking(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "room": row["room"],
        "slot": row["slot"],
        "student_ids": json.loads(row["student_ids"]),
        "created_at": row["created_at"],
    }


def all_bookings() -> list[dict]:
    """Every booking, sorted by room then slot."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, room, slot, student_ids, created_at FROM bookings ORDER BY room, slot"
        ).fetchall()
    return [_row_to_booking(row) for row in rows]


def get_booking(booking_id: str) -> dict | None:
    """One booking by its ID, or None if it doesn't exist."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, room, slot, student_ids, created_at FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()
    return _row_to_booking(row) if row else None


def insert_booking(booking: dict, idempotency_key: str | None = None,
                   request_hash: str | None = None) -> tuple[dict, bool]:
    """Save a new booking. Returns (booking, replayed).

    Without a key: insert, or raise DuplicateBooking if the room+slot is taken.
    With a key, all in one transaction:
      - key seen before with the same body  -> return the original booking, replayed=True
      - key seen before with a different body -> raise IdempotencyKeyConflict
      - key not seen -> insert the booking AND remember the key
    """
    with write_transaction() as conn:
        if idempotency_key:
            # Forget keys older than the TTL so the table doesn't grow forever.
            cutoff = (datetime.now(timezone.utc) - IDEMPOTENCY_KEY_TTL).isoformat()
            conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,))

            row = conn.execute(
                "SELECT request_hash, response FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise IdempotencyKeyConflict
                return json.loads(row["response"]), True

        try:
            conn.execute(
                "INSERT INTO bookings (id, room, slot, student_ids, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    booking["id"],
                    booking["room"],
                    booking["slot"],
                    json.dumps(booking["student_ids"]),
                    booking["created_at"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateBooking from exc

        if idempotency_key:
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash, response, created_at) VALUES (?, ?, ?, ?)",
                (idempotency_key, request_hash, json.dumps(booking), booking["created_at"]),
            )
    return booking, False


def delete_booking(booking_id: str) -> bool:
    """Delete a booking by its ID. Returns True if one was deleted."""
    with write_transaction() as conn:
        cursor = conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return cursor.rowcount > 0


def count_bookings() -> int:
    """How many room-slots are booked right now."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
