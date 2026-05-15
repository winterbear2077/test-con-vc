//! Network configuration via RTnetlink — raw libc syscalls only.
//!
//! All netlink constants and structs are defined locally; this avoids depending
//! on any specific libc version and lets the crate be syntax-checked on macOS.
//!
//! Interface discovery uses `/sys/class/net/<iface>/ifindex` (sysfs) instead of
//! a RTM_GETLINK dump — simpler and avoids 60+ lines of response parsing.

use std::io::{self, Write};

use crate::log::log;

// ── IPv4 helper ───────────────────────────────────────────────────────────────

pub fn parse_ipv4(s: &str) -> Option<[u8; 4]> {
    let segs: Vec<u8> = s.split('.').filter_map(|p| p.parse().ok()).collect();
    if segs.len() == 4 { Some([segs[0], segs[1], segs[2], segs[3]]) } else { None }
}

// ── Netlink constants (Linux; not all present in macOS libc) ─────────────────

const AF_NETLINK:    i32 = 16;
const AF_INET_U8:    u8  = 2;
const NETLINK_ROUTE: i32 = 0;

const NLM_F_REQUEST: u16 = 0x0001;
const NLM_F_ACK:     u16 = 0x0004;
const NLM_F_CREATE:  u16 = 0x0400;
const NLM_F_REPLACE: u16 = 0x0100;

const NLMSG_ERROR: u16 = 0x0002;
const NLMSG_DONE:  u16 = 0x0003;

const RTM_NEWLINK:  u16 = 16;
const RTM_NEWADDR:  u16 = 20;
const RTM_NEWROUTE: u16 = 24;

const IFA_LOCAL:   u16 = 2;
const IFA_ADDRESS: u16 = 1;
const RTA_GATEWAY: u16 = 5;
const RTA_OIF:     u16 = 4;

const RTN_UNICAST:       u8 = 1;
const RTPROT_STATIC:     u8 = 4;
const RT_SCOPE_UNIVERSE: u8 = 0;
const RT_TABLE_MAIN:     u8 = 254;

const NLMSG_HDR: usize = 16; // sizeof(nlmsghdr)

// ── sockaddr_nl (Linux-specific; not in macOS libc) ───────────────────────────

#[repr(C)]
struct SockaddrNl { nl_family: u16, nl_pad: u16, nl_pid: u32, nl_groups: u32 }

// ── Message builder helpers ───────────────────────────────────────────────────

fn align4(n: usize) -> usize { (n + 3) & !3 }

/// Append nlmsghdr with a placeholder length; returns the start offset so
/// `nl_fix` can back-fill it once the full message is known.
fn nl_hdr(buf: &mut Vec<u8>, msg_type: u16, flags: u16, seq: u32) -> usize {
    let pos = buf.len();
    buf.extend_from_slice(&0u32.to_ne_bytes());       // nlmsg_len — filled later
    buf.extend_from_slice(&msg_type.to_ne_bytes());
    buf.extend_from_slice(&flags.to_ne_bytes());
    buf.extend_from_slice(&seq.to_ne_bytes());
    buf.extend_from_slice(&0u32.to_ne_bytes());       // nlmsg_pid
    pos
}

fn nl_fix(buf: &mut Vec<u8>, pos: usize) {
    let len = (buf.len() - pos) as u32;
    buf[pos..pos+4].copy_from_slice(&len.to_ne_bytes());
}

/// Append an rtattr (4-byte header + data), padded to 4-byte boundary.
fn nl_attr(buf: &mut Vec<u8>, rta_type: u16, data: &[u8]) {
    let rta_len = 4 + data.len();
    buf.extend_from_slice(&(rta_len as u16).to_ne_bytes());
    buf.extend_from_slice(&rta_type.to_ne_bytes());
    buf.extend_from_slice(data);
    buf.extend(std::iter::repeat(0u8).take(align4(rta_len) - rta_len));
}

// ── NlSock — thin NETLINK_ROUTE socket wrapper ────────────────────────────────

struct NlSock(i32);

impl NlSock {
    fn open() -> io::Result<Self> {
        unsafe {
            let fd = libc::socket(AF_NETLINK, libc::SOCK_RAW, NETLINK_ROUTE);
            if fd < 0 { return Err(io::Error::last_os_error()); }
            let addr = SockaddrNl { nl_family: AF_NETLINK as u16, nl_pad: 0, nl_pid: 0, nl_groups: 0 };
            if libc::bind(fd,
                    &addr as *const _ as *const libc::sockaddr,
                    std::mem::size_of::<SockaddrNl>() as _) < 0 {
                let e = io::Error::last_os_error();
                libc::close(fd);
                return Err(e);
            }
            let tv = libc::timeval { tv_sec: 5, tv_usec: 0 };
            libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_RCVTIMEO,
                &tv as *const _ as _, std::mem::size_of_val(&tv) as _);
            Ok(Self(fd))
        }
    }

    fn send(&self, buf: &[u8]) -> io::Result<()> {
        let n = unsafe { libc::send(self.0, buf.as_ptr() as _, buf.len(), 0) };
        if n < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }

    /// Wait for one NLMSG_ERROR ack; treats EEXIST as success (idempotent).
    fn recv_ack(&self) -> io::Result<()> {
        let mut buf = vec![0u8; 4096];
        let n = unsafe { libc::recv(self.0, buf.as_mut_ptr() as _, buf.len(), 0) };
        if n < 0 { return Err(io::Error::last_os_error()); }
        let buf = &buf[..n as usize];

        let mut off = 0;
        while off + NLMSG_HDR <= buf.len() {
            let msg_len  = u32::from_ne_bytes(buf[off..off+4].try_into().unwrap()) as usize;
            let msg_type = u16::from_ne_bytes(buf[off+4..off+6].try_into().unwrap());
            match msg_type {
                NLMSG_DONE => return Ok(()),
                NLMSG_ERROR => {
                    if off + NLMSG_HDR + 4 <= buf.len() {
                        let err = i32::from_ne_bytes(
                            buf[off+NLMSG_HDR..off+NLMSG_HDR+4].try_into().unwrap());
                        if err != 0 && -err != libc::EEXIST {
                            return Err(io::Error::from_raw_os_error(-err));
                        }
                    }
                    return Ok(());
                }
                _ => {}
            }
            off += align4(msg_len.max(NLMSG_HDR));
        }
        Ok(())
    }
}

