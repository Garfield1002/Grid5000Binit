#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... submit-besteffort.sh [-w walltime] [-t type] [-q queue] <cluster...>
# Best-effort + idempotent OAR jobs (run on the site frontend), one per cluster on any free host.
# Best-effort jobs are killed when someone reserves the node;
# "idempotent" makes OAR resubmit them, on any host of the cluster, and the controller lets the new host
# resume where the previous one stopped.
# -t exotic is needed for exotic clusters, -q production for the production queue.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
G5K_WALLTIME="24:00:00" G5K_EXTRA="-t besteffort -t idempotent"
g5k_parse_opts '2,3p' "$@"
g5k_check_env
g5k_write_env
g5k_submit_all "$G5K_WALLTIME" "$G5K_EXTRA" "${G5K_CLUSTERS[@]}"
