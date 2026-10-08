#!/usr/bin/env bash
# Build the Aegis bootimage and the static musl worker into dist/, optionally rsync to a G5K site.
#
# Usage: scripts/build.sh [--site SITE] [--skip-kernel] [--skip-worker]
#   --site nancy   rsync dist/ to nancy.g5k:~/g5kbinit/ (ssh alias "<site>.g5k" if it resolves
#                  in your ssh config, otherwise <login>@access.grid5000.fr -> ProxyJump -> <site>).
#                  Set G5K_USER if your G5K login differs from your local user.
#                  Homes are NFS-shared per site, so one rsync per site covers all its nodes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
AEGIS="$ROOT/aegis"
TARGET=x86_64-unknown-linux-musl
site="" skip_kernel=0 skip_worker=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --site) site="${2:?--site needs a value}"; shift 2 ;;
        --skip-kernel) skip_kernel=1; shift ;;
        --skip-worker) skip_worker=1; shift ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$DIST/node"

if [[ $skip_kernel -eq 0 ]]; then
    echo "==> bootimage (aegis, branch $(git -C "$AEGIS" rev-parse --abbrev-ref HEAD))"
    # Needs: cargo-bootimage, llvm-tools-preview (via rust-toolchain.toml in aegis/).
    # The aegis crate's .cargo/config.toml sets the x86_64-target.json target + build-std;
    # the workspace target dir is aegis/target.
    (cd "$AEGIS/aegis" && cargo bootimage --release)
    img="$(find "$AEGIS/target" -path '*/release/bootimage-aegis.bin' -print0 | xargs -0 ls -t 2>/dev/null | head -n1)"
    [[ -n "$img" ]] || { echo "bootimage-aegis.bin not found under $AEGIS/target" >&2; exit 1; }
    cp "$img" "$DIST/aegis-bootimage.bin"
fi

if [[ $skip_worker -eq 0 ]]; then
    echo "==> worker ($TARGET)"
    rustup target add "$TARGET" >/dev/null 2>&1 || true
    # cd so worker/.cargo/config.toml (CC=clang for ring, crt-static) is picked up.
    # Off Linux, clang finds no musl headers and `cc` cannot link ELF: use a musl cross compiler
    # for both when there is one (macOS: brew install filosottile/musl-cross/musl-cross).
    if command -v x86_64-linux-musl-gcc >/dev/null; then
        export CC_x86_64_unknown_linux_musl="${CC_x86_64_unknown_linux_musl:-x86_64-linux-musl-gcc}"
        export CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER="${CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER:-x86_64-linux-musl-gcc}"
    fi
    (cd "$ROOT/worker" && cargo build --release --target "$TARGET")
    cp "$ROOT/worker/target/$TARGET/release/g5k-worker" "$DIST/g5k-worker"
fi

cp "$ROOT/node/run.sh" "$DIST/node/run.sh"
cp "$ROOT/scripts/common.sh" "$ROOT/scripts/submit-besteffort.sh" "$ROOT/scripts/submit-reserve.sh" \
   "$ROOT/scripts/status.sh" "$DIST/node/"
chmod +x "$DIST/node/"*.sh
ls -lh "$DIST"

if [[ -n "$site" ]]; then
    if ssh -G "$site.g5k" >/dev/null 2>&1 && ssh -o BatchMode=yes -o ConnectTimeout=10 "$site.g5k" true 2>/dev/null; then
        dest="$site.g5k"; ssh_cmd=(ssh)
    else
        user="${G5K_USER:-$USER}"
        dest="$user@$site"
        ssh_cmd=(ssh -J "$user@access.grid5000.fr")
        echo "ssh alias $site.g5k unavailable, using ProxyJump via access.grid5000.fr"
    fi
    echo "==> rsync dist/ -> $dest:~/g5kbinit/"
    rsync -av -e "${ssh_cmd[*]}" --rsync-path="mkdir -p ~/g5kbinit && rsync" \
        "$DIST/" "$dest:g5kbinit/"
    echo "Done. On the frontend: cd ~/g5kbinit/node && CONTROLLER_URL=... CONTROLLER_TOKEN=... ./submit-besteffort.sh <cluster>"
fi
