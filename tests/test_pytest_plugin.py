import asyncio

from yarl import URL

from apolo_events_client import EventsClient, EventType, SendEvent, StreamType
from apolo_events_client.pytest import EventsQueues


async def test_events_server_acknowledges_send(
    events_server: URL,
    events_token: str,
    events_client_name: str,
    events_queues: EventsQueues,
) -> None:
    async with EventsClient(
        url=events_server,
        token=events_token,
        name=events_client_name,
        resp_timeout=0.1,
    ) as client:
        sent = await client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        )

    event = await asyncio.wait_for(events_queues.income.get(), timeout=0.1)
    assert isinstance(event, SendEvent)
    assert sent is not None
    assert sent.id == event.id
    assert sent.stream == event.stream
