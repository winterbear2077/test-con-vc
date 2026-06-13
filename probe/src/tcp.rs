//! TCP connect probing — no external binary, uses std::net::TcpStream.
//!
//! Strategy:
//!   For each target IP, attempt TCP SYN to every requested port.
//!   PASS  if any port completes the three-way handshake.
//!   FAIL  if all ports time-out or receive a RST/unreachable.

use std::collections::HashMap;
use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use crate::log::log;
use crate::netconfig::parse_ipv4;

/// Try one TCP connect attempt to `ip:port` within `timeout_ms`.
pub fn tcp_connect_once(ip: [u8; 4], port: u16, timeout_ms: u64) -> bool {
    let addr = SocketAddr::from((ip, port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).is_ok()
}

/// Probe each target IP against the given list of ports.
///
/// Result for each target:
///   "PASS"  — at least one port accepted the connection
///   "FAIL"  — all ports timed out / RST / unreachable after 3 retries each
///
/// If `ports` is empty the function probes port 80 as a default.
pub fn run_tcp_connects(
    logf:    &mut dyn Write,
    targets: &[String],
    ports:   &[u16],
) -> HashMap<String, String> {
    let effective_ports: &[u16] = if ports.is_empty() { &[80] } else { ports };

    // Brief pause — same as ICMP path, lets routes settle after netlink config.
    std::thread::sleep(Duration::from_millis(500));

    let mut results = HashMap::new();

    for target in targets {
        if target.is_empty() {
            continue;
        }
        let ip = match parse_ipv4(target) {
            Some(v) => v,
            None => {
                log(logf, &format!("  tcp {} -> INVALID", target));
                results.insert(target.clone(), "FAIL".to_string());
                continue;
            }
        };

        let mut pass = false;
        'ports: for &port in effective_ports {
            for attempt in 0..3u8 {
                if tcp_connect_once(ip, port, 3000) {
                    log(logf, &format!("  tcp {}:{} -> PASS (attempt {})", target, port, attempt));
                    pass = true;
                    break 'ports;
                }
                if attempt < 2 {
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }

        if !pass {
            log(logf, &format!("  tcp {} ports {:?} -> FAIL", target, effective_ports));
        }
        results.insert(target.clone(), if pass { "PASS" } else { "FAIL" }.to_string());
    }

    results
}
