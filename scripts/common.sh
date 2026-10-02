#!/usr/bin/env bash
# Shared helpers for submit-*.sh. Source it; run those scripts ON a G5K site frontend
# (after `build.sh --site <site>`), from ~/g5kbinit/node or from the repo.
#
# Node enumeration: `oarnodes -J --sql "cluster='X' AND state != 'Dead'"` lists the cluster's
# resources with their network_address; we dedupe to hosts. This is robust because it reflects the
# real OAR resource table (handles gaps in numbering, retired/dead nodes, multi-socket nodes with
# several resources per host). The alternative `oarsub -l host=1` x N with host exclusion cannot
# guarantee one job per distinct node (the scheduler picks any free host, jobs can pile on the
# same fast node or leave some never covered), so each job is pinned instead with -p "host='<fqdn>'".

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

# g5k_nodes <cluster> -> one FQDN per line
g5k_nodes() {
    local cluster="$1"
    oarnodes -J --sql "cluster='$cluster' AND state != 'Dead'" 2>/dev/null \
        | grep -o '"network_address": *"[^"]*"' | sed 's/.*: *"\(.*\)"/\1/' | sort -uV
}

# g5k_submit_all <walltime> <extra oarsub args-as-string> <cluster...>
# Extra args are word-split on purpose (e.g. "-t besteffort -t idempotent").
g5k_submit_all() {
    local walltime="$1" extra="$2"; shift 2
    local total=0 cluster host out
    for cluster in "$@"; do
        local nodes; nodes="$(g5k_nodes "$cluster")"
        [[ -n "$nodes" ]] || { echo "cluster $cluster: no nodes found (wrong site/name?)" >&2; continue; }
        while read -r host; do
            [[ -n "$host" ]] || continue
            # shellcheck disable=SC2086
            out="$(oarsub -n "g5kbinit-${host%%.*}" $extra \
                -l "host=1,walltime=$walltime" -p "host='$host'" \
                -O "$G5K_BASE/logs/oar.${host%%.*}.%jobid%.out" \
                -E "$G5K_BASE/logs/oar.${host%%.*}.%jobid%.err" \
                "bash $G5K_BASE/node/run.sh" 2>&1)" \
                && echo "$host: $(grep -m1 OAR_JOB_ID <<<"$out")" \
                || echo "$host: FAILED: $out" >&2
            total=$((total + 1))
        done <<<"$nodes"
    done
    echo "submitted $total job(s). Monitor: oarstat -u ; logs in $G5K_BASE/logs/"
}
