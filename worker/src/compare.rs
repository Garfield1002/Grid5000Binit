//! Exact comparison of a captured outcome against the controller's expectation.
//!
//! Nothing is masked: every key and the exception kind must match exactly.
//! The single exception is FXSAVE mode, where the kernel cannot capture
//! YMM-high / ZMM / opmask state; those keys are dropped from both sides.

use serde::Deserialize;
use serde_json::{Map, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SaveMode {
    Fxsave,
    Xsave,
}

impl SaveMode {
    pub fn from_raw(v: u32) -> Option<SaveMode> {
        match v {
            libaegis::protocol::SAVE_MODE_FXSAVE => Some(SaveMode::Fxsave),
            libaegis::protocol::SAVE_MODE_XSAVE => Some(SaveMode::Xsave),
            _ => None,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            SaveMode::Fxsave => "fxsave",
            SaveMode::Xsave => "xsave",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Expected {
    #[serde(default)]
    pub final_state: Option<Map<String, Value>>,
    #[serde(default)]
    pub exception_kind: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Got {
    State(Map<String, Value>),
    Exception(String),
}

/// Keys not captured when the kernel uses FXSAVE: `ymmN`, `zmmN`, `kN`/`opmask*`.
pub fn uncaptured_in_fxsave(key: &str) -> bool {
    let num = |p: &str| {
        key.strip_prefix(p)
            .is_some_and(|r| !r.is_empty() && r.bytes().all(|b| b.is_ascii_digit()))
    };
    num("ymm") || num("zmm") || num("k") || key.starts_with("opmask")
}

fn relevant(m: &Map<String, Value>, mode: SaveMode) -> Map<String, Value> {
    m.iter()
        .filter(|(k, _)| mode != SaveMode::Fxsave || !uncaptured_in_fxsave(k))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// True when the observed outcome equals the expectation.
pub fn matches(expected: &Expected, got: &Got, mode: SaveMode) -> bool {
    match (got, &expected.final_state, &expected.exception_kind) {
        (Got::State(g), Some(e), _) => relevant(g, mode) == relevant(e, mode),
        (Got::Exception(g), None, Some(e)) => g == e,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn m(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }
    fn exp_state(v: Value) -> Expected {
        Expected { final_state: Some(m(v)), exception_kind: None }
    }

    #[test]
    fn exact_match() {
        let e = exp_state(json!({"rax": 1, "flag": 2}));
        assert!(matches(&e, &Got::State(m(json!({"rax": 1, "flag": 2}))), SaveMode::Xsave));
    }

    #[test]
    fn flags_are_never_masked() {
        let e = exp_state(json!({"rax": 1, "flag": 2}));
        // an AF-only difference is still a mismatch
        assert!(!matches(&e, &Got::State(m(json!({"rax": 1, "flag": 18}))), SaveMode::Xsave));
        assert!(!matches(&e, &Got::State(m(json!({"rax": 1, "flag": 18}))), SaveMode::Fxsave));
    }

    #[test]
    fn missing_or_extra_key_mismatches() {
        let e = exp_state(json!({"rax": 1, "flag": 2}));
        assert!(!matches(&e, &Got::State(m(json!({"rax": 1}))), SaveMode::Xsave));
        assert!(!matches(&e, &Got::State(m(json!({"rax": 1, "flag": 2, "rbx": 0}))), SaveMode::Xsave));
    }

    #[test]
    fn exceptions_exact() {
        let e = Expected { final_state: None, exception_kind: Some("Invalid Opcode".into()) };
        assert!(matches(&e, &Got::Exception("Invalid Opcode".into()), SaveMode::Xsave));
        assert!(!matches(&e, &Got::Exception("General Protection Fault".into()), SaveMode::Xsave));
        assert!(!matches(&e, &Got::State(m(json!({"flag": 2}))), SaveMode::Xsave));
        assert!(!matches(&exp_state(json!({"flag": 2})), &Got::Exception("Invalid Opcode".into()), SaveMode::Xsave));
    }

    #[test]
    fn fxsave_ignores_only_wide_vector_keys() {
        let e = exp_state(json!({"xmm0": "aa", "ymm0": "bb", "zmm1": 5, "k1": 3, "flag": 2}));
        let g = Got::State(m(json!({"xmm0": "aa", "ymm0": "00", "zmm1": 0, "flag": 2})));
        assert!(matches(&e, &g, SaveMode::Fxsave));
        assert!(!matches(&e, &g, SaveMode::Xsave));
        let bad = Got::State(m(json!({"xmm0": "ab", "ymm0": "bb", "flag": 2})));
        assert!(!matches(&e, &bad, SaveMode::Fxsave));
        assert!(!uncaptured_in_fxsave("mxcsr"));
        assert!(!uncaptured_in_fxsave("k"));
        assert!(!uncaptured_in_fxsave("mm0"));
        assert!(uncaptured_in_fxsave("zmm31"));
    }
}
