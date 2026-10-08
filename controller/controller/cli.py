import argparse
import logging
import sys

import psycopg
import uvicorn

from .config import Config
from .features import opcode_features
from .logging_setup import setup_logging

log = logging.getLogger("controller.cli")


def compute_features(dsn: str, conn_kwargs=None) -> dict:
    """Decode every distinct test_cases opcode and (re)fill instruction_features.

    Per instruction_id the feature set is the UNION over its opcodes.
    Reads x86db tables only; writes only instruction_features."""
    from .schema import DDL

    with psycopg.connect(dsn, **(conn_kwargs or {})) as conn:
        conn.execute(DDL)
        per: dict[int, set[str]] = {}
        bad = 0
        with conn.cursor(name="opcodes") as cur:
            cur.execute("SELECT DISTINCT instruction_id, opcode FROM test_cases")
            for iid, opcode in cur:
                f = opcode_features(opcode)
                if f is None:
                    bad += 1
                    log.warning("undecodable opcode", extra={"instruction_id": iid, "opcode": opcode})
                    f = set()
                per.setdefault(iid, set()).update(f)
        conn.execute("DELETE FROM instruction_features")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO instruction_features (instruction_id, feature) VALUES (%s,%s)",
                [(i, f) for i, fs in per.items() for f in sorted(fs)],
            )
        n = sum(len(v) for v in per.values())
    stats = {"instructions": len(per), "feature_rows": n, "undecodable": bad}
    log.info("compute-features done", extra=stats)
    return stats


def _value(v) -> str:
    return hex(v) if isinstance(v, int) and not isinstance(v, bool) else str(v)


def print_report(conn, cluster=None) -> None:
    """Replays that differ from the stored results, per cluster, with the proposed action."""
    from . import replay

    entries = replay.report(conn, cluster)
    for name, c in replay.counts(conn, cluster).items():
        mine = [p for p in entries if p["cluster"] == name]
        same = sum(1 for p in mine if p["note"] == "same")
        print(f"{name}: " + ", ".join(f"{c.get(k, 0)} {k}" for k in ("pending", "issued", "done", "applied"))
              + f"; {same} replayed with the same result")
        for p in mine:
            if p["note"] == "same":
                continue
            was = ", ".join(sorted({s["class"] for s in p["stored"]})) or "ok"
            print(f"  {p['action'] or p['note']:<10}{p['test_case_id']}/{p['state_index']}  {p['instruction']}"
                  f"  was: {was} (rows: {len(p['stored'])})  replay: {replay.tally(p['runs'])}"
                  + (f" = {p['class']}" if p["class"] not in (None, "ok") else "")
                  + f"  [{p['worker_version']}, {p['host']}]")
            for k, (old, new) in sorted(p["changed"].items()):
                print(f"            {k}: {_value(old)} -> {_value(new)}")


def run_replay(args, dsn: str) -> None:
    from psycopg.rows import dict_row

    from . import replay

    # No migration here: the tables come from `serve` or `migrate`, and the DDL takes locks that
    # would stall a running controller.
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if args.replay_cmd == "add":
            for cluster in args.cluster:
                queued, matching = replay.add(conn, cluster, args.tc, args.si, args.repeat, args.dry_run)
                known = conn.execute("SELECT 1 FROM g5k_nodes WHERE cluster = %s LIMIT 1", (cluster,)).fetchone()
                print(f"{cluster}: {matching} states match, "
                      + ("nothing queued (dry run)" if args.dry_run else f"{queued} queued (x{args.repeat})")
                      + ("" if known else "; warning: no node of this cluster has registered yet"))
        elif args.replay_cmd == "report":
            print_report(conn, args.cluster)
        else:
            done = replay.apply(conn, args.cluster, args.action, args.tc, args.insn)
            for p in done:
                print(f"{args.action}  {p['test_case_id']}/{p['state_index']}  {p['instruction']}"
                      f"  ({len(p['stored'])} rows stored)")
            print(f"{args.cluster}: {len(done)} inputs applied ({args.action})")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="controller")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("compute-features", help="fill instruction_features from opcodes via iced-x86")
    sub.add_parser("migrate", help="create/upgrade g5k_* tables")
    sub.add_parser("backfill-counts", help="rebuild g5k_class_counts from g5k_results (one long scan, no downtime)")
    sub.add_parser("serve", help="run the HTTP controller (default LISTEN=0.0.0.0:8080)")
    rp = sub.add_parser("replay", help="run chosen inputs again on a cluster, then act on the result")
    rsub = rp.add_subparsers(dest="replay_cmd", required=True)
    a = rsub.add_parser("add", help="queue a test case (one state, or all of them) for a cluster")
    a.add_argument("--cluster", action="append", required=True, help="repeatable")
    a.add_argument("--tc", type=int, required=True, help="test_case_id")
    a.add_argument("--si", type=int, help="state_index (default: every state)")
    a.add_argument("--repeat", type=int, default=1, help="runs per input (default 1)")
    a.add_argument("--dry-run", action="store_true", help="only count the matching states")
    a = rsub.add_parser("report", help="replays that differ from the stored results, with a proposed action")
    a.add_argument("--cluster")
    a = rsub.add_parser("apply", help="carry out one kind of proposed action on a cluster")
    a.add_argument("--cluster", required=True)
    a.add_argument("--action", required=True, choices=["delete", "overwrite", "insert"])
    a.add_argument("--tc", type=int, help="only this test_case_id")
    a.add_argument("--insn", help="only instructions containing this text")
    args = p.parse_args(argv)

    import os
    log_dir = os.environ.get("LOG_DIR", "./logs")
    setup_logging(log_dir)
    if args.cmd == "compute-features":
        dsn = os.environ.get("X86DB_DSN", "postgresql://x86db:x86db@localhost:5432/x86db")
        print(compute_features(dsn))
    elif args.cmd == "backfill-counts":
        from .schema import backfill_class_counts
        dsn = os.environ.get("X86DB_DSN", "postgresql://x86db:x86db@localhost:5432/x86db")
        print(f"counted {backfill_class_counts(dsn)} results")
    elif args.cmd == "replay":
        run_replay(args, os.environ.get("X86DB_DSN", "postgresql://x86db:x86db@localhost:5432/x86db"))
    elif args.cmd == "migrate":
        from psycopg_pool import ConnectionPool
        from .schema import migrate
        cfg = Config.from_env()
        pool = ConnectionPool(cfg.dsn, min_size=1, open=True)
        migrate(pool)
        pool.close()
        print("migrated")
    else:
        from .app import create_app
        cfg = Config.from_env()
        host, _, port = cfg.listen.rpartition(":")
        uvicorn.run(create_app(cfg), host=host or "0.0.0.0", port=int(port), log_config=None)


if __name__ == "__main__":
    main(sys.argv[1:])
