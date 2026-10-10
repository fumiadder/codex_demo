"""PostgreSQL sessions over Neon's official, certificate-verified WSS endpoint.

Only the transport changes: pg8000 still speaks PostgreSQL's native session
protocol, including transactions, parameter binding and transaction timeouts. The
official Neon SDK uses ``wss://<database hostname>/v2`` and disables inner
PostgreSQL TLS because the outer WebSocket already authenticates and encrypts
the entire connection. No database credentials enter the WebSocket URL or
handshake headers. This module deliberately has no HTTP-query fallback.
"""
from __future__ import annotations

import math
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, unquote, urlsplit


_SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Media is stored in S3; the largest application database row is well below
# one MiB. Leave headroom while refusing hostile PG lengths/WS allocations.
_MAX_PACKET_BYTES = 16 * 1024 * 1024
_MAX_BUFFER_BYTES = 2 * _MAX_PACKET_BYTES
_ALLOWED_TIMEOUTS = {
    "statement_timeout": 15000,
    "lock_timeout": 15000,
    "idle_in_transaction_session_timeout": 30000,
}
_REASONS = {
    "configuration", "tls_certificate", "channel_binding", "authentication",
    "connection_failure", "timeout", "dns", "unknown",
}
_SQLSTATE_REASONS = {
    "0A000": "configuration", "3D000": "configuration",
    "42704": "configuration", "53300": "connection_failure",
    "53400": "connection_failure", "57P03": "connection_failure",
}
_PROTOCOL_DETAILS = {
    "endpoint_missing", "endpoint_unknown", "endpoint_unavailable",
    "startup_parameters", "protocol_version", "protocol_message", "unknown",
}
# Only fixed labels may leave this module. Matching phrases are diagnostic
# hints, never instructions to alter routing, TLS or authentication.
_PROTOCOL_DETAIL_PHRASES = (
    ("endpoint_missing", (
        "endpoint id is not specified", "endpoint id is missing",
        "endpoint is not specified", "missing endpoint id",
    )),
    ("endpoint_unknown", (
        "endpoint not found", "endpoint is not found", "unknown endpoint",
        "invalid endpoint id", "project or endpoint not found",
    )),
    ("endpoint_unavailable", (
        "endpoint is not available", "endpoint is unavailable",
        "endpoint is disabled", "endpoint is suspended", "endpoint is not ready",
    )),
    ("startup_parameters", (
        "unsupported startup parameter", "unrecognized configuration parameter",
        "invalid startup parameter",
    )),
    ("protocol_version", (
        "unsupported frontend protocol", "unsupported protocol version",
        "invalid protocol version",
    )),
    ("protocol_message", (
        "invalid startup packet", "invalid startup message", "invalid message format",
        "unexpected message type", "invalid frontend message type",
        "message contents do not agree with length",
    )),
)


class Error(Exception):
    """Driver-compatible error containing only finite, non-secret diagnostics."""

    def __init__(self, *, reason="unknown", sqlstate=None, detail=None, connecting=False):
        self.reason = reason if type(reason) is str and reason in _REASONS else "unknown"
        self.sqlstate = sqlstate if type(sqlstate) is str and _SQLSTATE.fullmatch(sqlstate) else None
        self.detail = detail if self.sqlstate == "08P01" and type(detail) is str and detail in _PROTOCOL_DETAILS else None
        phase = "connection" if connecting else "operation"
        suffix = f", sqlstate={self.sqlstate}" if self.sqlstate else ""
        if self.detail is not None:
            suffix += f", detail={self.detail}"
        super().__init__(f"Cloud database {phase} failed (reason={self.reason}{suffix})")


class IntegrityError(Error):
    """PostgreSQL SQLSTATE class 23, matching the persistence facade contract."""


class IsolationLevel(Enum):
    READ_COMMITTED = "READ COMMITTED"


def _safe_attribute(value, name, default=None):
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _driver_error_fields(error):
    args = _safe_attribute(error, "args", ())
    return args[0] if type(args) is tuple and args and type(args[0]) is dict else {}


def _protocol_error_detail(error, sqlstate):
    if sqlstate != "08P01":
        return None
    if isinstance(error, Error):
        detail = _safe_attribute(error, "detail")
        return detail if type(detail) is str and detail in _PROTOCOL_DETAILS else "unknown"
    fields = _driver_error_fields(error)
    # Inspect bounded, exact strings only; never stringify arbitrary objects,
    # render server text, or carry it in the sanitized exception's attributes.
    messages = [fields.get(key) for key in ("M", "H")]
    messages = [value[:8192].lower() for value in messages if type(value) is str]
    for label, phrases in _PROTOCOL_DETAIL_PHRASES:
        if any(phrase in message for message in messages for phrase in phrases):
            return label
    return "unknown"


