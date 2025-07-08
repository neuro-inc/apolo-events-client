import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiohttp import WSMsgType, hdrs, web
from pytest_aiohttp import AiohttpServer
from yarl import URL

from apolo_events_client import (
    Ack,
    ClientMessage,
    ClientMsgTypes,
    Error,
    EventsClient,
    EventType,
    FilterItem,
    Message,
    RawEventsClient,
    RecvEvent,
    RecvEvents,
    Response,
    SendEvent,
    Sent,
    SentItem,
    ServerError,
    StreamType,
    Subscribe,
    Subscribed,
    SubscribeGroup,
    Tag,
)


def now() -> datetime:
    return datetime.now(tz=UTC)


type RespT = (
    Response
    | list[Response]
    | Callable[
        [web.WebSocketResponse, ClientMsgTypes], Awaitable[Response | list[Response]]
    ]
)


class App:
    def __init__(self, token: str) -> None:
        self.url = URL()  # initialize later
        self._token = token
        self._resps: list[
            tuple[
                type[Message],
                RespT,
            ]
        ] = []
        self.events: list[ClientMsgTypes] = []

    def add_resp(self, ev: type[Message], resp: RespT) -> None:
        self._resps.append((ev, resp))

    async def ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        if req.headers.get(hdrs.AUTHORIZATION) != "Bearer " + self._token:
            raise web.HTTPForbidden()

        await ws.prepare(req)

        async for ws_msg in ws:
            assert ws_msg.type == WSMsgType.TEXT
            msg = ClientMessage.model_validate_json(ws_msg.data)
            event = msg.root
            self.events.append(event)
            expected_type, resp = self._resps.pop(0)
            if type(event) is not expected_type:
                await ws.send_str(
                    Error(
                        code="unexpected type",
                        descr=f"{type(event)} != {expected_type}",
                    ).model_dump_json()
                )
            else:
                if callable(resp):
                    resp = await resp(ws, event)
                if not isinstance(resp, list):
                    resp = [resp]
                for resp_msg in resp:
                    resp_msg = resp_msg.model_copy(update={"timestamp": now()})
                    await ws.send_str(resp_msg.model_dump_json())

        return ws

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/stream", self.ws)
        return app


@pytest.fixture
def token() -> str:
    return "TOKEN"


@pytest.fixture
async def server(token: str, aiohttp_server: AiohttpServer) -> App:
    app = App(token)
    srv = await aiohttp_server(app.make_app())
    app.url = srv.make_url("")
    return app


@pytest.fixture
async def raw_client(server: App, token: str) -> AsyncIterator[RawEventsClient]:
    async def nothing() -> None:
        return

    cl = RawEventsClient(url=server.url, token=token, on_ws_connect=nothing)
    yield cl
    await cl.aclose()


@pytest.fixture
async def client(server: App, token: str) -> AsyncIterator[EventsClient]:
    cl = EventsClient(url=server.url, token=token, name="test-client", resp_timeout=0.1)
    yield cl
    await cl.aclose()


async def test_raw_send_recv(server: App, raw_client: RawEventsClient) -> None:
    events = [SentItem(id=uuid4(), stream="test-stream", tag="12345", timestamp=now())]
    server.add_resp(SendEvent, Sent(events=events))
    await raw_client.send(
        SendEvent(sender="test-sender", stream="test-stream", event_type="test-event")
    )

    msg = await raw_client.receive()
    assert isinstance(msg, Sent)
    assert msg.events == events


async def test_raw_send_err(server: App, raw_client: RawEventsClient) -> None:
    msg_id = uuid4()
    server.add_resp(
        SendEvent,
        Error(
            code="err-code",
            descr="err-descr",
            details_head="head",
            details=["a", "b"],
            msg_id=msg_id,
        ),
    )
    await raw_client.send(
        SendEvent(sender="test-sender", stream="test-stream", event_type="test-event")
    )

    with pytest.raises(ServerError) as ctx:
        await raw_client.receive()

    assert ctx.value.code == "err-code"
    assert ctx.value.descr == "err-descr"
    assert ctx.value.details_head == "head"
    assert ctx.value.details == ["a", "b"]
    assert ctx.value.msg_id == msg_id


async def test_raw_none_on_ws_closing(server: App, raw_client: RawEventsClient) -> None:
    attempt = 0

    async def resp(srv_ws: web.WebSocketResponse, event: ClientMsgTypes) -> Sent:
        nonlocal attempt
        attempt += 1
        if attempt < 3:
            await srv_ws.close()
        return Sent(events=events)

    ws = await raw_client._lazy_init()

    events = [SentItem(id=uuid4(), stream="test-stream", tag="12345", timestamp=now())]
    server.add_resp(SendEvent, resp)

    assert ws is raw_client._ws

    await raw_client.send(
        SendEvent(sender="test-sender", stream="test-stream", event_type="test-event")
    )

    msg = await raw_client.receive()
    assert msg is None


