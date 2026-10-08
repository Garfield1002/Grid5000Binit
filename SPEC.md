# Grid5000Binit — design spec (shared contract)

Goal: run Aegis hardware capture on as many distinct Grid5000 CPU models as possible (one or a
few nodes per cluster, clusters being homogeneous); test cases streamed from a
CONTROLLER; nodes report per-case `ok` or the observed diff. Differences in
*undefined* flags are a primary research target — never mask anything.

## Components

| Part | Location |
|---|---|
| Aegis kernel changes | `~/dev/Grid5000Binit/aegis` (clone of ~/dev/binary/binit/aegis master), branch `g5k` |
| Worker (new host client, Rust, static musl) | `~/dev/Grid5000Binit/worker/` (path-depends on `../aegis/libaegis`, branch `g5k`) |
| Controller (Python, uv, FastAPI, psycopg) | `~/dev/Grid5000Binit/controller/` |
| Node + submission scripts, docs | `~/dev/Grid5000Binit/{node,scripts}/`, `README.md` |

## 1. Kernel <-> worker (ivshmem mailbox)

- The worker spawns QEMU itself:
  `qemu-system-x86_64 -enable-kvm -cpu host -drive format=raw,file=<bootimage> -serial unix:<sock>,<reconnect> -device isa-debug-exit,iobase=0xf4,iosize=0x04 -object memory-backend-file,id=shm,mem-path=<shm>,size=16M,share=on -device ivshmem-plain,memdev=shm -display none -no-reboot`
  (No forced `+avx512*` flags. Plain `-cpu host`. `<reconnect>` is `reconnect-ms=1000` on QEMU >= 9.2, `reconnect=1` before; the worker auto-detects.)
- The worker creates the shm file and listens on the unix socket as **server** before spawning QEMU.
- Serial carries logs and the `HELLO` line only. The kernel blocks on a `HELLO` line after publishing the mailbox; the worker resends `HELLO` until the guest answers `HELLO`, which completes boot.
- The mailbox also carries a `save_mode` word (`fxsave` or `xsave`). In `fxsave` mode the kernel cannot capture YMM-high/ZMM/opmask, so `ymmN`, `zmmN`, `kN`, `opmask*` keys are excluded from comparison (worker and controller classifier); no other key is ever masked.
- Sync goes through a `#[repr(C)]` header at shm offset 0, defined in `libaegis/src/protocol.rs` as `Mailbox`:
  ```
  magic: u64          // 0x4145_4749_5347_354B ("AEGISG5K"), written by kernel at boot
  version: u32        // 1
  _pad: u32
  features: [u64; 4]  // CPUID feature bitmask, see FeatureBit enum in libaegis (written by kernel at boot)
  req_seq: u64        // host increments after writing a request
  ack_seq: u64        // kernel sets = req_seq after writing the result
  status: u32         // 0 = ok, 1 = exception, 2 = skipped_missing_feature
  exception: u32      // exception vector if status==1
  ```
  Then the existing request/result payload area follows (testcase in, final state/diff out), at a fixed offset of 4096.
- Use volatile/atomic accesses and fences on both sides. The kernel polls `req_seq`. The host polls `ack_seq` with a timeout (default 10s). On timeout the host kills and restarts QEMU and reports the case as `crash`.
- `FeatureBit` covers at least: SSE, SSE2, SSE3, SSSE3, SSE4_1, SSE4_2, POPCNT, LZCNT, BMI1, BMI2, ADX, AVX, AVX2, FMA, F16C, AVX512F, AVX512BW, AVX512DQ, AVX512VL, AVX512CD, AVX512VBMI, AVX512VNNI, AES, PCLMULQDQ, SHA, MOVBE, XSAVE, OSXSAVE, RDRAND, RDSEED, CMPXCHG16B, X87. The names match the iced-x86 `CpuidFeature` names where possible.
- The kernel must not assert features. It sets XCR0 from the features that are present.
- `#UD` on a case whose required feature is absent → `status=2`. The worker passes the required features along with each case; the kernel or the worker may decide.

## 2. Worker <-> controller (HTTP, JSON)

