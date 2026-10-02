//! Tiny stderr logger with unix timestamps.

use std::time::{SystemTime, UNIX_EPOCH};

pub fn ts() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    format!("{}.{:03}", d.as_secs(), d.subsec_millis())
}

macro_rules! log {
    ($($arg:tt)*) => {
        eprintln!("[{}] {}", $crate::log::ts(), format_args!($($arg)*))
    };
}
