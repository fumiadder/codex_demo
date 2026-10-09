"""Request-scoped SQLite/PostgreSQL persistence for the workbench.

SQLite remains the local default. PostgreSQL uses its native protocol by
default, with an opt-in Neon WebSocket transport. Each context owns one
transaction; no connection or credential is sent to the browser.
The small compatibility layer covers the SQL used by server.py and bedtime.py.
"""
from __future__ import annotations

import os
import re
import socket
import sqlite3
import ssl
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


# A shared lock namespace is needed for invitation consumption, permission
# revocation and capacity checks across all server connections. PostgreSQL
# releases a transaction-scoped lock on commit, rollback or connection loss.
_WRITER_LOCK = (0x464F4C49, 0x4F575249)
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")
_SCHEMA_TYPES = re.compile(r"\b(INTEGER|REAL)\b", re.IGNORECASE)
_SQLITE_PRAGMAS = re.compile(
    r"PRAGMA\s+(?:journal_mode\s*=\s*WAL|foreign_keys\s*=\s*ON|busy_timeout\s*=\s*15000)\s*;?",
    re.IGNORECASE,
)
# Only these server codes may appear in connection diagnostics. Never render
# arbitrary driver attributes, messages, URLs, hostnames or authentication data.
_CONNECTION_SQLSTATES = {
    "0A000": "configuration",
    "08000": "connection_failure",
    "08001": "connection_failure",
    "08003": "connection_failure",
    "08004": "connection_failure",
    "08006": "connection_failure",
    "08P01": "connection_failure",
    "28000": "authentication",
    "28P01": "authentication",
    "3D000": "configuration",
    "53300": "connection_failure",
    "53400": "connection_failure",
    "57P03": "connection_failure",
}
_CONNECTION_REASONS = frozenset({
    "configuration", "connection_failure", "authentication", "tls_certificate",
    "channel_binding", "timeout", "dns", "unknown",
})


def _connection_failure_message(error):
    """Return finite diagnostic labels, without exposing libpq error details."""
    try:
        sqlstate = getattr(error, "sqlstate", None)
    except Exception:
        sqlstate = None
    if type(sqlstate) is not str or sqlstate not in _CONNECTION_SQLSTATES:
        sqlstate = None
    reason = _CONNECTION_SQLSTATES.get(sqlstate, "unknown")
    # The optional transport reports fixed labels after hiding lower-level
    # errors. Treat them as a finite enum, never as arbitrary driver output.
    try:
        transport_reason = getattr(error, "reason", None)
    except Exception:
        transport_reason = None
    if type(transport_reason) is str and transport_reason in _CONNECTION_REASONS:
        # A transport without more specific detail must still preserve a
        # recognized server authentication/configuration diagnosis.
        if transport_reason != "unknown":
            reason = transport_reason
        suffix = f", sqlstate={sqlstate}" if sqlstate else ""
        return f"Cloud database connection failed (reason={reason}{suffix})"
    # libpq often reports DNS/TLS/timeouts as OperationalError without a server
    # SQLSTATE. Its text is inspected internally and never becomes output.
    try:
        detail = str(error).lower()
    except Exception:
        detail = ""
    if reason == "authentication":
        pass
    elif isinstance(error, ssl.SSLCertVerificationError) or any(fragment in detail for fragment in (
        "certificate verify failed", "certificate verification failed",
        "does not match host name", "certificate has expired",
        "self-signed certificate", "root certificate file",
    )):
        reason = "tls_certificate"
    elif "channel binding" in detail or "channel_binding" in detail:
        reason = "channel_binding"
    elif isinstance(error, TimeoutError) or "timeout" in detail or "timed out" in detail:
        reason = "timeout"
    elif isinstance(error, socket.gaierror) or any(fragment in detail for fragment in (
        "could not translate host name", "name or service not known",
        "temporary failure in name resolution", "nodename nor servname provided",
    )):
        reason = "dns"
    elif "password authentication failed" in detail or "authentication failed" in detail:
        reason = "authentication"
    elif any(fragment in detail for fragment in (
        "connection refused", "network is unreachable", "no route to host",
        "server closed the connection", "connection reset",
    )):
        reason = "connection_failure"
    elif isinstance(error, ValueError) or "unsupported startup parameter" in detail:
        reason = "configuration"
    suffix = f", sqlstate={sqlstate}" if sqlstate else ""
    return f"Cloud database connection failed (reason={reason}{suffix})"


class Row:
    """sqlite3.Row-compatible values with indexed and named access."""

    __slots__ = ("_columns", "_values", "_lookup")

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)
        # sqlite3.Row resolves the first occurrence of a duplicate column name.
        self._lookup = {}
        for index, name in enumerate(self._columns):
            self._lookup.setdefault(name.lower(), index)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._lookup[key.lower()]]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._columns)


