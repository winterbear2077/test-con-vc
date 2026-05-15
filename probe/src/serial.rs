//! Serial port — raw 115200 8N1 via /dev/ttyS0.

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
        libc::tcsetattr(fd, libc::TCSANOW, &tty);
    }
}

/// Open `/dev/ttyS0` for read/write and configure it as raw 115200 8N1.
pub fn open_serial() -> io::Result<File> {
    let serial = OpenOptions::new()
        .read(true).write(true)
        .custom_flags(libc::O_NOCTTY | libc::O_NDELAY)
        .open("/dev/ttyS0")?;
    let fd = serial.as_raw_fd();
    setup_serial(fd);
    // Switch back to blocking I/O after open (O_NDELAY was only for open())
    unsafe { libc::fcntl(fd, libc::F_SETFL, 0); }
    Ok(serial)
}
