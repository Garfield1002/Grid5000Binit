# Grid5000Binit

Run Aegis hardware capture (a tiny x86_64 kernel booted in QEMU/KVM) on as many distinct CPU
models as Grid5000 (G5K) offers, to measure how instructions behave across microarchitectures.
Clusters are homogeneous, so the unit of coverage is the cluster (one node each by default), not
the node. A central **controller** streams x86db test cases; each node's **worker** executes them
inside the guest and reports `ok` or the observed state diff. Differences in *undefined* flags are
a primary research target, so nothing is masked. Full contract: [SPEC.md](SPEC.md).

## Architecture

```
 x86db (Postgres) <-- controller (FastAPI) <== HTTP/JSON, bearer token ==> worker (one per G5K node)
                      controller/                                           worker/ (static musl)
                                                                              |  spawns
                                                                       qemu-system-x86_64 -enable-kvm -cpu host
                                                                              |  ivshmem mailbox (16M) + serial
                                                                       aegis kernel (bootimage, branch g5k)
```

- `aegis/` kernel (branch `g5k`), built into a bootimage. Worker and kernel sync through an ivshmem
  mailbox (SPEC section 1); a case that times out kills/restarts QEMU and is reported as `crash`.
- `controller/` serves each node every case whose required CPU features are a subset of the node's
  features, resuming from a per-node cursor (SPEC sections 2-3). See `controller/README.md`.
- `scripts/` (local build + submission on a frontend), `node/run.sh` (runs on each node).

## Prerequisites

- Local: Rust nightly (pinned by `aegis/rust-toolchain.toml`) with `cargo install bootimage`,
  `rustup target add x86_64-unknown-linux-musl`, a musl-capable linker (usually fine with
  pure-Rust deps; otherwise install `musl-tools`), `rsync`, `ssh`.
- A Grid5000 account with ssh access (`~/.ssh/config` with `Host *.g5k` ProxyJump through
  `access.grid5000.fr`, see the G5K Getting Started page).
- A running controller reachable from the nodes, backed by a populated x86db Postgres.
- `uv` for the controller.

## Controller hosting

G5K nodes have no inbound access from the internet, and outbound internet only goes through the site
HTTP proxy (`http://proxy:3128`; `node/run.sh` exports `http(s)_proxy` for you, override with
`HTTP_PROXY_URL`). The controller must be reachable from the nodes:

1. **Public VPS (recommended).** Run the controller + Postgres (or tunnel the DB) there, put TLS
   (Caddy/nginx) in front, set `CONTROLLER_URL=https://host`. Nodes reach it via the G5K proxy.
   Use a long random `CONTROLLER_TOKEN`; it is the only protection.
2. **Reverse SSH tunnel from your machine.** The controller stays local:
   `ssh -R 0.0.0.0:8080:localhost:8080 nancy.g5k` (needs `GatewayPorts clientspecified` on the
   frontend sshd, which may not be allowed; test it) then
   `CONTROLLER_URL=http://frontend.nancy.grid5000.fr:8080`. `*.grid5000.fr` is in `no_proxy` so
   nodes connect directly. The tunnel must stay up (use `autossh`); one tunnel per site. Frontends
   are shared, so this is a fallback only.

## First run

```sh
# 1. Controller (see controller/README.md)
cd controller
export X86DB_DSN=postgresql://... CONTROLLER_TOKEN=$(openssl rand -hex 24)
uv run controller compute-features && uv run controller      # listens on :8080

# 2. Build and ship (local machine)
scripts/build.sh --site nancy       # dist/{g5k-worker,aegis-bootimage.bin,node/} -> nancy.g5k:~/g5kbinit/

# 3. On the frontend
ssh nancy.g5k
export CONTROLLER_URL=https://your.host CONTROLLER_TOKEN=...
cd ~/g5kbinit/node
./submit-besteffort.sh gros grouille   # one node per cluster; or: ./submit-reserve.sh -w 4:00:00 gros
oarstat -u
```

`build.sh` options: `--site SITE`, `--skip-kernel`, `--skip-worker`. It tries ssh alias
`<site>.g5k`, falling back to `$G5K_USER@access.grid5000.fr` as ProxyJump (`G5K_USER` defaults to
`$USER`). Homes are NFS-shared per site, so one rsync serves every node of that site.

Smoke-test one node first: `oarsub -I -l host=1 -p "cluster='gros'"` then `bash ~/g5kbinit/node/run.sh`
(with `CONTROLLER_URL/TOKEN` exported).

### What the submit scripts do

Run on a frontend. By default they submit **one job per cluster**, not pinned to a host
(`-p "cluster='X'" -l host=1,walltime=...`), since the target is distinct CPU models and a cluster
is homogeneous: OAR runs it on any free host of the cluster. A run belongs to a hardware spec, not
to a host: a node that registers continues from the most advanced node with the same CPU model,
microcode, feature set and save mode (`resumed_from` in the `register` event; the previous host
shows as `moved`). So a best-effort job killed on `gros-12` and resubmitted on `gros-87` carries on
where it stopped. `node/run.sh` also exits 99 five minutes before the walltime, which makes OAR
resubmit an idempotent job that would otherwise just end.

