"""The background job that keeps explorer.summary current. One instance at a time (advisory lock);
every step is one short transaction, so the job can be stopped anywhere and resumes after a restart."""
import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Config
from .schema import ROW_HASH, ensure, has_table

log = logging.getLogger("explorer.jobs")
JOB_LOCK = 5000503
# applied_at is the start of the transaction that applied a replay, so one that commits late can
# carry an older applied_at than one already handled. Replays are looked up this far back.
REPLAY_OVERLAP = "10 minutes"

FOLD = f"""
INSERT INTO summary (node_id, test_case_id, class, diff_keys, n, sig)
SELECT node_id, test_case_id, class, diff_keys, count(*), bit_xor({ROW_HASH})
FROM g5k_results WHERE {{where}}
GROUP BY 1, 2, 3, 4
ON CONFLICT (node_id, test_case_id, class, diff_keys)
DO UPDATE SET n = summary.n + EXCLUDED.n, sig = summary.sig # EXCLUDED.sig
"""


class Job:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.safe = 0          # ids up to here are all committed or gone
        self.pending = None    # (max id, snapshot xmax) waiting for the transactions of that moment
        self.seen_replays: set = set()
        self.last_checks = 0.0

    def watermark(self, conn) -> dict:
        return conn.execute("SELECT * FROM watermark").fetchone()

    def look(self, conn) -> int:
        """Return max(id) and advance self.safe. Ids are handed out before their rows commit, so a
        row below max(id) can still appear; it cannot once every transaction that was open when
        max(id) was read has ended."""
        r = conn.execute(
            """SELECT coalesce((SELECT max(id) FROM g5k_results), 0) AS tip,
                      pg_snapshot_xmin(pg_current_snapshot())::text::bigint AS xmin,
                      pg_snapshot_xmax(pg_current_snapshot())::text::bigint AS xmax""").fetchone()
        if self.pending and r["xmin"] >= self.pending[1]:
            self.safe, self.pending = max(self.safe, self.pending[0]), None
        if r["xmin"] >= r["xmax"]:  # nothing is open
            self.safe, self.pending = max(self.safe, r["tip"]), None
        elif self.pending is None:
            self.pending = (r["tip"], r["xmax"])
        return r["tip"]

    def step_tc_states(self, conn) -> bool:
        n = conn.execute(
            """INSERT INTO tc_states (test_case_id, instruction_id, n)
               SELECT id, instruction_id,
                      CASE WHEN status LIKE 'ERR%%' THEN 0 ELSE jsonb_array_length(initial_states) END
               FROM test_cases
               WHERE id > (SELECT coalesce(max(test_case_id), 0) FROM tc_states)
               ORDER BY id LIMIT %s""", (self.cfg.tc_chunk,)).rowcount
        return n > 0

    def step_fold(self, conn, wm: dict, tip: int) -> bool:
        lo = wm["result_id"]
        hi = min(lo + self.cfg.chunk, self.safe)
        if hi <= lo:
            if tip != wm["tip_id"]:
                conn.execute("UPDATE watermark SET tip_id = %s", (tip,))
            return False
        with conn.transaction():
            conn.execute(FOLD.format(where="id > %s AND id <= %s"), (lo, hi))
            conn.execute("UPDATE watermark SET result_id = %s, tip_id = %s, updated_at = now()",
                         (hi, max(tip, hi)))
        return hi < self.safe

    def step_replays(self, conn, wm: dict) -> int:
        """`controller replay apply` deletes and overwrites g5k_results rows in place. For each
        applied replay, the summary of its cluster's nodes on its test case is rebuilt from the rows
        at or below the result watermark (the rows above are folded later, once)."""
        if not has_table(conn, "g5k_replays"):
            return 0
        mark = wm["replay_applied_at"]
        rows = conn.execute(
            f"""SELECT id, cluster, test_case_id, applied_at FROM g5k_replays
                WHERE status = 'applied' AND applied_at IS NOT NULL
                  AND (%(m)s::timestamptz IS NULL
                       OR applied_at > %(m)s::timestamptz - interval '{REPLAY_OVERLAP}')
                ORDER BY applied_at""", {"m": mark}).fetchall()
        new = [r for r in rows if (r["id"], r["applied_at"]) not in self.seen_replays]
        for cluster, tc in sorted({(r["cluster"], r["test_case_id"]) for r in new}):
            with conn.transaction():
                lo = conn.execute("SELECT result_id FROM watermark FOR UPDATE").fetchone()["result_id"]
                nodes = [n["node_id"] for n in conn.execute(
                    "SELECT node_id FROM g5k_nodes WHERE cluster = %s", (cluster,))]
                conn.execute("DELETE FROM summary WHERE node_id = ANY(%s) AND test_case_id = %s",
                             (nodes, tc))
                conn.execute(FOLD.format(where="node_id = ANY(%s) AND test_case_id = %s AND id <= %s"),
                             (nodes, tc, lo))
        if new:
            log.info("replays handled: %d", len(new))
        # Only what is still inside the lookup window needs remembering.
        self.seen_replays = {(r["id"], r["applied_at"]) for r in rows}
        if rows and rows[-1]["applied_at"] != mark:
            conn.execute("UPDATE watermark SET replay_applied_at = %s", (rows[-1]["applied_at"],))
        return len(new)

    def run_checks(self, conn) -> None:
        if has_table(conn, "g5k_class_counts"):
            # A running node's counts move ahead of the watermark, so only finished nodes are judged.
            diff = conn.execute(
                """SELECT node_id, class, s.n AS summary, c.n AS counts, g.host, g.state
                   FROM (SELECT node_id, class, sum(n)::bigint AS n FROM summary GROUP BY 1, 2) s
                   FULL JOIN g5k_class_counts c USING (node_id, class)
                   LEFT JOIN g5k_nodes g USING (node_id)
                   WHERE s.n IS DISTINCT FROM c.n ORDER BY node_id, class""").fetchall()
            bad = [d for d in diff if d["state"] != "running"]
            self.store(conn, "class_counts", not bad,
                       {"differences": bad[:200], "running_nodes_ahead": len(diff) - len(bad)})
        # XOR cancels a state held by two nodes of one model. Only test cases present on several
        # of a model's nodes can hide one (a run that changed host in the middle of a test case).
        shared = conn.execute(
            """SELECT g.cpu_model, s.test_case_id, array_agg(DISTINCT s.node_id) AS nodes
               FROM summary s JOIN g5k_nodes g USING (node_id)
               GROUP BY 1, 2 HAVING count(DISTINCT s.node_id) > 1 ORDER BY 1, 2""").fetchall()
        dup = []
        for s in shared[:self.cfg.check_max_tcs]:
            n = conn.execute(
                """SELECT count(*) AS n FROM (
                       SELECT state_index FROM g5k_results
                       WHERE node_id = ANY(%s) AND test_case_id = %s
                       GROUP BY 1 HAVING count(*) > 1) d""",
                (s["nodes"], s["test_case_id"])).fetchone()["n"]
            if n:
                dup.append({"cpu_model": s["cpu_model"], "test_case_id": s["test_case_id"],
                            "nodes": s["nodes"], "states_on_two_nodes": n})
        self.store(conn, "node_overlap", not dup,
                   {"shared_test_cases": len(shared),
                    "checked": min(len(shared), self.cfg.check_max_tcs), "overlaps": dup})

    @staticmethod
    def store(conn, name: str, ok: bool, detail: dict) -> None:
        conn.execute(
            """INSERT INTO checks (name, ok, detail) VALUES (%s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET ok = EXCLUDED.ok, detail = EXCLUDED.detail,
                                                checked_at = now()""", (name, ok, Jsonb(detail)))
        if not ok:
            log.warning("check failed: %s %s", name, detail)

    def tick(self, conn) -> bool:
        """One step. True when more work is waiting (the caller then pauses in proportion to the
        time the step took instead of idling)."""
        wm = self.watermark(conn)
        if wm["paused"]:
            return False
        if self.step_tc_states(conn):
            return True
        tip = self.look(conn)
        busy = self.step_fold(conn, wm, tip)
        self.step_replays(conn, wm)
        if not busy and time.time() - self.last_checks > self.cfg.check_every_s:
            self.last_checks = time.time()
            self.run_checks(conn)
        return busy


def connect(cfg: Config, timeout_s: float):
    return psycopg.connect(cfg.dsn, autocommit=True, row_factory=dict_row,
                           **cfg.conn_kwargs(timeout_s))


def run_forever(cfg: Config, stop: threading.Event) -> None:
    job = Job(cfg)
    while not stop.is_set():
        try:
            with connect(cfg, cfg.job_timeout_s) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (JOB_LOCK,)).fetchone()["ok"]:
                    log.info("another explorer runs the job; retrying later")
                    stop.wait(60)
                    continue
                with conn.transaction():
                    ensure(conn, cfg.schema)
                while not stop.is_set():
                    t0 = time.monotonic()
                    busy = job.tick(conn)
                    took = time.monotonic() - t0
                    # While a transaction of the controller is open, look again soon.
                    idle = min(5.0, cfg.idle_s) if job.pending else cfg.idle_s
                    stop.wait(took * (1 / cfg.duty - 1) if busy else idle)
        except Exception:
            log.exception("job step failed; reconnecting")
            stop.wait(10)
