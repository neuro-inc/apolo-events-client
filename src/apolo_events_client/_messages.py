import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, NewType, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    model_validator,
)
from pydantic_core import PydanticCustomError


Tag = NewType("Tag", str)
StreamId = NewType("StreamId", str)
GroupName = NewType("GroupName", str)
Type = NewType("Type", str)

NO_EXTRA = ConfigDict(extra="forbid", frozen=True)
ALLOW_EXTRA = ConfigDict(extra="allow", frozen=True)


_ID_RE = re.compile(r"[a-zA-Z](?:[-_]|\w)*", re.ASCII)


def _make_id_validator(name: str) -> Callable[[str], str]:
    txt1 = (
        f"{name} should start from a latin letter and"
        "contain only latin letters, digits, dash, and underscore"
    )
    txt2 = f"{name} should not exceed 255 characters"
    txt3 = f"{name} should be not empty"

    def _validate_id(val: str) -> str:
        if _ID_RE.fullmatch(val) is None:
            raise ValueError(txt1)
        if len(val) >= 256:
            raise ValueError(txt2)
        if not val:
            raise ValueError(txt3)
        return val

    return _validate_id


StreamType = Annotated[StreamId, AfterValidator(_make_id_validator("stream"))]
EventType = Annotated[Type, AfterValidator(_make_id_validator("type"))]


class Kind(StrEnum):
    PING = "PING"
    PONG = "PONG"
    SEND_EVENT = "SEND_EVENT"
    SEND_BATCH = "SEND_BATCH"
    SENT = "SENT"
    SUBSCRIBE = "SUBSCRIBE"
    SUBSCRIBE_GROUP = "SUBSCRIBE_GROUP"
    SUBSCRIBED = "SUBSCRIBED"
    RECV_EVENTS = "RECV_EVENTS"
    RECV_PAST_EVENTS = "RECV_PAST_EVENTS"
    ACK = "ACK"
    ERROR = "ERROR"

    # It generates better error messages, e.g.
    # 'expected': 'PONG'
    # instead of
    # 'expected': "<Kind.PONG: 'PONG'>"
    __repr__ = StrEnum.__str__


class Message(BaseModel):
    model_config = NO_EXTRA
    id: UUID = Field(default_factory=uuid4)
    kind: Kind


class Response(Message):
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    ping_id: UUID | None = None


class Ping(Message):
    kind: Literal[Kind.PING] = Kind.PING


class Pong(Response):
    kind: Literal[Kind.PONG] = Kind.PONG
    ping_id: UUID


def _validate_extra_keys(model: BaseModel) -> None:
    extra = model.model_extra
    if extra is not None:
        for key in extra:
            if not key.isidentifier():
                error_type = "invalid_extra_key"
                message_template = "Extra key '{name}' is not an identifier"
                ctx = {"name": key}
                raise PydanticCustomError(
                    error_type,
                    message_template,
                    ctx,
                )


class SendEvent(Message):
    model_config = ALLOW_EXTRA
    kind: Literal[Kind.SEND_EVENT] = Kind.SEND_EVENT
    sender: str
    stream: StreamType
    event_type: EventType
    org: str | None = None
    cluster: str | None = None
    project: str | None = None
    user: str | None = None

    @model_validator(mode="after")
    def _validate_extras(self) -> Self:
        _validate_extra_keys(self)
        return self


JsonT = str | int | float | bool | None | list["JsonT"] | dict[str, "JsonT"]


class BatchItem(BaseModel):
    model_config = ALLOW_EXTRA
    id: UUID = Field(default_factory=uuid4)
    stream: StreamType
    event_type: EventType
    cluster: str | None = None
    org: str | None = None
    project: str | None = None
    user: str | None = None

    @model_validator(mode="after")
    def _validate_extras(self) -> Self:
        _validate_extra_keys(self)
        return self


class SendBatch(Message):
    kind: Literal[Kind.SEND_BATCH] = Kind.SEND_BATCH
    sender: str
    events: list[BatchItem]


class SentItem(BaseModel):
    model_config = NO_EXTRA
    id: UUID
    stream: StreamType
    tag: Tag
    timestamp: AwareDatetime


class Sent(Response):
    kind: Literal[Kind.SENT] = Kind.SENT
    events: list[SentItem]


class FilterItem(BaseModel):
    model_config = NO_EXTRA
    orgs: frozenset[str] | None = None
    clusters: frozenset[str] | None = None
    projects: frozenset[str] | None = None
    users: frozenset[str] | None = None
    event_types: frozenset[str] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if all(not getattr(self, name) for name in FilterItem.model_fields):
            kind = "empty_filter"
            message_template = "One of {fields} should be not empty"
            raise PydanticCustomError(
                kind,
                message_template,
                {"fields": ",".join(sorted(FilterItem.model_fields))},
            )
        return self


FiltersType = tuple[FilterItem, ...] | None
FiltersAdapter: TypeAdapter[FiltersType] = TypeAdapter(FiltersType)


class BaseSubscribe(Message):
    stream: StreamType
    filters: FiltersType = None

    @model_validator(mode="after")
    def _not_empty_filters(self) -> Self:
        if self.filters == ():
            kind = "empty_filters"
            message_template = (
                ".filters=[] is not allowed, "
                "use .filters=None instead or omit the field"
            )
            raise PydanticCustomError(
                kind,
                message_template,
            )
        return self


class Subscribe(BaseSubscribe):
    kind: Literal[Kind.SUBSCRIBE] = Kind.SUBSCRIBE
    timestamp: AwareDatetime | None = None


class SubscribeGroup(BaseSubscribe):
    kind: Literal[Kind.SUBSCRIBE_GROUP] = Kind.SUBSCRIBE_GROUP
    groupname: Annotated[GroupName, _make_id_validator("groupname")]


class Subscribed(Response):
    kind: Literal[Kind.SUBSCRIBED] = Kind.SUBSCRIBED
    subscr_id: UUID


class RecvEvent(BaseModel):
    model_config = ALLOW_EXTRA
    tag: Tag
    timestamp: AwareDatetime
    sender: str
    stream: StreamType
    event_type: EventType
    org: str | None = None
    cluster: str | None = None
    project: str | None = None
    user: str | None = None


class _RecvEvents(Response):
    subscr_id: UUID
    events: list[RecvEvent]


class RecvEvents(_RecvEvents):
    kind: Literal[Kind.RECV_EVENTS] = Kind.RECV_EVENTS


class RecvPastEvents(_RecvEvents):
    kind: Literal[Kind.RECV_PAST_EVENTS] = Kind.RECV_PAST_EVENTS


class Ack(Message):
    kind: Literal[Kind.ACK] = Kind.ACK
    sender: str
    events: dict[StreamType, list[Tag]]


class Error(Response):
    kind: Literal[Kind.ERROR] = Kind.ERROR
    code: str
    descr: str
    details_head: str | None = None
    details: Any = None
    msg_id: UUID | None = None


ClientMsgTypes = Ping | SendEvent | SendBatch | Subscribe | SubscribeGroup | Ack
ClientMessage = RootModel[Annotated[ClientMsgTypes, Field(discriminator="kind")]]
ServerMsgTypes = Pong | Sent | Subscribed | RecvEvents | RecvPastEvents | Error
ServerMessage = RootModel[Annotated[ServerMsgTypes, Field(discriminator="kind")]]
