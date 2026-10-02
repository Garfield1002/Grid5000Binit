//! Host <-> VM protocol.
//!
//! Sync goes through a [`Mailbox`] header at offset 0 of the ivshmem region.
//! Serial only carries logs and the `HELLO` line.
//!
//! Shared memory layout (16 MiB ivshmem):
//!
//! ```text
//! 0x000000  Mailbox header (reserved: 4096 bytes)
//! 0x001000  REQUEST_OFFSET: request payload
//!             [u64; 4] LE   required FeatureBit mask (bit n = FeatureBit n)
//!             TestCase::to_bytes() encoding
//! 0x800000  RESULT_OFFSET: result payload
//!             TestResult::to_bytes() encoding (valid when status is 0 or 1)
//! ```
//!
//! Handshake: the host zeroes the region, spawns the VM, and waits for
//! `magic` to appear. The kernel writes `version`, `features`, zeroes the
//! sequence numbers, then writes `magic` last (release). To run a case the
//! host writes the request payload, then `req_seq += 1` (release). The kernel
//! polls `req_seq != ack_seq`, executes, writes the result payload, `status`
//! and `exception`, then sets `ack_seq = req_seq` (release). The host polls
//! `ack_seq == req_seq` (acquire) with a timeout.

use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use core::sync::atomic::{Ordering, fence};

/// Initialize a connection (serial)
pub const INIT_MSG: &str = "HELLO";

/// Legacy serial commands, only used by the superseded `client` crate.
pub const CONTINUE_MSG: &str = "CONTINUE";
pub const READ_MSG: &str = "READ";
pub const WRITE_MSG: &str = "WRITE";
pub const EXIT_MSG: &str = "EXIT";

/// `"AEGISG5K"` as a big-endian u64 literal; written by the kernel at boot.
pub const MAILBOX_MAGIC: u64 = 0x4145_4749_5347_354B;
pub const MAILBOX_VERSION: u32 = 2;

/// `Mailbox::save_mode`: the kernel saves/restores state with FXSAVE/FXRSTOR.
/// Captured: x87/MMX, XMM, MXCSR. YMM-high, opmask and ZMM are NOT captured;
/// in results they read as zero and `XSTATE_BV` (image offset 512) is zero.
pub const SAVE_MODE_FXSAVE: u32 = 0;
/// `Mailbox::save_mode`: XSAVE/XRSTOR (components 0-2, plus 5-7 when AVX-512
/// is enabled in XCR0). Captured: x87/MMX, XMM, MXCSR, YMM, opmask, ZMM.
pub const SAVE_MODE_XSAVE: u32 = 1;

/// Request payload offset (the mailbox header occupies the first page).
pub const PAYLOAD_OFFSET: usize = 4096;
pub const REQUEST_OFFSET: usize = PAYLOAD_OFFSET;
/// Result payload offset.
pub const RESULT_OFFSET: usize = 0x_0080_0000;
/// Legacy name for [`RESULT_OFFSET`].
pub const WRITE_REGION_OFFSET: usize = RESULT_OFFSET;
/// Size of the request area (including the 32 byte required-feature mask).
pub const REQUEST_MAX_LEN: usize = RESULT_OFFSET - REQUEST_OFFSET;
/// Bytes of the required-feature mask at the start of the request payload.
pub const REQUEST_FEATURES_LEN: usize = 32;

pub const STATUS_OK: u32 = 0;
pub const STATUS_EXCEPTION: u32 = 1;
pub const STATUS_SKIPPED_MISSING_FEATURE: u32 = 2;

/// Number of u64 words in a feature mask.
pub const FEATURE_WORDS: usize = 4;
/// A CPUID feature bitmask; bit `n` is `FeatureBit` with discriminant `n`.
pub type FeatureMask = [u64; FEATURE_WORDS];

