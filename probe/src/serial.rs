//! Serial port — raw 115200 8N1 via /dev/ttyS*.

use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;

pub fn setup_serial(fd: i32) {
    unsafe {
        let mut tty: libc::termios = std::mem::zeroed();
        if libc::tcgetattr(fd, &mut tty) != 0 { return; }
        libc::cfmakeraw(&mut tty);
        libc::cfsetispeed(&mut tty, libc::B115200);
        libc::cfsetospeed(&mut tty, libc::B115200);
        // VMIN=0, VTIME=N*100ms — non-blocking read with timeout.
        // The caller overwrites these before actual use; defaults kept at 0
        // so open_serial() itself never blocks on a read.
        tty.c_cc[libc::VMIN]  = 0;
        tty.c_cc[libc::VTIME] = 0;
        libc::tcsetattr(fd, libc::TCSANOW, &tty);
    }
}

/// Configure VMIN/VTIME for a read timeout of `deciseconds` × 100 ms.
/// Set deciseconds=0 to return immediately when no data is available.
/// Set deciseconds=255 (max) for a ~25-second blocking read.
pub fn set_read_timeout(fd: i32, deciseconds: u8) {
    unsafe {
        let mut tty: libc::termios = std::mem::zeroed();
        if libc::tcgetattr(fd, &mut tty) != 0 { return; }
        tty.c_cc[libc::VMIN]  = 0;
        tty.c_cc[libc::VTIME] = deciseconds;
        libc::tcsetattr(fd, libc::TCSANOW, &tty);
    }
}

/// Open `/dev/ttyS0..ttyS3` for read/write and configure as raw 115200 8N1.
///
/// Different ESXi/hardware combinations can enumerate the first virtual serial
/// device as ttyS1 (or higher), so we probe a small range instead of assuming
/// ttyS0 always maps to the VM serial backing.
pub fn open_serial() -> io::Result<File> {
    let candidates = ["/dev/ttyS0", "/dev/ttyS1", "/dev/ttyS2", "/dev/ttyS3"];
    let mut last_err: Option<io::Error> = None;

    for dev in candidates {
        match OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NOCTTY | libc::O_NDELAY)
            .open(dev)
        {
            Ok(serial) => {
                let fd = serial.as_raw_fd();
                setup_serial(fd);
                // Switch back to blocking I/O after open (O_NDELAY was only for open())
                unsafe { libc::fcntl(fd, libc::F_SETFL, 0); }
                return Ok(serial);
            }
            Err(e) => {
                last_err = Some(e);
            }
        }
    }

    Err(last_err.unwrap_or_else(|| {
        io::Error::new(io::ErrorKind::NotFound, "no serial device found in /dev/ttyS0..ttyS3")
    }))
}
