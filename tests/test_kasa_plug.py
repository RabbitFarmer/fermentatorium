"""Hardware-free tests for plug protocol probing and python-kasa connection logic."""
import ast
import asyncio
import os
import socket
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kasa_manager import _plug  # noqa: E402


def _load_probe():
    """Extract _probe_plug_port from app.py without importing the whole app."""
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "_probe_plug_port")
    ns = {"socket": socket}
    exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
    return ns["_probe_plug_port"]


def _fake_connect(open_ports):
    def fn(addr, timeout=None):
        if addr[1] in open_ports:
            return mock.MagicMock()
        raise OSError("refused")
    return fn


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = _load_probe()

    def _run(self, ports):
        with mock.patch("socket.create_connection", side_effect=_fake_connect(ports)):
            return self.probe("1.2.3.4")

    def test_legacy(self):
        r = self._run({9999, 80})
        self.assertEqual((r["protocol"], r["recommended_port"]), ("legacy", None))

    def test_klap(self):
        r = self._run({80})
        self.assertEqual((r["protocol"], r["recommended_port"], r["open_ports"]), ("klap", 80, [80]))

    def test_unreachable(self):
        r = self._run(set())
        self.assertEqual((r["protocol"], r["recommended_port"], r["open_ports"]), ("unknown", None, []))


class AuthenticationError(Exception):
    pass


class OpenDeviceTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def _patch(self, discover):
        fake = types.SimpleNamespace(discover_single=discover)
        return mock.patch.multiple(_plug, _Discover=fake, HAS_DISCOVER=True,
                                   PLUG_CLASS=mock.MagicMock(name="IotPlug"))

    def test_discovery_success_with_and_without_credentials(self):
        for creds in (None, object()):
            dev = mock.MagicMock()
            seen = {}

            async def discover(host, *, credentials=None, timeout=None, discovery_timeout=5, port=None):
                seen.update(credentials=credentials, port=port)
                return dev

            with self._patch(discover):
                device, needs = self.run_async(_plug._open_device("h", creds, 10, None))
            self.assertIs(device, dev)
            self.assertTrue(needs)
            self.assertIs(seen["credentials"], creds)
            self.assertIsNone(seen["port"])

    def test_explicit_port_is_honoured(self):
        seen = {}

        async def discover(host, *, credentials=None, timeout=None, discovery_timeout=5, port=None):
            seen["port"] = port
            return mock.MagicMock()

        with self._patch(discover):
            self.run_async(_plug._open_device("h", None, 10, 80))
        self.assertEqual(seen["port"], 80)

    def test_auth_required_without_credentials(self):
        async def discover(host, **kw):
            raise AuthenticationError("Device response did not match our challenge")

        with self._patch(discover):
            with self.assertRaises(RuntimeError) as cm:
                self.run_async(_plug._open_device("h", None, 10, None))
        self.assertIn("System Settings > Kasa Account Credentials", str(cm.exception))
        self.assertNotIn("9999", str(cm.exception))

    def test_discovery_failure_falls_back_to_legacy(self):
        async def discover(host, **kw):
            raise TimeoutError("no reply")

        with self._patch(discover):
            device, needs = self.run_async(_plug._open_device("h", None, 10, None))
            self.assertFalse(needs)
            _plug.PLUG_CLASS.assert_called_once_with("h")

    def test_plug_query_maps_auth_error(self):
        async def discover(host, **kw):
            raise AuthenticationError("bad")

        with self._patch(discover), mock.patch.object(_plug, "KASA_AVAILABLE", True):
            is_on, err = self.run_async(_plug.plug_query("h", None, 5))
        self.assertIsNone(is_on)
        self.assertIn("requires your TP-Link/Kasa account", err)

    def test_friendly_error_hint_for_refused_9999(self):
        msg = _plug._friendly_error(OSError("Unable to connect to the device: h:9999: [Errno 111] Connect call failed"), None)
        self.assertIn("TP-Link/Kasa account", msg)
        self.assertNotIn("secret", msg)


if __name__ == "__main__":
    unittest.main()
