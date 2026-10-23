from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class CreateBookingRequest(_message.Message):
    __slots__ = ("room", "slot", "student_ids", "idempotency_key")
    ROOM_FIELD_NUMBER: _ClassVar[int]
    SLOT_FIELD_NUMBER: _ClassVar[int]
    STUDENT_IDS_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    room: str
    slot: str
    student_ids: _containers.RepeatedScalarFieldContainer[str]
    idempotency_key: str
    def __init__(self, room: _Optional[str] = ..., slot: _Optional[str] = ..., student_ids: _Optional[_Iterable[str]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class Booking(_message.Message):
    __slots__ = ("id", "room", "slot", "student_ids", "group_size", "created_at", "replayed")
    ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_FIELD_NUMBER: _ClassVar[int]
    SLOT_FIELD_NUMBER: _ClassVar[int]
    STUDENT_IDS_FIELD_NUMBER: _ClassVar[int]
    GROUP_SIZE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    id: str
    room: str
    slot: str
    student_ids: _containers.RepeatedScalarFieldContainer[str]
    group_size: int
    created_at: str
    replayed: bool
    def __init__(self, id: _Optional[str] = ..., room: _Optional[str] = ..., slot: _Optional[str] = ..., student_ids: _Optional[_Iterable[str]] = ..., group_size: _Optional[int] = ..., created_at: _Optional[str] = ..., replayed: _Optional[bool] = ...) -> None: ...