def _sql_segments(sql):
    """Yield (is_code, text), respecting SQL literals and nested comments.

    This is a delimiter scanner, not a query parser. It prevents a literal
    question mark, percent sign or semicolon being treated as a SQL parameter
    or a schema statement boundary.
    """
    index, start, length = 0, 0, len(sql)
    while index < length:
        delimiter, end = None, index
        if sql.startswith("--", index):
            delimiter = "comment"
            newline = sql.find("\n", index + 2)
            end = length if newline < 0 else newline + 1
        elif sql.startswith("/*", index):
            delimiter, level, end = "comment", 1, index + 2
            while end < length and level:
                if sql.startswith("/*", end):
                    level += 1
                    end += 2
                elif sql.startswith("*/", end):
                    level -= 1
                    end += 2
                else:
                    end += 1
            if level:
                raise sqlite3.ProgrammingError("Unterminated SQL comment")
        elif sql[index] in ("'", '"'):
            delimiter, quote, end = "quoted", sql[index], index + 1
            # E'...' is PostgreSQL's explicitly escaped string literal.
            escaped = quote == "'" and index > 0 and sql[index - 1] in "eE" and (
                index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_")
            )
            while end < length:
                if escaped and sql[end] == "\\":
                    end += 2
                elif sql[end] == quote:
                    end += 1
                    if end < length and sql[end] == quote:
                        end += 1
                    else:
                        break
                else:
                    end += 1
            else:
                raise sqlite3.ProgrammingError("Unterminated SQL literal")
        elif sql[index] == "$" and (match := _DOLLAR_QUOTE.match(sql, index)):
            delimiter = "quoted"
            tag = match.group()
            closing = sql.find(tag, match.end())
            if closing < 0:
                raise sqlite3.ProgrammingError("Unterminated SQL literal")
            end = closing + len(tag)
        if delimiter:
            if start < index:
                yield True, sql[start:index]
            yield False, sql[index:end]
            index = start = end
        else:
            index += 1
    if start < length:
        yield True, sql[start:]


def _postgres_query(sql, parameters):
    """Convert qmark placeholders to psycopg's parameterized SQL format."""
    parts, count = [], 0
    for is_code, part in _sql_segments(sql):
        # psycopg also scans percent signs inside SQL literals and comments
        # when parameters are supplied. Escape existing signs before adding
        # the only placeholders that may bind application values.
        part = part.replace("%", "%%")
        if is_code:
            count += part.count("?")
            part = part.replace("?", "%s")
        parts.append(part)
    values = tuple(parameters) if parameters is not None else ()
    if len(values) != count:
        raise sqlite3.ProgrammingError("Incorrect number of SQL parameters")
    if not count:
        # Passing no parameter argument lets literal '%' remain unchanged.
        return sql, None
    return "".join(parts), values


def _schema_statements(script):
    """Split schema DDL without splitting semicolons inside literals."""
    parts = []
    for is_code, text in _sql_segments(script):
        if not is_code:
            parts.append(text)
            continue
        # Keep integer seconds/bytes and double precision positions compatible
        # with SQLite instead of PostgreSQL's narrower integer/real defaults.
        text = _SCHEMA_TYPES.sub(
            lambda match: "BIGINT" if match.group().upper() == "INTEGER" else "DOUBLE PRECISION",
            text,
        )
        pieces = text.split(";")
        parts.append(pieces[0])
        for piece in pieces[1:]:
            statement = "".join(parts).strip()
            if statement:
                yield statement
            parts = [piece]
    statement = "".join(parts).strip()
    if statement:
        yield statement


class _EmptyCursor:
    rowcount = -1
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._columns = tuple(column.name for column in cursor.description or ())

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def fetchone(self):
        values = self._cursor.fetchone()
        return None if values is None else Row(self._columns, values)

    def fetchall(self):
        return [Row(self._columns, values) for values in self._cursor.fetchall()]

    def __iter__(self):
        return (Row(self._columns, values) for values in self._cursor)


