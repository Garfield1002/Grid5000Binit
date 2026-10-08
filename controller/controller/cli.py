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


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="controller")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("compute-features", help="fill instruction_features from opcodes via iced-x86")
    sub.add_parser("migrate", help="create/upgrade g5k_* tables")
    sub.add_parser("backfill-counts", help="rebuild g5k_class_counts from g5k_results (one long scan, no downtime)")
    sub.add_parser("serve", help="run the HTTP controller (default LISTEN=0.0.0.0:8080)")
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
