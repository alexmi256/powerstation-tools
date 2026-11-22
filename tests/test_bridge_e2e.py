#!/usr/bin/env python3
"""End-to-end tests: the real bridge process against a fake power station.

Stands up a fake device on a loopback port (sends the unsolicited '61'
identity, acks '24', then pushes a '93' status every second), launches
powerstation_bridge.py with PS_DEVICE_HOST/PS_DEVICE_PORT pointing at it, and
drives the HTTP API exactly as the dashboard does.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import powerstation_control as pc

IDENT = ("383633393430303532353734373135"                # IMEI
         "00000000000000000000000000"                    # msisdn (13 bytes)
         "3839383532323430343030303039363631353837"      # ICCID
         "000002DA")                                     # device_id 730
# byte 19 (switches) = 0x81 -> acHz | master; byte 20 = 0x03 -> ampUp | sleep
STATUS = ("5A" "0000" "0000" "01F4" "0000" "02BC" "0000" "00000000"
          "01" "32" "81" "03" "0AAA")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeDevice(threading.Thread):
    """Minimal stand-in for the unit's TCP control socket."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.received = []
        self._stop = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(4)
        srv.settimeout(0.5)
        conns = []
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(0.5)
            conns.append(conn)
            try:
                conn.sendall(pc.send_data_conversion("0001", "61", IDENT).encode())
            except OSError:
                pass
        srv.close()
        for c in conns:
            c.close()

    def serve_frames(self, bridge_proc):
        """Answer commands until the bridge goes away."""
        while bridge_proc.poll() is None:
            time.sleep(0.2)

    def stop(self):
        self._stop.set()


def http(port, method, path, body=None, content_length=None, timeout=25):
    payload = body if isinstance(body, bytes) else (body or "").encode()
    length = content_length if content_length is not None else len(payload)
    req = (f"{method} {path} HTTP/1.1\r\nHost: t\r\n"
           f"Content-Length: {length}\r\nConnection: close\r\n\r\n").encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(req + payload)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0].decode(), rest.decode(errors="replace")


class BridgeProcess:
    """The bridge under test, plus a device stub that answers its commands."""

    def __init__(self):
        self.device_port = free_port()
        self.bridge_port = free_port()
        self.received = []
        self._stop = threading.Event()
        self._device = None
        self.proc = None

    def start(self):
        self._device = threading.Thread(target=self._device_loop, daemon=True)
        self._device.start()
        env = dict(os.environ,
                   PS_BRIDGE_HOST="127.0.0.1",
                   PS_BRIDGE_PORT=str(self.bridge_port),
                   PS_DEVICE_HOST="127.0.0.1",
                   PS_DEVICE_PORT=str(self.device_port))
        self.proc = subprocess.Popen([sys.executable, "powerstation_bridge.py"],
                                     cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if http(self.bridge_port, "GET", "/health", timeout=2)[0].endswith("200 OK"):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("bridge did not start")

    def _device_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.device_port))
        srv.listen(4)
        srv.settimeout(0.5)
        conns = []
        pushers = []
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(0.5)
            conns.append(conn)
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        srv.close()

    def _session(self, conn):
        try:
            conn.sendall(pc.send_data_conversion("0001", "61", IDENT).encode())
        except OSError:
            return
        stop_push = threading.Event()

        def push():
            while not stop_push.wait(0.5):
                try:
                    conn.sendall(pc.send_data_conversion("0001", "93", STATUS).encode())
                except OSError:
                    return

        buf = b""
        pushed = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while True:
                    text, buf = pc._next_frame(buf)
                    if text is None:
                        break
                    self.received.append(text)
                    parsed = pc.parse_response_frame(text)
                    if not parsed:
                        continue
                    cmd, _ = parsed
                    if cmd == "24":
                        conn.sendall(pc.send_data_conversion(text[8:12], "24", "55").encode())
                        if not pushed:
                            pushed = True
                            threading.Thread(target=push, daemon=True).start()
        except OSError:
            return
        finally:
            stop_push.set()

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def commands(self):
        out = []
        for text in self.received:
            parsed = pc.parse_response_frame(text)
            if parsed:
                out.append(parsed)
        return out


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = BridgeProcess()
        cls.bridge.start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def test_health(self):
        status, body = http(self.bridge.bridge_port, "GET", "/health")
        self.assertIn("200", status)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_status_reaches_the_configured_device(self):
        status, body = http(self.bridge.bridge_port, "GET", "/api/status")
        self.assertIn("200", status, body)
        result = json.loads(body)["result"]
        self.assertEqual(result["battery_percent"], 90)
        self.assertEqual(result["ac_input_watts"], 500)
        self.assertEqual(result["ac_output_watts"], 700)
        self.assertTrue(result["switches"]["master"])

    def test_string_false_switches_off(self):
        self.bridge.received.clear()
        status, body = http(self.bridge.bridge_port, "POST", "/api/master",
                            json.dumps({"on": "false"}))
        self.assertIn("200", status, body)
        cmds = [c for c in self.bridge.commands() if c[0] == "12"]
        self.assertTrue(cmds, self.bridge.commands())
        sent = int(cmds[-1][1], 16)
        self.assertEqual(sent & 0x01, 0, f"master bit set for 'false': {sent:#04x}")

    def test_string_off_switches_off(self):
        self.bridge.received.clear()
        status, body = http(self.bridge.bridge_port, "POST", "/api/ac",
                            json.dumps({"on": "off"}))
        self.assertIn("200", status, body)
        cmds = [c for c in self.bridge.commands() if c[0] == "12"]
        self.assertTrue(cmds, self.bridge.commands())
        sent = int(cmds[-1][1], 16)
        self.assertEqual((sent >> 1) & 0x01, 0, f"ac bit set for 'off': {sent:#04x}")

    def test_boolean_true_switches_on(self):
        self.bridge.received.clear()
        status, body = http(self.bridge.bridge_port, "POST", "/api/beep",
                            json.dumps({"on": True}))
        self.assertIn("200", status, body)
        cmds = [c for c in self.bridge.commands() if c[0] == "12"]
        self.assertTrue(cmds, self.bridge.commands())
        self.assertEqual((int(cmds[-1][1], 16) >> 3) & 0x01, 1)

    def test_oversized_body_is_rejected(self):
        status, body = http(self.bridge.bridge_port, "POST", "/api/master",
                            b"{}", content_length=10 * 1024 * 1024)
        self.assertIn("413", status, body)

    def test_malformed_content_length_is_rejected(self):
        status, body = http(self.bridge.bridge_port, "POST", "/api/master",
                            b"{}", content_length="nonsense")
        self.assertIn("400", status, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
