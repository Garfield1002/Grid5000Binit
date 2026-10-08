#!/usr/bin/env bash
# Shared helpers for submit-*.sh. Source it; run those scripts ON a G5K site frontend
# (after `build.sh --site <site>`), from ~/g5kbinit/node or from the repo.
#
# ONE job per cluster, not pinned to a host (-p "cluster='X'"): OAR runs it on any free host and,
# for an idempotent best-effort job, resubmits it on any other one. The controller hands the cursor
# over between hosts of the same CPU model / microcode / features, so the run continues. More jobs
# on a cluster would only duplicate work, since same-spec hosts share one run.

G5K_BASE="${G5KBINIT_DIR:-$HOME/g5kbinit}"

die() { echo "error: $*" >&2; exit 1; }

g5k_check_env() {
    : "${CONTROLLER_URL:?set CONTROLLER_URL}"
    : "${CONTROLLER_TOKEN:?set CONTROLLER_TOKEN}"
    command -v oarsub >/dev/null || die "oarsub not found: run this on a Grid5000 frontend"
    [[ -x "$G5K_BASE/g5k-worker" && -f "$G5K_BASE/aegis-bootimage.bin" ]] \
        || die "missing $G5K_BASE/g5k-worker or aegis-bootimage.bin (run scripts/build.sh --site <site>)"
}

# Writes CONTROLLER_URL/TOKEN to a private file read by node/run.sh (keeps the token out of oarstat).
g5k_write_env() {
    mkdir -p "$G5K_BASE/logs"
    ( umask 077
      { printf 'CONTROLLER_URL=%q\n' "$CONTROLLER_URL"
        printf 'CONTROLLER_TOKEN=%q\n' "$CONTROLLER_TOKEN"
        [[ -n "${BATCH_SIZE:-}" ]] && printf 'BATCH_SIZE=%q\n' "$BATCH_SIZE"
        [[ -n "${TIMEOUT_MS:-}" ]] && printf 'TIMEOUT_MS=%q\n' "$TIMEOUT_MS"
        true
      } > "$G5K_BASE/env" )
}

# g5k_parse_opts <usage-lines> "$@": options shared by submit-*.sh. Callers preset G5K_WALLTIME
# and G5K_EXTRA; this sets G5K_CLUSTERS and may override/extend the former.
#   -w walltime   -t type (extra OAR job type, repeatable, e.g. exotic)   -q queue (e.g. production)
#   -e            emulated run: the job sets EMULATED=1 for node/run.sh (QEMU TCG instead of KVM)
g5k_parse_opts() {
    local usage="$1" o; shift
    OPTIND=1
    G5K_EMULATED=""
    while getopts "w:t:q:eh" o; do
        case $o in
            w) G5K_WALLTIME=$OPTARG ;;
            t) G5K_EXTRA+=" -t $OPTARG" ;;
            q) G5K_EXTRA+=" -q $OPTARG" ;;
            e) G5K_EMULATED=1 ;;
            *) sed -n "$usage" "$0"; exit 2 ;;
        esac
    done
    shift $((OPTIND - 1))
    [[ $# -ge 1 ]] || { sed -n "$usage" "$0"; exit 2; }
    G5K_CLUSTERS=("$@")
}

# g5k_nodes <cluster> -> one FQDN per line, hosts that OAR does not report as Dead.
g5k_nodes() {
    local cluster="$1"
    oarnodes -J --sql "cluster='$cluster' AND state != 'Dead'" 2>/dev/null \
        | grep -o '"network_address" *: *"[^"]*"' | sed 's/.*: *"\(.*\)"/\1/' | sort -uV
}

# g5k_submit_all <walltime> <extra oarsub args-as-string> <cluster...>
# Extra args are word-split on purpose (e.g. "-t besteffort -t idempotent").
# A cluster without a single non-dead host is skipped: its job could never run.
g5k_submit_all() {
    local walltime="$1" extra="$2"; shift 2
    local total=0 cluster out name cmd="bash $G5K_BASE/node/run.sh"
    # Set on the job's command line, not in the env file that every job of the site reads.
    [[ -n "${G5K_EMULATED:-}" ]] && cmd="EMULATED=1 $cmd"
    for cluster in "$@"; do
        [[ -n "$(g5k_nodes "$cluster")" ]] \
            || { echo "$cluster: SKIPPED: no usable host (all dead, or wrong site/name?)" >&2; continue; }
        # shellcheck disable=SC2086
        name="${G5K_EMULATED:+tcg-}$cluster"
        out="$(oarsub -n "g5kbinit-$name" $extra \
            -l "host=1,walltime=$walltime" -p "cluster='$cluster'" \
            -O "$G5K_BASE/logs/oar.$name.%jobid%.out" \
            -E "$G5K_BASE/logs/oar.$name.%jobid%.err" \
            "$cmd" 2>&1)" \
            && echo "$cluster: $(grep -m1 OAR_JOB_ID <<<"$out")" \
            || echo "$cluster: FAILED: $out" >&2
        total=$((total + 1))
    done
    echo "submitted $total job(s). Monitor: oarstat -u ; logs in $G5K_BASE/logs/"
}
