"""Test-only WebSocket bridge to an explicitly supplied local PostgreSQL DB.

This is not an alternative cloud service. It forwards actual PostgreSQL wire
bytes so the optional Neon transport can be tested without a remote account.
Only loopback PostgreSQL URLs with sslmode=disable are accepted. Neither SQL,
credentials nor protocol payloads are logged.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from urllib.parse import parse_qs, urlsplit


def local_postgres_target(database_url):
    """Return a fixed loopback destination, rejecting all remote test targets."""
    try:
        parsed = urlsplit(database_url)
        query = parse_qs(parsed.query)
        if (
            parsed.scheme not in ("postgres", "postgresql")
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or query.get("sslmode") != ["disable"]
            or not parsed.path.strip("/")
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port or 5432
        if not 1 <= port <= 65535:
            raise ValueError
        # Never perform DNS resolution of an arbitrary supplied hostname.
        host = "::1" if parsed.hostname == "::1" else "127.0.0.1"
        return host, port
    except (TypeError, ValueError):
        raise ValueError("WebSocket PostgreSQL QA requires a loopback URL with sslmode=disable") from None


class PostgresWebSocketProxy:
    """A context-managed /v2 binary bridge, with a separate TCP session per WS."""

    def __init__(self, database_url):
        self._host, self._port = local_postgres_target(database_url)
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._stop = None
        self._server = None
        self._error = None
        self._session_count = 0
        self._client_bytes = 0
        self._server_bytes = 0
        self.url = None

    @property
    def session_count(self):
        with self._lock:
            return self._session_count

    @property
    def byte_counts(self):
        with self._lock:
            return self._client_bytes, self._server_bytes

    async def _bridge(self, websocket):
        if websocket.request.path != "/v2":
            await websocket.close(code=1008, reason="Unsupported test route")
            return
        writer = None
        tasks = []
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            with self._lock:
                self._session_count += 1

            async def to_postgres():
                # websockets reassembles fragmented incoming binary messages.
                async for payload in websocket:
                    if not isinstance(payload, bytes):
                        await websocket.close(code=1003, reason="Binary protocol required")
                        return
                    with self._lock:
                        self._client_bytes += len(payload)
                    writer.write(payload)
                    await writer.drain()

            async def to_websocket():
                while payload := await reader.read(16384):
                    with self._lock:
                        self._server_bytes += len(payload)
                    # Fragment every outgoing WS message. The payload is also
                    # split into arbitrary messages independently of PG frame
                    # boundaries, exercising the client's buffered byte stream.
                    fragments = [payload[:1], payload[1:5], payload[5:]]
                    await websocket.send([part for part in fragments if part])

            tasks = [asyncio.create_task(to_postgres()), asyncio.create_task(to_websocket())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            # Retrieve all exceptions, without serializing protocol or SQL data.
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except Exception:
            # The peer receives a finite close code; exception details may
            # contain connection diagnostics and must not become test output.
            await websocket.close(code=1011, reason="Local test bridge unavailable")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            await websocket.close()

    async def _serve(self):
        from websockets.asyncio.server import serve

        self._stop = asyncio.Event()
        # Explicitly disable library logging: raw authentication and database
        # payloads must never be exposed by a verbose websocket logger.
        logger = logging.Logger("zhixu.local-postgres-websocket-test")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        async with serve(
            self._bridge, "127.0.0.1", 0,
            compression=None, ping_interval=None, close_timeout=1,
            max_size=None, max_queue=16, logger=logger,
        ) as server:
            self._server = server
            self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/v2"
            self._ready.set()
            await self._stop.wait()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as error:
            # Keep only a finite error category, never a URL or arbitrary text.
            self._error = type(error).__name__
            self._ready.set()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def __enter__(self):
        if self._thread is not None:
            raise RuntimeError("Local WebSocket test bridge cannot be reused")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self._error or self.url is None:
            self.__exit__(None, None, None)
            raise RuntimeError("Local WebSocket PostgreSQL test bridge failed to start") from None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("Local WebSocket PostgreSQL test bridge did not stop")
        return False
