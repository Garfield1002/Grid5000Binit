# g5k-worker

Host-side worker for Grid5000Binit. It spawns QEMU/KVM with the Aegis kernel
(`aegis` branch `g5k`), pulls test cases from the controller, runs them through the ivshmem
mailbox, compares the captured state with the expected one and reports `ok`
ids and mismatches back. It path-depends on `../aegis/libaegis` (feature `std`).

## Build

```bash
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl   # static binary
cargo test                                                  # unit tests (host target)
```

`ring` (rustls) needs a C compiler for the musl target. `.cargo/config.toml`
points `CC_x86_64_unknown_linux_musl` at `clang`; override it (e.g. `musl-gcc`)
if clang is unavailable. TLS roots are bundled (webpki-roots), and
`https_proxy`/`http_proxy` are honored.

## Run

```bash
CONTROLLER_URL=http://host:8080 CONTROLLER_TOKEN=... \
  g5k-worker --bootimage bootimage-aegis.bin [--once]
```

| Option | Default | |
|---|---|---|
| `--bootimage` | required | raw boot image of the kernel |
| `--qemu` | `qemu-system-x86_64` | |
| `--emulated` | off | run the guest under QEMU's emulator (`-accel tcg -cpu max`) instead of KVM, see below |
| `--batch-size` | 1000 | cases per `GET /batch` |
| `--timeout-ms` | 10000 | per-case ack timeout (watchdog) |
| `--once` | off | exit when the controller has no more cases; otherwise poll every 60 s |
| `--shm-dir` | `/dev/shm` | where the shm file and serial socket live |

Env: `CONTROLLER_URL`, `CONTROLLER_TOKEN` (also `--controller-url/-token`).

## Behaviour

- Creates a zeroed 16 MiB shm file and listens on a unix socket (serial), then
  spawns QEMU with the SPEC section 1 arguments (plain `-cpu host`). The
  serial option is `reconnect=1`, or `reconnect-ms=1000` on QEMU >= 9.2
  (auto-detected). It waits for the mailbox magic and checks the version.
- The kernel still blocks on a serial `HELLO` after publishing the mailbox,
  so the worker resends `HELLO` until the guest answers `HELLO`. Serial output
  is forwarded to the log as `[guest] ...`.
- Registers (host, cluster = hostname prefix before `-`, cpu model, microcode,
  CPUID and kernel feature names, `save_mode`, worker version), then loops
  `GET /batch` -> run -> compare -> `POST /results`. Heartbeats go out every
  30 s from a thread. HTTP is retried with exponential backoff (1 s to 60 s)
  on network errors, 5xx, 408 and 429; other 4xx are fatal.
- With `--emulated` the run measures QEMU and not the machine, so it registers as host
  `tcg-<hostname>`, cluster `qemu-tcg`, CPU model `QEMU <version> TCG (max)`, no microcode and no
  host CPUID features (the guest's own feature list is used). The controller therefore keeps it
  apart from the hardware runs, and emulated runs of one QEMU version continue one another.
- Per case the request is `[32-byte required-feature mask][TestCase]`; the
  result is an XOR diff against the initial state, which the worker XORs back.
  JSON state conversion is adapted from the old `aegis/client`.
- Comparison is exact on every key and on the exception kind (the
  `ExceptionVector` display string). Flags are never masked. In `fxsave` mode
  the kernel cannot capture YMM-high/ZMM/opmask, so `ymmN`, `zmmN`, `kN`,
  `opmask*` keys are excluded from the comparison; the payload always carries
  `"save_mode"` (`fxsave` or `xsave`) and the controller applies the same
  exclusion when classifying.
- Statuses: `crash` (ack timeout or QEMU exit; QEMU is killed and restarted,
  `qemu_restarts` counted in heartbeats), `skipped` (unknown/missing required
  feature, kernel status 2, malformed case), `mismatch`.
- Exit code 0 only with `--once` when the controller has no more cases (without `--once` it polls forever). Exit code is 1 on fatal errors (QEMU cannot be started 5 times in a row,
  4xx from the controller); wrap in a loop (`node/run.sh`).