def _error_details(error):
    """Inspect driver exceptions internally without ever returning raw text."""
    sqlstate = None
    if isinstance(error, Error):
        reason, sqlstate = _safe_attribute(error, "reason"), _safe_attribute(error, "sqlstate")
        reason = reason if type(reason) is str and reason in _REASONS else "unknown"
        sqlstate = sqlstate if type(sqlstate) is str and _SQLSTATE.fullmatch(sqlstate) else None
        return reason, sqlstate
    fields = _driver_error_fields(error)
    if fields:
        candidate = fields.get("C")
        if type(candidate) is str and _SQLSTATE.fullmatch(candidate):
            sqlstate = candidate
    if sqlstate and sqlstate.startswith("28"):
        return "authentication", sqlstate
    if sqlstate and sqlstate.startswith("08"):
        return "connection_failure", sqlstate
    if sqlstate in _SQLSTATE_REASONS:
        return _SQLSTATE_REASONS[sqlstate], sqlstate
    # pg8000 sometimes wraps a socket exception. Only fixed labels survive;
    # arbitrary server messages, SQL, URI strings and row values never do.
    current, seen = error, set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        try:
            detail = str(current).lower()
        except Exception:
            detail = ""
        if isinstance(current, ssl.SSLCertVerificationError) or any(word in detail for word in (
            "certificate verify failed", "certificate verification failed",
            "hostname mismatch", "does not match host name", "self-signed certificate",
            "certificate has expired",
        )):
            return "tls_certificate", sqlstate
        if isinstance(current, TimeoutError) or "timeout" in detail or "timed out" in detail:
            return "timeout", sqlstate
        if isinstance(current, socket.gaierror) or any(word in detail for word in (
            "name or service not known", "temporary failure in name resolution",
            "nodename nor servname provided",
        )):
            return "dns", sqlstate
        if "channel binding" in detail or "channel_binding" in detail:
            return "channel_binding", sqlstate
        if "authentication failed" in detail:
            return "authentication", sqlstate
        if isinstance(current, (ConnectionError, OSError)) or any(word in detail for word in (
            "network error", "connection is closed", "connection closed",
            "connection refused", "network is unreachable", "connection reset",
        )):
            # A wrapper may hide a more specific DNS/TLS/timeout cause.
            cause = _safe_attribute(current, "__cause__")
            if cause is not None and id(cause) not in seen:
                current = cause
                continue
            return "connection_failure", sqlstate
        if isinstance(current, (ValueError, TypeError)):
            return "configuration", sqlstate
        current = _safe_attribute(current, "__cause__")
    return "unknown", sqlstate


def _safe_error(error, *, connecting=False):
    reason, sqlstate = _error_details(error)
    error_type = IntegrityError if sqlstate and sqlstate.startswith("23") else Error
    return error_type(
        reason=reason, sqlstate=sqlstate, detail=_protocol_error_detail(error, sqlstate),
        connecting=connecting,
    )