/// Shared header at ivshmem offset 0.
#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct Mailbox {
    /// [`MAILBOX_MAGIC`], written by the kernel at boot (last)
    pub magic: u64,
    /// [`MAILBOX_VERSION`]
    pub version: u32,
    pub _pad: u32,
    /// Features usable by the kernel (written at boot)
    pub features: FeatureMask,
    /// Host increments after writing a request
    pub req_seq: u64,
    /// Kernel sets = req_seq after writing the result
    pub ack_seq: u64,
    /// `STATUS_*`
    pub status: u32,
    /// Exception vector if status == STATUS_EXCEPTION
    pub exception: u32,
    /// `SAVE_MODE_*`, written by the kernel at boot (version >= 2)
    pub save_mode: u32,
    pub _pad2: u32,
}

const _: () = assert!(core::mem::size_of::<Mailbox>() <= PAYLOAD_OFFSET);
const _: () = assert!(core::mem::offset_of!(Mailbox, features) == 16);
const _: () = assert!(core::mem::offset_of!(Mailbox, req_seq) == 48);
const _: () = assert!(core::mem::offset_of!(Mailbox, ack_seq) == 56);
const _: () = assert!(core::mem::offset_of!(Mailbox, status) == 64);
const _: () = assert!(core::mem::offset_of!(Mailbox, exception) == 68);
const _: () = assert!(core::mem::offset_of!(Mailbox, save_mode) == 72);

/// Volatile accessors. All take a raw pointer to the mapped mailbox and are
/// safe to call from the host (mmap) or the kernel (ivshmem BAR).
impl Mailbox {
    /// Kernel, at boot: zero the header, publish version and features, then
    /// publish the magic (release).
    ///
    /// # Safety
    /// `p` must point to a valid, writable, 8-byte aligned mailbox mapping.
    pub unsafe fn kernel_init(p: *mut Mailbox, features: &FeatureMask, save_mode: u32) {
        unsafe {
            write_volatile(addr_of_mut!((*p).magic), 0);
            write_volatile(addr_of_mut!((*p).version), MAILBOX_VERSION);
            write_volatile(addr_of_mut!((*p)._pad), 0);
            for (i, w) in features.iter().enumerate() {
                write_volatile(addr_of_mut!((*p).features[i]), *w);
            }
            write_volatile(addr_of_mut!((*p).req_seq), 0);
            write_volatile(addr_of_mut!((*p).ack_seq), 0);
            write_volatile(addr_of_mut!((*p).status), 0);
            write_volatile(addr_of_mut!((*p).exception), 0);
            write_volatile(addr_of_mut!((*p).save_mode), save_mode);
            write_volatile(addr_of_mut!((*p)._pad2), 0);
            fence(Ordering::Release);
            write_volatile(addr_of_mut!((*p).magic), MAILBOX_MAGIC);
        }
    }

    /// Host: true once the kernel has published the magic. Afterwards
    /// [`Mailbox::read_features`] and [`Mailbox::read_version`] are valid.
    ///
    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn is_ready(p: *const Mailbox) -> bool {
        let m = unsafe { read_volatile(addr_of!((*p).magic)) };
        fence(Ordering::Acquire);
        m == MAILBOX_MAGIC
    }

    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn read_version(p: *const Mailbox) -> u32 {
        unsafe { read_volatile(addr_of!((*p).version)) }
    }

    /// Host: `SAVE_MODE_*` published by the kernel (valid once ready).
    ///
    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn read_save_mode(p: *const Mailbox) -> u32 {
        unsafe { read_volatile(addr_of!((*p).save_mode)) }
    }

    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn read_features(p: *const Mailbox) -> FeatureMask {
        let mut out = [0u64; FEATURE_WORDS];
        for (i, w) in out.iter_mut().enumerate() {
            *w = unsafe { read_volatile(addr_of!((*p).features[i])) };
        }
        out
    }

    /// Host: publish a request (payload must already be written). Returns the
    /// new `req_seq`. Release fence before the store.
    ///
    /// # Safety
    /// `p` must point to a valid, writable mailbox mapping.
    pub unsafe fn submit(p: *mut Mailbox) -> u64 {
        unsafe {
            let next = read_volatile(addr_of!((*p).req_seq)).wrapping_add(1);
            fence(Ordering::Release);
            write_volatile(addr_of_mut!((*p).req_seq), next);
            fence(Ordering::SeqCst);
            next
        }
    }

