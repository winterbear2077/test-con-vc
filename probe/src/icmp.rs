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
pub fn run_pings(logf: &mut dyn Write, targets: &[String]) -> HashMap<String, String> {
    // Brief pause for routes to become active after netlink config.
    thread::sleep(Duration::from_millis(500));
    let id = (std::process::id() & 0xffff) as u16;
    let mut results = HashMap::new();

    for (i, target) in targets.iter().enumerate() {
        if target.is_empty() { continue; }
        let ip = match parse_ipv4(target) {
            Some(v) => v,
            None => {
                log(logf, &format!("  ping {} -> INVALID", target));
                results.insert(target.clone(), "FAIL".to_string());
                continue;
            }
        };
        let mut pass = false;
        for attempt in 0..3u16 {
            if ping_once(ip, id, (i as u16) * 10 + attempt, 3000) {
                pass = true; break;
            }
            if attempt < 2 { thread::sleep(Duration::from_millis(500)); }
        }
        let r = if pass { "PASS" } else { "FAIL" };
        log(logf, &format!("  ping {} -> {}", target, r));
        results.insert(target.clone(), r.to_string());
    }
    results
}