class _WebSocketStream:
    """Minimal socket/file object preserving a PostgreSQL byte stream.

    WebSocket message boundaries are unrelated to PostgreSQL packet boundaries.
    Reads keep unused bytes and span any number of binary messages. pg8000's
    writes are coalesced until its explicit flush, then sent as a binary message.
    """

    def __init__(self, websocket, timeout):
        self._websocket = websocket
        self._timeout = timeout
        self._incoming = bytearray()
        self._outgoing = bytearray()
        self._eof = False
        self._closed = False

    def makefile(self, mode="rwb", *args, **kwargs):
        if mode != "rwb" or args or kwargs:
            raise Error(reason="configuration")
        return self

    def read(self, size):
        if type(size) is not int or size < 0:
            raise Error(reason="configuration")
        if size > _MAX_PACKET_BYTES:
            raise Error(reason="connection_failure")
        if size == 0:
            return b""
        deadline = time.monotonic() + self._timeout
        self._websocket._receive_deadline = deadline
        try:
            while len(self._incoming) < size and not (self._eof or self._closed):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error(reason="timeout")
                self._websocket.settimeout(remaining)
                # The bounded client handles ping/pong and returns validated
                # binary fragments incrementally, without message accumulation.
                opcode, data = self._websocket.recv_data()
                if time.monotonic() > deadline:
                    raise Error(reason="timeout")
                if opcode == 8:  # RFC 6455 CLOSE: EOF, never a database payload.
                    self._eof = True
                elif opcode in (9, 10):  # Client already replies to valid PING.
                    continue
                elif opcode == 2 and isinstance(data, (bytes, bytearray)):
                    if len(data) > _MAX_PACKET_BYTES or len(self._incoming) + len(data) > _MAX_BUFFER_BYTES:
                        raise Error(reason="connection_failure")
                    self._incoming.extend(data)
                else:  # In particular, a text message cannot enter PG framing.
                    raise Error(reason="connection_failure")
            data = bytes(self._incoming[:size])
            del self._incoming[:size]
            return data
        except Exception as error:
            raise _safe_error(error) from None
        finally:
            self._websocket._receive_deadline = None
            if not self._closed:
                try:
                    self._websocket.settimeout(self._timeout)
                except Exception:
                    pass

    def write(self, data):
        if self._closed or self._eof:
            raise Error(reason="connection_failure")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise Error(reason="configuration")
        if len(self._outgoing) + len(data) > _MAX_PACKET_BYTES:
            raise Error(reason="connection_failure")
        self._outgoing.extend(data)
        return len(data)

    def flush(self):
        if self._closed or self._eof:
            raise Error(reason="connection_failure")
        if self._outgoing:
            try:
                self._websocket.send_binary(bytes(self._outgoing))
                self._outgoing.clear()
            except Exception as error:
                raise _safe_error(error) from None

    def close(self):
        if self._closed:
            return
        self._closed, self._eof = True, True
        self._outgoing.clear()
        self._incoming.clear()
        try:
            self._websocket.close(timeout=1)
        except Exception as error:
            raise _safe_error(error) from None


@dataclass(frozen=True)
class _Column:
    name: str


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        try:
            description = self._cursor.description
            return None if description is None else tuple(_Column(column[0]) for column in description)
        except Exception as error:
            raise _safe_error(error) from None

    @property
    def rowcount(self):
        try:
            return self._cursor.rowcount
        except Exception as error:
            raise _safe_error(error) from None

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        except Exception as error:
            raise _safe_error(error) from None

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        except Exception as error:
            raise _safe_error(error) from None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._cursor)
        except StopIteration:
            raise
        except Exception as error:
            raise _safe_error(error) from None


def _native_query(sql, parameters):
    """Translate the persistence facade's format SQL without driver rescanning.

    psycopg escapes every percent sign when parameters are supplied, including
    in literals and comments. pg8000's DBAPI format scanner has different
    escaping rules and does not support tagged dollar quotes/nested comments.
    Reuse the application's delimiter scanner, decode escaped percents in all
    segments, and introduce numbered placeholders only in executable SQL.
    """
    from persistence import _sql_segments

    values = tuple(parameters)
    parts, count = [], 0
    for is_code, segment in _sql_segments(sql):
        if not is_code:
            parts.append(segment.replace("%%", "%"))
            continue
        index, converted = 0, []
        while index < len(segment):
            char = segment[index]
            if char != "%":
                converted.append(char)
                index += 1
            elif segment.startswith("%%", index):
                converted.append("%")
                index += 2
            elif segment.startswith("%s", index):
                count += 1
                converted.append(f"${count}")
                index += 2
            else:
                raise Error(reason="configuration")
        parts.append("".join(converted))
    if count != len(values):
        raise Error(reason="configuration")
    return "".join(parts), values


