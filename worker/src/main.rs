#[macro_use]
mod log;
mod api;
mod compare;
mod convert;
mod sysinfo;
mod vm;

use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use clap::Parser;
use libaegis::{
    cpu::CpuState,
    protocol::{FeatureMask, STATUS_SKIPPED_MISSING_FEATURE, mask_is_subset, names},
    testcase::{TestCase, TestOutcome},
};
use serde_json::{Value, json};

use api::{Api, Case};
use compare::{Got, SaveMode, matches};
use vm::{RunError, Vm, VmConfig};

const WORKER_VERSION: &str = concat!(env!("CARGO_PKG_NAME"), " ", env!("CARGO_PKG_VERSION"));
const HEARTBEAT_EVERY: Duration = Duration::from_secs(30);
const IDLE_POLL: Duration = Duration::from_secs(60);
const START_ATTEMPTS: u32 = 5;

#[derive(Parser, Debug)]
#[command(version, about = "Aegis host worker: runs controller test cases in QEMU/KVM and reports diffs")]
struct Args {
    /// Controller base URL
    #[arg(long, env = "CONTROLLER_URL")]
    controller_url: String,

    /// Bearer token
    #[arg(long, env = "CONTROLLER_TOKEN", hide_env_values = true)]
    controller_token: String,

    /// Aegis kernel boot image (raw disk image)
    #[arg(long)]
    bootimage: PathBuf,

    /// QEMU binary
    #[arg(long, default_value = "qemu-system-x86_64")]
    qemu: String,

    /// Run the guest under QEMU's emulator (TCG, `-cpu max`) instead of KVM. The run is
    /// registered as that QEMU version, not as this host's CPU.
    #[arg(long)]
    emulated: bool,

    /// Cases requested per batch
    #[arg(long, default_value_t = 1000)]
    batch_size: usize,

    /// Per-case acknowledgement timeout in milliseconds
    #[arg(long, default_value_t = 10_000)]
    timeout_ms: u64,

    /// Exit when the controller has no more cases (default: keep polling)
    #[arg(long)]
    once: bool,

    /// Directory for the shared-memory file and serial socket
    /// (default: /dev/shm, else the temp dir)
    #[arg(long)]
    shm_dir: Option<PathBuf>,
}

#[derive(Default)]
struct Progress {
    batch_id: Option<i64>,
    done: u64,
    restarts: u64,
}

struct Runner {
    cfg: VmConfig,
    vm: Option<Vm>,
    instance: u64,
    next_id: usize,
    timeout: Duration,
    mode: SaveMode,
    features: FeatureMask,
    progress: Arc<Mutex<Progress>>,
}

/// Outcome of one case.
enum Outcome {
    Ok,
    Mismatch(Value),
}

fn mismatch(c: &Case, status: &str, state: Option<Value>, exc: Option<String>) -> Outcome {
    Outcome::Mismatch(json!({
        "test_case_id": c.test_case_id,
        "state_index": c.state_index,
        "got_final_state": state,
        "got_exception_kind": exc,
        "status": status,
    }))
}

fn start_vm(cfg: &VmConfig, instance: &mut u64) -> Result<Vm> {
    let mut last = None;
    for attempt in 0..START_ATTEMPTS {
        *instance += 1;
        match Vm::start(cfg, *instance) {
            Ok(vm) => return Ok(vm),
            Err(e) => {
                log!("QEMU start failed (attempt {}/{START_ATTEMPTS}): {e:#}", attempt + 1);
                last = Some(e);
                thread::sleep(Duration::from_secs(1 << attempt));
            }
        }
    }
    Err(last.unwrap()).context("giving up starting QEMU")
}

impl Runner {
    fn restart(&mut self) -> Result<()> {
        self.vm = None; // kills QEMU, removes files
        self.progress.lock().unwrap().restarts += 1;
        let vm = start_vm(&self.cfg, &mut self.instance)?;
        if vm.features != self.features || SaveMode::from_raw(vm.save_mode_raw) != Some(self.mode) {
            log!("warning: kernel features/save mode changed across restart");
        }
        self.vm = Some(vm);
        self.next_id = 1;
        Ok(())
    }

