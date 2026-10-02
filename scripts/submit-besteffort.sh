#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... submit-besteffort.sh [-w walltime] <cluster...>
# One best-effort + idempotent OAR job per node of each cluster (run on the site frontend).
# Best-effort jobs are killed when someone reserves the node; "idempotent" makes OAR resubmit them
# automatically, and the controller cursor lets the worker resume where it stopped.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
walltime="24:00:00"
while getopts "w:h" o; do case $o in w) walltime=$OPTARG;; *) sed -n '2,3p' "$0"; exit 2;; esac; done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || { sed -n '2,3p' "$0"; exit 2; }
g5k_check_env
g5k_write_env
g5k_submit_all "$walltime" "-t besteffort -t idempotent" "$@"
