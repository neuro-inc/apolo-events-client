import abc
import asyncio
import dataclasses
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import NotRequired, Self, TypedDict, override
from uuid import UUID

import aiohttp
from aiohttp import hdrs
from yarl import URL

from ._constants import PING_DELAY, RESP_TIMEOUT
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
        self._connected = asyncio.Event()

    async def _lazy_init(self) -> aiohttp.ClientWebSocketResponse:
        if self._closing:
            msg = "Operation on the closed client"
            raise RuntimeError(msg)
        if self._session is None:
            self._session = aiohttp.ClientSession()

        if self._ws is None or self._ws.closed:
            async with self._lock:
                if self._ws is None or self._ws.closed:
                    self._connected.clear()
                    self._ws = await self._session.ws_connect(
                        self._url / "v1" / "stream",
                        headers={hdrs.AUTHORIZATION: "Bearer " + self._token},
                    )
                    await self._on_ws_connect()
                    self._connected.set()

        assert self._ws is not None
        await self._connected.wait()
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
        self._connected.clear()
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
            self._connected.clear()
            await ws.close()

    async def reset(self) -> None:
        """Reset the current transport so the next operation reconnects."""
        ws = self._ws
        if ws is not None:
            await self._close_ws(ws)

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
                log.info("Disconnect on transport error", exc_info=True)
                await self._close_ws(ws)
                return None
            if ws_msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                log.info("Disconnect on closing transport [%s]", ws_msg.type)
                await self._close_ws(ws)
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
class _SubscrData:
    filters: tuple[FilterItem, ...] | None
    callback: Callable[[RecvEvent], Awaitable[None]]
    timestamp: datetime | None = None
    auto_ack: bool | None = None


class CtorArgs(TypedDict):
    url: URL | str
    token: str
    name: str
    ping_delay: NotRequired[float]
    resp_timeout: NotRequired[float]