async def test_send(server: App, client: EventsClient) -> None:
    async def gen_resp(srv_ws: web.WebSocketResponse, event: ClientMsgTypes) -> Sent:
        events = [
            SentItem(id=event.id, stream="test-stream", tag="12345", timestamp=now())
        ]
        return Sent(events=events)

    server.add_resp(SendEvent, gen_resp)
    ret = await client.send(
        sender="test-sender",
        stream=StreamType("test-stream"),
        event_type=EventType("test-event"),
    )

    assert isinstance(ret, SentItem)
    assert ret.tag == "12345"


async def test_subscribe(server: App, client: EventsClient) -> None:
    async def gen_resp(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> Subscribed:
        return Subscribed(subscr_id=event.id)

    server.add_resp(Subscribe, gen_resp)

    async def cb(resp: RecvEvent) -> None:
        pass

    dt = now()
    await client.subscribe(
        stream=StreamType("test-stream"),
        callback=cb,
        filters=[FilterItem(orgs=["o1"], projects=["p1", "p2"])],
        timestamp=dt,
    )

    ev = server.events[-1]
    assert isinstance(ev, Subscribe)
    assert ev.stream == "test-stream"
    assert ev.filters == (FilterItem(orgs=["o1"], projects=["p1", "p2"]),)
    assert ev.timestamp == dt


async def test_subscribe_group(server: App, client: EventsClient) -> None:
    async def gen_resp(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> Subscribed:
        return Subscribed(subscr_id=event.id)

    server.add_resp(SubscribeGroup, gen_resp)

    async def cb(resp: RecvEvent) -> None:
        pass

    await client.subscribe_group(
        stream=StreamType("test-stream"),
        callback=cb,
        filters=[FilterItem(orgs=["o1"], projects=["p1", "p2"])],
    )

    ev = server.events[-1]
    assert isinstance(ev, SubscribeGroup)
    assert ev.stream == "test-stream"
    assert ev.filters == (FilterItem(orgs=["o1"], projects=["p1", "p2"]),)
    assert ev.groupname == "test-client"


async def test_resubscribe(server: App, client: EventsClient) -> None:
    async def gen_subscr(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> Subscribed:
        return Subscribed(subscr_id=event.id)

    server.add_resp(Subscribe, gen_subscr)

    attempt = 0

    async def gen_sent(srv_ws: web.WebSocketResponse, event: ClientMsgTypes) -> Sent:
        nonlocal attempt
        attempt += 1
        if attempt < 2:
            await srv_ws.close()
            events = [
                SentItem(id=uuid4(), stream="test-stream", tag="12345", timestamp=now())
            ]
        return Sent(events=events)

    server.add_resp(SendEvent, gen_sent)

    async def cb(resp: RecvEvent) -> None:
        pass

    dt = now()
    await client.subscribe(
        stream=StreamType("test-stream"),
        callback=cb,
        filters=[FilterItem(orgs=["o1"], projects=["p1", "p2"])],
        timestamp=dt,
    )

    await client.send(
        stream=StreamType("test-stream"),
        event_type=EventType("test-type"),
    )

    # subscr = client._subscriptions[StreamType("test-stream")]
    # assert subscr.timestamp > dt


async def test_recv(server: App, client: EventsClient) -> None:
    async def gen_subscr(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> list[Response]:
        return [
            Subscribed(subscr_id=event.id),
            RecvEvents(
                subscr_id=event.id,
                events=[
                    RecvEvent(
                        tag="123",
                        timestamp=now(),
                        sender="test-sender",
                        stream="test-stream",
                        event_type="event-type",
                    )
                ],
            ),
        ]

    server.add_resp(Subscribe, gen_subscr)

    lst: list[RecvEvent] = []

    async def cb(resp: RecvEvent) -> None:
        lst.append(resp)

    await client.subscribe(
        stream=StreamType("test-stream"),
        callback=cb,
    )

    await asyncio.sleep(0.1)
    assert len(lst) == 1
    assert lst[0].event_type == "event-type"


async def test_ack(server: App, client: EventsClient) -> None:
    async def gen_subscr(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> list[Response]:
        return []

    server.add_resp(Ack, gen_subscr)

    events = {StreamType("test-stream"): [Tag("1")]}

    await client.ack(
        sender="test-sender2",
        events=events,
    )

    await asyncio.sleep(0.01)
    ev = server.events[-1]
    assert isinstance(ev, Ack)
    assert ev.sender == "test-sender2"
    assert ev.events == events
