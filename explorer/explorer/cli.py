import argparse
import logging
import sys

import uvicorn

from .config import Config
from .jobs import connect
from .schema import ensure


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="explorer")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the web frontend and the summary job (default)")
    sub.add_parser("init", help="create the explorer's tables in its schema")
    sub.add_parser("pause", help="stop the summary job after its current step")
    sub.add_parser("resume", help="let the summary job continue")
    sub.add_parser("status", help="print the watermark")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    if args.cmd in (None, "serve"):
        from .app import create_app
        host, _, port = cfg.listen.rpartition(":")
        uvicorn.run(create_app(cfg), host=host or "0.0.0.0", port=int(port))
        return
    with connect(cfg, cfg.page_timeout_s) as conn:
        with conn.transaction():
            ensure(conn, cfg.schema)
        if args.cmd in ("pause", "resume"):
            conn.execute("UPDATE watermark SET paused = %s", (args.cmd == "pause",))
        w = conn.execute("SELECT * FROM watermark").fetchone()
        pct = 100 * w["result_id"] / w["tip_id"] if w["tip_id"] else 100.0
        print(f"result watermark {w['result_id']} of {w['tip_id']} ({pct:.2f}%), "
              f"replays handled up to {w['replay_applied_at']}, paused={w['paused']}")


if __name__ == "__main__":
    main(sys.argv[1:])
