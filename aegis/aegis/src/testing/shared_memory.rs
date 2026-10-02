//! Mailbox-based host <-> kernel transport over ivshmem.
//!
//! See `libaegis::protocol` for the layout. Serial is only used for logs and
//! the `HELLO` handshake.

use alloc::vec::Vec;
use libaegis::{
    compressible::CodecError,
    protocol::{
        FeatureMask, INIT_MSG, Mailbox, REQUEST_FEATURES_LEN, REQUEST_MAX_LEN, REQUEST_OFFSET,
        RESULT_OFFSET, mask_from_bytes,
    },
    testcase::{TestCase, TestResult},
};
use spin::Mutex;
use x86_64::VirtAddr;

use crate::{kernel::driver::serial::SERIAL1, println, serial_println};

pub static SHARED_MEMORY_MANAGER: Mutex<SharedMemoryManager> =
    Mutex::new(SharedMemoryManager::new());

/// Receives a line (without `\r`/`\n`) from the serial port.
fn recv_line(buff: &mut Vec<u8>) -> &str {
    loop {
        let b = SERIAL1.lock().receive();
        match b {
            b'\n' => break,
            b'\r' => {}
            _ => buff.push(b),
        }
    }
    core::str::from_utf8(buff).expect("Received raw bytes")
}

/// A request read from the mailbox.
pub struct Request {
    pub seq: u64,
    pub required: FeatureMask,
    pub test: TestCase,
}

pub struct SharedMemoryManager {
    base: VirtAddr,
    size: usize,
    /// Last `req_seq` that was acknowledged
    last_seq: u64,
}

impl SharedMemoryManager {
    const fn new() -> Self {
        Self {
            base: VirtAddr::zero(),
            size: 0,
            last_seq: 0,
        }
    }

    fn mailbox(&self) -> *mut Mailbox {
        self.base.as_mut_ptr()
    }

    /// Publishes the mailbox header (version, features, then magic).
    pub fn init(&mut self, base: VirtAddr, size: usize, features: &FeatureMask, save_mode: u32) {
        assert!(size > RESULT_OFFSET, "ivshmem too small for the mailbox");
        self.base = base;
        self.size = size;
        self.last_seq = 0;
        unsafe { Mailbox::kernel_init(self.mailbox(), features, save_mode) };
    }

    /// Serial handshake: the host sends `HELLO`, we answer `HELLO`.
    pub fn hello(&self) {
        println!("[*] Awaiting client");
        let mut buff = Vec::new();

        loop {
            let res = recv_line(&mut buff);
            println!("[*] Received client '{}' (expected '{}')", res, INIT_MSG);
            if res == INIT_MSG {
                break;
            }
            buff.clear();
        }

        while SERIAL1.lock().try_receive().is_ok() {}

        serial_println!("{}", INIT_MSG);
        println!("[*] Sent '{}'", INIT_MSG);
    }

    /// Busy-waits for the next request and decodes it.
    pub fn wait_request(&mut self) -> Request {
        let mb = self.mailbox();
        let seq = loop {
            let seq = unsafe { Mailbox::load_req_seq(mb) };
            if seq != self.last_seq {
                break seq;
            }
            core::hint::spin_loop();
        };

        let payload: &[u8] = unsafe {
            core::slice::from_raw_parts(
                (self.base.as_u64() as usize + REQUEST_OFFSET) as *const u8,
                REQUEST_MAX_LEN,
            )
        };
        let required = mask_from_bytes(payload).expect("short request");
        let (_, test) = TestCase::from_bytes(&payload[REQUEST_FEATURES_LEN..])
            .expect("Failed to decode request payload");

        Request {
            seq,
            required,
            test,
        }
    }

    /// Writes the result payload, then acknowledges the request.
    pub fn complete(&mut self, seq: u64, res: &TestResult, status: u32, exception: u32) {
        let out: &mut [u8] = unsafe {
            core::slice::from_raw_parts_mut(
                (self.base.as_u64() as usize + RESULT_OFFSET) as *mut u8,
                self.size - RESULT_OFFSET,
            )
        };
        res.to_bytes(out)
            .map_err(|_: CodecError| ())
            .expect("Result does not fit in the result area");
        self.ack(seq, status, exception);
    }

    /// Acknowledges a request without a result payload.
    pub fn ack(&mut self, seq: u64, status: u32, exception: u32) {
        unsafe { Mailbox::ack(self.mailbox(), seq, status, exception) };
        self.last_seq = seq;
    }
}
