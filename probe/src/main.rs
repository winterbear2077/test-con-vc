//! netprobe — entry point and channel auto-detection.
//!
//! Statically compiled (x86_64-unknown-linux-musl).
//! No external binaries (`ip`, `ping`) are invoked.
//!
//! Module layout:
//! - `serial`    — serial port open + termios setup
//! - `vsock`     — AF_VSOCK connect to VMADDR_CID_HOST
//! - `netconfig` — RTnetlink network configuration
//! - `icmp`      — raw-socket ICMP echo probing
//! - `protocol`  — JSON probe protocol + guestinfo fallback
//! - `log`       — tiny write helper
//!
//! Channel auto-detection order: serial → vsock → guestinfo.

mod icmp;
mod log;
mod netconfig;
mod protocol;
mod serial;
mod vsock;

use std::fs::OpenOptions;
use std::io::{self, BufReader, BufWriter, Write};
use std::os::unix::fs::FileTypeExt;

use log::log;
use protocol::{run_guestinfo_probe, run_probe_protocol};
use serial::open_serial;
use vsock::vsock_connect;

fn main() {
    let mut logf: Box<dyn Write> = OpenOptions::new()
        .create(true).append(true)
        .open("/var/log/netprobe.log")
        .map(|f| Box::new(f) as Box<dyn Write>)
        .unwrap_or_else(|_| Box::new(io::stderr()));

    log(&mut *logf, "=== netprobe start (rust) ===");

    let vsock_port: u32 = std::env::var("VSOCK_PORT")
        .ok().and_then(|s| s.parse().ok()).unwrap_or(9000);

    // ── Channel 1: serial (/dev/ttyS0) ───────────────────────────────────────
    let is_serial = std::fs::metadata("/dev/ttyS0")
        .map(|m| m.file_type().is_char_device())
        .unwrap_or(false);

    if is_serial {
        log(&mut *logf, "  Channel: serial (/dev/ttyS0)");
        let result: io::Result<()> = (|| {
            let serial = open_serial()?;
            let mut reader = BufReader::new(serial.try_clone()?);
            let mut writer = BufWriter::new(serial);
            run_probe_protocol(&mut *logf, &mut reader, &mut writer)
        })();
        finish(logf, result);
        return;
    }

    // ── Channel 2: vsock ──────────────────────────────────────────────────────
    match vsock_connect(vsock_port, 3) {
        Ok(sock) => {
            log(&mut *logf, &format!("  Channel: vsock (port={})", vsock_port));
            let result: io::Result<()> = (|| {
                let mut reader = BufReader::new(sock.try_clone()?);
                let mut writer = BufWriter::new(sock);
                run_probe_protocol(&mut *logf, &mut reader, &mut writer)
            })();
            finish(logf, result);
            return;
        }
        Err(e) => log(&mut *logf, &format!("  vsock unavailable: {}", e)),
    }

    // ── Channel 3: guestinfo (vmtoolsd) ──────────────────────────────────────
    let has_vmtoolsd = std::path::Path::new("/usr/bin/vmtoolsd").exists()
        || std::path::Path::new("/usr/local/bin/vmtoolsd").exists();

    if has_vmtoolsd {
        log(&mut *logf, "  Channel: guestinfo (vmtoolsd)");
        finish(logf, run_guestinfo_probe(&mut io::stderr()));
        return;
    }

    log(&mut *logf, "  ERROR: no channel available (no serial, no vsock, no vmtoolsd)");
    std::process::exit(1);
}

fn finish(mut logf: Box<dyn Write>, result: io::Result<()>) {
    match result {
        Ok(()) => log(&mut *logf, "=== netprobe done ==="),
        Err(e) => {
            log(&mut *logf, &format!("  ERROR: {}", e));
            std::process::exit(1);
        }
    }
}