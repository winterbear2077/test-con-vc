//! AF_VSOCK connection to VMADDR_CID_HOST (CID=2).

use std::fs::File;
use std::io;
use std::os::unix::io::FromRawFd;

/// Matches the Linux `sockaddr_vm` layout (16 bytes on x86_64).
#[repr(C)]
struct SockaddrVm {
    svm_family:    u16, // AF_VSOCK = 40
    svm_reserved1: u16,
    svm_port:      u32,
    svm_cid:       u32,
    svm_flags:     u8,
    _padding:      [u8; 3],
}

/// Connect to the ESXi host (VMADDR_CID_HOST = 2) on the given vsock port.
pub fn vsock_connect(port: u32, timeout_secs: u64) -> io::Result<File> {
    const AF_VSOCK: i32 = 40;
    const VMADDR_CID_HOST: u32 = 2;

    unsafe {
        let fd = libc::socket(AF_VSOCK, libc::SOCK_STREAM, 0);
        if fd < 0 { return Err(io::Error::last_os_error()); }

        let tv = libc::timeval { tv_sec: timeout_secs as _, tv_usec: 0 };
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_SNDTIMEO,
            &tv as *const libc::timeval as *const libc::c_void,
            std::mem::size_of::<libc::timeval>() as _);

        let addr = SockaddrVm {
            svm_family:    AF_VSOCK as u16,
            svm_reserved1: 0,
            svm_port:      port,
            svm_cid:       VMADDR_CID_HOST,
            svm_flags:     0,
            _padding:      [0; 3],
        };
        let ret = libc::connect(fd,
            &addr as *const SockaddrVm as *const libc::sockaddr,
            std::mem::size_of::<SockaddrVm>() as u32);
        if ret < 0 {
            let e = io::Error::last_os_error();
            libc::close(fd);
            return Err(e);
        }
        Ok(File::from_raw_fd(fd))
    }
}
