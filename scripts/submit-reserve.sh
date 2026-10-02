#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... submit-reserve.sh [-w walltime] <cluster...>
# One regular (non-besteffort) OAR job per node of each cluster. Default walltime 2:00:00
# (note: G5K restricts long jobs during the day; use -w 12:00:00 and a night/weekend submission).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
walltime="2:00:00"
while getopts "w:h" o; do case $o in w) walltime=$OPTARG;; *) sed -n '2,3p' "$0"; exit 2;; esac; done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || { sed -n '2,3p' "$0"; exit 2; }
g5k_check_env
g5k_write_env
g5k_submit_all "$walltime" "" "$@"
