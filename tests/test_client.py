from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiohttp import WSMsgType, hdrs, web
from pytest_aiohttp import AiohttpServer
from yarl import URL

from apolo_events_client import (
    ClientMessage,
    Error,
    Message,
    RawEventsClient,
    Response,
    SendEvent,
    Sent,
    SentItem,
)


def now() -> datetime:
    return datetime.now(tz=UTC)


class App:
    def __init__(self, token: str) -> None:
        self.url = URL()  # initialize later
        self._token = token
        self._resps: list[tuple[type[Message], Response]] = []

    def add_resp(self, ev: type[Message], resp: Response) -> None:
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
    cl = RawEventsClient(server.url, token)
    yield cl
    await cl.aclose()


async def test_send_recv(server: App, raw_client: RawEventsClient) -> None:
    events = [SentItem(id=uuid4(), stream="test-stream", tag="12345", timestamp=now())]
    server.add_resp(SendEvent, Sent(events=events))
    await raw_client.send(
        SendEvent(sender="test-sender", stream="test-stream", event_type="test-event")
    )

    it = raw_client.iter_received()
    msg = await anext(it)
    assert isinstance(msg, Sent)
    assert msg.events == events
