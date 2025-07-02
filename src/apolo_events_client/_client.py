import asyncio
from collections.abc import AsyncIterator
from types import TracebackType

import aiohttp
from aiohttp import hdrs
from yarl import URL

from ._exceptions import ServerError
from ._messages import (
    ClientMsgTypes,
    Error,
    Pong,
    ServerMessage,
    ServerMsgTypes,
)


class RawEventsClient:
    def __init__(self, url: URL | str, token: str, *, ping_delay: float = 60) -> None:
        self._closing = False
        self._url = URL(url)
        self._token = token
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ping_delay = ping_delay

    async def _lazy_init(self) -> aiohttp.ClientWebSocketResponse:
        if self._closing:
            msg = "Operation on the closed client"
            raise RuntimeError(msg)
        if self._session is None:
            self._session = aiohttp.ClientSession()

            self._ws = await self._session.ws_connect(
                self._url, headers={hdrs.AUTHORIZATION: "Bearer " + self._token}
            )
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
        if self._closing:
            return
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def send(self, msg: ClientMsgTypes) -> None:
        """Send a message through the wire."""
        ws = await self._lazy_init()
        await ws.send_str(msg.model_dump_json())

    async def iter_received(self) -> AsyncIterator[ServerMsgTypes]:
        """Receive next upcoming message"""
        ws = await self._lazy_init()
        try:
            async for ws_msg in ws:
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
                        yield resp.root
        except Exception:
            await ws.close()
            self._ws = None
            raise


class EventsClient:
    def __init__(self, url: URL | str, token: str, *, ping_delay: float = 60) -> None:
        self._raw_client = RawEventsClient(url, token, ping_delay=ping_delay)
        self._out: asyncio.Queue[ServerMsgTypes | Exception] = asyncio.Queue(10_000)
        self._task = asyncio.create_task(self._loop())

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
        await self._raw_client.aclose()
        self._out.shutdown()
        await self._out.join()

    async def iter_received(self) -> AsyncIterator[ServerMsgTypes]:
        while True:
            try:
                val = await self._out.get()
            except asyncio.QueueShutDown:
                # WebSocket is closed
                return
            self._out.task_done()
            if isinstance(val, Exception):
                raise val
            else:
                yield val

    async def _loop(self) -> None:
        try:
            while True:
                await self._loop_once()
        except aiohttp.ClientError:
            self._out.shutdown()
        except Exception as ex:
            await self._out.put(ex)

    async def _loop_once(self) -> None:
        async for msg in self._raw_client.iter_received():
            await self._out.put(msg)
