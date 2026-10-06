#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... scripts/status.sh [--raw]
# Fetches /status.json and prints a per-run summary (needs python3).
set -euo pipefail
: "${CONTROLLER_URL:?set CONTROLLER_URL}"
: "${CONTROLLER_TOKEN:?set CONTROLLER_TOKEN}"
json="$(curl -fsS -H "Authorization: Bearer $CONTROLLER_TOKEN" "${CONTROLLER_URL%/}/status.json")"
if [[ "${1:-}" == "--raw" ]]; then echo "$json"; exit 0; fi
python3 /dev/fd/3 3<<'PY' <<<"$json"
import json, sys

d = json.load(sys.stdin)
ns = d["nodes"]

def num(x):
    return "-" if x is None else "{:,.0f}".format(x)

def eta(s):
    return "-" if s is None else "{:.1f}h".format(s / 3600)

def classes(c):
    return " ".join("%s=%s" % kv for kv in sorted(c.items())) or "-"

print("%d nodes, %d silent, %s cases done" % (
    len(ns), len(d["silent_nodes"]), num(sum(r["done_cases"] for r in d["runs"]))))
o = d.get("objective")
if o and o["models"]["total"]:
    m = o["models"]
    print("objective: %d/%d CPU models done (%d running, %d stalled), %d/%d without Abaca, %d/%d microarchitectures" % (
        m["done"], m["total"], m["active"], m["stalled"], o["reachable"]["done"], o["reachable"]["total"],
        o["microarchs"]["done"], o["microarchs"]["total"]))
print("%-28s %-8s %12s %12s %8s %7s %6s %4s  %s" % (
    "nodes", "state", "done", "total", "cases/s", "eta", "seen", "qrst", "mismatches"))
for n in d["runs"]:
    print("%-28s %-8s %12s %12s %8s %7s %5.0fs %4s  %s%s" % (
        ",".join(h.split(".")[0] for h in n["hosts"])[:28], n["state"], num(n["done_cases"]), num(n["total_cases"]),
        num(n.get("wall_cases_per_s") or n["rate_cases_per_s"]), eta(n["eta_s"]), n["last_seen_ago_s"],
        n["qemu_restarts"], classes(n["mismatch_classes"]),
        " SILENT" if n["silent"] else ""))
print("\nrecent events:")
for e in d["events"][:10]:
    print("  %s %-14s %s" % (e["ts"][:19], e["kind"], e["host"] or ""))
PY