Every request carries `Authorization: Bearer $CONTROLLER_TOKEN`. Base URL is `$CONTROLLER_URL`. Honor `https_proxy`/`http_proxy`.

- `POST /register` body `{host, cluster, cpu_model, microcode, cpuid_features:[names], kernel_features:[names], save_mode, worker_version}` → `{node_id}`
- `GET /batch?node_id=..&size=1000` → `{batch_id, cases:[{test_case_id, state_index, instruction, opcode_hex, required_features:[names], initial_state:{...}, expected:{final_state|null, exception_kind|null}}]}`. An empty `cases` means done.
  The controller serves each node every eligible case (required ⊆ node features), resuming from the node's cursor. At `/register` a node takes over the cursor of the most advanced node with the same cpu_model, microcode, features and save_mode (a resubmitted best-effort job lands on any host of the cluster).
- `POST /results` `{node_id, batch_id, save_mode, ok_count, ok_ids:[[tc,si],...], mismatches:[{test_case_id, state_index, got_final_state|null, got_exception_kind|null, status:"mismatch"|"crash"|"skipped"}], elapsed_s}`. The controller advances the cursor and classifies each mismatch.
- `POST /heartbeat` `{node_id, batch_id, done_in_batch, qemu_restarts}`, sent about every 30s.
- A batch may be a replay batch: inputs queued for the node's cluster (section 3), served ahead of the cursor and possibly several times in a row. The worker runs and reports it like any other batch; the controller keeps its results apart and moves neither the cursor nor the counters.

State/diff JSON uses the same flat keys as x86db (`rax`, `flag`, `x87_r0`, `mm0`, `xmm0`, `mem0_value`, ...). Comparison is exact over every key.

## 3. Controller

- Reads `test_cases` and `test_results` from the local x86db (`X86DB_DSN`). It never writes to them.
- New tables: `g5k_nodes`, `g5k_results`, `g5k_events`, `g5k_batches`, `g5k_replays`, `instruction_features(instruction_id, feature)` (filled by a CLI command using iced-x86 Python on the opcode).
- Mismatch classes: `undef_flags_only` (only bits in `instruction_undefined_flags` differ), `defined_state`, `exception_mismatch`, `crash`.
- Replay: `controller replay add` queues an input (test case and state) for a cluster in `g5k_replays`; any node of the cluster runs it. The outcome is only recorded there. `controller replay report` compares it, exactly (status, exception kind, every captured key), with the rows the cluster's nodes hold in `g5k_results` and proposes `delete` (now ok), `overwrite` (another mismatch) or `insert` (was ok); nothing is proposed when repeated runs disagree. `controller replay apply` carries one kind of action out on every row of the cluster for the inputs concerned.
- Monitoring:
  - structured logs to stdout and a rotating JSONL file
  - `GET /status`: a self-refreshing HTML dashboard: objective coverage (one finished node per CPU model of `controller/controller/targets.csv`, grouped by microarchitecture), per-run progress, rate, ETA, mismatch classes, last seen and silent-node warnings; recent events
  - `GET /status.json`
  - `GET /mismatches?class=&host=&insn=`
- Config comes from env: `X86DB_DSN`, `CONTROLLER_TOKEN`, `LISTEN` (default 0.0.0.0:8080), `LOG_DIR`, `TARGETS_FILE` (default: the packaged `targets.csv`).

## 4. Grid5000

- `scripts/build.sh` (local) builds the bootimage on aegis branch `g5k` and the static musl worker, then rsyncs `dist/` to `<site>.g5k:~/g5kbinit/`.
- `node/run.sh` (on the node) runs `sudo-g5k`, installs qemu-system-x86 with apt if missing, makes `/dev/kvm` accessible, and loops `g5k-worker --once`: exit 0 = controller has no more cases (stop), non-zero = restart with backoff.
- `scripts/submit-besteffort.sh <cluster...>` and `scripts/submit-reserve.sh <cluster...> [walltime]` share `scripts/common.sh`. They `oarsub` one unpinned job per cluster (`-l host=1 -p "cluster='X'"`); `-t`/`-q` pass extra OAR job types and the queue (exotic, production).
