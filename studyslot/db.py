"""Storage layer: every booking lives in one SQLite database file.

This file is the single source of truth. The HTTP API and the gRPC service
(Part B) run in separate containers but open the SAME file through a shared
Docker volume, so they always see the same bookings.
"""
import json
import os
import sqlite3
from contextlib import contextmanager

# Where the database file lives. Docker sets this to /data/studyslot.db (a
# named volume); when running locally it defaults to data/studyslot.db.
DB_PATH = os.getenv("STUDYSLOT_DB_PATH", "data/studyslot.db")


class DuplicateBooking(Exception):
    """Raised when a room+slot pair is already taken."""


@contextmanager
def connect():
    """Open a connection, commit if the block succeeds, roll back if it fails, always close."""
    conn = sqlite3.connect(DB_PATH, timeout=5)  # wait up to 5 s if another process is writing
    conn.row_factory = sqlite3.Row             # rows behave like dicts: row["room"]
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create the database file and bookings table if they don't exist yet."""
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
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


def booked_rooms(slot: str) -> set[str]:
    """Rooms that already have a booking for this slot."""
    with connect() as conn:
        rows = conn.execute("SELECT room FROM bookings WHERE slot = ?", (slot,)).fetchall()
    return {row["room"] for row in rows}


def all_bookings() -> list[dict]:
    """Every booking, sorted by room then slot."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, room, slot, student_ids, created_at FROM bookings ORDER BY room, slot"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "room": row["room"],
            "slot": row["slot"],
            "student_ids": json.loads(row["student_ids"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def insert_booking(booking: dict) -> None:
    """Save a new booking. Raises DuplicateBooking if the room+slot is taken."""
    try:
        with connect() as conn:
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


def delete_booking(room: str, slot: str) -> bool:
    """Delete the booking for room+slot. Returns True if one was deleted."""
    with connect() as conn:
        cursor = conn.execute("DELETE FROM bookings WHERE room = ? AND slot = ?", (room, slot))
    return cursor.rowcount > 0


def count_bookings() -> int:
    """How many room-slots are booked right now."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]