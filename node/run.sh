#!/usr/bin/env bash
# Runs ON a Grid5000 node (as the OAR job command). Prepares the node and loops g5k-worker
# until the controller reports no more work. The worker is run with --once: exit 0 means the
# controller has no more cases (done, stop); any non-zero exit (fatal error, signal) restarts it.
# Needs CONTROLLER_URL / CONTROLLER_TOKEN, from the environment or from ~/g5kbinit/env
# (written 0600 by the submit scripts, so the token is not visible in `oarstat`).
# Optional: BATCH_SIZE (1000), TIMEOUT_MS (10000), HTTP_PROXY_URL (http://proxy:3128).
set -uo pipefail

BASE="${G5KBINIT_DIR:-$HOME/g5kbinit}"
HOST="$(hostname -s)"
mkdir -p "$BASE/logs"
exec > >(tee -a "$BASE/logs/$HOST.log") 2>&1
log() { echo "[$(date -u +%FT%TZ)] run.sh: $*"; }

[[ -f "$BASE/env" ]] && { set -a; . "$BASE/env"; set +a; }
: "${CONTROLLER_URL:?CONTROLLER_URL not set}"
: "${CONTROLLER_TOKEN:?CONTROLLER_TOKEN not set}"
export CONTROLLER_URL CONTROLLER_TOKEN

# Outbound internet from G5K nodes goes through the site web proxy.
proxy="${HTTP_PROXY_URL:-http://proxy:3128}"
export http_proxy="${http_proxy:-$proxy}" https_proxy="${https_proxy:-$proxy}"
export HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy"
export no_proxy="${no_proxy:-localhost,127.0.0.1,.grid5000.fr}" NO_PROXY="${no_proxy}"

# Root: sudo-g5k with no args enables plain sudo for the lifetime of the job.
log "enabling sudo (sudo-g5k)"
sudo-g5k || log "WARNING: sudo-g5k failed"

if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    log "installing qemu-system-x86"
    sudo-g5k apt-get update -y || true
    sudo-g5k apt-get install -y qemu-system-x86 || { log "qemu install failed"; exit 1; }
fi
QEMU="$(command -v qemu-system-x86_64)"

if [[ ! -e /dev/kvm ]]; then
    log "ERROR: /dev/kvm missing (virtualization disabled on this node?)"; exit 1
fi
if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    sudo chmod 666 /dev/kvm || sudo-g5k chmod 666 /dev/kvm || { log "cannot chmod /dev/kvm"; exit 1; }
fi

cpu="$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs)"
log "host=$HOST cpu='$cpu' qemu=$QEMU controller=$CONTROLLER_URL proxy=$http_proxy"

chmod +x "$BASE/g5k-worker" 2>/dev/null || true
fails=0
while :; do
    log "starting worker"
    start=$SECONDS
    "$BASE/g5k-worker" --bootimage "$BASE/aegis-bootimage.bin" --qemu "$QEMU" \
        --batch-size "${BATCH_SIZE:-1000}" --timeout-ms "${TIMEOUT_MS:-10000}" --once
    rc=$?
    if [[ $rc -eq 0 ]]; then
        log "worker exited 0: controller reported done"; exit 0
    fi
    # A run that lasted a while resets the backoff.
    (( SECONDS - start > 120 )) && fails=0
    fails=$((fails + 1))
    delay=$(( fails < 6 ? 5 * fails : 30 ))
    log "worker exited rc=$rc (failure #$fails), restarting in ${delay}s"
    sleep "$delay"
done
