from yarl import URL

from apolo_events_client import (
    AbstractEventsClient,
    DummyEventsClient,
    EventsClient,
    EventsClientConfig,
    from_config,
)


def test_dummy() -> None:
    client = from_config(None)
    assert isinstance(client, DummyEventsClient)
    assert isinstance(client, AbstractEventsClient)


def test_real() -> None:
    cfg = EventsClientConfig(
        url=URL("http://example.com"), token="<token>", name="name"
    )
    client = from_config(cfg)
    assert isinstance(client, EventsClient)
    assert isinstance(client, AbstractEventsClient)
    assert client._raw_client._url == cfg.url
    assert client._raw_client._token == cfg.token
    assert client._name == cfg.name