    fn run_case(&mut self, c: &Case) -> Result<Outcome> {
        let skip = |why: String| {
            log!("skip {}/{}: {why}", c.test_case_id, c.state_index);
            mismatch(c, "skipped", None, None)
        };
        let mask = match names::mask_from_names(c.required_features.iter().map(String::as_str)) {
            Ok(m) => m,
            Err(unknown) => return Ok(skip(format!("unknown feature {unknown}"))),
        };
        let vm = self.vm.as_mut().expect("vm running");
        if !mask_is_subset(&mask, &vm.features) {
            return Ok(skip("required features not available".into()));
        }
        let mut insn = [0u8; 15];
        let enc = match decode_hex(&c.opcode_hex) {
            Some(b) if !b.is_empty() && b.len() <= 15 => b,
            _ => return Ok(skip(format!("bad opcode {:?}", c.opcode_hex))),
        };
        insn[..enc.len()].copy_from_slice(&enc);
        let mut state = CpuState::zero();
        let keys = match convert::apply_state(&mut state, &c.initial_state) {
            Ok(k) => k,
            Err(e) => return Ok(skip(format!("bad initial state: {e}"))),
        };
        let id = self.next_id;
        self.next_id += 1;
        let tc = TestCase { id, state, insn, size: enc.len() as u8 };

        let crash = |this: &mut Runner, why: String| -> Result<Outcome> {
            log!("crash on {}/{} ({}): {why}", c.test_case_id, c.state_index, c.instruction.as_deref().unwrap_or("?"));
            this.restart()?;
            Ok(mismatch(c, "crash", None, None))
        };

        let reply = match vm.run_case(&mask, &tc, self.timeout) {
            Ok(r) => r,
            Err(RunError::Encode) => return Ok(skip("case does not fit the request area".into())),
            Err(RunError::Timeout) => return crash(self, format!("no ack within {:?}", self.timeout)),
            Err(RunError::QemuExited(s)) => return crash(self, format!("QEMU exited: {s}")),
        };
        if reply.status == STATUS_SKIPPED_MISSING_FEATURE {
            return Ok(skip("kernel reports missing feature".into()));
        }
        let result = match reply.result {
            Some(r) if r.id == id => r,
            Some(r) => return crash(self, format!("result id {} != request id {id}", r.id)),
            None => return crash(self, format!("undecodable result (status {})", reply.status)),
        };
        let expected_keys: Vec<String> = c
            .expected
            .final_state
            .as_ref()
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default();
        let got = match &result.outcome {
            TestOutcome::Completed(diff) => {
                let fin = tc.state.diff(diff);
                Got::State(convert::serialize_state(&fin, &keys, &expected_keys))
            }
            TestOutcome::Exception(info) => Got::Exception(info.kind.to_string()),
        };
        if matches(&c.expected, &got, self.mode) {
            Ok(Outcome::Ok)
        } else {
            Ok(match got {
                Got::State(s) => mismatch(c, "mismatch", Some(Value::Object(s)), None),
                Got::Exception(k) => mismatch(c, "mismatch", None, Some(k)),
            })
        }
    }
}

fn decode_hex(s: &str) -> Option<Vec<u8>> {
    let s = s.strip_prefix("0x").unwrap_or(s);
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len() / 2).map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).ok()).collect()
}

