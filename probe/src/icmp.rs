//! ICMP echo probing via raw AF_INET socket — no external `ping` binary.

use std::collections::HashMap;
use std::io::Write;
use std::thread;
use std::time::Duration;

use crate::log::log;
use crate::netconfig::parse_ipv4;

/// Portable `sockaddr_in` without the BSD `sin_len` prefix field.
/// Layout matches Linux `struct sockaddr_in` (16 bytes).
#[repr(C)]
struct SockaddrIn4 {
    sin_family: u16,
    sin_port:   u16,
    sin_addr:   u32,  // network byte order
    sin_zero:   [u8; 8],
}

/// Internet checksum (RFC 1071).
fn inet_cksum(data: &[u8]) -> u16 {
    let mut sum = 0u32;
    let mut i = 0;
    while i + 1 < data.len() {
        sum += u16::from_be_bytes([data[i], data[i+1]]) as u32;
        i += 2;
    }
    if i < data.len() { sum += (data[i] as u32) << 8; }
    while sum >> 16 != 0 { sum = (sum & 0xffff) + (sum >> 16); }
    !(sum as u16)
}

/// Send one ICMP echo request and wait up to `timeout_ms` for a matching reply.
pub fn ping_once(dst: [u8; 4], id: u16, seq: u16, timeout_ms: u64) -> bool {
    const AF_INET:      i32 = 2;
    const IPPROTO_ICMP: i32 = 1;

    unsafe {
        let fd = libc::socket(AF_INET, libc::SOCK_RAW, IPPROTO_ICMP);
        if fd < 0 { return false; }

        let tv = libc::timeval {
            tv_sec:  (timeout_ms / 1000) as _,
            tv_usec: ((timeout_ms % 1000) * 1000) as _,
        };
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_RCVTIMEO,
            &tv as *const _ as _, std::mem::size_of_val(&tv) as _);

        // ICMP echo request: type=8, code=0, cksum(2), id(2), seq(2)
        let mut pkt = [0u8; 8];
        pkt[0] = 8;
        pkt[4..6].copy_from_slice(&id.to_be_bytes());
        pkt[6..8].copy_from_slice(&seq.to_be_bytes());
        let csum = inet_cksum(&pkt);
        pkt[2..4].copy_from_slice(&csum.to_be_bytes());

        let dst_addr = SockaddrIn4 {
            sin_family: AF_INET as u16,
            sin_port:   0,
            sin_addr:   u32::from_ne_bytes(dst),
            sin_zero:   [0; 8],
        };
        let sent = libc::sendto(fd, pkt.as_ptr() as _, pkt.len(), 0,
            &dst_addr as *const _ as *const libc::sockaddr,
            std::mem::size_of::<SockaddrIn4>() as _);

        let ok = if sent < 0 {
            false
        } else {
            // Drain up to 8 packets; skip ICMP unreachables by checking id.
            let mut matched = false;
            for _ in 0..8u8 {
                let mut buf = [0u8; 128];
                let n = libc::recv(fd, buf.as_mut_ptr() as _, buf.len(), 0);
                if n < 0 { break; }
                let n = n as usize;
                // Strip IP header (low nibble of byte 0, * 4)
                let ihl = ((buf[0] & 0xf) as usize) * 4;
                if n < ihl + 8 { continue; }
                let icmp = &buf[ihl..n];
                // type=0 (ECHO REPLY), code=0, id matches
                if icmp[0] == 0 && icmp[1] == 0
                    && u16::from_be_bytes([icmp[4], icmp[5]]) == id
                {
                    matched = true;
                    break;
                }
            }
            matched
        };
        libc::close(fd);
        ok
    }
}

/// Probe each target with up to 3 attempts; return PASS/FAIL map.
/// All targets are probed in parallel so total time equals the slowest
/// single ping rather than the sum — prevents serial-probe IO timeout
/// when many cross-VRF targets are blocked (each timing out at 3 s × 3
/// attempts = 9 s, which adds up to minutes when run sequentially).
pub fn run_pings(logf: &mut dyn Write, targets: &[String]) -> HashMap<String, String> {
    use std::sync::{Arc, Mutex};

    // Brief pause for routes to become active after netlink config.
    thread::sleep(Duration::from_millis(500));

    let base_id = (std::process::id() & 0xffff) as u16;
    let log_lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let results:   Arc<Mutex<HashMap<String, String>>> = Arc::new(Mutex::new(HashMap::new()));

    let mut handles = Vec::new();
    for (i, target) in targets.iter().enumerate() {
        if target.is_empty() { continue; }
        let target = target.clone();
        let log_lines = Arc::clone(&log_lines);
        let results   = Arc::clone(&results);
        let id = base_id.wrapping_add(i as u16);

        let handle = thread::spawn(move || {
            let ip = match parse_ipv4(&target) {
                Some(v) => v,
                None => {
                    log_lines.lock().unwrap().push(format!("  ping {} -> INVALID", target));
                    results.lock().unwrap().insert(target, "FAIL".to_string());
                    return;
                }
            };
            let mut pass = false;
            for attempt in 0..3u16 {
                if ping_once(ip, id, attempt, 3000) {
                    pass = true; break;
                }
                if attempt < 2 { thread::sleep(Duration::from_millis(500)); }
            }
            let r = if pass { "PASS" } else { "FAIL" };
            log_lines.lock().unwrap().push(format!("  ping {} -> {}", target, r));
            results.lock().unwrap().insert(target, r.to_string());
        });
        handles.push(handle);
    }

    for h in handles { let _ = h.join(); }

    // Flush collected log lines in insertion order
    for line in log_lines.lock().unwrap().iter() {
        log(logf, line);
    }

    Arc::try_unwrap(results).unwrap().into_inner().unwrap()
}