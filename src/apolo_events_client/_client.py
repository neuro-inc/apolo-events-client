import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC
from types import TracebackType
from typing import cast
from uuid import UUID

import aiohttp
from aiohttp import hdrs
from datetype import AwareDateTime
from yarl import URL

from ._exceptions import ServerError
from ._messages import (
    ClientMsgTypes,
    Error,
    EventType,
    FilterItem,
    JsonT,
    Pong,
    SendEvent,
    Sent,
    SentItem,
    ServerMessage,
    ServerMsgTypes,
    StreamType,
    Subscribe,
    Subscribed,
    SubscribeGroup,
)


log = logging.getLogger(__package__)


class RawEventsClient:
    def __init__(
        self,
        *,
        url: URL | str,
        token: str,
        ping_delay: float = 60,
        on_ws_connect: Callable[[], Awaitable[None]],
    ) -> None:
        self._url = URL(url)
        self._token = token
        self._closing = False
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ping_delay = ping_delay
        self._on_ws_connect = on_ws_connect

    async def _lazy_init(self) -> aiohttp.ClientWebSocketResponse:
        if self._closing:
            msg = "Operation on the closed client"
            raise RuntimeError(msg)
        if self._session is None:
            self._session = aiohttp.ClientSession()

        if self._ws is None or self._ws.closed:
            async with self._lock:
                if self._ws is None or self._ws.closed:
                    self._ws = await self._session.ws_connect(
                        self._url, headers={hdrs.AUTHORIZATION: "Bearer " + self._token}
                    )
                    await self._on_ws_connect()
        assert self._ws is not None
        return self._ws

    async def __aenter__(self) -> None:
        await self._lazy_init()

    async def __aexit__(
        self,
        exc_typ: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closing = True
        if self._ws is not None:
            ws = self._ws
            self._ws = None
            await ws.close()
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        if self._ws is ws:
            self._ws = None
            await ws.close()

    async def send(self, msg: ClientMsgTypes) -> None:
        """Send a message through the wire."""
        while not self._closing:
            ws = await self._lazy_init()
            try:
                await ws.send_str(msg.model_dump_json())
                return
            except aiohttp.ClientError:
                await self._close_ws(ws)

    async def receive(self) -> ServerMsgTypes | None:
        """Receive next upcoming message.

        Returns None if the client is closed."""
        while not self._closing:
            ws = await self._lazy_init()
            try:
                ws_msg = await ws.receive()
            except aiohttp.ClientError:
                log.info("Reconnect on transport error", exc_info=True)
                await self._close_ws(ws)
                return None
            if ws_msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                log.info("Reconnect on closing transport [%s]", ws_msg.type)
                self._ws = None
                return None
            if ws_msg.type == aiohttp.WSMsgType.BINARY:
                log.warning("Ignore unexpected BINARY message")
                continue

            assert ws_msg.type == aiohttp.WSMsgType.TEXT
            resp = ServerMessage.model_validate_json(ws_msg.data)
            match resp.root:
                case Pong():
                    pass
                case Error() as err:
                    raise ServerError(
                        err.code,
                        err.descr,
                        err.details_head,
                        err.details,
                        err.msg_id,
                    )
                case _:
                    return resp.root

        return None


@dataclasses.dataclass
class _SubscrData:
    filters: tuple[FilterItem, ...] | None
    timestamp: AwareDateTime


class EventsClient:
    def __init__(
        self, *, url: URL | str, token: str, ping_delay: float = 60, resp_timeout=30
    ) -> None:
        self._closing = False
        self._raw_client = RawEventsClient(
            url=url,
            token=token,
            ping_delay=ping_delay,
            on_ws_connect=self._on_ws_connect,
        )
        self._resp_timeout = resp_timeout
        self._task = asyncio.create_task(self._loop())

        self._sent: dict[UUID, asyncio.Future[SentItem]] = {}
        self._subscribed: dict[UUID, asyncio.Future[Subscribed]] = {}
        self._subscriptions: dict[StreamType, _SubscrData] = {}
        self._subscr_groups: dict[StreamType, SubscribeGroup] = {}

    async def __aenter__(self) -> None:
        await self._raw_client.__aenter__()

    async def __aexit__(
        self,
        exc_typ: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closing = True
        await self._raw_client.aclose()

    async def _loop(self) -> None:
        try:
            while not self._closing:
                await self._loop_once()
        except Exception as ex:
            for fut in self._sent.values():
                if not fut.done():
                    fut.set_exception(ex)

    async def _loop_once(self) -> None:
        msg = await self._raw_client.receive()
        match msg:
            case None:
                pass
            case Sent():
                for event in msg.events:
                    sent_fut = self._sent.pop(event.id, None)
                    if sent_fut is not None:
                        sent_fut.set_result(event)
                    else:
                        log.warning(
                            "Received Sent response for unknown id %s", event.id
                        )
            case Subscribed():
                subscr_fut = self._subscribed.pop(msg.subscr_id, None)
                if subscr_fut is not None:
                    subscr_fut.set_result(msg)
                else:
                    log.warning(
                        "Received Subscribed response for unknown id %s", msg.id
                    )

    async def _on_ws_connect(self) -> None:
        pass

    async def send(
        self,
        *,
        sender: str,
        stream: StreamType,
        event_type: EventType,
        org: str | None = None,
        cluster: str | None = None,
        project: str | None = None,
        user: str | None = None,
        **kwargs: JsonT,
    ) -> SentItem:
        ev = SendEvent(
            sender=sender,
            stream=stream,
            event_type=event_type,
            org=org,
            cluster=cluster,
            project=project,
            user=user,
            **kwargs,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[SentItem] = loop.create_future()
        self._sent[ev.id] = fut
        await self._raw_client.send(ev)
        try:
            async with asyncio.timeout(self._resp_timeout):
                return await fut
        except TimeoutError:
            self._sent.pop(ev.id, None)

    async def subscribe(
        self,
        stream: StreamType,
        *,
        filters: Sequence[FilterItem] | None = None,
        timestamp: AwareDateTime | None = None,
    ) -> None:
        ev = Subscribe(stream=stream, filters=filters, timestamp=timestamp)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Subscribed] = loop.create_future()
        self._subscribed[ev.id] = fut
        self._subscriptions[stream] = _SubscrData(
            ev.filters, cast(AwareDateTime, ev.timestamp or AwareDateTime.now(tz=UTC))
        )
        await self._raw_client.send(ev)
        try:
            async with asyncio.timeout(self._resp_timeout):
                ret = await fut
            # reconnection could bump the timestamp
            self._subscriptions[stream].timestamp = max(
                ret.timestamp, self._subscriptions[stream].timestamp
            )
        except TimeoutError:
            self._subscribed.pop(ev.id, None)
