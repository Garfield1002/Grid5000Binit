#!/usr/bin/env bash
# Usage: CONTROLLER_URL=... CONTROLLER_TOKEN=... scripts/status.sh [--raw]
# Fetches /status.json and prints a per-node and per-CPU-model summary (needs python3).
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
    len(ns), len(d["silent_nodes"]), num(sum(n["done_cases"] for n in ns))))
print("%-28s %-8s %12s %12s %8s %7s %6s %4s  %s" % (
    "host", "state", "done", "total", "cases/s", "eta", "seen", "qrst", "mismatches"))
for n in ns:
    print("%-28s %-8s %12s %12s %8s %7s %5.0fs %4s  %s%s" % (
        n["host"][:28], n["state"], num(n["done_cases"]), num(n["total_cases"]),
        num(n["rate_cases_per_s"]), eta(n["eta_s"]), n["last_seen_ago_s"],
        n["qemu_restarts"], classes(n["mismatch_classes"]),
        " SILENT" if n["silent"] else ""))
print("\nper CPU model:")
for k, m in sorted(d["cpu_models"].items()):
    print("  %-50s nodes=%d silent=%d done=%s  %s" % (
        k[:50], m["nodes"], m["silent"], num(m["done_cases"]), classes(m["mismatch_classes"])))
print("\nrecent events:")
for e in d["events"][:10]:
    print("  %s %-14s %s" % (e["ts"][:19], e["kind"], e["host"] or ""))
PY
