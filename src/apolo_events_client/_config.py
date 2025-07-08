import dataclasses

from yarl import URL

from ._client import AbstractEventsClient, DummyEventsClient, EventsClient
from ._constants import PING_DELAY, RESP_TIMEOUT


@dataclasses.dataclass(frozen=True)
class EventsClientConfig:
    url: URL
    token: str
    name: str
    ping_delay: float = PING_DELAY
    resp_timeout: float = RESP_TIMEOUT


def from_config(config: EventsClientConfig | None) -> AbstractEventsClient:
    if config is None:
        return DummyEventsClient()
    return EventsClient(
        url=config.url,
        token=config.token,
        name=config.name,
        ping_delay=config.ping_delay,
        resp_timeout=config.resp_timeout,
    )
