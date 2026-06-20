#!/usr/bin/env python3
"""Entry point for the packaged vcnet-test binary.

Usage (web UI):
    vcnet-test [--host HOST] [--port PORT] [--tls] [--cert FILE] [--key FILE]

Usage (internal runner, invoked by the web server as a subprocess):
    vcnet-test --_runner [nettest_runner args ...]

The binary is built with PyInstaller (see vcnet-test.spec).
External files that must sit beside the binary:
    nettest.config.json   — vCenter credentials and test settings
    ovf/                  — Alpine OVF template
    artifacts/            — created automatically; stores run output
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn


def _generate_self_signed_cert(cert_path, key_path, host: str) -> None:
    """Generate a self-signed TLS certificate using the cryptography library."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])

    san: list = [x509.DNSName(host)]
    if host != "localhost":
        san.append(x509.DNSName("localhost"))
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        pass
    # Always include 127.0.0.1
    try:
        san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"[TLS] Self-signed certificate generated: {cert_path}")


def main() -> None:
    # Internal dispatch: the web server spawns itself with --_runner to run
    # the test runner in a subprocess without needing a separate Python script.
    if "--_runner" in sys.argv:
        sys.argv.remove("--_runner")
        import nettest_runner
        raise SystemExit(nettest_runner.run())

    # Web UI mode
    import argparse
    parser = argparse.ArgumentParser(
        description="vCenter Network Policy Test — web UI",
        add_help=True,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="HTTP port (default: 0 = random available port)")
    parser.add_argument("--tls", action="store_true", help="Enable HTTPS (required for vCenter plugin mode)")
    parser.add_argument("--cert", default="", help="Path to TLS certificate PEM (auto-generated if omitted)")
    parser.add_argument("--key",  default="", help="Path to TLS private key PEM (auto-generated if omitted)")
    args, _ = parser.parse_known_args()

    import socket
    import threading
    import webbrowser
    # Import the app object directly — passing a string import path fails
    # in frozen mode because uvicorn can't find the module on disk.
    from web_app import app

    # Resolve port=0 to an actual free port before uvicorn binds.
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port = s.getsockname()[1]

    # ── TLS setup ──────────────────────────────────────────────────────────
    ssl_certfile = ssl_keyfile = None
    scheme = "http"
    if args.tls:
        from nettest.paths import get_workspace
        ws = get_workspace()
        cert_path = Path(args.cert) if args.cert else ws / "tls" / "cert.pem"
        key_path  = Path(args.key)  if args.key  else ws / "tls" / "key.pem"
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        if not cert_path.exists() or not key_path.exists():
            bind_host = args.host if args.host not in ("0.0.0.0", "") else "127.0.0.1"
            _generate_self_signed_cert(cert_path, key_path, bind_host)
        ssl_certfile = str(cert_path)
        ssl_keyfile  = str(key_path)
        scheme = "https"

    display_host = args.host if args.host != "0.0.0.0" else "127.0.0.1"
    url = f"{scheme}://{display_host}:{port}"
    print(f"Starting web UI → {url}")
    # Open browser after a short delay so uvicorn has time to bind the port.
    threading.Timer(1.2, webbrowser.open, args=[url]).start()

    if ssl_certfile and ssl_keyfile:
        # Build SSL context explicitly so ALPN advertises http/1.1.
        # Without this uvicorn leaves ALPN empty; Java/vCenter HTTP clients
        # fail to negotiate and hang on the request.
        import asyncio as _asyncio
        import ssl as _ssl
        _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        _ssl_ctx.load_cert_chain(ssl_certfile, ssl_keyfile)
        _ssl_ctx.set_alpn_protocols(["http/1.1"])
        _cfg = uvicorn.Config(app, host=args.host, port=port)
        _cfg.load()
        _cfg.ssl = _ssl_ctx
        try:
            _asyncio.run(uvicorn.Server(_cfg).serve())
        except (KeyboardInterrupt, SystemExit):
            pass
    else:
        try:
            uvicorn.run(app, host=args.host, port=port)
        except (KeyboardInterrupt, SystemExit):
            pass


if __name__ == "__main__":
    main()
