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
mod tcp;
mod vsock;

use std::fs::OpenOptions;
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::time::{Duration, Instant};

use log::log;
use protocol::{run_guestinfo_probe, run_probe_protocol};
use serial::{open_serial, set_read_timeout};
use vsock::vsock_connect;

/// Open the serial port, send a hello to tell the controller the probe is ready,
/// then wait up to `response_timeout_secs` for the controller to send the first
/// byte of its JSON config.  Returns an error on timeout (serial not connected).
fn run_serial_protocol(logf: &mut dyn Write, response_timeout_secs: u64) -> io::Result<()> {
    use std::io::Cursor;

    let serial = open_serial()?;
    let fd = std::os::unix::io::AsRawFd::as_raw_fd(&serial);

    // ── Step 1: send hello ────────────────────────────────────────────────
    // This signals to the controller that the VM has opened /dev/ttyS0 and is
    // ready to receive config.  The controller MUST NOT send config until it
    // receives this message, otherwise data races with the VM boot sequence.
    let hello = b"{\"status\":\"hello\"}\n";
    let n = unsafe { libc::write(fd, hello.as_ptr() as *const libc::c_void, hello.len()) };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    log(logf, "  Serial: sent hello — waiting for controller config...");

    // ── Step 2: wait for first byte of controller response ────────────────
    // Poll with 5-second read windows so we don't spin.
    set_read_timeout(fd, 50); // 50 × 100 ms = 5 s per read attempt
    let deadline = Instant::now() + Duration::from_secs(response_timeout_secs);
    let first_byte: u8 = loop {
        let mut buf = [0u8; 1];
        let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, 1) };
        if n > 0 {
            break buf[0];
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("no controller response within {}s after hello", response_timeout_secs),
            ));
        }
    };

    // ── Step 3: run probe protocol ────────────────────────────────────────
    // Prepend the already-consumed first byte and use longer per-read timeout.
    set_read_timeout(fd, 255); // 25.5 s per read — protocol handles overall flow
    let prefix: Cursor<[u8; 1]> = Cursor::new([first_byte]);
    let chained = prefix.chain(serial.try_clone()?);
    let mut reader = BufReader::new(chained);
    let mut writer = BufWriter::new(serial);
    run_probe_protocol(logf, &mut reader, &mut writer)
}

fn main() {
    let mut logf: Box<dyn Write> = OpenOptions::new()
        .create(true).append(true)
        .open("/var/log/netprobe.log")
        .map(|f| Box::new(f) as Box<dyn Write>)
        .unwrap_or_else(|_| Box::new(io::stderr()));

    log(&mut *logf, "=== netprobe start (rust) ===");

    let vsock_port: u32 = std::env::var("VSOCK_PORT")
        .ok().and_then(|s| s.parse().ok()).unwrap_or(9000);
    // Total time to wait for controller config after sending hello.
    // Covers the full provisioning window (all VMs being powered on in parallel).
    let serial_wait_secs: u64 = std::env::var("SERIAL_WAIT_SECS")
        .ok().and_then(|s| s.parse().ok()).unwrap_or(300);

    // ── Channel 1: serial (/dev/ttyS0) ───────────────────────────────────────
    // Long-lived mode: after each completed probe round (or disconnect), send
    // hello again and wait for the next command so Python retries can re-probe
    // without recreating the VM.
    let is_serial = std::fs::metadata("/dev/ttyS0")
        .map(|m| m.file_type().is_char_device())
        .unwrap_or(false);

    if is_serial {
        log(&mut *logf, "  Channel: serial (long-lived)");
        loop {
            log(
                &mut *logf,
                &format!("  Serial round start — hello sent, waiting up to {}s...", serial_wait_secs),
            );
            match run_serial_protocol(&mut *logf, serial_wait_secs) {
                Ok(()) => {
                    log(&mut *logf, "  Serial round complete; waiting for next command...");
                    continue;
                }
                Err(e) => {
                    log(
                        &mut *logf,
                        &format!("  serial round failed: {} (will retry serial)", e),
                    );
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }
    }

    // ── Channel 2: vsock ──────────────────────────────────────────────────────
    // Long-lived mode: reconnect and continue serving new rounds whenever the
    // controller closes the current vsock stream.
    let mut vsock_seen = false;
    loop {
        match vsock_connect(vsock_port, 3) {
            Ok(sock) => {
                vsock_seen = true;
                log(&mut *logf, &format!("  Channel: vsock connected (port={})", vsock_port));
                let result: io::Result<()> = (|| {
                    let mut reader = BufReader::new(sock.try_clone()?);
                    let mut writer = BufWriter::new(sock);
                    run_probe_protocol(&mut *logf, &mut reader, &mut writer)
                })();
                match result {
                    Ok(()) => log(&mut *logf, "  vsock round complete/disconnected; waiting reconnect..."),
                    Err(e) => log(&mut *logf, &format!("  vsock protocol error: {}", e)),
                }
                std::thread::sleep(Duration::from_millis(300));
            }
            Err(e) => {
                if !vsock_seen {
                    log(&mut *logf, &format!("  vsock unavailable: {}", e));
                }
                break;
            }
        }
    }

    // ── Channel 3: guestinfo (vmtoolsd) ──────────────────────────────────────
    let has_vmtoolsd = std::path::Path::new("/usr/bin/vmtoolsd").exists()
        || std::path::Path::new("/usr/local/bin/vmtoolsd").exists()
        || std::path::Path::new("/usr/sbin/vmtoolsd").exists();

    if has_vmtoolsd {
        log(&mut *logf, "  Channel: guestinfo (vmtoolsd)");
        finish(logf, run_guestinfo_probe(&mut io::stderr()));
        return;
    }

    log(&mut *logf, "  ERROR: no channel available (no serial data, no vsock, no vmtoolsd)");
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