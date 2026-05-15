"""VMCI/vsock probe server for lightweight test VMs.

Uses AF_VSOCK so probes travel over the VMware VMCI bus instead of the network
or a serial port — zero data-plane configuration required inside the guest.

Architecture
------------
* The controller runs a single AF_VSOCK server on ``vsock_base_port``.
* Each VM connects OUTBOUND to ``VMADDR_CID_HOST`` (CID=2, the hypervisor)
  on that port.  The hypervisor delivers the connection to whichever process is
  listening with VMADDR_CID_ANY on the controller.
* The controller dispatches by source CID, matching each connection to a
  VMInstance via the VMCI context ID read from vSphere before power-on
  (``VirtualVMCIDevice.id``).

Requirement
-----------
The controller must be a VM running on the **same ESXi host** (or be the ESXi
host itself).  For remote-controller setups use ``poll_method=serial`` instead.
If ``AF_VSOCK`` is unavailable on the controller OS a ``RuntimeError`` is raised
at startup with a clear message.

Protocol (identical to serial_probe — newline-delimited JSON, two-phase)
------------------------------------------------------------------------
Single-phase:
  Ctrl → VM : {"ip":"...","prefix":24,"gw":"...","targets":["..."]}
  VM → Ctrl : {"status":"done","results":{"...":"PASS"}}

Two-phase (intra-subnet sync):
  Ctrl → VM : {"ip":"...","prefix":24,"gw":"..."}
  VM → Ctrl : {"status":"ready"}
  Ctrl → VM : {"targets":["..."]}
  VM → Ctrl : {"status":"done","results":{...}}
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AF_VSOCK: int = 40          # Linux constant; matches socket.AF_VSOCK on 3.9+
VMADDR_CID_ANY: int = 0xFFFFFFFF
VMADDR_CID_HOST: int = 2    # the hypervisor as seen from a guest


def _vsock_available() -> bool:
    try:
        s = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
        s.close()
        return True
    except OSError:
        return False


# ── Per-CID connection session ─────────────────────────────────────────────────

class _VsockConn:
    """Thin wrapper around a connected vsock client socket."""

    def __init__(self, sock: socket.socket, cid: int, io_timeout: int):
        self._sock = sock
        self.cid = cid
        self._buf = b""
        self._sock.settimeout(io_timeout)

    def send(self, obj: Dict) -> None:
        self._sock.sendall((json.dumps(obj) + "\n").encode())

    def recv(self, io_timeout: int) -> Dict:
        deadline = time.time() + io_timeout
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return json.loads(line.decode().strip())
            if time.time() > deadline:
                raise TimeoutError(f"vsock recv timeout (cid={self.cid})")
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise TimeoutError(f"vsock socket timeout (cid={self.cid})")
            if not chunk:
                raise EOFError(f"vsock connection closed by VM (cid={self.cid})")
            self._buf += chunk

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ── Shared listener (all VMs connect to one port) ─────────────────────────────

class VsockProbeServer:
    """Single AF_VSOCK server that handles all VMs for a test run.

    Usage::

        server = VsockProbeServer(port=9000)
        server.start()          # spawns accept-loop thread

        # … power on VMs, they will connect back …

        results = server.run_subnet_probe(
            configs=[{"cid": 42, "ip": "...", "prefix": 24, "gw": "...",
                      "targets": [...]}],
            sync=False,
        )
        server.stop()

    ``cid`` in each config is ``VirtualVMCIDevice.id`` read via vSphere API
    before VM power-on.
    """

    def __init__(self, port: int = 9000, connect_timeout: int = 300, io_timeout: int = 180):
        if not _vsock_available():
            raise RuntimeError(
                "AF_VSOCK is not available on this host.  "
                "The controller must be a VM on the same ESXi host as the test VMs "
                "to use poll_method=vsock.  Use poll_method=serial for remote controllers."
            )
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout

        self._server_sock: Optional[socket.socket] = None
        self._connections: Dict[int, _VsockConn] = {}   # cid → connection
        self._lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Bind and start the background accept loop."""
        self._server_sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((VMADDR_CID_ANY, self.port))
        self._server_sock.listen(64)
        self._server_sock.settimeout(2)  # short timeout so stop() is responsive
        logger.debug("Vsock probe server listening on port %s", self.port)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="vsock-accept"
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        if self._accept_thread:
            self._accept_thread.join(timeout=5)
        with self._lock:
            for conn in self._connections.values():
                conn.close()
            self._connections.clear()

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                assert self._server_sock is not None
                client_sock, addr = self._server_sock.accept()
                # addr is (cid, port) for AF_VSOCK
                src_cid = addr[0]
                conn = _VsockConn(client_sock, src_cid, self.io_timeout)
                with self._lock:
                    self._connections[src_cid] = conn
                logger.debug("Vsock connection from CID=%s", src_cid)
            except socket.timeout:
                continue
            except OSError:
                break

    def _wait_for_cid(self, cid: int, deadline: float) -> Optional[_VsockConn]:
        """Block until a connection from *cid* appears or deadline is reached."""
        while time.time() < deadline:
            with self._lock:
                if cid in self._connections:
                    return self._connections[cid]
            time.sleep(0.5)
        return None

    def run_subnet_probe(
        self,
        configs: List[Dict],
        sync: bool,
    ) -> List[Dict]:
        """Run probes for all VMs in *configs*.

        Each config dict must contain:
            ``cid``     – VMCI context ID (int) from ``VirtualVMCIDevice.id``
            ``ip``      – IP to assign inside the guest.
            ``prefix``  – prefix length (int).
            ``gw``      – default gateway.
            ``targets`` – list of IPs to ping.

        Args:
            configs: One entry per VM.
            sync:    Two-phase handshake (True = intra-subnet tests).

        Returns:
            List of ``{"results": {ip: "PASS"|"FAIL"}, "error": str|None}``
            in the same order as *configs*.
        """
        n = len(configs)
        results: List[Optional[Dict]] = [None] * n
        errors:  List[Optional[str]]  = [None] * n
        ready_events = [threading.Event() for _ in range(n)]
        deadline = time.time() + self.connect_timeout

        def _run_one(idx: int, cfg: Dict) -> None:
            cid     = cfg["cid"]
            ip      = cfg["ip"]
            prefix  = cfg["prefix"]
            gw      = cfg["gw"]
            targets = cfg.get("targets", [])

            conn = self._wait_for_cid(cid, deadline)
            if conn is None:
                errors[idx] = f"vsock-connect-timeout:cid={cid}"
                ready_events[idx].set()
                return

            try:
                if sync:
                    conn.send({"ip": ip, "prefix": prefix, "gw": gw})
                    msg = conn.recv(self.io_timeout)
                    if msg.get("status") != "ready":
                        errors[idx] = f"unexpected-phase1:{msg}"
                        ready_events[idx].set()
                        return
                    ready_events[idx].set()

                    # Wait for all peers to be ready
                    for ev in ready_events:
                        if not ev.wait(timeout=self.connect_timeout):
                            errors[idx] = "peer-ready-timeout"
                            return

                    conn.send({"targets": targets})
                else:
                    conn.send({"ip": ip, "prefix": prefix, "gw": gw,
                               "targets": targets})
                    ready_events[idx].set()

                done = conn.recv(self.io_timeout)
                if done.get("status") == "done":
                    results[idx] = done.get("results", {})
                else:
                    errors[idx] = f"unexpected-done:{done}"

            except Exception as exc:
                errors[idx] = str(exc)
                ready_events[idx].set()
            finally:
                conn.close()
                with self._lock:
                    self._connections.pop(cid, None)

        threads = [
            threading.Thread(target=_run_one, args=(i, cfg), daemon=True)
            for i, cfg in enumerate(configs)
        ]
        for t in threads:
            t.start()
        budget = self.connect_timeout + self.io_timeout + 30
        for t in threads:
            t.join(timeout=budget)

        return [
            {"results": results[i] or {}, "error": errors[i]}
            for i in range(n)
        ]
