//! x86db JSON state <-> `CpuState`.
//!
//! Adapted from the old `aegis/client` (`apply_state` / `get_state_value` /
//! `serialize_state`). Keys are the flat x86db keys (`rax`, `flag`,
//! `x87_r0`, `mm0`, `xmm0`, `mem0_value`, ...).

use std::error::Error;

use libaegis::cpu::{AvxState, CpuState, FlagState, SCRATCH_MEMORY_SIZE};
use serde_json::{Map, Number, Value};

pub type DbState = Map<String, Value>;
type Res<T> = Result<T, Box<dyn Error>>;

fn json_u64(v: u64) -> Value {
    Value::Number(Number::from(v))
}

fn indexed(key: &str, prefix: &str, limit: usize) -> Option<usize> {
    key.strip_prefix(prefix)?
        .parse::<usize>()
        .ok()
        .filter(|&i| i < limit)
}

fn x87_logical_index(key: &str) -> Option<usize> {
    indexed(key, "x87_st", 8)
}
fn x87_physical_index(key: &str) -> Option<usize> {
    indexed(key, "x87_r", 8)
}
fn xmm_index(key: &str) -> Option<usize> {
    indexed(key, "xmm", 16)
}
fn ymm_index(key: &str) -> Option<usize> {
    indexed(key, "ymm", 16)
}
fn zmm_index(key: &str) -> Option<usize> {
    indexed(key, "zmm", 32)
}
fn mm_index(key: &str) -> Option<usize> {
    indexed(key, "mm", 8)
}

fn value_to_hex_bytes<const N: usize>(what: &str, value: &Value) -> Res<[u8; N]> {
    let Value::String(text) = value else {
        return Err(format!(
            "{what} value must be a {}-digit hexadecimal string, got {value}",
            N * 2
        )
        .into());
    };
    let text = text.strip_prefix("0x").unwrap_or(text);
    if text.len() != N * 2 || !text.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!(
            "invalid {what} encoding {text:?}; expected {} hexadecimal digits",
            N * 2
        )
        .into());
    }
    let mut out = [0; N];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16)?;
    }
    Ok(out)
}

fn set_vector<const N: usize>(bytes: &mut [u8; N], value: u64) {
    bytes[..8].copy_from_slice(&value.to_le_bytes());
}

/// Hex string, or a JSON number naming the low lane only.
fn value_to_vector<const N: usize>(what: &str, value: &Value) -> Res<[u8; N]> {
    match value {
        Value::String(_) => value_to_hex_bytes(what, value),
        Value::Number(_) => {
            let mut bytes = [0; N];
            set_vector(&mut bytes, value_to_u64(value)?);
            Ok(bytes)
        }
        other => Err(format!(
            "{what} value must be a {}-digit hexadecimal string or a number, got {other}",
            N * 2
        )
        .into()),
    }
}

fn json_hex_bytes(value: &[u8]) -> Value {
    Value::String(value.iter().map(|b| format!("{b:02x}")).collect())
}

fn value_to_scratch(value: &Value) -> Res<[u8; SCRATCH_MEMORY_SIZE]> {
    let Value::String(text) = value else {
        return Err(format!("scratch_memory must be a hexadecimal string, got {value}").into());
    };
    if text.len() != SCRATCH_MEMORY_SIZE * 2
        || !text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(format!(
            "invalid scratch_memory; expected {} lowercase hexadecimal digits",
            SCRATCH_MEMORY_SIZE * 2
        )
        .into());
    }
    let mut out = [0; SCRATCH_MEMORY_SIZE];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16)?;
    }
    Ok(out)
}

pub fn value_to_u64(value: &Value) -> Res<u64> {
    match value {
        Value::Number(n) => {
            if let Some(v) = n.as_u64() {
                Ok(v)
            } else if let Some(v) = n.as_i64() {
                Ok(v as u64)
            } else {
                Err(format!("unsupported floating-point state value: {n}").into())
            }
        }
        Value::String(s) => Ok(s.parse::<u64>()?),
        other => Err(format!("unsupported state value: {other}").into()),
    }
}

