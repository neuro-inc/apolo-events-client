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
        self.websockets: list[web.WebSocketResponse] = []

    def add_resp(self, ev: type[Message], resp: RespT) -> None:
        self._resps.append((ev, resp))

    async def ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        if req.headers.get(hdrs.AUTHORIZATION) != "Bearer " + self._token:
            raise web.HTTPForbidden()

        await ws.prepare(req)
        self.websockets.append(ws)

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
        auto_ack=False,
        stream=StreamType("test-stream"),
        callback=cb,
        filters=[FilterItem(orgs=["o1"], projects=["p1", "p2"])],
    )

    ev = server.events[-1]
    assert isinstance(ev, SubscribeGroup)
    assert ev.stream == "test-stream"
    assert ev.filters == (FilterItem(orgs=["o1"], projects=["p1", "p2"]),)
    assert ev.groupname == "test-client"


async def test_resubscribe_group_after_reconnect(
    server: App, client: EventsClient
) -> None:
    resubscribe_requested = asyncio.Event()
    allow_resubscribe_ack = asyncio.Event()
    attempts = 0

    async def gen_subscr(
        srv_ws: web.WebSocketResponse, event: ClientMsgTypes
    ) -> Subscribed:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            resubscribe_requested.set()
            await allow_resubscribe_ack.wait()
        return Subscribed(subscr_id=event.id)

    server.add_resp(SubscribeGroup, gen_subscr)
    server.add_resp(SubscribeGroup, gen_subscr)

    async def cb(resp: RecvEvent) -> None:
        pass

    await client.subscribe_group(
        stream=StreamType("test-stream"),
        callback=cb,
        auto_ack=False,
        filters=[FilterItem(orgs=["o1"], projects=["p1", "p2"])],
    )

    await server.websockets[0].close()

    await asyncio.wait_for(resubscribe_requested.wait(), timeout=1)
    resubscribe_task = client._resubscribe_task
    assert resubscribe_task is not None
    assert not resubscribe_task.done()
    allow_resubscribe_ack.set()
    await asyncio.wait_for(asyncio.shield(resubscribe_task), timeout=1)

    assert len(server.websockets) == 2
    assert not client._subscribed
    assert all(isinstance(event, SubscribeGroup) for event in server.events)
    assert server.events[0].id != server.events[1].id


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


async def test_recv_group(server: App, client: EventsClient) -> None:
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

    server.add_resp(SubscribeGroup, gen_subscr)

    lst: list[RecvEvent] = []

    async def cb(resp: RecvEvent) -> None:
        lst.append(resp)

    await client.subscribe_group(
        auto_ack=False,
        stream=StreamType("test-stream"),
        callback=cb,
    )

    await asyncio.sleep(0.1)
    assert len(lst) == 1
    assert lst[0].event_type == "event-type"


async def test_recv_group_auto_ack(server: App, client: EventsClient) -> None:
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

    server.add_resp(SubscribeGroup, gen_subscr)
    server.add_resp(Ack, [])

    lst: list[RecvEvent] = []

    async def cb(resp: RecvEvent) -> None:
        lst.append(resp)

    await client.subscribe_group(
        auto_ack=True,
        stream=StreamType("test-stream"),
        callback=cb,
    )

    await asyncio.sleep(0.1)
    assert len(lst) == 1
    assert lst[0].event_type == "event-type"

    assert len(server.events) == 2
    assert isinstance(server.events[0], SubscribeGroup)
    ev = server.events[1]
    assert isinstance(ev, Ack)
    assert ev.sender == "test-client"
    assert ev.events == {
        "test-stream": [
            "123",
        ],
    }


async def test_recv_group_no_auto_ack_on_error(
    server: App, client: EventsClient
) -> None:
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

    server.add_resp(SubscribeGroup, gen_subscr)

    lst: list[RecvEvent] = []

    async def cb(resp: RecvEvent) -> None:
        txt = "Not handled"
        raise Exception(txt)

    await client.subscribe_group(
        auto_ack=True,
        stream=StreamType("test-stream"),
        callback=cb,
    )

    await asyncio.sleep(0.1)
    assert len(lst) == 0

    assert len(server.events) == 1
    assert isinstance(server.events[0], SubscribeGroup)


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


