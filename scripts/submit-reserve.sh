#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... submit-reserve.sh [-w walltime] [-t type] [-q queue] [-e] <cluster...>
# Regular (non-besteffort) OAR jobs, one per cluster on any free host. Default walltime 2:00:00
# (note: G5K restricts long jobs during the day; use -w 12:00:00 and a night/weekend submission).
# -t exotic is needed for exotic clusters, -q production for the production queue.
# -e submits an emulated run (QEMU TCG instead of KVM) on a host of that cluster.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
G5K_WALLTIME="2:00:00" G5K_EXTRA=""
g5k_parse_opts '2,3p' "$@"
g5k_check_env
g5k_write_env
g5k_submit_all "$G5K_WALLTIME" "$G5K_EXTRA" "${G5K_CLUSTERS[@]}"