/// Scalar u64 fields addressed by key: generates a getter and a setter.
macro_rules! scalar_fields {
    ($( $key:literal => $($path:ident).+ ),* $(,)?) => {
        fn scalar_get(s: &CpuState, key: &str) -> Option<u64> {
            match key { $( $key => Some(s.$($path).+), )* _ => None }
        }
        fn scalar_set(s: &mut CpuState, key: &str, v: u64) -> bool {
            match key { $( $key => { s.$($path).+ = v; true } )* _ => false }
        }
    };
}

scalar_fields! {
    "x87_ip" => fpu.instruction_pointer,
    "x87_dp" => fpu.data_pointer,
    "mem0_value" => mem0,
    "mem1_value" => mem1,
    "rax" => gpr.rax, "rbx" => gpr.rbx, "rcx" => gpr.rcx, "rdx" => gpr.rdx,
    "rsi" => gpr.rsi, "rdi" => gpr.rdi, "rbp" => gpr.rbp, "rsp" => gpr.rsp,
    "r8" => gpr.r8, "r9" => gpr.r9, "r10" => gpr.r10, "r11" => gpr.r11,
    "r12" => gpr.r12, "r13" => gpr.r13, "r14" => gpr.r14, "r15" => gpr.r15,
    "rip" => rip,
    "cs" => seg.cs, "ds" => seg.ds, "es" => seg.es,
    "fs" => seg.fs, "gs" => seg.gs, "ss" => seg.ss,
}

