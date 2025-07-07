import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

import aiohttp
from aiohttp import hdrs
from yarl import URL

from ._exceptions import ServerError
from ._messages import (
    Ack,
    ClientMsgTypes,
    Error,
    EventType,
    FilterItem,
    GroupName,
    JsonT,
    Pong,
    RecvEvent,
    SendEvent,
    Sent,
    SentItem,
    ServerMessage,
    ServerMsgTypes,
    StreamType,
    Subscribe,
    Subscribed,
    SubscribeGroup,
    Tag,
    _RecvEvents,
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
                        self._url / "v1" / "stream",
                        headers={hdrs.AUTHORIZATION: "Bearer " + self._token},
                    )
                    await self._on_ws_connect()
        assert self._ws is not None
        return self._ws

    async def __aenter__(self) -> Self:
        return self

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


@dataclasses.dataclass(kw_only=True)
class _BaseSubscrData:
    filters: tuple[FilterItem, ...] | None
    callback: Callable[[RecvEvent], Awaitable[None]]


@dataclasses.dataclass(kw_only=True)
class _SubscrData(_BaseSubscrData):
    timestamp: datetime


@dataclasses.dataclass(kw_only=True)
class _SubscrGroupData(_BaseSubscrData):
    groupname: GroupName


class EventsClient:
    def __init__(
        self,
        *,
        url: URL | str,
        token: str,
        ping_delay: float = 60,
        resp_timeout: float = 30,
        sender: str | None = None,
    ) -> None:
        self._closing = False
        self._raw_client = RawEventsClient(
            url=url,
            token=token,
            ping_delay=ping_delay,
            on_ws_connect=self._on_ws_connect,
        )
        self._resp_timeout = resp_timeout
        self._sender = sender
        self._task = asyncio.create_task(self._loop())

        self._sent: dict[UUID, asyncio.Future[SentItem]] = {}
        self._subscribed: dict[UUID, asyncio.Future[Subscribed]] = {}
        self._subscriptions: dict[StreamType, _SubscrData] = {}
        self._subscr_groups: dict[StreamType, _SubscrGroupData] = {}

    async def __aenter__(self) -> Self:
        await self._raw_client.__aenter__()
        return self

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
            case _RecvEvents():
                for ev in msg.events:
                    stream = ev.stream
                    data1 = self._subscriptions.get(stream)
                    if data1:
                        await data1.callback(ev)
                    data2 = self._subscr_groups.get(stream)
                    if data2:
                        await data2.callback(ev)

    async def _on_ws_connect(self) -> None:
        for stream, data1 in self._subscriptions.items():
            await self.subscribe(
                stream=stream,
                filters=data1.filters,
                timestamp=data1.timestamp,
                callback=data1.callback,
            )
        for stream, data2 in self._subscr_groups.items():
            await self.subscribe_group(
                stream=stream,
                filters=data2.filters,
                groupname=data2.groupname,
                callback=data2.callback,
            )

    def _get_sender(self, sender: str | None) -> str:
        if sender is not None:
            return sender
        sender = self._sender
        if sender is not None:
            return sender
        msg = "Either initialize EventsClient with a sender or pass it explicitly"
        raise ValueError(msg)

    async def send(
        self,
        *,
        stream: StreamType,
        event_type: EventType,
        sender: str | None = None,
        org: str | None = None,
        cluster: str | None = None,
        project: str | None = None,
        user: str | None = None,
        **kwargs: JsonT,
    ) -> SentItem | None:
        ev = SendEvent(
            sender=self._get_sender(sender),
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
            # in case of timeout, we don't want to raise an exception
            # do we need a strategy for resending unconfirmed events?
            return None

    async def subscribe(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        filters: Sequence[FilterItem] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        if timestamp is not None and timestamp.tzinfo is None:
            msg = "timespamp should be timezone-aware value"
            raise TypeError(msg)
        ev = Subscribe(stream=stream, filters=filters, timestamp=timestamp)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Subscribed] = loop.create_future()
        self._subscribed[ev.id] = fut
        self._subscriptions[stream] = _SubscrData(
            filters=ev.filters,
            timestamp=ev.timestamp or datetime.now(tz=UTC),
            callback=callback,
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
            # On reconnection, we re-subscribe for everything.
            # Thus, the method never fails
            self._subscribed.pop(ev.id, None)

    async def subscribe_group(
        self,
        stream: StreamType,
        groupname: GroupName,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        filters: Sequence[FilterItem] | None = None,
    ) -> None:
        ev = SubscribeGroup(stream=stream, filters=filters, groupname=groupname)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Subscribed] = loop.create_future()
        self._subscribed[ev.id] = fut
        self._subscr_groups[stream] = _SubscrGroupData(
            filters=ev.filters,
            groupname=groupname,
            callback=callback,
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
            # On reconnection, we re-subscribe for everything.
            # Thus, the method never fails
            self._subscribed.pop(ev.id, None)

    async def ack(
        self, events: dict[StreamType, list[Tag]], *, sender: str | None = None
    ) -> None:
        ev = Ack(sender=self._get_sender(sender), events=events)
        await self._raw_client.send(ev)
