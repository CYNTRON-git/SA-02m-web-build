"""Run against the exact installed asyncua, inside a dedicated loopback namespace."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import socket
import time
import unittest
from unittest.mock import Mock, patch

from asyncua import ua
from asyncua.ua.ua_binary import uatcp_to_binary

entry = Path(__file__).resolve().parents[1] / "sa02m-mqtt-opcua-managed.py"
spec = importlib.util.spec_from_file_location("managed_gateway", entry)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual({name for _, name in socket.if_nameindex()}, {"lo"})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.server = gateway.OpcuaServer()
        self.addCleanup(self.server.stop)
        self.aio = self.server.aio_obj
        self.nets = gateway._parse_allow_from(["127.0.0.1/32"])

    def guard(self):
        self.stack.enter_context(gateway._transport_policy(self.aio, self.nets))

    def protocol(self):
        return gateway.binary.BinaryServer(self.aio.iserver, "", 0, self.aio.limits)._make_protocol()

    def test_factory_and_processor_boundaries(self):
        self.guard()
        server = self.aio
        self.assertEqual((server.limits.max_recv_buffer, server.limits.max_send_buffer,
                          server.limits.max_chunk_count, server.limits.max_message_size),
                         (65536, 65536, 16, 1048576))
        self.assertEqual((server.iserver.max_connections, server.iserver.max_pending_messages_per_connection), (4, 8))
        self.assertTrue(gateway._peer_allowed(self.nets, "::ffff:127.0.0.1"))
        for remote in (None, (), ("invalid", 1), ("127.0.0.2", 1)):
            with self.subTest(remote=remote):
                transport = Mock()
                transport.get_extra_info.return_value = remote
                protocol = self.protocol()
                with patch.object(gateway.binary, "UaProcessor", side_effect=AssertionError("early processor")):
                    protocol.connection_made(transport)
                transport.close.assert_called_once()
                protocol.connection_lost(None)
                self.assertIsNone(protocol.processor)
                self.assertFalse(protocol.clients or protocol.closing_tasks)

    def test_factory_replacement_refuses_before_open(self):
        with patch.object(gateway.binary.BinaryServer, "_make_protocol", lambda self: object()):
            with self.assertRaises(gateway.AccessConfigError):
                self.guard()
        with patch.object(gateway.provider, "BinaryServer", object()):
            with self.assertRaises(gateway.AccessConfigError):
                self.guard()
        with patch.object(gateway.binary.BinaryServer, "start", lambda self: None):
            with self.assertRaises(gateway.AccessConfigError):
                self.guard()
        with patch.object(gateway.binary.OPCUAProtocol, "connection_made", None):
            with self.assertRaises(gateway.AccessConfigError):
                self.guard()

    def test_provider_parses_complete_fragmented_and_coalesced_frames(self):
        hello = ua.Hello()
        hello.EndpointUrl = "opc.tcp://127.0.0.1:12345/sa02m/"
        frame = uatcp_to_binary(ua.MessageType.Hello, hello)
        # Use an ordinary encoded greeting as the test-sized cap. This exercises
        # the exact full-buffer boundary without generating oversized traffic.
        self.stack.enter_context(patch.object(gateway, "CHUNK_BYTES", len(frame)))
        self.guard()
        for fragments in ([frame], [frame[:1], frame[1:-1], frame[-1:]],
                          [frame * 3], [frame + frame[:2], frame[2:]]):
            with self.subTest(sizes=list(map(len, fragments))):
                proto = self.protocol()
                proto.transport = Mock()
                proto.transport.is_closing.return_value = False
                for part in fragments:
                    proto.data_received(part)
                    self.assertLessEqual(len(proto._buffer), len(frame))
                self.assertEqual(proto._buffer, b"")
                self.assertEqual(proto.messages.qsize(), sum(map(len, fragments)) // len(frame))
                while not proto.messages.empty():
                    header, _ = proto.messages.get_nowait()
                    self.assertEqual(header.MessageType, ua.MessageType.Hello)
                proto.transport.close.assert_not_called()

    def test_actual_loopback_filter(self):
        self.guard()
        self.server.set_endpoint("opc.tcp://127.0.0.1:0/sa02m/")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.server.start()
        port = self.aio.bserver.port
        with patch.object(gateway.binary, "UaProcessor", wraps=gateway.binary.UaProcessor) as allocation:
            with socket.socket() as refused:
                refused.settimeout(4)
                refused.bind(("127.0.0.2", 0))
                refused.connect(("127.0.0.1", port))
                self.assertEqual(refused.recv(1), b"")
            self.assertEqual(allocation.call_count, 0)
            with socket.create_connection(("127.0.0.1", port), timeout=4):
                deadline = time.monotonic() + 2
                while not allocation.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(allocation.call_count, 1)


if __name__ == "__main__":
    unittest.main()
