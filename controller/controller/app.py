import asyncio
import hmac
import html
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from .classify import CLASSES, classify
from .config import Config
from .features import normalize
from .schema import migrate

log = logging.getLogger("controller")
MAX_BATCH = 5000
CRASH_EVENTS_PER_POST = 20


# ── models ────────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    host: str
    cluster: Optional[str] = None
    cpu_model: Optional[str] = None
    microcode: Optional[str] = None
    cpuid_features: list[str] = []
    kernel_features: list[str] = []
    worker_version: Optional[str] = None
    save_mode: Optional[str] = None


class Mismatch(BaseModel):
    test_case_id: int
    state_index: int
    got_final_state: Optional[dict[str, Any]] = None
    got_exception_kind: Optional[str] = None
    status: str = "mismatch"


class ResultsBody(BaseModel):
    node_id: int
    batch_id: int
    ok_count: int = 0
    ok_ids: list[list[int]] = []
    mismatches: list[Mismatch] = []
    elapsed_s: float = 0.0
    save_mode: Optional[str] = None


class HeartbeatBody(BaseModel):
    node_id: int
    batch_id: Optional[int] = None
    done_in_batch: int = 0
    qemu_restarts: int = 0


# ── helpers ───────────────────────────────────────────────────────────

def effective_features(cpuid: list[str], kernel: list[str]) -> list[str]:
    """Features the node can actually run: kernel-reported ∩ CPUID-reported
    (whichever is non-empty if only one is given)."""
    c = {normalize(x) for x in cpuid}
    k = {normalize(x) for x in kernel}
    if c and k:
        return sorted(c & k)
    return sorted(c or k)


def add_event(conn, kind: str, node_id=None, host=None, **detail) -> None:
    conn.execute(
        "INSERT INTO g5k_events (kind, node_id, host, detail) VALUES (%s,%s,%s,%s)",
        (kind, node_id, host, Jsonb(detail)),
    )
    log.info(kind, extra={"event": kind, "node_id": node_id, "host": host, **detail})


BATCH_SQL = """
SELECT tc.id AS test_case_id, s.idx AS state_index, tc.instruction, tc.opcode,
       tc.instruction_id, s.initial_state,
       tr.final_state, tr.exception_kind,
       COALESCE((SELECT array_agg(f.feature ORDER BY f.feature)
                 FROM instruction_features f WHERE f.instruction_id = tc.instruction_id),
                ARRAY[]::text[]) AS required_features
FROM test_cases tc
CROSS JOIN LATERAL (SELECT (CASE WHEN tc.id = %(tc)s THEN %(si)s + 1 ELSE 0 END)::int AS first) w
-- The window is sliced out of initial_states once per test case: `initial_states -> idx` on every
-- row reads the whole array (up to ~100,000 states) each time.
CROSS JOIN LATERAL (
    SELECT (w.first + e.ord - 1)::int AS idx, e.value AS initial_state
    FROM jsonb_array_elements(jsonb_path_query_array(
        tc.initial_states, '$[$a to $b]',
        jsonb_build_object('a', w.first, 'b', w.first + %(n)s::int - 1))) WITH ORDINALITY AS e(value, ord)) s
JOIN test_results tr ON tr.test_case_id = tc.id AND tr.state_index = s.idx
WHERE tc.id >= %(tc)s  -- sargable: range-scan test_cases_pkey from the cursor; at most n states per case are needed
  AND (tc.status IS NULL OR tc.status NOT LIKE 'ERR%%')
  AND NOT EXISTS (SELECT 1 FROM instruction_features f
                  WHERE f.instruction_id = tc.instruction_id
                    AND NOT (f.feature = ANY(%(feats)s::text[])))
ORDER BY tc.id, s.idx
LIMIT %(n)s
"""

TOTAL_SQL = """
SELECT count(*) AS n
FROM test_cases tc
CROSS JOIN LATERAL generate_series(0, jsonb_array_length(tc.initial_states) - 1) AS s(idx)
JOIN test_results tr ON tr.test_case_id = tc.id AND tr.state_index = s.idx
WHERE (tc.status IS NULL OR tc.status NOT LIKE 'ERR%%')
  AND NOT EXISTS (SELECT 1 FROM instruction_features f
                  WHERE f.instruction_id = tc.instruction_id
                    AND NOT (f.feature = ANY(%(feats)s::text[])))
"""


