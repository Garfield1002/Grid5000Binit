"""What each view shows, as plain dicts (the .json twins return them as they are). Every query here
reads the summary table or probes g5k_results through its (node_id, test_case_id, state_index) index."""
import json
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

GRID_PAGE = 100


@dataclass(frozen=True)
class Filter:
    """Classes and diff keys switched off. A summary row is hidden when its class is off, or when
    it has diff keys and all of them are off."""
    xc: frozenset = frozenset()
    xk: frozenset = frozenset()

    @classmethod
    def parse(cls, xc: str | None, xk: str | None) -> "Filter":
        split = lambda s: frozenset(x for x in (s or "").split(",") if x)
        return cls(split(xc), split(xk))

    def keep(self, klass: str, keys) -> bool:
        if klass in self.xc:
            return False
        return not keys or any(k not in self.xk for k in keys)

    def query(self, **extra) -> str:
        """The filter as a query string, with a leading '?' (empty when there is nothing to say)."""
        parts = [f"{k}={v}" for k, v in (("xc", ",".join(sorted(self.xc))),
                                         ("xk", ",".join(sorted(self.xk)))) if v]
        parts += [f"{k}={v}" for k, v in extra.items() if v not in (None, "", False)]
        return "?" + "&".join(parts) if parts else ""

    def as_dict(self) -> dict:
        return {"hidden_classes": sorted(self.xc), "hidden_keys": sorted(self.xk)}


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def load_uarch(path: Path | None = None) -> dict:
    """normalised cpu_model -> {label, vendor, uarch, year, order}."""
    path = path or Path(__file__).with_name("uarch.toml")
    with open(path, "rb") as f:
        models = tomllib.load(f).get("model", [])
    return {_norm(m["cpu_model"]): {"label": m["label"], "vendor": m["vendor"], "uarch": m["uarch"],
                                    "year": m.get("year"), "order": i} for i, m in enumerate(models)}


