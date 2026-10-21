"""StudySlot business rules: the ONE place that decides how booking works.

The HTTP API (main.py) uses this module, and so will the gRPC service in
Part B, so the rules can never drift apart. Nothing here knows about HTTP or
gRPC: when a rule is broken it raises a BookingError, and each API decides
which status code that becomes.
"""
import uuid
from datetime import datetime, timezone

import db

# --- Rooms, each with a capacity and a minimum group size ---
# capacity  = the most students the room fits
# min_group = the fewest students allowed to book it (stops a big room
#             being hogged by one or two people)
ROOMS = {
    "R1": {"capacity": 20, "min_group": 10},
    "R2": {"capacity": 12, "min_group": 6},
    "R3": {"capacity": 10, "min_group": 5},
    "R4": {"capacity": 8, "min_group": 3},
    "R5": {"capacity": 8, "min_group": 3},
}

SLOTS = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
         "12:00", "12:30", "13:00", "13:30", "14:00", "14:30",
         "15:00", "15:30", "16:00", "16:30"]


# --- Errors: one class per broken rule ---
class BookingError(Exception):
    """Base class. `reason` is a short machine-readable code; `message` is for humans."""
    reason = "booking_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details      # extra info, e.g. suggested_rooms


class UnknownRoom(BookingError):
    reason = "unknown_room"


class UnknownSlot(BookingError):
    reason = "unknown_slot"


class NoStudents(BookingError):
    reason = "no_student_ids"


class InvalidStudentIds(BookingError):
    reason = "invalid_student_ids"


class GroupTooLarge(BookingError):
    reason = "group_too_large"


class GroupTooSmall(BookingError):
    reason = "group_too_small"


class AlreadyBooked(BookingError):
    reason = "double_booked"


class BookingNotFound(BookingError):
    reason = "not_found"


# --- Helpers ---
def _fits(room: str, group_size: int) -> bool:
    info = ROOMS[room]
    return info["min_group"] <= group_size <= info["capacity"]


def _with_group_size(booking: dict) -> dict:
    """Add the derived group_size field to a stored booking."""
    return {**booking, "group_size": len(booking["student_ids"])}


def rooms_that_fit(group_size: int, slot: str) -> list[str]:
    """Rooms whose size range fits this group AND are free for this slot."""
    taken = db.booked_rooms(slot)
    return [room for room in ROOMS if _fits(room, group_size) and room not in taken]


# --- Operations ---
def list_rooms() -> list[dict]:
    """Every room with its size limits and which slots are booked."""
    booked: dict[str, list[str]] = {}
    for booking in db.all_bookings():
        booked.setdefault(booking["room"], []).append(booking["slot"])
    result = []
    for room, info in ROOMS.items():
        booked_slots = sorted(booked.get(room, []))
        result.append({
            "room": room,
            "capacity": info["capacity"],
            "min_group": info["min_group"],
            "booked_slots": booked_slots,
            "free_slot_count": len(SLOTS) - len(booked_slots),
        })
    return result


def availability(slot: str, group_size: int | None = None) -> dict:
    """Free rooms for a slot; with group_size, only rooms that fit the group."""
    if slot not in SLOTS:
        raise UnknownSlot(f"Unknown slot '{slot}'")
    taken = db.booked_rooms(slot)
    free = [room for room in ROOMS if room not in taken]
    if group_size is not None:
        free = [room for room in free if _fits(room, group_size)]
    return {
        "slot": slot,
        "group_size": group_size,
        "free_rooms": [
            {"room": r, "capacity": ROOMS[r]["capacity"], "min_group": ROOMS[r]["min_group"]}
            for r in free
        ],
    }


def create_booking(room: str, slot: str, student_ids: list[str]) -> dict:
    """Book a room for a slot. Enforces size limits and no double-booking."""
    if room not in ROOMS:
        raise UnknownRoom(f"Unknown room '{room}'")
    if slot not in SLOTS:
        raise UnknownSlot(f"Unknown slot '{slot}'")
    if not student_ids:
        raise NoStudents("At least one student ID is required")
    if any(not sid.strip() for sid in student_ids):
        raise InvalidStudentIds("Student IDs cannot be blank")
    duplicates = sorted({sid for sid in student_ids if student_ids.count(sid) > 1})
    if duplicates:
        raise InvalidStudentIds("Each student can only be listed once", duplicates=duplicates)

    info = ROOMS[room]
    group_size = len(student_ids)
    if group_size > info["capacity"]:
        raise GroupTooLarge(
            f"Room {room} holds at most {info['capacity']} students; your group has {group_size}.",
            suggested_rooms=rooms_that_fit(group_size, slot),
        )
    if group_size < info["min_group"]:
        raise GroupTooSmall(
            f"Room {room} needs at least {info['min_group']} students; your group has {group_size}.",
            suggested_rooms=rooms_that_fit(group_size, slot),
        )

    booking = {
        "id": str(uuid.uuid4()),
        "room": room,
        "slot": slot,
        "student_ids": list(student_ids),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.insert_booking(booking)      # the UNIQUE(room, slot) rule makes this atomic
    except db.DuplicateBooking:
        raise AlreadyBooked(f"Room {room} is already booked for {slot}.") from None
    return _with_group_size(booking)


def list_bookings() -> list[dict]:
    """Librarian view: everything currently booked."""
    return [_with_group_size(b) for b in db.all_bookings()]


def get_booking(booking_id: str) -> dict:
    """One booking by its ID."""
    booking = db.get_booking(booking_id)
    if booking is None:
        raise BookingNotFound(f"No booking with id '{booking_id}'")
    return _with_group_size(booking)


def cancel_booking(booking_id: str) -> dict:
    """Cancel a booking by its ID and return what was cancelled."""
    booking = get_booking(booking_id)          # raises BookingNotFound
    if not db.delete_booking(booking_id):      # someone else cancelled it a moment ago
        raise BookingNotFound(f"No booking with id '{booking_id}'")
    return booking