There is no option to run several nodes of a cluster: hosts with the same spec share one run, so
they would duplicate work instead of splitting it. To check a result on a given host, submit by
hand with `-p "host='<fqdn>'"`.

- `submit-besteffort.sh [opts] <cluster...>`: `-t besteffort -t idempotent`, default 24h. Jobs
  get killed when someone reserves the node and OAR resubmits them; the worker resumes from the
  controller cursor.
- `submit-reserve.sh [opts] <cluster...>`: normal jobs, default 2:00:00.
- Options: `-w walltime`, `-t type` (repeatable; exotic clusters need `-t exotic`),
  `-q queue` (production clusters need `-q production` and the matching access rights).
- `CONTROLLER_URL/TOKEN` (and optional `BATCH_SIZE`, `TIMEOUT_MS`) are written to `~/g5kbinit/env`
  (mode 600) and sourced by `node/run.sh`, so the token does not show in `oarstat`.

`node/run.sh` on each node: `sudo-g5k` (root, node is reinstalled afterwards), installs
`qemu-system-x86` if absent, `chmod 666 /dev/kvm`, exports the proxy, runs `g5k-worker --once` in a restart
loop with backoff until it exits 0 (controller has no more cases); any non-zero exit restarts it. Log: `~/g5kbinit/logs/<host>.log`;
OAR stdout/stderr in `~/g5kbinit/logs/oar.*`.

## Monitoring

- `scripts/status.sh` (needs `CONTROLLER_URL/TOKEN`): per-node progress, rate, ETA, mismatch classes,
  per-CPU-model aggregate, recent events. `--raw` prints the JSON.
- Browser: `$CONTROLLER_URL/status?token=...` (auto-refresh), `/mismatches?class=&host=&insn=`.
- Silent nodes (no heartbeat for `SILENT_AFTER_S`, default 120 s) are flagged; events `node_silent` /
  `node_recovered` are logged. On the frontend: `oarstat -u`, `tail -f ~/g5kbinit/logs/*.log`.

## Interpreting results

Mismatch classes (assigned by the controller):

| class | meaning |
|---|---|
| `undef_flags_only` | only `flag` differs, and only in bits the instruction documents as undefined. Expected, and the research target: compare values across CPU models/microcodes. |
| `defined_state` | any other state difference (register, defined flag, memory, x87/SIMD). A real bug candidate, or kernel/capture issue. |
| `exception_mismatch` | exception kind differs from expected (e.g. unexpected `#UD`, missing `#GP`). Often a missing-feature or privilege-level issue. |
| `crash` | guest hang/triple fault/timeout; QEMU was restarted. Persistent crashes on one case are interesting; many across a node point to the node. |
| `skipped` | worker status 2: required feature absent in the guest (`skipped_missing_feature`). |

`save_mode` (`fxsave` or `xsave`, reported at `/register` and `/results`, shown per node in `/status`):
how the kernel captures SIMD state. In `fxsave` mode YMM-high/ZMM/opmask cannot be captured, so the `ymmN`,
`zmmN`, `kN`, `opmask*` keys are excluded from comparison (worker and controller classifier); nothing else is
masked. Treat `fxsave` and `xsave` nodes as not directly comparable for those keys. The controller stores only
mismatches (full got/expected states); OK cases are only counted.

Comparing the same case across `cpu_model` groups in `/status.json` shows which undefined-flag
behaviour differs between microarchitectures.

## Troubleshooting

- `/dev/kvm missing`: the node or cluster has virtualization disabled/unavailable; skip that cluster.
- `sudo-g5k` fails: only works in the standard environment (not kadeploy/custom images) and on whole-node
  jobs (`host=1`, as submitted here).
- apt cannot install qemu: check the proxy (`curl -I --proxy http://proxy:3128 https://deb.debian.org`)
  and that `apt update` succeeded. Alternative: ship a static qemu in `~/g5kbinit` and pass `--qemu`.
- Worker cannot reach controller: `curl -v -H "Authorization: Bearer $CONTROLLER_TOKEN" $CONTROLLER_URL/status.json`
  from the node. 401s show as `auth_failure` events. For tunnels, check `GatewayPorts` and `no_proxy`.
- Lots of `crash`: raise `TIMEOUT_MS`, inspect the node log for QEMU stderr; verify `-cpu host` works
  (nested virtualization not involved on bare-metal nodes).
- Nodes silent after being killed: expected for besteffort; idempotent resubmission restarts the job on
  any host of the cluster, which takes the run over (a batch not reported is re-served).
- Jobs never start: `oarstat -fj <id>`; besteffort only runs on idle resources; use reserve.
- Stop everything: `oardel $(oarstat -u -J | grep -o '"Job_Id": *"[0-9]*"' | grep -o '[0-9]*')`.
