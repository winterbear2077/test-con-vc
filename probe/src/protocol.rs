//! JSON probe protocol (serial/vsock two-phase) and guestinfo fallback.

use std::io::{self, BufRead, Write};
use std::process::Command;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

use crate::icmp::run_pings;
use crate::log::log;
use crate::netconfig::configure_network;
use crate::tcp::run_tcp_connects;

// ── Serial / vsock channel ────────────────────────────────────────────────────

/// Run the newline-delimited JSON probe protocol over an already-open channel.
///
/// Single-phase: controller sends `{ip,prefix,gw,targets}` → probe replies
/// `{status:"done",results:{...}}`.
///
/// Two-phase: controller sends `{ip,prefix,gw}` (no targets) → probe replies
/// `{status:"ready"}` → controller sends `{targets:[...]}` → probe replies
/// `{status:"done",results:{...}}`.
pub fn run_probe_protocol(
    logf:   &mut dyn Write,
    reader: &mut dyn BufRead,
    writer: &mut dyn Write,
) -> io::Result<()> {
    let mut line1 = String::new();
    reader.read_line(&mut line1)?;
    let line1 = line1.trim();
    log(logf, &format!("  RX: {}", line1));

    let msg1: Value = serde_json::from_str(line1)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e.to_string()))?;

    let ip     = msg1["ip"].as_str().unwrap_or("").to_string();
    let prefix = msg1["prefix"].as_u64().unwrap_or(24) as u8;
    let gw     = msg1["gw"].as_str().unwrap_or("").to_string();

    if ip.is_empty() || gw.is_empty() {
        writeln!(writer, "{}", json!({"status":"error","reason":"bad-config"}))?;
        writer.flush()?;
        return Err(io::Error::new(io::ErrorKind::InvalidData, "missing ip/gw"));
    }

    configure_network(logf, &ip, prefix, &gw)?;

    let targets: Vec<String> = match msg1["targets"].as_array() {
        Some(arr) => arr.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
        None => {
            // Two-phase: no targets in first message — send ready and wait
            log(logf, "  Two-phase: sending ready");
            writeln!(writer, "{}", json!({"status":"ready"}))?;
            writer.flush()?;

            let mut line2 = String::new();
            reader.read_line(&mut line2)?;
            let line2 = line2.trim();
            log(logf, &format!("  RX phase-2: {}", line2));

            let msg2: Value = serde_json::from_str(line2)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e.to_string()))?;
            msg2["targets"].as_array()
                .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                .unwrap_or_default()
        }
    };

    // Probe type and optional TCP ports
    let probe_type = msg1["probe_type"].as_str().unwrap_or("icmp").to_string();
    let tcp_ports: Vec<u16> = msg1["tcp_ports"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_u64().map(|n| n as u16)).collect())
        .unwrap_or_default();

    let results = if probe_type == "tcp" {
        run_tcp_connects(logf, &targets, &tcp_ports)
    } else {
        run_pings(logf, &targets)
    };
    let reply = json!({"status":"done","results": results}).to_string();
    log(logf, &format!("  TX: {}", reply));
    writeln!(writer, "{}", reply)?;
    writer.flush()?;
    Ok(())
}

// ── guestinfo / vmtoolsd channel ──────────────────────────────────────────────

fn vmget(key: &str) -> String {
    Command::new("vmtoolsd")
        .args(["--cmd", &format!("info-get {}", key)])
        .output().ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

fn vmset(key: &str, val: &str) {
    let _ = Command::new("vmtoolsd")
        .args(["--cmd", &format!("info-set {} {}", key, val)])
        .status();
}

pub fn run_guestinfo_probe(logf: &mut dyn Write) -> io::Result<()> {
    // Poll until controller writes guestinfo.nettest.ip (up to 4 min)
    let mut ip = String::new();
    for _ in 0..120 {
        ip = vmget("guestinfo.nettest.ip");
        if !ip.is_empty() { break; }
        thread::sleep(Duration::from_secs(2));
    }
    if ip.is_empty() {
        return Err(io::Error::new(io::ErrorKind::TimedOut,
            "timeout waiting for guestinfo.nettest.ip"));
    }

    let prefix: u8 = vmget("guestinfo.nettest.prefix").parse().unwrap_or(24);
    let gw          = vmget("guestinfo.nettest.gw");
    let ready       = vmget("guestinfo.nettest.ready");
    let targets_raw = vmget("guestinfo.nettest.targets");

    log(logf, &format!("  guestinfo: ip={} prefix={} gw={} ready={}", ip, prefix, gw, ready));
    configure_network(logf, &ip, prefix, &gw)?;

    if ready == "wait" {
        vmset("guestinfo.nettest.status", "waiting");
        log(logf, "  Waiting for ready=go...");
        for _ in 0..150 {
            if vmget("guestinfo.nettest.ready") == "go" { break; }
            thread::sleep(Duration::from_secs(2));
        }
    }

    // Parse JSON array from guestinfo (may be bare or quoted)
    let targets: Vec<String> = serde_json::from_str::<Vec<String>>(&targets_raw)
        .unwrap_or_else(|_| {
            targets_raw.trim_matches(|c: char| c == '[' || c == ']' || c == ' ')
                .split(',')
                .map(|s| s.trim().trim_matches('"').to_string())
                .filter(|s| !s.is_empty())
                .collect()
        });

    let probe_type  = vmget("guestinfo.nettest.probe_type");
    let ports_raw   = vmget("guestinfo.nettest.tcp_ports");
    let tcp_ports: Vec<u16> = ports_raw
        .split(',')
        .filter_map(|s| s.trim().parse().ok())
        .collect();

    let results = if probe_type == "tcp" {
        log(logf, &format!("  probe_type=tcp ports={:?}", tcp_ports));
        run_tcp_connects(logf, &targets, &tcp_ports)
    } else {
        log(logf, "  probe_type=icmp");
        run_pings(logf, &targets)
    };
    let results_json = serde_json::to_string(&results).unwrap_or_else(|_| "{}".to_string());
    vmset("guestinfo.nettest.results", &results_json);
    vmset("guestinfo.nettest.status", "done");
    log(logf, "  guestinfo results written");
    Ok(())
}