impl Drop for NlSock {
    fn drop(&mut self) { unsafe { libc::close(self.0); } }
}

// ── Sysfs interface discovery ─────────────────────────────────────────────────

fn find_iface() -> io::Result<(i32, String)> {
    for entry in std::fs::read_dir("/sys/class/net")?.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name == "lo" { continue; }
        if let Ok(s) = std::fs::read_to_string(
                format!("/sys/class/net/{}/ifindex", name)) {
            if let Ok(idx) = s.trim().parse::<i32>() {
                return Ok((idx, name));
            }
        }
    }
    Err(io::Error::new(io::ErrorKind::NotFound, "no non-loopback interface"))
}

// ── RTnetlink operations ──────────────────────────────────────────────────────

fn nl_link_up(nl: &NlSock, ifindex: i32) -> io::Result<()> {
    let mut buf: Vec<u8> = Vec::new();
    let h = nl_hdr(&mut buf, RTM_NEWLINK, NLM_F_REQUEST | NLM_F_ACK, 1);
    // ifinfomsg: family(1) _pad(1) type(2) index(4) flags(4) change(4)
    buf.extend_from_slice(&[0u8, 0, 0, 0]);
    buf.extend_from_slice(&ifindex.to_ne_bytes());
    buf.extend_from_slice(&(libc::IFF_UP as u32).to_ne_bytes()); // ifi_flags
    buf.extend_from_slice(&(libc::IFF_UP as u32).to_ne_bytes()); // ifi_change
    nl_fix(&mut buf, h);
    nl.send(&buf)?; nl.recv_ack()
}

fn nl_set_addr(nl: &NlSock, ifindex: i32, ip: [u8; 4], prefix: u8) -> io::Result<()> {
    let mut buf: Vec<u8> = Vec::new();
    let h = nl_hdr(&mut buf, RTM_NEWADDR,
        NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_REPLACE, 2);
    // ifaddrmsg: family, prefixlen, flags=0, scope, ifindex
    buf.extend_from_slice(&[AF_INET_U8, prefix, 0, RT_SCOPE_UNIVERSE]);
    buf.extend_from_slice(&(ifindex as u32).to_ne_bytes());
    nl_attr(&mut buf, IFA_LOCAL,   &ip);
    nl_attr(&mut buf, IFA_ADDRESS, &ip);
    nl_fix(&mut buf, h);
    nl.send(&buf)?; nl.recv_ack()
}

fn nl_add_default_route(nl: &NlSock, ifindex: i32, gw: [u8; 4]) -> io::Result<()> {
    let mut buf: Vec<u8> = Vec::new();
    let h = nl_hdr(&mut buf, RTM_NEWROUTE,
        NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_REPLACE, 3);
    // rtmsg: family, dst_len=0(default), src_len=0, tos=0, table, proto, scope, type
    buf.extend_from_slice(&[AF_INET_U8, 0, 0, 0,
        RT_TABLE_MAIN, RTPROT_STATIC, RT_SCOPE_UNIVERSE, RTN_UNICAST]);
    buf.extend_from_slice(&0u32.to_ne_bytes()); // rtm_flags
    nl_attr(&mut buf, RTA_GATEWAY, &gw);
    nl_attr(&mut buf, RTA_OIF,     &(ifindex as u32).to_ne_bytes());
    nl_fix(&mut buf, h);
    nl.send(&buf)?; nl.recv_ack()
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Bring up the first non-loopback interface, assign `ip/prefix`, and set `gw`
/// as the default route.  Uses sysfs for discovery, RTnetlink for config.
pub fn configure_network(logf: &mut dyn Write, ip: &str, prefix: u8, gw: &str) -> io::Result<()> {
    let ip_b = parse_ipv4(ip)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid IP"))?;
    let gw_b = parse_ipv4(gw)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid gateway"))?;

    let (ifindex, iface) = find_iface()
        .map_err(|e| { log(logf, &format!("  ERROR find-iface: {}", e)); e })?;
    log(logf, &format!("  Interface: {} (index {})", iface, ifindex));

    let nl = NlSock::open()?;
    nl_link_up(&nl, ifindex)
        .map_err(|e| { log(logf, &format!("  WARN link-up: {}", e)); e })?;
    nl_set_addr(&nl, ifindex, ip_b, prefix)
        .map_err(|e| { log(logf, &format!("  WARN set-addr: {}", e)); e })?;
    nl_add_default_route(&nl, ifindex, gw_b)
        .map_err(|e| { log(logf, &format!("  WARN add-route: {}", e)); e })?;

    log(logf, &format!("  Network: {}/{} gw={} on {}", ip, prefix, gw, iface));
    Ok(())
}