class RawConnection:
    def __init__(self, connection, stream, timeouts=()):
        self._connection, self._stream = connection, stream
        self._timeouts = timeouts
        self._isolation_level = IsolationLevel.READ_COMMITTED

    def _begin_if_needed(self):
        if self._connection.autocommit:
            if self._timeouts:
                raise Error(reason="configuration")
            return
        if not self._connection._in_transaction:
            # Neon's WSS proxy may reject the PostgreSQL startup `options`
            # field. Configure the same safeguards through ordinary PG SQL in
            # the actual transaction, before any application query or lock.
            self._connection.execute_simple("BEGIN ISOLATION LEVEL READ COMMITTED")
            for name, milliseconds in self._timeouts:
                self._connection.execute_simple(f"SET LOCAL {name} = {milliseconds}")

    def execute(self, sql, parameters=None):
        try:
            query, values = (None, None) if parameters is None else _native_query(sql, parameters)
            self._begin_if_needed()
            cursor = self._connection.cursor()
            # Omitting parameters selects pg8000's simple-query path. Passing
            # None explicitly would fail len(args), and would change the path.
            if parameters is None:
                cursor.execute(sql)
            else:
                # Preserve DBAPI's implicit transaction and buffered cursor
                # behavior, but bypass its incompatible format scanner. These
                # attributes are the pinned pg8000 1.31.5 DBAPI implementation.
                cursor._context = self._connection.execute_unnamed(
                    query, vals=values, oids=cursor._input_oids,
                )
                cursor._row_iter = None if cursor._context.rows is None else iter(cursor._context.rows)
                cursor._input_oids = ()
                cursor.input_types = []
            return Cursor(cursor)
        except Exception as error:
            raise _safe_error(error) from None

    @property
    def isolation_level(self):
        return self._isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        if value is not IsolationLevel.READ_COMMITTED:
            raise Error(reason="configuration")
        try:
            if self._connection.autocommit:
                raise Error(reason="configuration")
            self._begin_if_needed()
            self._isolation_level = value
        except Exception as error:
            raise _safe_error(error) from None

    def commit(self):
        try:
            self._connection.commit()
        except Exception as error:
            raise _safe_error(error) from None

    def rollback(self):
        try:
            self._connection.rollback()
        except Exception as error:
            raise _safe_error(error) from None

    def close(self):
        try:
            self._connection.close()
        except Exception as error:
            raise _safe_error(error) from None
        finally:
            self._stream.close()


def _connection_settings(database_url, sslmode, sslrootcert):
    parsed = urlsplit(database_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme not in {"postgresql", "postgres"} or parsed.fragment
        or not parsed.hostname or not parsed.username or not parsed.path.strip("/")
        or (parsed.port is not None and parsed.port < 1)
        or any(len(values) != 1 for values in query.values())
        or set(query) - {"sslmode", "sslrootcert", "channel_binding"}
    ):
        raise Error(reason="configuration")
    binding = query.get("channel_binding", [None])[0]
    if binding == "require":
        # Channel binding is defined for inner PostgreSQL TLS, not the outer
        # WSS tunnel. Refuse it instead of silently dropping a security rule.
        raise Error(reason="channel_binding")
    if binding not in {None, "prefer", "disable"}:
        raise Error(reason="configuration")
    host = parsed.hostname.lower()
    url_mode = query.get("sslmode", [None])[0]
    effective_mode = sslmode if sslmode is not None else url_mode
    override = os.environ.get("NEON_WS_TEST_URL")
    local = host in _LOCAL_HOSTS
    if local:
        if url_mode != "disable" or effective_mode != "disable" or not override:
            raise Error(reason="configuration")
        fixture = urlsplit(override)
        if (
            fixture.scheme != "ws" or fixture.hostname not in _LOCAL_HOSTS
            or fixture.port is None or fixture.port < 1
            or fixture.username is not None or fixture.password is not None
            or fixture.path != "/v2" or fixture.query or fixture.fragment
        ):
            raise Error(reason="configuration")
        ws_url, ca_file = override, None
    else:
        if (
            override is not None
            or parsed.port not in {None, 5432}
            or not _HOSTNAME.fullmatch(host) or len(host) > 253
            or any(not label or len(label) > 63 for label in host.split("."))
            or not host.endswith((".neon.tech", ".neon.build"))
            or "-pooler" in host.split(".")[0]
            or url_mode not in {None, "require", "verify-ca", "verify-full"}
            or effective_mode not in {None, "require", "verify-ca", "verify-full"}
        ):
            raise Error(reason="configuration")
        ws_url = f"wss://{host}/v2"
        ca_file = sslrootcert or query.get("sslrootcert", [None])[0]
    user, password, database = (
        unquote(parsed.username), unquote(parsed.password or ""), unquote(parsed.path[1:]),
    )
    if any("\x00" in value for value in (user, password, database)):
        raise Error(reason="configuration")
    return host, user, password, database, ws_url, ca_file


def _load_dependencies():
    try:
        import pg8000.dbapi
        import websocket
    except ImportError:
        raise Error(reason="configuration") from None
    return pg8000.dbapi, websocket