def fetch_batch(conn, cursor_tc, cursor_si, feats, n):
    return conn.execute(
        BATCH_SQL, {"tc": cursor_tc, "si": cursor_si, "feats": feats, "n": n}
    ).fetchall()


def touch(conn, node_id: int):
    """Update last_seen; return (host, was_silent) or None if unknown node."""
    r = conn.execute(
        """UPDATE g5k_nodes n SET last_seen = now(), silent = false
           FROM (SELECT node_id, silent FROM g5k_nodes WHERE node_id = %s FOR UPDATE) o
           WHERE n.node_id = o.node_id RETURNING n.host, o.silent AS was_silent""",
        (node_id,),
    ).fetchone()
    if r is None:
        return None
    if r["was_silent"]:
        add_event(conn, "node_recovered", node_id, r["host"])
    return r["host"]


def check_silent(pool, silent_after_s: float) -> int:
    with pool.connection() as conn:
        rows = conn.execute(
            """UPDATE g5k_nodes SET silent = true
               WHERE state = 'running' AND NOT silent
                 AND last_seen < now() - make_interval(secs => %s)
               RETURNING node_id, host, last_seen""",
            (silent_after_s,),
        ).fetchall()
        for r in rows:
            add_event(conn, "node_silent", r["node_id"], r["host"],
                      last_seen=r["last_seen"].isoformat(), silent_after_s=silent_after_s)
    return len(rows)


# ── app ───────────────────────────────────────────────────────────────

