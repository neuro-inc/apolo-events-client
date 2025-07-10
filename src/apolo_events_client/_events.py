from collections.abc import Hashable
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, RootModel

from ._messages import Tag


class BaseEvent(BaseModel):
    stream: str
    event_type: str
    org: str | None = None
    cluster: str | None = None
    project: str | None = None
    user: str | None = None
    tag: Tag | None = None
    timestamp: AwareDatetime | None = None


class ProjectRemoveEvent(BaseEvent):
    stream: Literal["platform-admin"] = "platform-admin"
    event_type: Literal["project-remove"] = "project-remove"
    org: str
    cluster: str
    project: str
    user: str


class TestEvent(BaseEvent):
    # remove me later
    stream: Literal["test"] = "test"
    event_type: Literal["test"] = "test"


EventTypes = ProjectRemoveEvent | TestEvent


def discriminator(event: EventTypes) -> Hashable:
    return (event.stream, event.event_type)


EventModels = RootModel[Annotated[EventTypes, Field(discriminator=discriminator)]]