    /// Kernel: current `req_seq` (acquire).
    ///
    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn load_req_seq(p: *const Mailbox) -> u64 {
        let v = unsafe { read_volatile(addr_of!((*p).req_seq)) };
        fence(Ordering::Acquire);
        v
    }

    /// Host: current `ack_seq` (acquire). The result is complete when it
    /// equals the `req_seq` returned by [`Mailbox::submit`].
    ///
    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn load_ack_seq(p: *const Mailbox) -> u64 {
        let v = unsafe { read_volatile(addr_of!((*p).ack_seq)) };
        fence(Ordering::Acquire);
        v
    }

    /// Kernel: publish a result (payload already written): sets status and
    /// exception, then `ack_seq = seq` (release).
    ///
    /// # Safety
    /// `p` must point to a valid, writable mailbox mapping.
    pub unsafe fn ack(p: *mut Mailbox, seq: u64, status: u32, exception: u32) {
        unsafe {
            write_volatile(addr_of_mut!((*p).status), status);
            write_volatile(addr_of_mut!((*p).exception), exception);
            fence(Ordering::Release);
            write_volatile(addr_of_mut!((*p).ack_seq), seq);
            fence(Ordering::SeqCst);
        }
    }

    /// Host: read `(status, exception)` after `ack_seq` matched.
    ///
    /// # Safety
    /// `p` must point to a valid mailbox mapping.
    pub unsafe fn read_status(p: *const Mailbox) -> (u32, u32) {
        unsafe {
            (
                read_volatile(addr_of!((*p).status)),
                read_volatile(addr_of!((*p).exception)),
            )
        }
    }
}

macro_rules! features {
    ($( $variant:ident = $name:literal ),* $(,)?) => {
        /// A CPUID feature. The discriminant is the bit index in the mask.
        /// Names match iced-x86 `CpuidFeature` names (`name()`), which is what
        /// the controller sends in `required_features`.
        #[repr(u8)]
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        #[allow(clippy::upper_case_acronyms)]
        pub enum FeatureBit {
            $( $variant ),*
        }

        impl FeatureBit {
            /// Every feature, in bit order.
            pub const ALL: &'static [FeatureBit] = &[ $( FeatureBit::$variant ),* ];

            /// The iced-x86 style name, e.g. `"AVX512_VBMI"`.
            pub const fn name(self) -> &'static str {
                match self { $( FeatureBit::$variant => $name ),* }
            }
        }
    };
}

features! {
    X87 = "X87",
    MMX = "MMX",
    SSE = "SSE",
    SSE2 = "SSE2",
    SSE3 = "SSE3",
    SSSE3 = "SSSE3",
    SSE4_1 = "SSE4_1",
    SSE4_2 = "SSE4_2",
    POPCNT = "POPCNT",
    LZCNT = "LZCNT",
    BMI1 = "BMI1",
    BMI2 = "BMI2",
    ADX = "ADX",
    AVX = "AVX",
    AVX2 = "AVX2",
    FMA = "FMA",
    F16C = "F16C",
    AVX512F = "AVX512F",
    AVX512BW = "AVX512BW",
    AVX512DQ = "AVX512DQ",
    AVX512VL = "AVX512VL",
    AVX512CD = "AVX512CD",
    AVX512VBMI = "AVX512_VBMI",
    AVX512VNNI = "AVX512_VNNI",
    AES = "AES",
    PCLMULQDQ = "PCLMULQDQ",
    SHA = "SHA",
    MOVBE = "MOVBE",
    XSAVE = "XSAVE",
    OSXSAVE = "OSXSAVE",
    RDRAND = "RDRAND",
    RDSEED = "RDSEED",
    CMPXCHG16B = "CMPXCHG16B",
}

impl FeatureBit {
    /// Bit index in the mask.
    pub const fn bit(self) -> u8 {
        self as u8
    }

    pub const fn from_bit(bit: u8) -> Option<FeatureBit> {
        if (bit as usize) < Self::ALL.len() {
            Some(Self::ALL[bit as usize])
        } else {
            None
        }
    }