class _PostgresConnection:
    dialect = "postgresql"

    def __init__(self, raw, driver):
        self._raw, self._driver = raw, driver

    @contextmanager
    def _safe_errors(self):
        try:
            yield
        except self._driver.IntegrityError:
            # Driver errors include SQL/row details. Preserve the API's existing
            # exception handling without exposing password hashes or payloads.
            raise sqlite3.IntegrityError("Database integrity constraint failed") from None
        except self._driver.Error:
            raise sqlite3.OperationalError("Cloud database operation failed") from None

    def execute(self, sql, parameters=None):
        if not isinstance(sql, str):
            raise sqlite3.ProgrammingError("SQL must be text")
        command = sql.strip().rstrip(";").strip()
        if command.upper() == "BEGIN IMMEDIATE":
            if parameters:
                raise sqlite3.ProgrammingError("Transaction command takes no parameters")
            # A previous SELECT may already have started the same transaction.
            # READ COMMITTED gives the subsequent authorization recheck a fresh
            # snapshot after this global writer lock has been acquired.
            query, values = "SELECT pg_advisory_xact_lock(%s,%s)", _WRITER_LOCK
        elif command.upper().startswith("PRAGMA"):
            if parameters or not _SQLITE_PRAGMAS.fullmatch(sql.strip()):
                raise sqlite3.NotSupportedError("SQLite PRAGMA is not supported by PostgreSQL")
            return _EmptyCursor()
        else:
            query, values = _postgres_query(sql, parameters)
        with self._safe_errors():
            cursor = self._raw.execute(query) if values is None else self._raw.execute(query, values)
        return _PostgresCursor(cursor)

    def executescript(self, script):
        cursor = _EmptyCursor()
        for statement in _schema_statements(script):
            cursor = self.execute(statement)
        return cursor

    def commit(self):
        with self._safe_errors():
            self._raw.commit()

    def rollback(self):
        with self._safe_errors():
            self._raw.rollback()


class Database:
    """Choose durable PostgreSQL when configured, otherwise local SQLite.

    Each context closes its connection so idle applications can scale to zero.
    No shared connection pool or mutable connection is reused between threads.
    SQLite/SQL data import is a separate explicit operation; configuring a URL
    never silently copies or deletes existing local records.
    """

    def __init__(self, path, database_url=None):
        self.path = Path(path)
        self._database_url = database_url or None
        self.dialect = "postgresql" if self._database_url else "sqlite"
        if self._database_url:
            # A global cloud transport setting never changes local SQLite
            # fixtures or imports an optional network dependency for them.
            self._transport = os.environ.get("DATABASE_TRANSPORT", "native")
            if self._transport not in ("native", "neon-ws"):
                raise ValueError("DATABASE_TRANSPORT must be native or neon-ws") from None
            try:
                parsed = urlsplit(self._database_url)
                if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or not parsed.path.strip("/"):
                    raise ValueError
                query = parse_qs(parsed.query)
                modes = query.get("sslmode", [])
                local = parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1")
                self._local_insecure = local and modes == ["disable"]
                if any(mode not in ("require", "verify-ca", "verify-full") for mode in modes) and not self._local_insecure:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("DATABASE_URL must be a PostgreSQL URL with TLS for remote hosts") from None

    def __repr__(self):
        # repr must never accidentally serialize a database credential.
        return f"Database(dialect={self.dialect!r})"

    @contextmanager
    def connect_context(self):
        if self.dialect == "sqlite":
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=15000")
                with conn:
                    yield conn
            finally:
                conn.close()
            return
        if self._transport == "neon-ws":
            try:
                import neon_transport as driver
            except ImportError:
                raise RuntimeError("Neon WebSocket storage requires its transport dependencies") from None
        else:
            try:
                import psycopg as driver
            except ImportError:
                raise RuntimeError("PostgreSQL storage requires the psycopg dependency") from None
        connection_options = {
            "connect_timeout": 10,
            "options": "-c statement_timeout=15000 -c lock_timeout=15000 -c idle_in_transaction_session_timeout=30000",
            "autocommit": False,
        }
        if self._local_insecure:
            connection_options["sslmode"] = "disable"
        else:
            ca_file = ssl.get_default_verify_paths().cafile
            if not ca_file or not Path(ca_file).is_file():
                raise RuntimeError("Cloud database TLS requires the system CA certificates")
            connection_options.update(sslmode="verify-full", sslrootcert=ca_file)
        raw = None
        try:
            raw = driver.connect(self._database_url, **connection_options)
            raw.isolation_level = driver.IsolationLevel.READ_COMMITTED
        except (driver.Error, ValueError) as error:
            # The transport may establish a socket successfully, then fail
            # while setting transaction isolation. Close that session before
            # returning the original sanitized setup diagnostic.
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            raise sqlite3.OperationalError(_connection_failure_message(error)) from None
        conn = _PostgresConnection(raw, driver)
        try:
            yield conn
            conn.commit()
        except BaseException:
            # Avoid hiding the original API/constraint error with a rollback
            # error if the connection has already failed or been terminated.
            try:
                raw.rollback()
            except driver.Error:
                pass
            raise
        finally:
            raw.close()