class Store:
    """Small in-process caches over the tables that change slowly."""

    def __init__(self, uarch: dict | None = None):
        self.uarch = uarch if uarch is not None else load_uarch()
        self.lock = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.cache: dict = {}

    def cached(self, name: str, ttl: float, fn, key=None):
        """fn() at most once per ttl; with a key, the value is also kept while the key is unchanged."""
        with self.lock:
            lock = self.locks.setdefault(name, threading.Lock())
        with lock:  # one lock per name: a loader may use another cached value
            c = self.cache.get(name)
            now = time.monotonic()
            if c and (now - c[0] < ttl or (key is not None and c[1] == key)):
                return c[2]
            v = fn()
            self.cache[name] = (now, key, v)
            return v

    # ── slow-changing inputs ──────────────────────────────────────────

    def watermark(self, conn) -> dict:
        return self.cached("watermark", 5, lambda: conn.execute("SELECT * FROM watermark").fetchone())

    def coverage(self, conn) -> dict:
        w = self.watermark(conn)
        tip, done = w["tip_id"], w["result_id"]
        return {"result_id": done, "tip_id": tip, "paused": w["paused"],
                "percent": 100.0 if tip <= 0 else min(100.0, 100.0 * done / tip),
                # ids, not rows: the two differ by the gaps in the id sequence
                "caught_up": tip - done <= max(50000, tip // 1000)}

    def corpus(self, conn) -> dict:
        def load():
            ins = {r["id"]: r for r in conn.execute("SELECT id, name, url FROM instructions")}
            tcs = {r["test_case_id"]: (r["instruction_id"], r["n"])
                   for r in conn.execute("SELECT test_case_id, instruction_id, n FROM tc_states")}
            states: dict[int, int] = {}
            span: dict[int, tuple] = {}  # first and last test case that is run, per instruction
            for tc, (iid, n) in tcs.items():
                states[iid] = states.get(iid, 0) + n
                if n:
                    lo, hi = span.get(iid, (tc, tc))
                    span[iid] = (min(lo, tc), max(hi, tc))
            # The controller's own count (states that have a reference result), when it is there.
            if conn.execute("SELECT to_regclass('g5k_instruction_states') IS NOT NULL AS ok").fetchone()["ok"]:
                for r in conn.execute("SELECT instruction_id, n FROM g5k_instruction_states"):
                    states[r["instruction_id"]] = r["n"]
            feats: dict[int, frozenset] = {}
            for r in conn.execute("SELECT instruction_id, array_agg(feature) AS f "
                                  "FROM instruction_features GROUP BY 1"):
                feats[r["instruction_id"]] = frozenset(r["f"])
            return {"instructions": ins, "by_name": {i["name"]: i for i in ins.values()},
                    "tcs": tcs, "states": states, "span": span, "features": feats}
        return self.cached("corpus", 600, load)

    def models(self, conn) -> list[dict]:
        """One entry per CPU model, in column order. A run that changed host is spread over several
        nodes; the model's cursor and counters are those of its most advanced node."""
        def load():
            corpus = self.corpus(conn)
            by: dict[str, dict] = {}
            for n in conn.execute(
                    """SELECT node_id, host, cluster, cpu_model, microcode, save_mode, features, state,
                              cursor_tc, cursor_si, done_cases FROM g5k_nodes ORDER BY node_id"""):
                key = n["cpu_model"] or "?"
                u = self.uarch.get(_norm(key), {})
                m = by.setdefault(key, {
                    "cpu_model": key, "label": u.get("label", key), "vendor": u.get("vendor", "?"),
                    "uarch": u.get("uarch", "?"), "year": u.get("year"),
                    "order": u.get("order", 10**6), "clusters": [],
                    "node_ids": [], "microcodes": [], "done": False, "cursor": (0, -1),
                    "done_cases": 0, "features": frozenset(), "save_mode": None})
                m["node_ids"].append(n["node_id"])
                for k, v in (("clusters", n["cluster"]), ("microcodes", n["microcode"])):
                    if v and v not in m[k]:
                        m[k].append(v)
                m["done"] = m["done"] or n["state"] == "done"
                if (n["cursor_tc"], n["cursor_si"]) >= m["cursor"]:
                    m["cursor"] = (n["cursor_tc"], n["cursor_si"])
                    m["features"], m["save_mode"] = frozenset(n["features"]), n["save_mode"]
                m["done_cases"] = max(m["done_cases"], n["done_cases"])
            for m in by.values():
                total = sum(n for iid, n in corpus["states"].items()
                            if corpus["features"].get(iid, frozenset()) <= m["features"])
                m["eligible_states"] = total
                m["progress"] = 1.0 if m["done"] else (min(1.0, m["done_cases"] / total) if total else 0.0)
            return sorted(by.values(), key=lambda m: (m["order"], m["cpu_model"]))
        return self.cached("models", 30, load)

    def matrix_counts(self, conn) -> list[dict]:
        """(instruction, model, class, diff keys) -> states: the whole summary, rolled up. The one
        read of all of explorer.summary, redone when the watermark has moved, at most once a minute."""
        w = self.watermark(conn)

        def load():
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = '60s'")
                return conn.execute(
                    """SELECT t.instruction_id, g.cpu_model, s.class, s.diff_keys, sum(s.n)::bigint AS n
                       FROM summary s JOIN g5k_nodes g USING (node_id)
                       JOIN tc_states t USING (test_case_id)
                       GROUP BY 1, 2, 3, 4""").fetchall()
        return self.cached("matrix", 60, load, key=(w["result_id"], w["replay_applied_at"]))


def model_json(m: dict) -> dict:
    return {k: m[k] for k in ("cpu_model", "label", "vendor", "uarch", "year", "clusters", "microcodes",
                              "save_mode", "done", "progress", "eligible_states")}


def supports(model: dict, corpus: dict, iid: int) -> bool:
    return corpus["features"].get(iid, frozenset()) <= model["features"]


def tc_status(model: dict, corpus: dict, tc: int, iid: int, si: int | None = None) -> str:
    """unsupported | run | running (the run's cursor is inside this test case) | notrun."""
    if not supports(model, corpus, iid):
        return "unsupported"
    ctc, csi = model["cursor"]
    if model["done"] or tc < ctc or (si is not None and (tc, si) <= (ctc, csi)):
        return "run"
    return "running" if tc == ctc and si is None else "notrun"


def ins_status(model: dict, corpus: dict, iid: int) -> str:
    """tc_status for a whole instruction: running when the run's cursor is among its test cases."""
    if not supports(model, corpus, iid):
        return "unsupported"
    span = corpus["span"].get(iid)
    if model["done"] or span is None or span[1] < model["cursor"][0]:
        return "run"
    return "notrun" if span[0] > model["cursor"][0] else "running"


def _cell(rows: list[tuple], flt: Filter) -> dict | None:
    """rows: (class, diff_keys, n) of one cell. Returns the cell after filtering."""
    classes: dict[str, int] = {}
    keys: dict[str, int] = {}
    for klass, dk, n in rows:
        if not flt.keep(klass, dk):
            continue
        classes[klass] = classes.get(klass, 0) + n
        for k in dk:
            keys[k] = keys.get(k, 0) + n
    if not classes:
        return None
    return {"n": sum(classes.values()), "classes": classes, "keys": keys}


def _inventory(counts) -> dict:
    """Classes and diff keys present, with their state counts, for the filter controls."""
    classes: dict[str, int] = {}
    keys: dict[str, int] = {}
    for klass, dk, n in counts:
        classes[klass] = classes.get(klass, 0) + n
        for k in dk:
            keys[k] = keys.get(k, 0) + n
    return {"classes": classes, "keys": dict(sorted(keys.items(), key=lambda kv: -kv[1]))}


def matrix(store: Store, conn, flt: Filter, show_all: bool = False) -> dict:
    corpus, models = store.corpus(conn), store.models(conn)
    cells: dict[tuple, list] = {}
    counts = store.matrix_counts(conn)
    for r in counts:
        cells.setdefault((r["instruction_id"], r["cpu_model"]), []).append(
            (r["class"], r["diff_keys"], r["n"]))
    rows = []
    for ins in sorted(corpus["instructions"].values(), key=lambda i: i["name"]):
        states = corpus["states"].get(ins["id"], 0)
        row, any_n = {}, False
        for m in models:
            st = ins_status(m, corpus, ins["id"])
            c = None if st == "unsupported" else _cell(cells.get((ins["id"], m["cpu_model"]), []), flt)
            if c:
                any_n = True
                c["share"] = c["n"] / states if states else None
                if st != "run":
                    c["status"] = st
                row[m["cpu_model"]] = c
            elif st != "run":
                row[m["cpu_model"]] = {"status": st}
        if any_n or show_all:
            rows.append({"instruction": ins["name"], "instruction_id": ins["id"], "states": states,
                         "cells": row})
    return {"coverage": store.coverage(conn), "filter": flt.as_dict(), "show_all": show_all,
            "inventory": _inventory((r["class"], r["diff_keys"], r["n"]) for r in counts),
            "models": [model_json(m) for m in models], "rows": rows,
            "instructions_total": len(corpus["instructions"])}


def _summary_by_model(conn, models: list[dict], tcs: list[int]) -> dict:
    """(test_case_id, cpu_model) -> [(class, diff_keys, n, sig)], from the summary rows of the
    model's nodes."""
    node_model = {nid: m["cpu_model"] for m in models for nid in m["node_ids"]}
    out: dict[tuple, list] = {}
    for r in conn.execute("SELECT node_id, test_case_id, class, diff_keys, n, sig FROM summary "
                          "WHERE test_case_id = ANY(%s)", (tcs,)):
        model = node_model.get(r["node_id"])
        if model is not None:
            out.setdefault((r["test_case_id"], model), []).append(
                (r["class"], r["diff_keys"], r["n"], r["sig"]))
    return out


def instruction(store: Store, conn, name: str, flt: Filter, show_all: bool = False) -> dict | None:
    corpus, models = store.corpus(conn), store.models(conn)
    ins = corpus["by_name"].get(name)
    if ins is None:
        return None
    tcs = conn.execute("SELECT id, instruction, opcode FROM test_cases WHERE instruction_id = %s "
                       "ORDER BY id", (ins["id"],)).fetchall()
    summ = _summary_by_model(conn, models, [t["id"] for t in tcs])
    rows, inv = [], []
    for t in tcs:
        states = corpus["tcs"].get(t["id"], (ins["id"], 0))[1]
        row, any_n = {}, False
        for m in models:
            st = tc_status(m, corpus, t["id"], ins["id"])
            got = summ.get((t["id"], m["cpu_model"]), [])
            inv += [(k, dk, n) for k, dk, n, _ in got]
            c = _cell([(k, dk, n) for k, dk, n, _ in got], flt)
            if c:
                any_n = True
                c["share"] = c["n"] / states if states else None
                if st != "run":
                    c["status"] = st
                row[m["cpu_model"]] = c
            elif st != "run":
                row[m["cpu_model"]] = {"status": st}
        if any_n or show_all:
            rows.append({"test_case_id": t["id"], "instruction": t["instruction"],
                         "opcode": t["opcode"], "states": states, "cells": row})
    return {"coverage": store.coverage(conn), "filter": flt.as_dict(), "show_all": show_all,
            "inventory": _inventory(inv), "name": ins["name"], "url": ins["url"],
            "instruction_id": ins["id"], "test_cases_total": len(tcs),
            "models": [model_json(m) for m in models], "rows": rows}


GROUP_ORDER = {"mismatch": 0, "match": 1, "running": 2, "notrun": 3, "unsupported": 4}


def _tc_header(store: Store, conn, tc: int) -> dict | None:
    t = conn.execute("SELECT id, instruction, opcode, instruction_id FROM test_cases WHERE id = %s",
                     (tc,)).fetchone()
    if t is None:
        return None
    corpus = store.corpus(conn)
    ins = corpus["instructions"].get(t["instruction_id"], {})
    return {"test_case_id": t["id"], "instruction": t["instruction"], "opcode": t["opcode"],
            "instruction_id": t["instruction_id"], "mnemonic": ins.get("name"), "url": ins.get("url"),
            "states": corpus["tcs"].get(t["id"], (0, 0))[1]}


def test_case(store: Store, conn, tc: int, flt: Filter, after: int = -1) -> dict | None:
    head = _tc_header(store, conn, tc)
    if head is None:
        return None
    corpus, models = store.corpus(conn), store.models(conn)
    summ = _summary_by_model(conn, models, [tc])
    groups: dict[tuple, dict] = {}
    inv = []
    for m in models:
        st = tc_status(m, corpus, tc, head["instruction_id"])
        got = summ.get((tc, m["cpu_model"]), [])
        inv += [(k, dk, n) for k, dk, n, _ in got]
        kept = [g for g in got if flt.keep(g[0], g[1])]
        sig = 0
        for g in kept:
            sig ^= g[3]
        n = sum(g[2] for g in kept)
        if st == "run":
            kind = "mismatch" if n else "match"
        else:
            kind = st
        # Same signature and same count: the models got the same values on the same states.
        key = (kind, sig, n) if n else (kind, 0, 0)
        g = groups.setdefault(key, {"kind": kind, "n": n, "models": [], "node_ids": m["node_ids"],
                                    "breakdown": _cell([(k, dk, c) for k, dk, c, _ in kept], flt)})
        g["models"].append(m["label"])
    ordered = sorted(groups.values(), key=lambda g: (GROUP_ORDER[g["kind"]], -g["n"], g["models"]))
    for i, g in enumerate(ordered):
        g["id"] = i + 1

    # State x group grid, from one representative model per group that has mismatches.
    shown = [g for g in ordered if g["n"]]
    fetched, cut = {}, None
    for g in shown:
        rows = conn.execute(
            """SELECT r.* FROM unnest(%s::bigint[]) AS n(node_id)
               CROSS JOIN LATERAL (
                   SELECT state_index, class, diff_keys, got_exception_kind, expected_exception_kind
                   FROM g5k_results
                   WHERE node_id = n.node_id AND test_case_id = %s AND state_index > %s
                   ORDER BY state_index LIMIT %s) r
               ORDER BY r.state_index LIMIT %s""",
            (g["node_ids"], tc, after, GRID_PAGE, GRID_PAGE)).fetchall()
        fetched[g["id"]] = rows
        # A group whose page is full may have more states below its last one: the page ends there.
        if len(rows) == GRID_PAGE:
            cut = rows[-1]["state_index"] if cut is None else min(cut, rows[-1]["state_index"])
    grid: dict[int, dict] = {}
    for gid, rows in fetched.items():
        for r in rows:
            if (cut is None or r["state_index"] <= cut) and flt.keep(r["class"], r["diff_keys"]):
                grid.setdefault(r["state_index"], {})[gid] = {
                    "class": r["class"], "diff_keys": r["diff_keys"],
                    "got_exception_kind": r["got_exception_kind"],
                    "expected_exception_kind": r["expected_exception_kind"]}
    for g in ordered:
        del g["node_ids"]
    return {"coverage": store.coverage(conn), "filter": flt.as_dict(), "inventory": _inventory(inv),
            **head, "groups": ordered, "after": after, "next_after": cut,
            "grid": [{"state_index": si, "groups": grid[si]} for si in sorted(grid)]}


def state(store: Store, conn, tc: int, si: int, show_all: bool = False) -> dict | None:
    head = _tc_header(store, conn, tc)
    if head is None:
        return None
    corpus, models = store.corpus(conn), store.models(conn)
    ref = conn.execute(
        """SELECT (SELECT initial_states -> %(si)s::int FROM test_cases WHERE id = %(tc)s) AS initial,
                  tr.final_state, tr.exception_kind
           FROM (SELECT 1) one LEFT JOIN test_results tr
                ON tr.test_case_id = %(tc)s AND tr.state_index = %(si)s""",
        {"tc": tc, "si": si}).fetchone()
    if ref["initial"] is None:
        return None
    node_model = {nid: m for m in models for nid in m["node_ids"]}
    rows = conn.execute(
        """SELECT node_id, class, diff_keys, got_final_state, got_exception_kind FROM g5k_results
           WHERE node_id = ANY(%s) AND test_case_id = %s AND state_index = %s""",
        (list(node_model), tc, si)).fetchall()
    by_model = {node_model[r["node_id"]]["cpu_model"]: r for r in rows}
    groups: dict[tuple, dict] = {}
    for m in models:
        r = by_model.get(m["cpu_model"])
        if r is not None:
            key = ("mismatch", json.dumps(r["got_final_state"], sort_keys=True), r["got_exception_kind"])
            g = groups.setdefault(key, {"kind": "mismatch", "models": [], "class": r["class"],
                                        "diff_keys": r["diff_keys"], "final_state": r["got_final_state"],
                                        "exception_kind": r["got_exception_kind"]})
        else:
            st = tc_status(m, corpus, tc, head["instruction_id"], si)
            kind = "match" if st == "run" else st
            g = groups.setdefault((kind,), {"kind": kind, "models": []})
        g["models"].append(m["label"])
    ordered = sorted(groups.values(), key=lambda g: (GROUP_ORDER[g["kind"]], -len(g["models"]), g["models"]))
    for i, g in enumerate(ordered):
        g["id"] = i + 1
    expected = ref["final_state"] or {}
    differing = sorted({k for g in ordered for k in g.get("diff_keys", [])})
    keys = differing
    if show_all:
        keys = sorted(set(expected) | set(ref["initial"]) | set(differing)
                      | {k for g in ordered for k in (g.get("final_state") or {})})
    return {"coverage": store.coverage(conn), **head, "state_index": si, "show_all": show_all,
            "initial_state": ref["initial"], "expected_final_state": ref["final_state"],
            "expected_exception_kind": ref["exception_kind"], "groups": ordered, "keys": keys,
            "differing_keys": differing}


def checks(store: Store, conn) -> dict:
    rows = conn.execute("SELECT name, ok, detail, checked_at FROM checks ORDER BY name").fetchall()
    return {"coverage": store.coverage(conn),
            "checks": [{**r, "checked_at": r["checked_at"].isoformat()} for r in rows]}
