"""Serial-port probe server for lightweight test VMs.

Manages per-VM TCP socket servers that back vSphere virtual serial ports.
The VM connects to the TCP server when powered on; the controller sends
probe configuration and receives ICMP results over the raw TCP stream.

No vmtools required — the VM only needs: sh, ip, ping, and /dev/ttyS0.

Protocol (newline-delimited JSON, two-phase for intra-subnet sync):

  Phase 1 — network config:
    Controller → VM : {"ip":"1.2.3.4","prefix":24,"gw":"1.2.3.1"}
    VM → Controller : {"status":"ready"}

  Phase 2 — probe:
    Controller → VM : {"targets":["1.2.3.5","10.0.0.1"]}
    VM → Controller : {"status":"done","results":{"1.2.3.5":"PASS","10.0.0.1":"FAIL"}}

When no intra-subnet synchronisation is needed both phases are merged into
one round-trip (targets are included in the first message and the VM skips
the "ready" handshake):

    Controller → VM : {"ip":"...","prefix":24,"gw":"...","targets":["..."]}
    VM → Controller : {"status":"done","results":{...}}
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Low-level session ──────────────────────────────────────────────────────────

class SerialProbeSession:
    """Context manager that binds a TCP server on *port* and handles one VM session.

    Usage::

        with SerialProbeSession(port=10042, connect_timeout=300) as sess:
            if sess.accept():
                sess.send({"ip": "...", "prefix": 24, "gw": "..."})
                msg = sess.recv()   # {"status": "ready"}
                ...
    """

    def __init__(self, port: int, connect_timeout: int = 300, io_timeout: int = 120):
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self._server_sock: Optional[socket.socket] = None
        self._client_sock: Optional[socket.socket] = None
        self._buf = b""

    def __enter__(self) -> "SerialProbeSession":
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("", self.port))
        self._server_sock.listen(1)
        self._server_sock.settimeout(self.connect_timeout)
        logger.debug("Serial probe server listening on port %s", self.port)
        return self

    def __exit__(self, *_: Any) -> None:
        for s in (self._client_sock, self._server_sock):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        self._client_sock = None
        self._server_sock = None

    def accept(self) -> bool:
        """Wait for the VM to connect.  Returns True on success, False on timeout."""
        try:
            assert self._server_sock is not None
            self._client_sock, addr = self._server_sock.accept()
            self._client_sock.settimeout(self.io_timeout)
            logger.debug("VM connected from %s on port %s", addr, self.port)
            return True
        except socket.timeout:
            logger.warning("Timeout waiting for VM serial connection on port %s", self.port)
            return False

    def send(self, obj: Dict) -> None:
        """Send one JSON line to the VM."""
        assert self._client_sock is not None
        line = json.dumps(obj) + "\n"
        self._client_sock.sendall(line.encode())

    def recv(self) -> Dict:
        """Read one JSON line from the VM (blocking, honours io_timeout)."""
        assert self._client_sock is not None
        deadline = time.time() + self.io_timeout
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return json.loads(line.decode().strip())
            if time.time() > deadline:
                raise TimeoutError(f"Timeout waiting for VM response on port {self.port}")
            try:
                chunk = self._client_sock.recv(4096)
            except socket.timeout:
                raise TimeoutError(f"Socket timeout waiting for VM response on port {self.port}")
            if not chunk:
                raise EOFError("VM closed serial connection unexpectedly")
            self._buf += chunk


# ── High-level coordinator ─────────────────────────────────────────────────────

class SerialProbeServer:
    """Allocates TCP ports and coordinates serial probe sessions for a test run.

    Usage::

        server = SerialProbeServer(server_ip="192.168.1.10", base_port=10000)

        # Allocate one port per VM before creating the VMs.
        port = server.alloc_port()

        # After VMs are powered on, run the probe (blocking).
        results = server.run_subnet_probe(
            configs=[{"ip": "...", "prefix": 24, "gw": "...",
                      "port": port, "targets": [...]}],
            sync=False,
        )
    """

    def __init__(self, server_ip: str, base_port: int = 10000):
        self.server_ip = server_ip
        self.base_port = base_port
        self._next_port = base_port
        self._lock = threading.Lock()

    def alloc_port(self) -> int:
        """Return the next free TCP port (not thread-safe across calls yet)."""
        with self._lock:
            port = self._next_port
            self._next_port += 1
        return port

    def run_subnet_probe(
        self,
        configs: List[Dict],
        sync: bool,
        connect_timeout: int = 300,
        io_timeout: int = 180,
    ) -> List[Dict]:
        """Run probes for all VMs described by *configs* in parallel.

        Args:
            configs: One dict per VM with keys:
                ``port``    – TCP port the VM's serial port connects to.
                ``ip``      – Static IP to assign inside the VM.
                ``prefix``  – Prefix length (int).
                ``gw``      – Default gateway IP.
                ``targets`` – List of IP strings to ping.
            sync: When True, use the two-phase ready-handshake so all VMs
                  finish network configuration before any begin probing
                  (required for intra-subnet tests).
            connect_timeout: Seconds to wait for the VM TCP connection.
            io_timeout: Seconds for each send/recv operation.

        Returns:
            List of result dicts (same order as *configs*), each containing:
                ``results``  – {ip: "PASS"|"FAIL"} or empty on error.
                ``error``    – None or error string.
        """
        n = len(configs)
        results: List[Optional[Dict]] = [None] * n
        errors:  List[Optional[str]]  = [None] * n

        # ready_events[i] is set when VM i finishes network configuration.
        ready_events = [threading.Event() for _ in range(n)]

        def _run_one(idx: int, cfg: Dict) -> None:
            port    = cfg["port"]
            ip      = cfg["ip"]
            prefix  = cfg["prefix"]
            gw      = cfg["gw"]
            targets = cfg.get("targets", [])

            try:
                with SerialProbeSession(port, connect_timeout, io_timeout) as sess:
                    if not sess.accept():
                        errors[idx] = "serial-connect-timeout"
                        ready_events[idx].set()
                        return

                    if sync:
                        # ── Phase 1: send network config, wait for "ready" ──
                        sess.send({"ip": ip, "prefix": prefix, "gw": gw})
                        msg = sess.recv()
                        if msg.get("status") != "ready":
                            errors[idx] = f"unexpected-phase1-status:{msg}"
                            ready_events[idx].set()
                            return
                        ready_events[idx].set()

                        # ── Wait for ALL peers before sending targets ──────
                        for ev in ready_events:
                            if not ev.wait(timeout=connect_timeout):
                                errors[idx] = "peer-ready-timeout"
                                return

                        # ── Phase 2: send targets, receive results ─────────
                        sess.send({"targets": targets})
                    else:
                        # Merged single round-trip: config + targets together.
                        sess.send({"ip": ip, "prefix": prefix, "gw": gw,
                                   "targets": targets})
                        ready_events[idx].set()

                    done = sess.recv()
                    if done.get("status") == "done":
                        results[idx] = done.get("results", {})
                    else:
                        errors[idx] = f"unexpected-done-status:{done}"

            except Exception as exc:
                errors[idx] = str(exc)
                ready_events[idx].set()  # unblock peers even on error

        threads = [
            threading.Thread(target=_run_one, args=(i, cfg), daemon=True)
            for i, cfg in enumerate(configs)
        ]
        for t in threads:
            t.start()
        # Total budget: connect + io + small buffer
        budget = connect_timeout + io_timeout + 30
        for t in threads:
            t.join(timeout=budget)

        return [
            {"results": results[i] or {}, "error": errors[i]}
            for i in range(n)
        ]


# ── Helper ─────────────────────────────────────────────────────────────────────

def detect_controller_ip(vcenter_host: str) -> str:
    """Return the outbound IP this machine uses to reach *vcenter_host*.

    ESXi hosts must be able to reach the returned IP on the serial probe ports.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((vcenter_host, 443))
            return s.getsockname()[0]
    except Exception:
        return socket.gethostbyname(socket.gethostname())
