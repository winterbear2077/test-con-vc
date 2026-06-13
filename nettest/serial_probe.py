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

    Pass a pre-bound socket via *pre_bound_sock* (returned by
    :meth:`SerialProbeServer.alloc_port`) so the port is listening **before**
    the VM is powered on — without this the VM's first TCP connection attempt
    arrives before the server socket exists and the VM never retries.

    Usage::

        with SerialProbeSession(port=10042, connect_timeout=300) as sess:
            if sess.accept():
                sess.send({"ip": "...", "prefix": 24, "gw": "..."})
                msg = sess.recv()   # {"status": "ready"}
                ...
    """

    def __init__(
        self,
        port: int,
        connect_timeout: int = 300,
        io_timeout: int = 120,
        pre_bound_sock: Optional[socket.socket] = None,
    ):
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        # If a pre-bound socket is supplied we adopt it without re-binding.
        self._server_sock: Optional[socket.socket] = pre_bound_sock
        self._owns_server_sock: bool = pre_bound_sock is None
        self._client_sock: Optional[socket.socket] = None
        self._buf = b""

    def __enter__(self) -> "SerialProbeSession":
        if self._owns_server_sock:
            # No pre-bound socket — bind now (legacy / standalone usage).
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("", self.port))
            self._server_sock.listen(1)
            logger.debug("Serial probe server listening on port %s", self.port)
        assert self._server_sock is not None
        self._server_sock.settimeout(self.connect_timeout)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._client_sock is not None:
            try:
                self._client_sock.close()
            except Exception:
                pass
            self._client_sock = None
        # Only close the server socket if we created it; pre-bound sockets are
        # owned (and closed) by SerialProbeServer.
        if self._owns_server_sock and self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass
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
        # port -> pre-bound listening socket (bound at alloc_port() time so the
        # socket is ready before the VM is powered on)
        self._pre_bound: Dict[int, socket.socket] = {}

    def alloc_port(self) -> int:
        """Allocate the next TCP port and immediately bind a listening socket.

        Binding here — before the VM is created and powered on — guarantees
        the server is accepting connections by the time the VM boots and its
        serial port tries to connect.  Without this the VM's first (and only)
        connection attempt arrives before the socket exists and the timeout
        fires with no chance of recovery.
        """
        with self._lock:
            port = self._next_port
            self._next_port += 1
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", port))
        srv.listen(1)
        with self._lock:
            self._pre_bound[port] = srv
        logger.debug("Pre-bound serial probe port %s", port)
        return port

    def close(self) -> None:
        """Close all pre-bound sockets (call when the run is complete)."""
        with self._lock:
            socks, self._pre_bound = dict(self._pre_bound), {}
        for port, srv in socks.items():
            try:
                srv.close()
            except Exception:
                pass
            logger.debug("Closed pre-bound serial probe port %s", port)

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
            vm_name = str(cfg.get("vm_name", f"vm_idx={idx}"))
            ip      = cfg["ip"]
            prefix  = cfg["prefix"]
            gw      = cfg["gw"]
            targets = cfg.get("targets", [])
            power_on_ts = float(cfg.get("power_on_ts", 0.0) or 0.0)

            try:
                pre_bound = self._pre_bound.get(port)
                with SerialProbeSession(port, connect_timeout, io_timeout, pre_bound_sock=pre_bound) as sess:
                    if not sess.accept():
                        errors[idx] = "serial-connect-timeout"
                        ready_events[idx].set()
                        return

                    logger.info(
                        "Serial probe connected: vm=%s port=%s sync=%s",
                        vm_name,
                        port,
                        sync,
                    )

                    # ── Wait for probe hello ────────────────────────────────
                    # The probe sends {"status":"hello"} after opening
                    # /dev/ttyS0 to signal it is ready.  We MUST wait here
                    # before sending config; otherwise the config arrives
                    # before the VM opens the serial device and is silently
                    # dropped by ESXi.
                    try:
                        hello = sess.recv()
                    except Exception as exc:
                        errors[idx] = f"serial-hello-timeout: {exc}"
                        ready_events[idx].set()
                        return
                    if hello.get("status") != "hello":
                        logger.warning(
                            "Unexpected hello from vm=%s port=%s: %s",
                            vm_name, port, hello,
                        )
                    logger.info(
                        "Serial probe hello received: vm=%s port=%s delay_from_poweron=%.1fs",
                        vm_name,
                        port,
                        max(0.0, time.time() - power_on_ts) if power_on_ts > 0 else 0.0,
                    )

                    if power_on_ts > 0:
                        delay_sec = max(0.0, time.time() - power_on_ts)
                        logger.info(
                            "Serial probe timing: vm=%s port=%s power_on_to_config_start=%.1fs",
                            vm_name,
                            port,
                            delay_sec,
                        )

                    if sync:
                        # ── Phase 1: send network config, wait for "ready" ──
                        logger.info(
                            "Serial probe TX phase1 config: vm=%s port=%s ip=%s/%s gw=%s",
                            vm_name,
                            port,
                            ip,
                            prefix,
                            gw,
                        )
                        sess.send({"ip": ip, "prefix": prefix, "gw": gw})
                        msg = sess.recv()
                        # Some probe builds can emit an extra hello; ignore and
                        # continue waiting for the phase-1 ready marker.
                        if msg.get("status") == "hello":
                            logger.info(
                                "Serial probe RX phase1 extra hello: vm=%s port=%s msg=%s",
                                vm_name,
                                port,
                                msg,
                            )
                            msg = sess.recv()
                        logger.info(
                            "Serial probe RX phase1: vm=%s port=%s msg=%s",
                            vm_name,
                            port,
                            msg,
                        )
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
                        logger.info(
                            "Serial probe TX phase2 targets: vm=%s port=%s count=%s",
                            vm_name,
                            port,
                            len(targets),
                        )
                        sess.send({"targets": targets})
                    else:
                        # Merged single round-trip: config + targets together.
                        logger.info(
                            "Serial probe TX merged config: vm=%s port=%s ip=%s/%s gw=%s targets=%s",
                            vm_name,
                            port,
                            ip,
                            prefix,
                            gw,
                            len(targets),
                        )
                        sess.send({"ip": ip, "prefix": prefix, "gw": gw,
                                   "targets": targets})
                        ready_events[idx].set()

                    done = sess.recv()
                    # Ignore one trailing duplicate hello and keep waiting for
                    # the actual done payload.
                    if done.get("status") == "hello":
                        logger.info(
                            "Serial probe RX done extra hello: vm=%s port=%s msg=%s",
                            vm_name,
                            port,
                            done,
                        )
                        done = sess.recv()
                    logger.info(
                        "Serial probe RX done: vm=%s port=%s msg=%s",
                        vm_name,
                        port,
                        done,
                    )
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