    /// Parses an iced-x86 style name (case-insensitive; the underscore in
    /// `AVX512_VBMI`/`AVX512_VNNI` is optional).
    pub fn from_name(name: &str) -> Option<FeatureBit> {
        Self::ALL.iter().copied().find(|f| {
            let n = f.name();
            n.eq_ignore_ascii_case(name)
                || (n.contains('_')
                    && n.len() == name.len() + 1
                    && n.bytes()
                        .filter(|&b| b != b'_')
                        .map(|b| b.to_ascii_uppercase())
                        .eq(name.bytes().map(|b| b.to_ascii_uppercase())))
        })
    }
}

/// Helpers over [`FeatureMask`].
pub const fn mask_set(mask: &mut FeatureMask, f: FeatureBit) {
    mask[(f as usize) / 64] |= 1u64 << ((f as usize) % 64);
}

pub const fn mask_has(mask: &FeatureMask, f: FeatureBit) -> bool {
    mask[(f as usize) / 64] & (1u64 << ((f as usize) % 64)) != 0
}

/// True when every bit of `required` is also set in `have`.
pub const fn mask_is_subset(required: &FeatureMask, have: &FeatureMask) -> bool {
    let mut i = 0;
    while i < FEATURE_WORDS {
        if required[i] & !have[i] != 0 {
            return false;
        }
        i += 1;
    }
    true
}

/// Serializes a mask as 32 little-endian bytes (request payload prefix).
pub fn mask_to_bytes(mask: &FeatureMask) -> [u8; REQUEST_FEATURES_LEN] {
    let mut out = [0u8; REQUEST_FEATURES_LEN];
    for (i, w) in mask.iter().enumerate() {
        out[i * 8..i * 8 + 8].copy_from_slice(&w.to_le_bytes());
    }
    out
}

/// Parses the 32 byte request payload prefix.
pub fn mask_from_bytes(bytes: &[u8]) -> Option<FeatureMask> {
    if bytes.len() < REQUEST_FEATURES_LEN {
        return None;
    }
    let mut out = [0u64; FEATURE_WORDS];
    for (i, w) in out.iter_mut().enumerate() {
        *w = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    Some(out)
}

/// `std`-only helpers (allocate).
#[cfg(feature = "std")]
pub mod names {
    use super::*;

    /// Names of the features set in `mask`.
    pub fn mask_to_names(mask: &FeatureMask) -> std::vec::Vec<&'static str> {
        FeatureBit::ALL
            .iter()
            .filter(|f| mask_has(mask, **f))
            .map(|f| f.name())
            .collect()
    }

    /// Builds a mask from names. Returns `Err(name)` for the first unknown name.
    pub fn mask_from_names<'a, I: IntoIterator<Item = &'a str>>(
        names: I,
    ) -> Result<FeatureMask, &'a str> {
        let mut mask = [0u64; FEATURE_WORDS];
        for n in names {
            mask_set(&mut mask, FeatureBit::from_name(n).ok_or(n)?);
        }
        Ok(mask)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_roundtrip() {
        for (i, f) in FeatureBit::ALL.iter().enumerate() {
            assert_eq!(f.bit() as usize, i);
            assert_eq!(FeatureBit::from_bit(i as u8), Some(*f));
            assert_eq!(FeatureBit::from_name(f.name()), Some(*f));
        }
        assert_eq!(
            FeatureBit::from_name("avx512vbmi"),
            Some(FeatureBit::AVX512VBMI)
        );
        assert_eq!(FeatureBit::from_name("nope"), None);
        assert!(FeatureBit::ALL.len() <= 256);
    }

    #[test]
    fn subset_and_bytes() {
        let mut a = [0u64; 4];
        let mut b = [0u64; 4];
        mask_set(&mut a, FeatureBit::AVX);
        mask_set(&mut b, FeatureBit::AVX);
        mask_set(&mut b, FeatureBit::SSE);
        assert!(mask_is_subset(&a, &b));
        assert!(!mask_is_subset(&b, &a));
        assert_eq!(mask_from_bytes(&mask_to_bytes(&b)), Some(b));
    }

    #[cfg(feature = "std")]
    #[test]
    fn std_names() {
        let m = names::mask_from_names(["AVX", "SSE4_1"]).unwrap();
        assert_eq!(names::mask_to_names(&m), ["SSE4_1", "AVX"]);
    }
}