class AbstractEventsClient(abc.ABC):
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_typ: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @abc.abstractmethod
    async def aclose(self) -> None:
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
    async def subscribe(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        filters: Sequence[FilterItem] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def subscribe_group(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        auto_ack: bool,
        filters: Sequence[FilterItem] | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def ack(
        self, events: dict[StreamType, list[Tag]], *, sender: str | None = None
    ) -> None:
        pass


class DummyEventsClient(AbstractEventsClient):
    @override
    async def aclose(self) -> None:
        pass

    @override
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
        pass

    @override
    async def subscribe(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        filters: Sequence[FilterItem] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        pass

    @override
    async def subscribe_group(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        auto_ack: bool,
        filters: Sequence[FilterItem] | None = None,
    ) -> None:
        pass

    @override
    async def ack(
        self, events: dict[StreamType, list[Tag]], *, sender: str | None = None
    ) -> None:
        pass


class EventsClient(AbstractEventsClient):
    def __init__(
        self,
        *,
        url: URL | str,
        token: str,
        name: str,
        ping_delay: float = PING_DELAY,
        resp_timeout: float = RESP_TIMEOUT,
    ) -> None:
        self._closing = False
        self._raw_client = RawEventsClient(
            url=url,
            token=token,
            ping_delay=ping_delay,
            on_ws_connect=self._on_ws_connect,
        )
        self._resp_timeout = resp_timeout
        self._name = name
        self._task: asyncio.Task[None] | None = None
        self._resubscribe_task: asyncio.Task[None] | None = None
        self._resubscribe_again = False
        self._has_connected = False

        self._sent: dict[UUID, asyncio.Future[SentItem]] = {}
        self._subscribed: dict[UUID, asyncio.Future[Subscribed]] = {}
        self._subscriptions: dict[StreamType, _SubscrData] = {}
        self._subscr_groups: dict[StreamType, _SubscrData] = {}
        self._resubscribe: set[StreamType] = set()
        self._resubscribe_group: set[StreamType] = set()

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

    @override
    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True

        for sent_fut in self._sent.values():
            if not sent_fut.done():
                sent_fut.cancel()
        for subscribed_fut in self._subscribed.values():
            if not subscribed_fut.done():
                subscribed_fut.cancel()
        self._sent.clear()
        self._subscribed.clear()

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in (self._resubscribe_task, self._task)
            if task is not None and task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        await self._raw_client.aclose()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._resubscribe_task = None
        if self._task is not current_task:
            self._task = None

    def _fail_pending(self, ex: Exception) -> None:
        for sent_fut in self._sent.values():
            if not sent_fut.done():
                sent_fut.set_exception(ex)
        for subscribed_fut in self._subscribed.values():
            if not subscribed_fut.done():
                subscribed_fut.set_exception(ex)
        self._sent.clear()
        self._subscribed.clear()

    async def _loop(self) -> None:
        while not self._closing:
            try:
                await self._loop_once()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.exception("Receiver failed; reconnecting")
                self._fail_pending(ex)
                await self._raw_client.reset()

    async def _loop_once(self) -> None:
        msg = await self._raw_client.receive()
        match msg:
            case None:
                pass
            case Sent():
                for event in msg.events:
                    sent_fut = self._sent.pop(event.id, None)
                    if sent_fut is not None and not sent_fut.done():
                        sent_fut.set_result(event)
                    else:
                        log.warning(
                            "Received Sent response for unknown id %s", event.id
                        )
            case Subscribed():
                subscr_fut = self._subscribed.pop(msg.subscr_id, None)
                if subscr_fut is not None and not subscr_fut.done():
                    subscr_fut.set_result(msg)
                else:
                    log.warning(
                        "Received Subscribed response for unknown id %s", msg.id
                    )
            case _RecvEvents():
                auto_acks: defaultdict[StreamType, list[Tag]] = defaultdict(list)
                for ev in msg.events:
                    stream = ev.stream
                    data1 = self._subscriptions.get(stream)
                    if data1:
                        try:
                            await data1.callback(ev)
                            data1.timestamp = ev.timestamp
                        except Exception:
                            log.exception(
                                "Unhandled error during processing %r(%r)",
                                data1.callback,
                                msg,
                            )

                    data2 = self._subscr_groups.get(stream)
                    if data2:
                        try:
                            await data2.callback(ev)
                            if data2.auto_ack:
                                auto_acks[stream].append(ev.tag)
                        except Exception:
                            log.exception(
                                "Unhandled error during processing %r(%r)",
                                data2.callback,
                                msg,
                            )

                if auto_acks:
                    await self.ack(auto_acks)

    async def _on_ws_connect(self) -> None:
        if self._task is None or self._task.done():
            # start receiver
            self._task = asyncio.create_task(self._loop(), name="apolo-events-receiver")
            self._task.add_done_callback(self._receiver_done)

        is_reconnect = self._has_connected
        self._has_connected = True
        if is_reconnect:
            self._schedule_resubscribe()

    def _receiver_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Websocket receiver task stopped")

    def _schedule_resubscribe(self) -> None:
        if not self._resubscribe and not self._resubscribe_group:
            return
        if self._resubscribe_task is not None and not self._resubscribe_task.done():
            self._resubscribe_again = True
            return
        self._resubscribe_again = False
        task = asyncio.create_task(
            self._resubscribe_all(), name="apolo-events-resubscribe"
        )
        self._resubscribe_task = task
        task.add_done_callback(self._resubscribe_done)

    def _resubscribe_done(self, task: asyncio.Task[None]) -> None:
        if self._resubscribe_task is task:
            self._resubscribe_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Unhandled error during websocket resubscription")
        if self._resubscribe_again and not self._closing:
            self._schedule_resubscribe()

    async def _resubscribe_all(self) -> None:
        for stream in tuple(self._resubscribe):
            data = self._subscriptions[stream]
            assert data.auto_ack is None
            try:
                await self.subscribe(
                    stream=stream,
                    filters=data.filters,
                    timestamp=data.timestamp,
                    callback=data.callback,
                )
            except Exception:
                log.exception(
                    "Failed subscribe(%r, %r, filters=%r, timestamp=%r)",
                    stream,
                    data.callback,
                    data.filters,
                    data.timestamp,
                )
        for stream in tuple(self._resubscribe_group):
            data = self._subscr_groups[stream]
            assert data.timestamp is None
            assert data.auto_ack is not None
            try:
                await self.subscribe_group(
                    stream=stream,
                    filters=data.filters,
                    callback=data.callback,
                    auto_ack=data.auto_ack,
                )
            except Exception:
                log.exception(
                    "Failed subscribe_group(%r, %r, filters=%r)",
                    stream,
                    data.callback,
                    data.filters,
                )

    @override
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
            sender=sender or self._name,
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
        try:
            async with asyncio.timeout(self._resp_timeout):
                await self._raw_client.send(ev)
                return await fut
        except TimeoutError:
            log.warning("Send timeout for %s/%s", stream, event_type)
            return None
        finally:
            self._sent.pop(ev.id, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()

    @override
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
        self._resubscribe.add(stream)
        try:
            async with asyncio.timeout(self._resp_timeout):
                await self._raw_client.send(ev)
                await fut
        except TimeoutError:
            # On reconnection, we re-subscribe for everything.
            # Thus, the method never fails
            pass
        finally:
            self._subscribed.pop(ev.id, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()

    @override
    async def subscribe_group(
        self,
        stream: StreamType,
        callback: Callable[[RecvEvent], Awaitable[None]],
        *,
        auto_ack: bool,
        filters: Sequence[FilterItem] | None = None,
    ) -> None:
        ev = SubscribeGroup(
            stream=stream, filters=filters, groupname=GroupName(self._name)
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Subscribed] = loop.create_future()
        self._subscribed[ev.id] = fut
        self._subscr_groups[stream] = _SubscrData(
            filters=ev.filters,
            callback=callback,
            auto_ack=auto_ack,
        )
        self._resubscribe_group.add(stream)
        try:
            async with asyncio.timeout(self._resp_timeout):
                await self._raw_client.send(ev)
                await fut
        except TimeoutError:
            # On reconnection, we re-subscribe for everything.
            # Thus, the method never fails
            pass
        finally:
            self._subscribed.pop(ev.id, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()

    @override
    async def ack(
        self, events: dict[StreamType, list[Tag]], *, sender: str | None = None
    ) -> None:
        ev = Ack(sender=sender or self._name, events=events)
        try:
            async with asyncio.timeout(self._resp_timeout):
                await self._raw_client.send(ev)
        except TimeoutError:
            log.warning("Ack timeout")