/// Builds the initial `CpuState` from an x86db state object. Returns the keys
/// that must be read back from the final state (`flag` is always added by
/// [`serialize_state`]).
pub fn apply_state(cpu: &mut CpuState, data: &DbState) -> Res<Vec<String>> {
    let has_logical_x87 = data.keys().any(|k| x87_logical_index(k).is_some());
    let has_physical_x87 = data.keys().any(|k| x87_physical_index(k).is_some());
    if has_logical_x87 && has_physical_x87 {
        return Err("x87_stN logical and x87_rN physical fields cannot be mixed".into());
    }
    if data.contains_key("scratch_memory")
        && (data.contains_key("mem0_value") || data.contains_key("mem1_value"))
    {
        return Err("scratch_memory cannot be mixed with mem0_value or mem1_value".into());
    }
    let has_x87 = has_logical_x87
        || has_physical_x87
        || data.keys().any(|k| {
            matches!(
                k.as_str(),
                "x87_control"
                    | "x87_status"
                    | "x87_top"
                    | "x87_tag"
                    | "x87_opcode"
                    | "x87_ip"
                    | "x87_dp"
            )
        });
    let has_mmx = data.keys().any(|k| mm_index(k).is_some());
    if has_mmx && !has_x87 {
        cpu.fpu.initialize_mmx();
    }
    // TOP first: JSON object order must not decide which physical slot a
    // logical ST(i) lands in.
    if let Some(status) = data.get("x87_status") {
        cpu.fpu.status = value_to_u64(status)?.try_into()?;
    }
    if let Some(top) = data.get("x87_top") {
        let top = value_to_u64(top)?;
        if top > 7 {
            return Err(format!("x87_top must be in 0..=7, got {top}").into());
        }
        if let Some(status) = data.get("x87_status") {
            let status_top = (value_to_u64(status)? >> 11) & 7;
            if status_top != top {
                return Err(format!(
                    "x87_top ({top}) conflicts with x87_status.TOP ({status_top})"
                )
                .into());
            }
        }
        cpu.fpu.status = (cpu.fpu.status & !(7 << 11)) | ((top as u16) << 11);
    }
    if has_x87 && has_mmx {
        for phys in 0..8 {
            let Some(mmx) = data.get(&format!("mm{phys}")) else {
                continue;
            };
            let x87_key = if has_physical_x87 {
                format!("x87_r{phys}")
            } else {
                format!("x87_st{}", (phys + 8 - cpu.fpu.top()) & 7)
            };
            let Some(x87) = data.get(&x87_key) else {
                continue;
            };
            let x87: [u8; 10] = value_to_hex_bytes("x87 register", x87)?;
            if u64::from_le_bytes(x87[..8].try_into()?) != value_to_u64(mmx)? {
                return Err(format!(
                    "mm{phys} conflicts with {x87_key}: both name physical FPU slot {phys}"
                )
                .into());
            }
        }
    }

    let has_sse = data
        .keys()
        .any(|k| k == "mxcsr" || xmm_index(k).is_some() || ymm_index(k).is_some());
    cpu.avx.set_mxcsr(AvxState::MXCSR_DEFAULT);

    let mut keys = Vec::new();
    if has_sse {
        keys.push("mxcsr".to_string());
    }

    for (key, value) in data {
        let k = key.as_str();
        if k == "scratch_memory" {
            cpu.scratch_memory = value_to_scratch(value)?;
            cpu.scratch_memory_len = SCRATCH_MEMORY_SIZE as u16;
        } else if let Some(i) = x87_logical_index(k) {
            cpu.fpu.set_st(i, value_to_hex_bytes("x87 register", value)?);
        } else if let Some(i) = x87_physical_index(k) {
            cpu.fpu.registers[i] = value_to_hex_bytes("x87 register", value)?;
        } else if let Some(i) = xmm_index(k) {
            cpu.avx.set_xmm(i, &value_to_vector("xmm register", value)?);
        } else if let Some(i) = ymm_index(k) {
            cpu.avx.set_ymm(i, &value_to_vector("ymm register", value)?);
        } else if let Some(i) = zmm_index(k) {
            let mut bytes = [0; 64];
            set_vector(&mut bytes, value_to_u64(value)?);
            cpu.avx.set_zmm(i, &bytes);
        } else if let Some(i) = mm_index(k) {
            cpu.fpu.set_mmx(i, value_to_u64(value)?);
        } else {
            let v = value_to_u64(value)?;
            match k {
                "x87_control" => cpu.fpu.control = v.try_into()?,
                "x87_status" => cpu.fpu.status = v.try_into()?,
                // Redundant with x87_status.TOP; validated above, not recorded.
                "x87_top" => continue,
                "x87_tag" => {
                    if has_logical_x87 {
                        cpu.fpu.set_logical_tag(v.try_into()?);
                    } else {
                        cpu.fpu.tag = v.try_into()?;
                    }
                }
                "x87_opcode" => cpu.fpu.opcode = v.try_into()?,
                "flag" => {
                    cpu.flags = FlagState(v);
                    continue;
                }
                "mxcsr" => {
                    if v > 0xffff {
                        return Err(format!(
                            "mxcsr must fit in 16 bits with no reserved bits set, got {v:#x}"
                        )
                        .into());
                    }
                    cpu.avx.set_mxcsr(v as u32);
                }
                _ => {
                    if !scalar_set(cpu, k, v) {
                        return Err(format!("unknown CPU state key: {k}").into());
                    }
                }
            }
        }
        if !keys.contains(key) {
            keys.push(key.clone());
        }
    }
    Ok(keys)
}

pub fn get_state_value(cpu: &CpuState, key: &str) -> Res<Value> {
    if let Some(i) = x87_logical_index(key) {
        return Ok(json_hex_bytes(cpu.fpu.st(i)));
    }
    if let Some(i) = x87_physical_index(key) {
        return Ok(json_hex_bytes(&cpu.fpu.registers[i]));
    }
    if let Some(i) = xmm_index(key) {
        return Ok(json_hex_bytes(&cpu.avx.get_xmm(i)));
    }
    if let Some(i) = ymm_index(key) {
        return Ok(json_hex_bytes(&cpu.avx.get_ymm(i)));
    }
    if let Some(i) = zmm_index(key) {
        return Ok(json_u64(u64::from_le_bytes(
            cpu.avx.get_zmm(i)[..8].try_into()?,
        )));
    }
    if let Some(i) = mm_index(key) {
        return Ok(json_u64(cpu.fpu.mmx(i)));
    }
    let v = match key {
        "x87_control" => cpu.fpu.control as u64,
        "x87_status" => cpu.fpu.status as u64,
        "x87_top" => cpu.fpu.top() as u64,
        "x87_tag" => cpu.fpu.tag as u64,
        "x87_opcode" => cpu.fpu.opcode as u64,
        "scratch_memory" => return Ok(json_hex_bytes(&cpu.scratch_memory)),
        "mxcsr" => cpu.avx.mxcsr() as u64,
        "flag" => cpu.flags.0,
        _ => scalar_get(cpu, key).ok_or_else(|| format!("unknown CPU state key: {key}"))?,
    };
    Ok(json_u64(v))
}