fn main() {
    if let Err(e) = run(Args::parse()) {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

fn run(args: Args) -> Result<()> {
    if !args.bootimage.is_file() {
        bail!("bootimage {} not found", args.bootimage.display());
    }
    let cfg = VmConfig {
        reconnect_opt: vm::detect_reconnect_opt(&args.qemu),
        qemu: args.qemu.clone(),
        emulated: args.emulated,
        bootimage: args.bootimage.canonicalize()?,
        boot_timeout: Duration::from_millis(args.timeout_ms.max(10_000) * 3),
        shm_dir: args.shm_dir.clone().unwrap_or_else(vm::default_shm_dir),
    };
    let mut instance = 0;
    log!("starting QEMU ({})", cfg.bootimage.display());
    let vm = start_vm(&cfg, &mut instance)?;
    let features = vm.features;
    let mode = SaveMode::from_raw(vm.save_mode_raw)
        .with_context(|| format!("unknown save_mode {}", vm.save_mode_raw))?;
    let kernel_features: Vec<&str> = names::mask_to_names(&features);
    log!("guest ready: save_mode={} features={}", mode.as_str(), kernel_features.join(","));

    let api = Api::new(&args.controller_url, &args.controller_token);
    // An emulated run measures QEMU, not this machine: it gets its own host name, cluster and
    // CPU model so that the controller never continues or counts it as a hardware run.
    let (host, cluster, cpu_model, microcode, cpuid_features) = if args.emulated {
        let version = vm::qemu_version(&args.qemu).unwrap_or_else(|| "unknown".into());
        (
            format!("tcg-{}", sysinfo::hostname()),
            "qemu-tcg".to_string(),
            Some(format!("QEMU {version} TCG (max)")),
            None,
            Vec::new(),
        )
    } else {
        let host = sysinfo::hostname();
        let (cpu_model, microcode) = sysinfo::cpuinfo();
        let cluster = sysinfo::cluster_of(&host);
        (host, cluster, cpu_model, microcode, names::mask_to_names(&sysinfo::cpuid_mask()))
    };
    let node_id = api.register(&json!({
        "host": host,
        "cluster": cluster,
        "cpu_model": cpu_model,
        "microcode": microcode,
        "cpuid_features": cpuid_features,
        "kernel_features": kernel_features,
        "save_mode": mode.as_str(),
        "worker_version": WORKER_VERSION,
    }))?;
    log!("registered as node {node_id} ({host})");

    let progress = Arc::new(Mutex::new(Progress::default()));
    {
        let (api, progress) = (api.clone(), progress.clone());
        thread::spawn(move || {
            loop {
                thread::sleep(HEARTBEAT_EVERY);
                let (b, d, r) = {
                    let p = progress.lock().unwrap();
                    (p.batch_id, p.done, p.restarts)
                };
                if let Err(e) = api.heartbeat(node_id, b, d, r) {
                    log!("heartbeat failed: {e}");
                }
            }
        });
    }

    let mut runner = Runner {
        cfg,
        vm: Some(vm),
        instance,
        next_id: 1,
        timeout: Duration::from_millis(args.timeout_ms),
        mode,
        features,
        progress: progress.clone(),
    };

    loop {
        let batch = api.batch(node_id, args.batch_size)?;
        let Some(batch_id) = batch.batch_id.filter(|_| !batch.cases.is_empty()) else {
            if args.once {
                log!("no more cases; exiting (--once)");
                return Ok(());
            }
            log!("no cases available; polling again in {IDLE_POLL:?}");
            thread::sleep(IDLE_POLL);
            continue;
        };
        {
            let mut p = progress.lock().unwrap();
            p.batch_id = Some(batch_id);
            p.done = 0;
        }
        let started = Instant::now();
        let mut ok_ids: Vec<[i64; 2]> = Vec::new();
        let mut mismatches: Vec<Value> = Vec::new();
        for c in &batch.cases {
            match runner.run_case(c)? {
                Outcome::Ok => ok_ids.push([c.test_case_id, c.state_index]),
                Outcome::Mismatch(m) => mismatches.push(m),
            }
            progress.lock().unwrap().done += 1;
        }
        let elapsed = started.elapsed().as_secs_f64();
        log!(
            "batch {batch_id}: {} ok, {} mismatches in {elapsed:.1}s",
            ok_ids.len(),
            mismatches.len()
        );
        api.results(&json!({
            "node_id": node_id,
            "batch_id": batch_id,
            "ok_count": ok_ids.len(),
            "ok_ids": ok_ids,
            "mismatches": mismatches,
            "elapsed_s": elapsed,
            "save_mode": mode.as_str(),
        }))?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_decode() {
        assert_eq!(decode_hex("4801d8"), Some(vec![0x48, 0x01, 0xd8]));
        assert_eq!(decode_hex("0xff"), Some(vec![0xff]));
        assert_eq!(decode_hex("abc"), None);
        assert_eq!(decode_hex("zz"), None);
    }

    #[test]
    fn batch_json_parses() {
        let v = json!({"batch_id": 3, "cases": [{
            "test_case_id": 1, "state_index": 0, "instruction": "add rax, rbx",
            "opcode_hex": "4801d8", "required_features": ["X87"],
            "initial_state": {"rax": 1, "flag": 2},
            "expected": {"final_state": {"rax": 1, "flag": 2}, "exception_kind": null}}]});
        let b: api::Batch = serde_json::from_value(v).unwrap();
        assert_eq!(b.batch_id, Some(3));
        assert_eq!(b.cases[0].expected.final_state.as_ref().unwrap()["rax"], json!(1));
        let empty: api::Batch = serde_json::from_value(json!({"batch_id": null, "cases": []})).unwrap();
        assert!(empty.cases.is_empty());
    }
}
