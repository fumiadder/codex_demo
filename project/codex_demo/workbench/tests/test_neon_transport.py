"""Deterministic Neon stream/security contracts; no external network calls."""
import socket
import ssl
import sys
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import neon_transport as transport


class FakeWebSocket:
    def __init__(self, frames=(), status=101):
        self.frames = iter(frames)
        self.status = status
        self.sent = []
        self.timeouts = []
        self.closed = 0

    def getstatus(self):
        return self.status

    def recv_data(self):
        return next(self.frames)

    def send_binary(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self, **kwargs):
        self.closed += 1


class NeonByteStreamTests(unittest.TestCase):
    def test_reads_join_frames_and_preserve_unconsumed_bytes(self):
        ws = FakeWebSocket([(2, b"a"), (2, b"bcdef"), (2, b"gh"), (8, b"")])
        stream = transport._WebSocketStream(ws, 10)
        self.assertIs(stream.makefile(mode="rwb"), stream)
        self.assertEqual(stream.read(0), b"")
        self.assertEqual(stream.read(4), b"abcd")
        self.assertEqual(stream.read(3), b"efg")
        self.assertEqual(stream.read(3), b"h")
        self.assertEqual(stream.read(1), b"")
        self.assertTrue(all(0 < timeout <= 10 for timeout in ws.timeouts))

    def test_writes_coalesce_until_flush_and_close_is_idempotent(self):
        ws = FakeWebSocket()
        stream = transport._WebSocketStream(ws, 10)
        self.assertEqual(stream.write(b"header"), 6)
        self.assertEqual(stream.write(memoryview(b"body")), 4)
        self.assertEqual(ws.sent, [])
        stream.flush()
        stream.flush()
        self.assertEqual(ws.sent, [b"headerbody"])
        stream.close()
        stream.close()
        self.assertEqual(ws.closed, 1)
        self.assertEqual(stream.read(1), b"")
        with self.assertRaises(transport.Error):
            stream.write(b"closed")

    def test_text_frames_are_rejected_without_exposing_payload(self):
        stream = transport._WebSocketStream(FakeWebSocket([(1, b"private-password")]), 10)
        with self.assertRaises(transport.Error) as raised:
            stream.read(1)
        self.assertEqual(raised.exception.reason, "connection_failure")
        self.assertNotIn("private-password", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_empty_frames_cannot_extend_read_deadline(self):
        ws = FakeWebSocket([(2, b""), (2, b"after-deadline")])
        stream = transport._WebSocketStream(ws, 10)
        with patch("neon_transport.time.monotonic", side_effect=[0, 0, 11]):
            with self.assertRaises(transport.Error) as raised:
                stream.read(1)
        self.assertEqual(raised.exception.reason, "timeout")

    def test_control_frames_and_slow_binary_chunks_cannot_extend_deadline(self):
        stream = transport._WebSocketStream(FakeWebSocket([(9, b"ping"), (2, b"late")]), 10)
        with patch("neon_transport.time.monotonic", side_effect=[0, 0, 0, 11]):
            with self.assertRaises(transport.Error) as raised:
                stream.read(1)
        self.assertEqual(raised.exception.reason, "timeout")
        stream = transport._WebSocketStream(FakeWebSocket([(2, b"late")]), 10)
        with patch("neon_transport.time.monotonic", side_effect=[0, 0, 11]):
            with self.assertRaises(transport.Error) as raised:
                stream.read(1)
        self.assertEqual(raised.exception.reason, "timeout")
        self.assertIsNone(stream._websocket._receive_deadline)

    def test_io_exceptions_are_sanitized(self):
        ws = FakeWebSocket()
        ws.recv_data = Mock(side_effect=TimeoutError("secret-host password-secret timed out"))
        stream = transport._WebSocketStream(ws, 10)
        with self.assertRaises(transport.Error) as raised:
            stream.read(1)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(raised.exception.reason, "timeout")
        self.assertNotIn("password-secret", formatted)
        self.assertIsNone(raised.exception.__cause__)

    def test_oversized_pg_reads_writes_and_binary_frames_are_rejected(self):
        with patch("neon_transport._MAX_PACKET_BYTES", 8), patch("neon_transport._MAX_BUFFER_BYTES", 16):
            stream = transport._WebSocketStream(FakeWebSocket([(2, b"oversized")]), 10)
            for action in (lambda: stream.read(9), lambda: stream.write(b"oversized"), lambda: stream.read(1)):
                with self.assertRaises(transport.Error) as raised:
                    action()
                self.assertEqual(raised.exception.reason, "connection_failure")

    def test_frame_payload_is_bounded_before_allocation_and_fragments_stream(self):
        class BaseWebSocket:
            def __init__(self, **kwargs):
                self.fire_cont_frame = kwargs["fire_cont_frame"]
                self.receive = Mock(return_value=b"payload")
                self.frame_buffer = SimpleNamespace(recv_strict=self.receive)
                self.recv_data_frame = Mock(side_effect=[
                    (2, SimpleNamespace(fin=False, data=b"start")),
                    (0, SimpleNamespace(fin=False, data=b"middle")),
                    (0, SimpleNamespace(fin=True, data=b"end")),
                ])
                self.settimeout = Mock()

            def _recv(self, size):
                return b"late"
        client = transport._bounded_websocket_class(SimpleNamespace(WebSocket=BaseWebSocket))()
        self.assertTrue(client.fire_cont_frame)
        with self.assertRaises(transport.Error):
            client.frame_buffer.recv_strict(transport._MAX_PACKET_BYTES + 1)
        client.receive.assert_not_called()
        self.assertEqual(client.frame_buffer.recv_strict(7), b"payload")
        self.assertEqual([client.recv_data() for _ in range(3)], [(2, b"start"), (2, b"middle"), (2, b"end")])
        self.assertEqual(client.recv_data_frame.call_args.kwargs, {"control_frame": True})
        client._receive_deadline = 10
        with patch("neon_transport.time.monotonic", side_effect=[0, 11]):
            with self.assertRaises(transport.Error) as raised:
                client._recv(4)
        self.assertEqual(raised.exception.reason, "timeout")
        client.settimeout.assert_called_with(10)
        client.recv_data_frame = Mock(return_value=(0, SimpleNamespace(fin=True, data=b"invalid")))
        with self.assertRaises(transport.Error):
            client.recv_data()


class NeonConnectionTests(unittest.TestCase):
    url = "postgresql://o%20wner:p%40ss@ep-demo.ap-southeast-1.aws.neon.tech/zhi%20xu?sslmode=require"

    def dependencies(self, status=101):
        self.ws = FakeWebSocket(status=status)
        self.raw = Mock()
        self.dbapi = SimpleNamespace(connect=Mock(return_value=self.raw))
        self.websocket = SimpleNamespace(
            create_connection=Mock(return_value=self.ws), enableTrace=Mock(),
            WebSocket=type("UnusedWebSocketBase", (), {}),
        )
        return patch("neon_transport._load_dependencies", return_value=(self.dbapi, self.websocket))

    def test_remote_uses_fixed_verified_wss_without_startup_options(self):
        with patch.dict("os.environ", {}, clear=True), self.dependencies():
            connection = transport.connect(self.url, options="-c statement_timeout=15000", sslmode="verify-full")
        args, kwargs = self.websocket.create_connection.call_args
        self.assertEqual(args, ("wss://ep-demo.ap-southeast-1.aws.neon.tech/v2",))
        self.assertEqual(kwargs["redirect_limit"], 0)
        self.assertTrue(kwargs["suppress_origin"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["sslopt"]["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["sslopt"]["context"].check_hostname)
        self.assertNotIn("header", kwargs)
        self.assertNotIn("subprotocols", kwargs)
        self.websocket.enableTrace.assert_called_once_with(False)
        parameters = self.dbapi.connect.call_args.kwargs
        self.assertEqual(parameters["user"], "o wner")
        self.assertEqual(parameters["password"], "p@ss")
        self.assertEqual(parameters["database"], "zhi xu")
        self.assertEqual(parameters["startup_params"], {})
        self.assertEqual(connection._timeouts, (("statement_timeout", 15000),))
        self.assertIs(parameters["ssl_context"], False)
        self.assertFalse(self.raw.autocommit)
        connection.close()
        self.assertEqual(self.ws.closed, 1)

    def test_ca_file_is_used_without_disabling_hostname_verification(self):
        context = Mock()
        with patch.dict("os.environ", {}, clear=True), self.dependencies(), patch(
            "neon_transport.ssl.create_default_context", return_value=context,
        ) as create:
            transport.connect(self.url, sslrootcert="/private/trusted-ca.pem")
        create.assert_called_once_with(cafile="/private/trusted-ca.pem")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_redirect_is_rejected_before_postgres_authentication(self):
        with patch.dict("os.environ", {}, clear=True), self.dependencies(status=302):
            with self.assertRaises(transport.Error) as raised:
                transport.connect(self.url)
        self.dbapi.connect.assert_not_called()
        self.assertEqual(self.ws.closed, 1)
        self.assertEqual(raised.exception.reason, "connection_failure")

    def test_unsafe_hosts_security_options_and_urls_fail_before_connection(self):
        urls = [
            self.url.replace("ep-demo.", "ep-demo-pooler."),
            self.url.replace("aws.neon.tech", "example.com"),
            self.url.replace("sslmode=require", "sslmode=disable"),
            self.url + "&channel_binding=require",
            self.url + "&sslmode=require",
            self.url + "&sslcert=/private/client.pem",
            self.url.replace("/zhi%20xu", ":6543/zhi%20xu"),
            self.url + "#fragment",
        ]
        for url in urls:
            with self.subTest(url=url), patch.dict("os.environ", {}, clear=True), self.dependencies():
                with self.assertRaises(transport.Error) as raised:
                    transport.connect(url, sslmode="verify-full")
                self.websocket.create_connection.assert_not_called()
                self.assertNotIn("p@ss", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_remote_database_cannot_use_test_override(self):
        with patch.dict("os.environ", {"NEON_WS_TEST_URL": "ws://127.0.0.1:8765/v2"}, clear=True), self.dependencies():
            with self.assertRaises(transport.Error):
                transport.connect(self.url)
        self.websocket.create_connection.assert_not_called()

    def test_loopback_fixture_requires_explicit_plaintext_test_database(self):
        database = "postgresql://tester:fixture@127.0.0.1/test?sslmode=disable"
        overrides = [
            "ws://127.0.0.1:8765/v2", "ws://localhost:8765/v2", "ws://[::1]:8765/v2",
        ]
        for override in overrides:
            with self.subTest(override=override), patch.dict("os.environ", {"NEON_WS_TEST_URL": override}, clear=True), self.dependencies():
                transport.connect(database, sslmode="disable")
                self.assertEqual(self.websocket.create_connection.call_args.args, (override,))
                self.assertEqual(self.websocket.create_connection.call_args.kwargs["sslopt"], {})
        for override in ("ws://example.com:8765/v2", "ws://user:pwd@localhost:8765/v2", "ws://localhost:8765/v2?a=1", "ws://localhost:8765/v2#f", "wss://localhost:8765/v2", "ws://localhost:8765/other"):
            with self.subTest(override=override), patch.dict("os.environ", {"NEON_WS_TEST_URL": override}, clear=True), self.dependencies():
                with self.assertRaises(transport.Error):
                    transport.connect(database, sslmode="disable")
                self.websocket.create_connection.assert_not_called()
        with patch.dict("os.environ", {}, clear=True), self.dependencies():
            with self.assertRaises(transport.Error):
                transport.connect(database, sslmode="disable")

    def test_connect_failure_is_sanitized_and_classified(self):
        error = socket.gaierror("cannot resolve secret-host with password-secret")
        with patch.dict("os.environ", {}, clear=True), self.dependencies():
            self.websocket.create_connection.side_effect = error
            with self.assertRaises(transport.Error) as raised:
                transport.connect(self.url)
        self.assertEqual(raised.exception.reason, "dns")
        self.assertIn("connection failed", str(raised.exception))
        self.assertNotIn("secret-host", "".join(traceback.format_exception(raised.exception)))
        self.assertIsNone(raised.exception.__cause__)

    def test_timeout_options_reject_autocommit_and_arbitrary_gucs_before_network(self):
        cases = (
            ("-c statement_timeout=15000", True),
            ("-c statement_timeout=0", False),
            ("-c lock_timeout=20000", False),
            ("-c search_path=private", False),
            ("-c statement_timeout=15000;DROP TABLE users", False),
            ("-c statement_timeout=15000 -c statement_timeout=15000", False),
            ("-c statement_timeout=15000 -c", False),
            ("", False),
        )
        for options, autocommit in cases:
            with self.subTest(options=options), patch.dict("os.environ", {}, clear=True), self.dependencies():
                with self.assertRaises(transport.Error) as raised:
                    transport.connect(self.url, options=options, autocommit=autocommit)
                self.websocket.create_connection.assert_not_called()
                self.assertEqual(raised.exception.reason, "configuration")


class NeonDriverFacadeTests(unittest.TestCase):
    def setUp(self):
        self.cursor = Mock()
        self.cursor._input_oids = ()
        self.raw = Mock(autocommit=False)
        self.raw._in_transaction = False
        self.raw.execute_unnamed.return_value = SimpleNamespace(rows=[])
        self.raw.execute_simple.side_effect = self._execute_simple
        self.raw.commit.side_effect = self._end_transaction
        self.raw.rollback.side_effect = self._end_transaction
        self.raw.cursor.return_value = self.cursor
        self.stream = Mock()
        self.connection = transport.RawConnection(self.raw, self.stream)

    def _execute_simple(self, sql):
        if sql.startswith("BEGIN"):
            self.raw._in_transaction = True

    def _end_transaction(self):
        self.raw._in_transaction = False

    def test_execute_preserves_absent_and_bound_parameter_arguments(self):
        self.connection.execute("SELECT 'literal 50%'")
        self.cursor.execute.assert_called_once_with("SELECT 'literal 50%'")
        self.cursor.execute.reset_mock()
        self.connection.execute("SELECT %s", ("bound",))
        self.cursor.execute.assert_not_called()
        self.raw.execute_simple.assert_called_once_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        self.raw.execute_unnamed.assert_called_once_with("SELECT $1", vals=("bound",), oids=())

    def test_format_translation_preserves_literals_comments_and_numbered_binding(self):
        from persistence import _postgres_query
        sql = "SELECT ?, '50% %s?', $bedtime$?;100% %s$bedtime$, $$50% ?$$ /* %s ? /* % ? */ */ -- %s ?\n, ?"
        query, parameters = _postgres_query(sql, ("first", "second"))
        native, values = transport._native_query(query, parameters)
        self.assertEqual(values, ("first", "second"))
        self.assertEqual(native, sql.replace("SELECT ?", "SELECT $1").replace(", ?", ", $2"))
        self.connection.execute(query, parameters)
        self.assertEqual(self.raw.execute_unnamed.call_args.args, (native,))
        self.assertEqual(self.raw.execute_unnamed.call_args.kwargs["vals"], values)

    def test_parameter_count_and_unescaped_percent_fail_before_query(self):
        for query, values in (("SELECT %s", ()), ("SELECT 50%", ("unexpected",)), ("SELECT %s", (1, 2))):
            with self.subTest(query=query), self.assertRaises(transport.Error):
                self.connection.execute(query, values)
        self.raw.execute_unnamed.assert_not_called()

    def test_cursor_names_rowcount_and_exhaustion_match_facade(self):
        cursor = transport.Cursor(SimpleNamespace(
            description=[("value", 25, None, None, None, None, None)], rowcount=1,
            fetchone=lambda: ["story"], fetchall=lambda: [["story"]],
        ))
        self.assertEqual(cursor.description[0].name, "value")
        self.assertEqual(cursor.rowcount, 1)
        self.assertEqual(cursor.fetchone(), ["story"])
        self.assertEqual(cursor.fetchall(), [["story"]])
        self.assertEqual(list(transport.Cursor(iter([["one"], ["two"]]))), [["one"], ["two"]])

    def test_isolation_change_starts_real_transaction_without_session_sql(self):
        self.connection.isolation_level = transport.IsolationLevel.READ_COMMITTED
        self.assertFalse(self.raw.autocommit)
        self.raw.execute_simple.assert_called_once_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        self.cursor.execute.assert_not_called()
        self.assertIs(self.connection.isolation_level, transport.IsolationLevel.READ_COMMITTED)
        self.connection.rollback()
        self.raw.execute_simple.side_effect = RuntimeError("private-sql")
        with self.assertRaises(transport.Error):
            self.connection.isolation_level = transport.IsolationLevel.READ_COMMITTED
        self.assertFalse(self.raw.autocommit)

    def test_timeouts_are_local_and_reapplied_after_commit_or_rollback(self):
        options = "-c statement_timeout=15000 -c lock_timeout=15000 -c idle_in_transaction_session_timeout=30000"
        self.connection = transport.RawConnection(self.raw, self.stream, transport._transaction_timeouts(options))
        expected = [
            "BEGIN ISOLATION LEVEL READ COMMITTED",
            "SET LOCAL statement_timeout = 15000",
            "SET LOCAL lock_timeout = 15000",
            "SET LOCAL idle_in_transaction_session_timeout = 30000",
        ]
        self.connection.isolation_level = transport.IsolationLevel.READ_COMMITTED
        self.connection.execute("SELECT 1")
        self.connection.execute("SELECT %s", (2,))
        self.assertEqual([call.args[0] for call in self.raw.execute_simple.call_args_list], expected)
        for end_transaction in (self.connection.commit, self.connection.rollback):
            end_transaction()
            self.raw.execute_simple.reset_mock()
            self.connection.execute("SELECT 1")
            self.assertEqual([call.args[0] for call in self.raw.execute_simple.call_args_list], expected)
        self.raw.autocommit = True
        with self.assertRaises(transport.Error):
            self.connection.execute("SELECT 1")

    def test_failed_timeout_setup_prevents_application_query(self):
        self.connection = transport.RawConnection(self.raw, self.stream, (("statement_timeout", 15000),))
        def execute(sql):
            self._execute_simple(sql)
            if sql.startswith("SET LOCAL"):
                raise RuntimeError("private-timeout-error")
        self.raw.execute_simple.side_effect = execute
        with self.assertRaises(transport.Error):
            self.connection.execute("SELECT 1")
        self.cursor.execute.assert_not_called()
        self.raw.execute_unnamed.assert_not_called()

    def test_integrity_errors_expose_only_validated_sqlstate(self):
        self.raw.execute_unnamed.side_effect = Exception({"C": "23505", "M": "private-row secret-password", "D": "private-data"})
        with self.assertRaises(transport.IntegrityError) as raised:
            self.connection.execute("INSERT INTO private_table VALUES(%s)", ("secret-password",))
        self.assertEqual(raised.exception.sqlstate, "23505")
        self.assertNotIn("secret-password", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.cursor.execute.side_effect = Exception({"C": "23505, secret-password"})
        with self.assertRaises(transport.Error) as raised:
            self.connection.execute("SELECT 1")
        self.assertIsNone(raised.exception.sqlstate)
        self.assertNotIsInstance(raised.exception, transport.IntegrityError)

    def test_cursor_commit_rollback_and_close_failures_are_sanitized(self):
        cursor = transport.Cursor(Mock())
        for action in ("fetchone", "fetchall"):
            getattr(cursor._cursor, action).side_effect = Exception("password-secret private-row")
            with self.subTest(action=action), self.assertRaises(transport.Error) as raised:
                getattr(cursor, action)()
            self.assertNotIn("password-secret", str(raised.exception))
        for action in ("commit", "rollback", "close"):
            getattr(self.raw, action).side_effect = Exception("password-secret private-query")
            with self.subTest(action=action), self.assertRaises(transport.Error) as raised:
                getattr(self.connection, action)()
            self.assertNotIn("password-secret", str(raised.exception))
        self.stream.close.assert_called_once()


class NeonProtocolDiagnosticTests(unittest.TestCase):
    def test_known_protocol_messages_produce_only_fixed_details(self):
        cases = (
            ("The endpoint ID is not specified: secret-host", "endpoint_missing"),
            ("Unknown endpoint secret-host", "endpoint_unknown"),
            ("Endpoint is disabled for secret-account", "endpoint_unavailable"),
            ("Unsupported startup parameter secret-option", "startup_parameters"),
            ("Unsupported frontend protocol secret-version", "protocol_version"),
            ("Message contents do not agree with length: secret-payload", "protocol_message"),
            ("arbitrary private response", "unknown"),
        )
        for message, detail in cases:
            with self.subTest(detail=detail):
                error = transport._safe_error(Exception({"C": "08P01", "M": message, "H": "secret-password"}), connecting=True)
                self.assertEqual(error.reason, "connection_failure")
                self.assertEqual(error.sqlstate, "08P01")
                self.assertEqual(error.detail, detail)
                self.assertEqual(str(error), f"Cloud database connection failed (reason=connection_failure, sqlstate=08P01, detail={detail})")
                self.assertNotIn("secret-", repr(error))
                wrapped = transport._safe_error(error, connecting=True)
                self.assertEqual(wrapped.detail, detail)

    def test_protocol_detail_requires_exact_known_enum_and_sqlstate(self):
        for sqlstate, detail in (("28P01", "endpoint_missing"), ("08P01", "secret-host"), ("08P01-secret", "endpoint_missing"), ("08P01", [])):
            with self.subTest(sqlstate=sqlstate, detail=detail):
                error = transport.Error(sqlstate=sqlstate, detail=detail)
                self.assertIsNone(error.detail)
                self.assertNotIn("secret", str(error))
        error = transport._safe_error(Exception({"C": "28P01", "M": "Endpoint id is missing secret-password"}))
        self.assertIsNone(error.detail)
        self.assertEqual(error.reason, "authentication")

    def test_hostile_attributes_and_server_field_objects_are_not_rendered(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("secret-password must never be rendered")
        class HostileDriverError(Exception):
            @property
            def args(self):
                raise RuntimeError("secret-password must never escape")
            def __str__(self):
                raise RuntimeError("secret-password must never escape")
        class HostileSanitizedError(transport.Error):
            @property
            def detail(self):
                raise RuntimeError("secret-password must never escape")
            @detail.setter
            def detail(self, value):
                pass
        error = transport._safe_error(Exception({"C": "08P01", "M": Hostile(), "H": Hostile()}))
        self.assertEqual(error.detail, "unknown")
        self.assertNotIn("secret", str(error))
        error = transport._safe_error(HostileDriverError())
        self.assertEqual(error.reason, "unknown")
        self.assertIsNone(error.detail)
        hostile = HostileSanitizedError.__new__(HostileSanitizedError)
        Exception.__init__(hostile, "safe")
        hostile.reason, hostile.sqlstate = "connection_failure", "08P01"
        error = transport._safe_error(hostile)
        self.assertEqual(error.detail, "unknown")
        self.assertNotIn("secret", str(error))


if __name__ == "__main__":
    unittest.main()