/// Serializes `keys` (+ `flag`, + `rdx` like the old client) from `cpu`.
/// `extra_keys` (e.g. the keys of the expected state) are read as well; keys
/// the worker cannot read come back as JSON null so they show up as a
/// mismatch instead of being silently dropped.
pub fn serialize_state(cpu: &CpuState, keys: &[String], extra_keys: &[String]) -> DbState {
    let mut all: Vec<&String> = Vec::new();
    for k in keys.iter().chain(extra_keys) {
        if !all.contains(&k) {
            all.push(k);
        }
    }
    let rdx = "rdx".to_string();
    if !all.contains(&&rdx) {
        all.push(&rdx);
    }
    let mut out = Map::new();
    for k in all {
        out.insert(k.clone(), get_state_value(cpu, k).unwrap_or(Value::Null));
    }
    out.insert("flag".into(), json_u64(cpu.flags.0));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn obj(v: Value) -> DbState {
        v.as_object().unwrap().clone()
    }

    fn f80(b: u8) -> Value {
        Value::String(format!("{b:02x}").repeat(10))
    }

    #[test]
    fn gpr_flag_rip_roundtrip() {
        let st = obj(json!({"rax": u64::MAX, "rbx": 7, "r15": 9, "rip": 0x1000, "flag": 0x246, "cs": 0x33, "mem0_value": 5}));
        let mut cpu = CpuState::zero();
        let keys = apply_state(&mut cpu, &st).unwrap();
        assert_eq!(cpu.gpr.rax, u64::MAX);
        assert_eq!(cpu.gpr.r15, 9);
        assert_eq!(cpu.rip, 0x1000);
        assert_eq!(cpu.flags.0, 0x246);
        assert_eq!(cpu.seg.cs, 0x33);
        assert_eq!(cpu.mem0, 5);
        assert!(!keys.contains(&"flag".to_string()));
        let out = serialize_state(&cpu, &keys, &[]);
        assert_eq!(out["rax"], json!(u64::MAX));
        assert_eq!(out["flag"], json!(0x246));
        assert_eq!(out["rdx"], json!(0));
        assert_eq!(out["mem0_value"], json!(5));
    }

    #[test]
    fn physical_x87_roundtrip() {
        let st = obj(json!({"x87_status": 3 << 11, "x87_top": 3, "x87_tag": 0x24, "x87_r0": f80(0x10), "x87_r3": f80(0x33)}));
        let mut cpu = CpuState::zero();
        let keys = apply_state(&mut cpu, &st).unwrap();
        assert_eq!(cpu.fpu.registers[0], [0x10; 10]);
        assert_eq!(cpu.fpu.registers[3], [0x33; 10]);
        assert_eq!(cpu.fpu.top(), 3);
        assert_eq!(cpu.fpu.tag, 0x24);
        assert!(!keys.contains(&"x87_top".to_string()));
        let out = serialize_state(&cpu, &keys, &[]);
        assert_eq!(out["x87_r3"], f80(0x33));
        assert!(out.get("x87_top").is_none());
        assert_eq!(out["x87_status"], json!(3 << 11));
    }

    #[test]
    fn logical_x87_follows_top() {
        let st = obj(json!({"x87_status": 3 << 11, "x87_st0": f80(0x11)}));
        let mut cpu = CpuState::zero();
        apply_state(&mut cpu, &st).unwrap();
        assert_eq!(cpu.fpu.registers[3], [0x11; 10]);
        assert_eq!(get_state_value(&cpu, "x87_st0").unwrap(), f80(0x11));
    }

    #[test]
    fn x87_mmx_conflict_and_mix_rejected() {
        let st = obj(json!({"x87_r2": f80(0x11), "mm2": 0x2222}));
        let e = apply_state(&mut CpuState::zero(), &st).unwrap_err();
        assert!(e.to_string().contains("mm2 conflicts with x87_r2"));
        let st = obj(json!({"x87_r2": f80(1), "x87_st1": f80(1)}));
        assert!(apply_state(&mut CpuState::zero(), &st).is_err());
    }

    #[test]
    fn mmx_only_initializes_mmx() {
        let st = obj(json!({"mm3": 0x1122334455667788u64}));
        let mut cpu = CpuState::zero();
        apply_state(&mut cpu, &st).unwrap();
        assert_eq!(cpu.fpu.tag, 0xff);
        assert_eq!(get_state_value(&cpu, "mm3").unwrap(), json!(0x1122334455667788u64));
    }

    #[test]
    fn xmm_ymm_roundtrip() {
        let hex = "000102030405060708090a0b0c0d0e0f";
        let yhex: String = (0..32).map(|i| format!("{i:02x}")).collect();
        let st = obj(json!({"xmm3": hex, "xmm4": 0x1122334455667788u64, "ymm5": yhex}));
        let mut cpu = CpuState::zero();
        let keys = apply_state(&mut cpu, &st).unwrap();
        assert!(keys.contains(&"mxcsr".to_string()));
        let out = serialize_state(&cpu, &keys, &[]);
        assert_eq!(out["xmm3"], json!(hex));
        assert_eq!(out["xmm4"], json!("8877665544332211".to_string() + &"0".repeat(16)));
        assert_eq!(out["ymm5"], json!(yhex));
        assert_eq!(out["mxcsr"], json!(0x1f80));
    }

    #[test]
    fn malformed_values_rejected() {
        for (k, v) in [("xmm0", json!("00ff")), ("ymm0", json!("00".repeat(16))), ("xmm1", json!(true)), ("x87_r0", json!(5)), ("mxcsr", json!(0x10000)), ("bogus", json!(1))] {
            let st = obj(json!({ k: v }));
            assert!(apply_state(&mut CpuState::zero(), &st).is_err(), "{k}");
        }
    }

    #[test]
    fn scratch_memory_roundtrip_and_exclusive() {
        let bytes: String = (0..SCRATCH_MEMORY_SIZE).map(|i| format!("{:02x}", i as u8)).collect();
        let st = obj(json!({"scratch_memory": bytes}));
        let mut cpu = CpuState::zero();
        let keys = apply_state(&mut cpu, &st).unwrap();
        assert_eq!(cpu.scratch_memory_len as usize, SCRATCH_MEMORY_SIZE);
        assert_eq!(serialize_state(&cpu, &keys, &[])["scratch_memory"], json!(bytes));
        let st = obj(json!({"scratch_memory": bytes, "mem0_value": 0}));
        assert!(apply_state(&mut CpuState::zero(), &st).is_err());
    }

    #[test]
    fn extra_keys_and_unknown_become_null() {
        let cpu = CpuState::zero();
        let out = serialize_state(&cpu, &[], &["rbx".into(), "weird".into()]);
        assert_eq!(out["rbx"], json!(0));
        assert_eq!(out["weird"], Value::Null);
    }

    #[test]
    fn diff_against_initial_reconstructs_final() {
        // Kernel returns initial ^ final; the worker XORs the initial back in.
        let st = obj(json!({"rax": 0xff00, "flag": 2}));
        let mut init = CpuState::zero();
        apply_state(&mut init, &st).unwrap();
        let mut fin = init.clone();
        fin.gpr.rax = 0x1234;
        fin.flags = FlagState(0x246);
        let xor = init.diff(&fin);
        assert_eq!(xor.gpr.rax, 0xff00 ^ 0x1234);
        let rebuilt = init.diff(&xor);
        assert_eq!(rebuilt.gpr.rax, 0x1234);
        assert_eq!(rebuilt.flags.0, 0x246);
    }
}
