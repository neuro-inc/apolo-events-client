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


# platform-admin

class ProjectRemoveEvent(BaseEvent):
    stream: Literal["platform-admin"] = "platform-admin"
    event_type: Literal["project-remove"] = "project-remove"
    org: str
    cluster: str
    project: str
    user: str


# platform-config

class ClusterAddEvent(BaseEvent):
    # remove me later
    stream: Literal["platform-config"] = "platform-config"
    event_type: Literal["cluster-add"] = "cluster-add"
    cluster: str
    user: str

class ClusterUpdateEvent(BaseEvent):
    # remove me later
    stream: Literal["platform-config"] = "platform-config"
    event_type: Literal["cluster-update"] = "cluster-update"
    cluster: str
    user: str

class ClusterRemoveEvent(BaseEvent):
    # remove me later
    stream: Literal["platform-config"] = "platform-config"
    event_type: Literal["cluster-remove"] = "cluster-remove"
    cluster: str
    user: str
    

EventTypes = ProjectRemoveEvent | TestEvent


def discriminator(event: EventTypes) -> Hashable:
    return (event.stream, event.event_type)


EventModels = RootModel[Annotated[EventTypes, Field(discriminator=discriminator)]]