async def test_send_timeout_includes_raw_send(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client._resp_timeout = 0.01

    async def send_forever(event: ClientMsgTypes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(client._raw_client, "send", send_forever)

    result = await asyncio.wait_for(
        client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        ),
        timeout=0.2,
    )

    assert result is None
    assert not client._sent


async def test_send_timeout_includes_connection_establishment(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client._resp_timeout = 0.01

    async def connect_forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(client._raw_client, "_lazy_init", connect_forever)

    result = await asyncio.wait_for(
        client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        ),
        timeout=0.2,
    )

    assert result is None
    assert not client._sent


async def test_send_timeout_includes_websocket_write(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client._resp_timeout = 0.01

    class BlockingWebsocket:
        async def send_str(self, data: str) -> None:
            await asyncio.Event().wait()

    async def connected_websocket() -> BlockingWebsocket:
        return BlockingWebsocket()

    monkeypatch.setattr(client._raw_client, "_lazy_init", connected_websocket)

    result = await asyncio.wait_for(
        client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        ),
        timeout=0.2,
    )

    assert result is None
    assert not client._sent


@pytest.mark.parametrize("group", [False, True])
async def test_subscribe_timeout_includes_raw_send(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch, group: bool
) -> None:
    client._resp_timeout = 0.01

    async def send_forever(event: ClientMsgTypes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(client._raw_client, "send", send_forever)

    async def callback(event: RecvEvent) -> None:
        pass

    if group:
        operation = client.subscribe_group(
            StreamType("test-stream"), callback, auto_ack=False
        )
    else:
        operation = client.subscribe(StreamType("test-stream"), callback)
    await asyncio.wait_for(operation, timeout=0.2)

    assert not client._subscribed


async def test_ack_timeout_includes_raw_send(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client._resp_timeout = 0.01

    async def send_forever(event: ClientMsgTypes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(client._raw_client, "send", send_forever)

    await asyncio.wait_for(
        client.ack({StreamType("test-stream"): [Tag("1")]}), timeout=0.2
    )


@pytest.mark.parametrize("operation_name", ["send", "subscribe", "subscribe_group"])
async def test_pending_futures_cleaned_on_send_failure(
    client: EventsClient,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
) -> None:
    async def send_failure(event: ClientMsgTypes) -> None:
        msg = "send failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(client._raw_client, "send", send_failure)

    async def callback(event: RecvEvent) -> None:
        pass

    if operation_name == "send":
        operation = client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        )
    elif operation_name == "subscribe":
        operation = client.subscribe(StreamType("test-stream"), callback)
    else:
        operation = client.subscribe_group(
            StreamType("test-stream"), callback, auto_ack=False
        )

    with pytest.raises(RuntimeError, match="send failed"):
        await operation

    assert not client._sent
    assert not client._subscribed


@pytest.mark.parametrize("operation_name", ["send", "subscribe", "subscribe_group"])
async def test_pending_futures_cleaned_on_cancellation(
    client: EventsClient,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
) -> None:
    started = asyncio.Event()

    async def send_forever(event: ClientMsgTypes) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client._raw_client, "send", send_forever)

    async def callback(event: RecvEvent) -> None:
        pass

    if operation_name == "send":
        operation = client.send(
            stream=StreamType("test-stream"),
            event_type=EventType("test-event"),
        )
    elif operation_name == "subscribe":
        operation = client.subscribe(StreamType("test-stream"), callback)
    else:
        operation = client.subscribe_group(
            StreamType("test-stream"), callback, auto_ack=False
        )

    task = asyncio.create_task(operation)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not client._sent
    assert not client._subscribed


async def test_close_cancels_background_tasks_and_pending_futures(
    client: EventsClient,
) -> None:
    async def run_forever() -> None:
        await asyncio.Event().wait()

    receiver_task = asyncio.create_task(run_forever())
    resubscribe_task = asyncio.create_task(run_forever())
    client._task = receiver_task
    client._resubscribe_task = resubscribe_task
    sent = asyncio.get_running_loop().create_future()
    subscribed = asyncio.get_running_loop().create_future()
    client._sent[uuid4()] = sent
    client._subscribed[uuid4()] = subscribed

    await client.aclose()

    assert receiver_task.cancelled()
    assert resubscribe_task.cancelled()
    assert sent.cancelled()
    assert subscribed.cancelled()
    assert not client._sent
    assert not client._subscribed
    assert client._task is None
    assert client._resubscribe_task is None


async def test_reconnects_coalesce_without_overlapping_resubscriptions(
    client: EventsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    runs = 0
    active = 0
    max_active = 0

    async def resubscribe() -> None:
        nonlocal active, max_active, runs
        runs += 1
        active += 1
        max_active = max(max_active, active)
        if runs == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        active -= 1

    monkeypatch.setattr(client, "_resubscribe_all", resubscribe)
    client._resubscribe.add(StreamType("test-stream"))

    client._schedule_resubscribe()
    await first_started.wait()
    first_task = client._resubscribe_task
    client._schedule_resubscribe()

    assert client._resubscribe_task is first_task
    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    second_task = client._resubscribe_task
    assert second_task is not None
    await second_task

    assert runs == 2
    assert max_active == 1
