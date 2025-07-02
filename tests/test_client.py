from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiohttp import WSMsgType, hdrs, web
from pytest_aiohttp import AiohttpServer
from yarl import URL

from apolo_events_client import (
    ClientMessage,
    Error,
    EventsClient,
    Message,
    RawEventsClient,
    Response,
    SendEvent,
    Sent,
    SentItem,
    ServerError,
)


def now() -> datetime:
    return datetime.now(tz=UTC)


type RespT = Response | Callable[[web.WebSocketResponse], Awaitable[Response]]


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
                    resp = await resp(ws)
                resp = resp.model_copy(update={"timestamp": now()})
                await ws.send_str(resp.model_dump_json())

        return ws

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        return app


@pytest.fixture
def token() -> str:
    return "TOKEN"


@pytest.fixture
async def server(token: str, aiohttp_server: AiohttpServer) -> App:
    app = App(token)
    srv = await aiohttp_server(app.make_app())
    app.url = srv.make_url("/ws")
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
    cl = EventsClient(url=server.url, token=token)
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

    async def resp(srv_ws: web.WebSocketResponse) -> Sent:
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