def _transaction_timeouts(options):
    """Accept only the application's fixed timeout settings, never SQL/GUCs."""
    if options is None:
        return ()
    if type(options) is not str:
        raise Error(reason="configuration")
    tokens = options.split()
    if not tokens or len(tokens) % 2:
        raise Error(reason="configuration")
    selected = {}
    for index in range(0, len(tokens), 2):
        if tokens[index] != "-c":
            raise Error(reason="configuration")
        name, separator, value = tokens[index + 1].partition("=")
        if (
            separator != "=" or name not in _ALLOWED_TIMEOUTS or name in selected
            or value != str(_ALLOWED_TIMEOUTS[name])
        ):
            raise Error(reason="configuration")
        selected[name] = _ALLOWED_TIMEOUTS[name]
    # Names and numeric values used in SET LOCAL come exclusively from this
    # allowlist; no caller-provided fragment is ever interpolated into SQL.
    return tuple((name, selected[name]) for name in _ALLOWED_TIMEOUTS if name in selected)


def _bounded_websocket_class(websocket):
    """Bound allocation before websocket-client reads a frame's payload.

    The pinned client's frame_buffer requests each complete payload via
    recv_strict after reading its length. Wrapping that boundary rejects an
    oversized length before allocation. fire_cont_frame returns fragments
    immediately, so continuous_frame never accumulates an unbounded message.
    Its standard validator still checks opcode/continuation ordering.
    """
    class BoundedWebSocket(websocket.WebSocket):
        def __init__(self, *args, **kwargs):
            self._receive_deadline = None
            kwargs["fire_cont_frame"] = True
            super().__init__(*args, **kwargs)
            self._binary_fragment = False
            receive = self.frame_buffer.recv_strict

            def bounded_receive(size):
                if size > _MAX_PACKET_BYTES:
                    raise Error(reason="connection_failure")
                return receive(size)

            self.frame_buffer.recv_strict = bounded_receive

        def _recv(self, size):
            deadline = self._receive_deadline
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error(reason="timeout")
                self.settimeout(remaining)
            data = super()._recv(size)
            if deadline is not None and time.monotonic() > deadline:
                raise Error(reason="timeout")
            return data

        def recv_data(self, control_frame=False):
            # Return control frames to the outer deadline loop as well. Hiding
            # an endless PING/PONG stream here would defeat an absolute timeout.
            opcode, frame = self.recv_data_frame(control_frame=True)
            if opcode == 2:
                self._binary_fragment = not frame.fin
            elif opcode == 0:
                if not self._binary_fragment:
                    raise Error(reason="connection_failure")
                self._binary_fragment = not frame.fin
                opcode = 2
            return opcode, frame.data

    return BoundedWebSocket


def connect(database_url, *, connect_timeout=10, options=None, autocommit=False,
            sslmode=None, sslrootcert=None):
    """Open one native PostgreSQL session through the fixed official endpoint."""
    stream = None
    websocket_connection = None
    try:
        timeout = float(connect_timeout)
        if not math.isfinite(timeout) or timeout <= 0 or type(autocommit) is not bool:
            raise Error(reason="configuration")
        timeouts = _transaction_timeouts(options)
        if autocommit and timeouts:
            raise Error(reason="configuration")
        host, user, password, database, ws_url, ca_file = _connection_settings(
            database_url, sslmode, sslrootcert,
        )
        dbapi, websocket = _load_dependencies()
        # Never enable frame tracing: PostgreSQL authentication and queries are
        # binary payloads and must not appear in application/deployment logs.
        websocket.enableTrace(False)
        ssl_options = {}
        if ws_url.startswith("wss:"):
            context = ssl.create_default_context(cafile=ca_file)
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            ssl_options = {"context": context, "cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}
        websocket_connection = websocket.create_connection(
            ws_url, timeout=timeout, sslopt=ssl_options, redirect_limit=0,
            suppress_origin=True, http_proxy_timeout=timeout,
            class_=_bounded_websocket_class(websocket),
        )
        # websocket-client accepts redirect handshakes when redirect_limit=0;
        # inspect status before pg8000 can send credentials to this connection.
        if websocket_connection.getstatus() != 101:
            raise Error(reason="connection_failure")
        stream = _WebSocketStream(websocket_connection, timeout)
        connection = dbapi.connect(
            user=user, password=password, database=database, host=host,
            port=5432, sock=stream, ssl_context=False, timeout=timeout,
            startup_params={},
        )
        connection.autocommit = autocommit
        return RawConnection(connection, stream, timeouts)
    except Exception as error:
        if stream is not None:
            try:
                stream.close()
            except Error:
                pass
        elif websocket_connection is not None:
            try:
                websocket_connection.close(timeout=1)
            except Exception:
                pass
        raise _safe_error(error, connecting=True) from None