def create_app(cfg: Config) -> FastAPI:
    pool_holder: dict[str, Any] = {}
    total_cache: dict[tuple, tuple[float, Optional[int]]] = {}
    total_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        kw = dict(cfg.conn_kwargs or {})
        kw["row_factory"] = dict_row
        pool = ConnectionPool(cfg.dsn, min_size=1, max_size=8, kwargs=kw, open=True)
        pool.wait()
        migrate(pool)
        pool_holder["pool"] = pool
        app.state.pool = pool

        async def silent_loop():
            while True:
                await asyncio.sleep(max(1.0, min(15.0, cfg.silent_after_s / 4)))
                try:
                    await asyncio.to_thread(check_silent, pool, cfg.silent_after_s)
                except Exception:
                    log.exception("silent check failed")

        task = asyncio.create_task(silent_loop())
        log.info("controller started", extra={"listen": cfg.listen})
        try:
            yield
        finally:
            task.cancel()
            pool.close()

    app = FastAPI(title="Grid5000Binit controller", lifespan=lifespan)

    def get_pool() -> ConnectionPool:
        return pool_holder["pool"]

    def auth(request: Request, token: Optional[str] = Query(None)):
        h = request.headers.get("authorization", "")
        supplied = h[7:] if h.lower().startswith("bearer ") else (token or "")
        if not hmac.compare_digest(supplied.encode(), cfg.token.encode()):
            try:
                with get_pool().connection() as conn:
                    add_event(conn, "auth_failure",
                              ip=request.client.host if request.client else None,
                              path=request.url.path, had_header=bool(h))
            except Exception:
                log.exception("could not record auth failure")
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.post("/register", dependencies=[Depends(auth)])
    def register(b: RegisterBody):
        feats = effective_features(b.cpuid_features, b.kernel_features)
        with get_pool().connection() as conn:
            r = conn.execute(
                """INSERT INTO g5k_nodes (host, cluster, cpu_model, microcode, cpuid_features,
                       kernel_features, features, worker_version, save_mode)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (host) DO UPDATE SET cluster=EXCLUDED.cluster,
                       cpu_model=EXCLUDED.cpu_model, microcode=EXCLUDED.microcode,
                       cpuid_features=EXCLUDED.cpuid_features,
                       kernel_features=EXCLUDED.kernel_features, features=EXCLUDED.features,
                       worker_version=EXCLUDED.worker_version,
                       save_mode=COALESCE(EXCLUDED.save_mode, g5k_nodes.save_mode), last_seen=now(),
                       silent=false, state='running'
                   RETURNING node_id, cursor_tc, cursor_si, (xmax <> 0) AS reregistered""",
                (b.host, b.cluster, b.cpu_model, b.microcode, b.cpuid_features,
                 b.kernel_features, feats, b.worker_version, b.save_mode),
            ).fetchone()
            add_event(conn, "register", r["node_id"], b.host, cluster=b.cluster,
                      cpu_model=b.cpu_model, n_features=len(feats),
                      resumed=r["reregistered"], cursor=[r["cursor_tc"], r["cursor_si"]])
        return {"node_id": r["node_id"]}

    @app.get("/batch", dependencies=[Depends(auth)])
    def batch(node_id: int, size: int = 1000):
        size = max(1, min(size, MAX_BATCH))
        with get_pool().connection() as conn:
            host = touch(conn, node_id)
            if host is None:
                raise HTTPException(404, "unknown node_id; register first")
            n = conn.execute("SELECT * FROM g5k_nodes WHERE node_id=%s", (node_id,)).fetchone()
            rows = fetch_batch(conn, n["cursor_tc"], n["cursor_si"], n["features"], size)
            if not rows:
                conn.execute("UPDATE g5k_nodes SET state='done', current_batch_id=NULL WHERE node_id=%s", (node_id,))
                add_event(conn, "node_done", node_id, host, done_cases=n["done_cases"])
                return {"batch_id": None, "cases": []}
            last = rows[-1]
            bid = conn.execute(
                """INSERT INTO g5k_batches (node_id, n_cases, last_tc, last_si)
                   VALUES (%s,%s,%s,%s) RETURNING batch_id""",
                (node_id, len(rows), last["test_case_id"], last["state_index"]),
            ).fetchone()["batch_id"]
            conn.execute(
                "UPDATE g5k_nodes SET current_batch_id=%s, done_in_batch=0, state='running' WHERE node_id=%s",
                (bid, node_id),
            )
            add_event(conn, "batch_start", node_id, host, batch_id=bid, n_cases=len(rows),
                      first=[rows[0]["test_case_id"], rows[0]["state_index"]],
                      last=[last["test_case_id"], last["state_index"]])
        return {
            "batch_id": bid,
            "cases": [
                {
                    "test_case_id": r["test_case_id"],
                    "state_index": r["state_index"],
                    "instruction": r["instruction"],
                    "opcode_hex": r["opcode"],
                    "required_features": r["required_features"],
                    "initial_state": r["initial_state"],
                    "expected": {"final_state": r["final_state"],
                                 "exception_kind": r["exception_kind"]},
                }
                for r in rows
            ],
        }

    @app.post("/results", dependencies=[Depends(auth)])
    def results(b: ResultsBody):
        with get_pool().connection() as conn:
            host = touch(conn, b.node_id)
            if host is None:
                raise HTTPException(404, "unknown node_id")
            bt = conn.execute(
                "SELECT * FROM g5k_batches WHERE batch_id=%s AND node_id=%s FOR UPDATE",
                (b.batch_id, b.node_id),
            ).fetchone()
            if bt is None:
                raise HTTPException(404, "unknown batch for this node")
            if bt["status"] == "done":
                return {"ok": True, "duplicate": True}

            counts: dict[str, int] = {}
            n_save_mode = (conn.execute("SELECT save_mode FROM g5k_nodes WHERE node_id=%s",
                                        (b.node_id,)).fetchone() or {}).get("save_mode")
            if b.mismatches:
                keys = [(m.test_case_id, m.state_index) for m in b.mismatches]
                exp = {
                    (r["test_case_id"], r["state_index"]): r
                    for r in conn.execute(
                        """SELECT tr.test_case_id, tr.state_index, tr.final_state, tr.exception_kind,
                                  tc.instruction, tc.instruction_id
                           FROM unnest(%s::bigint[], %s::int[]) AS q(tc, si)
                           JOIN test_results tr ON tr.test_case_id=q.tc AND tr.state_index=q.si
                           JOIN test_cases tc ON tc.id = tr.test_case_id""",
                        ([k[0] for k in keys], [k[1] for k in keys]),
                    ).fetchall()
                }
                iids = list({e["instruction_id"] for e in exp.values()})
                undef: dict[int, list[str]] = {
                    r["instruction_id"]: r["flags"]
                    for r in conn.execute(
                        """SELECT instruction_id, array_agg(flag) AS flags
                           FROM instruction_undefined_flags WHERE instruction_id = ANY(%s)
                           GROUP BY instruction_id""",
                        (iids,),
                    ).fetchall()
                }
                crash_events = 0
                for m in b.mismatches:
                    e = exp.get((m.test_case_id, m.state_index))
                    if e is None:
                        log.warning("mismatch for unknown case", extra={"tc": m.test_case_id, "si": m.state_index})
                    cls, dk = classify(
                        m.status,
                        e["final_state"] if e else None, e["exception_kind"] if e else None,
                        m.got_final_state, m.got_exception_kind,
                        undef.get(e["instruction_id"], []) if e else [],
                        b.save_mode or n_save_mode,
                    )
                    counts[cls] = counts.get(cls, 0) + 1
                    conn.execute(
                        """INSERT INTO g5k_results (node_id, batch_id, test_case_id, state_index,
                               instruction, status, class, diff_keys, got_final_state,
                               got_exception_kind, expected_final_state, expected_exception_kind)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (node_id, test_case_id, state_index) DO NOTHING""",
                        (b.node_id, b.batch_id, m.test_case_id, m.state_index,
                         e["instruction"] if e else None, m.status, cls, dk,
                         Jsonb(m.got_final_state) if m.got_final_state is not None else None,
                         m.got_exception_kind,
                         Jsonb(e["final_state"]) if e and e["final_state"] is not None else None,
                         e["exception_kind"] if e else None),
                    )
                    if cls == "crash" and crash_events < CRASH_EVENTS_PER_POST:
                        crash_events += 1
                        add_event(conn, "crash", b.node_id, host, batch_id=b.batch_id,
                                  test_case_id=m.test_case_id, state_index=m.state_index,
                                  instruction=e["instruction"] if e else None)
            n_mm = len(b.mismatches)
            if b.ok_count + n_mm != bt["n_cases"]:
                log.warning("result count differs from batch size",
                            extra={"batch_id": b.batch_id, "ok": b.ok_count,
                                   "mismatches": n_mm, "n_cases": bt["n_cases"]})
            conn.execute(
                """UPDATE g5k_batches SET status='done', finished_at=now(), ok_count=%s,
                       mismatch_count=%s, elapsed_s=%s WHERE batch_id=%s""",
                (b.ok_count, n_mm, b.elapsed_s, b.batch_id),
            )
            conn.execute(
                """UPDATE g5k_nodes SET done_cases = done_cases + %s, ok_count = ok_count + %s,
                       work_s = work_s + %s, current_batch_id = NULL, done_in_batch = 0,
                       save_mode = COALESCE(%s, save_mode),
                       cursor_tc = CASE WHEN (%s, %s) > (cursor_tc, cursor_si) THEN %s ELSE cursor_tc END,
                       cursor_si = CASE WHEN (%s, %s) > (cursor_tc, cursor_si) THEN %s ELSE cursor_si END
                   WHERE node_id = %s""",
                (bt["n_cases"], b.ok_count, b.elapsed_s, b.save_mode,
                 bt["last_tc"], bt["last_si"], bt["last_tc"],
                 bt["last_tc"], bt["last_si"], bt["last_si"], b.node_id),
            )
            add_event(conn, "batch_done", b.node_id, host, batch_id=b.batch_id,
                      ok=b.ok_count, mismatches=n_mm, classes=counts, elapsed_s=b.elapsed_s)
        return {"ok": True, "classes": counts}

    @app.post("/heartbeat", dependencies=[Depends(auth)])
    def heartbeat(b: HeartbeatBody):
        with get_pool().connection() as conn:
            host = touch(conn, b.node_id)
            if host is None:
                raise HTTPException(404, "unknown node_id")
            conn.execute(
                "UPDATE g5k_nodes SET done_in_batch=%s, qemu_restarts=%s WHERE node_id=%s",
                (b.done_in_batch, b.qemu_restarts, b.node_id),
            )
        log.info("heartbeat", extra={"node_id": b.node_id, "host": host,
                                     "batch_id": b.batch_id, "done_in_batch": b.done_in_batch})
        return {"ok": True}

    # ── monitoring ────────────────────────────────────────────────────

    def total_for(conn, feats: list[str]) -> Optional[int]:
        key = tuple(feats)
        with total_lock:
            c = total_cache.get(key)
            if c and time.time() - c[0] < 600:
                return c[1]
        try:
            conn.execute("SET LOCAL statement_timeout = '20s'")
            v = conn.execute(TOTAL_SQL, {"feats": list(feats)}).fetchone()["n"]
        except Exception:
            log.exception("total count failed")
            v = None
            conn.rollback()
        with total_lock:
            total_cache[key] = (time.time(), v)
        return v

    def build_status() -> dict:
        with get_pool().connection() as conn:
            nodes = conn.execute(
                """SELECT node_id, host, cluster, cpu_model, microcode, worker_version, state,
                          silent, save_mode, done_cases, ok_count, work_s, qemu_restarts, features,
                          current_batch_id, done_in_batch, cursor_tc, cursor_si,
                          registered_at, last_seen,
                          extract(epoch FROM now() - last_seen) AS last_seen_ago_s
                   FROM g5k_nodes ORDER BY host"""
            ).fetchall()
            cls = {}
            for r in conn.execute("SELECT node_id, class, count(*) AS n FROM g5k_results GROUP BY 1,2"):
                cls.setdefault(r["node_id"], {})[r["class"]] = r["n"]
            events = conn.execute(
                "SELECT id, ts, kind, node_id, host, detail FROM g5k_events ORDER BY id DESC LIMIT 50"
            ).fetchall()
            models: dict[str, dict] = {}
            out_nodes = []
            for n in nodes:
                total = total_for(conn, n["features"])
                rate = n["done_cases"] / n["work_s"] if n["work_s"] > 0 else None
                remaining = max(total - n["done_cases"], 0) if total is not None else None
                eta = remaining / rate if (rate and remaining is not None) else None
                c = cls.get(n["node_id"], {})
                d = {k: n[k] for k in ("node_id", "host", "cluster", "cpu_model", "microcode",
                                       "worker_version", "save_mode", "state", "silent", "done_cases",
                                       "ok_count", "qemu_restarts", "current_batch_id",
                                       "done_in_batch")}
                d.update(total_cases=total, rate_cases_per_s=rate, eta_s=eta, mismatch_classes=c,
                         last_seen=n["last_seen"].isoformat(),
                         last_seen_ago_s=float(n["last_seen_ago_s"]),
                         cursor=[n["cursor_tc"], n["cursor_si"]])
                out_nodes.append(d)
                m = models.setdefault(n["cpu_model"] or "?", {"nodes": 0, "done_cases": 0, "ok_count": 0,
                                                              "silent": 0, "mismatch_classes": {}})
                m["nodes"] += 1
                m["done_cases"] += n["done_cases"]
                m["ok_count"] += n["ok_count"]
                m["silent"] += 1 if n["silent"] else 0
                for k, v in c.items():
                    m["mismatch_classes"][k] = m["mismatch_classes"].get(k, 0) + v
        return {
            "generated_at": time.time(),
            "nodes": out_nodes,
            "cpu_models": models,
            "silent_nodes": [n["host"] for n in out_nodes if n["silent"]],
            "events": [{**e, "ts": e["ts"].isoformat()} for e in events],
        }

    @app.get("/status.json", dependencies=[Depends(auth)])
    def status_json():
        return JSONResponse(build_status())

    @app.get("/status", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def status_page(request: Request):
        s = build_status()
        return HTMLResponse(render_status(s, request.url.query))

    @app.get("/mismatches", dependencies=[Depends(auth)])
    def mismatches(class_: Optional[str] = Query(None, alias="class"), host: Optional[str] = None,
                   insn: Optional[str] = None, limit: int = 100, offset: int = 0):
        limit = max(1, min(limit, 2000))
        where, params = [], []
        if class_:
            where.append("r.class = %s"); params.append(class_)
        if host:
            where.append("n.host = %s"); params.append(host)
        if insn:
            where.append("r.instruction ILIKE %s"); params.append(f"%{insn}%")
        sql = """SELECT r.id, n.host, n.cpu_model, r.batch_id, r.test_case_id, r.state_index,
                        r.instruction, r.status, r.class, r.diff_keys, r.got_final_state,
                        r.got_exception_kind, r.expected_final_state, r.expected_exception_kind,
                        r.created_at
                 FROM g5k_results r JOIN g5k_nodes n USING (node_id)"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.id DESC LIMIT %s OFFSET %s"
        with get_pool().connection() as conn:
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return JSONResponse([{**r, "created_at": r["created_at"].isoformat()} for r in rows])

    return app


def _dur(s) -> str:
    if s is None:
        return "?"
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def render_status(s: dict, query: str = "") -> str:
    e = html.escape
    def cls_str(c):
        return ", ".join(f"{e(k)}={v}" for k, v in sorted(c.items())) or "-"
    rows = []
    for n in s["nodes"]:
        warn = n["silent"]
        pct = f"{100 * n['done_cases'] / n['total_cases']:.1f}%" if n["total_cases"] else "?"
        rate = f"{n['rate_cases_per_s']:.1f}/s" if n["rate_cases_per_s"] else "?"
        rows.append(
            f"<tr class='{'warn' if warn else ''}'><td>{e(n['host'])}{' SILENT' if warn else ''}</td>"
            f"<td>{e(str(n['cpu_model']))}</td><td>{e(str(n['save_mode'] or '?'))}</td><td>{e(n['state'])}</td>"
            f"<td>{n['done_cases']} / {n['total_cases'] if n['total_cases'] is not None else '?'} ({pct})</td>"
            f"<td>{rate}</td><td>{_dur(n['eta_s'])}</td><td>{cls_str(n['mismatch_classes'])}</td>"
            f"<td>{n['qemu_restarts']}</td><td>{_dur(n['last_seen_ago_s'])} ago</td></tr>"
        )
    models = "".join(
        f"<tr><td>{e(k)}</td><td>{m['nodes']}</td><td>{m['done_cases']}</td><td>{m['ok_count']}</td>"
        f"<td>{cls_str(m['mismatch_classes'])}</td><td>{m['silent']}</td></tr>"
        for k, m in sorted(s["cpu_models"].items())
    )
    evs = "".join(
        f"<tr><td>{e(ev['ts'])}</td><td>{e(ev['kind'])}</td><td>{e(str(ev['host'] or ''))}</td>"
        f"<td>{e(str(ev['detail']))}</td></tr>" for ev in s["events"]
    )
    banner = (f"<p class='warn'>Silent nodes: {e(', '.join(s['silent_nodes']))}</p>"
              if s["silent_nodes"] else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10;url=/status{('?' + e(query)) if query else ''}">
<title>Grid5000Binit status</title>
<style>body{{font-family:sans-serif;margin:1em}}table{{border-collapse:collapse;margin-bottom:1.5em}}
td,th{{border:1px solid #ccc;padding:2px 8px;text-align:left;font-size:13px}}
.warn{{background:#fdd;color:#900}}td{{font-family:monospace}}</style></head><body>
<h1>Grid5000Binit controller</h1>{banner}
<h2>Nodes</h2><table><tr><th>host</th><th>cpu</th><th>save</th><th>state</th><th>progress</th><th>rate</th>
<th>ETA</th><th>mismatches</th><th>qemu restarts</th><th>last seen</th></tr>{''.join(rows)}</table>
<h2>CPU models</h2><table><tr><th>model</th><th>nodes</th><th>done</th><th>ok</th><th>mismatches</th><th>silent</th></tr>{models}</table>
<h2>Recent events</h2><table><tr><th>time</th><th>kind</th><th>host</th><th>detail</th></tr>{evs}</table>
<p><a href="/status.json?{e(query)}">status.json</a> | <a href="/mismatches?{e(query)}">mismatches</a></p>
</body></html>"""
