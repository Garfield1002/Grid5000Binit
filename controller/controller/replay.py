"""Replay of chosen inputs on a cluster.

A replay is requested for (cluster, test_case_id, state_index) and served through /batch, ahead of
the cursor, to any node of that cluster. Its outcome is only recorded in g5k_replays: `report`
compares it with the rows the cluster's nodes hold in g5k_results and proposes an action, and
`apply` carries the accepted ones out."""
from psycopg.types.json import Jsonb

from .classify import classify, diff_keys

ACTIONS = ("delete", "overwrite", "insert")
MAX_REPEAT = 1000

# Same columns as BATCH_SQL. The states are picked out of initial_states once per test case.
CASES_SQL = """
SELECT tc.id AS test_case_id, s.idx AS state_index, tc.instruction, tc.opcode,
       tc.instruction_id, s.initial_state,
       tr.final_state, tr.exception_kind,
       COALESCE((SELECT array_agg(f.feature ORDER BY f.feature)
                 FROM instruction_features f WHERE f.instruction_id = tc.instruction_id),
                ARRAY[]::text[]) AS required_features
FROM (SELECT q.tc, array_agg(q.si) AS sis
      FROM unnest(%s::bigint[], %s::int[]) AS q(tc, si) GROUP BY q.tc) w
JOIN test_cases tc ON tc.id = w.tc
CROSS JOIN LATERAL (
    SELECT (e.ord - 1)::int AS idx, e.value AS initial_state
    FROM jsonb_array_elements(tc.initial_states) WITH ORDINALITY AS e(value, ord)
    WHERE (e.ord - 1)::int = ANY(w.sis)) s
JOIN test_results tr ON tr.test_case_id = tc.id AND tr.state_index = s.idx
"""

# The latest replay of each input, kept when it is finished and not applied yet.
LATEST_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (p.cluster, p.test_case_id, p.state_index)
           p.id, p.cluster, p.test_case_id, p.state_index, p.repeat, p.status,
           p.batch_id, p.node_id, p.worker_version, p.runs, n.host, n.save_mode,
           tc.instruction, tr.final_state, tr.exception_kind,
           (SELECT array_agg(u.flag) FROM instruction_undefined_flags u
            WHERE u.instruction_id = tc.instruction_id) AS undefined_flags
    FROM g5k_replays p
    LEFT JOIN g5k_nodes n ON n.node_id = p.node_id
    LEFT JOIN test_cases tc ON tc.id = p.test_case_id
    LEFT JOIN test_results tr ON tr.test_case_id = p.test_case_id
                             AND tr.state_index = p.state_index
    WHERE p.status IN ('done', 'applied') AND (%s::text IS NULL OR p.cluster = %s)
    ORDER BY p.cluster, p.test_case_id, p.state_index, p.id DESC) latest
WHERE status = 'done'
ORDER BY cluster, test_case_id, state_index
"""

STORED_SQL = """
SELECT q.cluster, r.id, r.node_id, r.test_case_id, r.state_index, r.status, r.class,
       r.got_final_state, r.got_exception_kind
FROM unnest(%s::text[], %s::bigint[], %s::int[]) AS q(cluster, tc, si)
JOIN g5k_nodes n ON n.cluster = q.cluster
JOIN g5k_results r ON r.node_id = n.node_id AND r.test_case_id = q.tc AND r.state_index = q.si
ORDER BY r.id
"""

OBS = ("status", "got_final_state", "got_exception_kind")


def key(r) -> tuple[int, int]:
    """The input a row is about."""
    return r["test_case_id"], r["state_index"]


def obs(r) -> tuple:
    """What was observed for an input, in the order of OBS."""
    return tuple(r[k] for k in OBS)


def columns(rows, *names) -> tuple[list, ...]:
    """One list per name: the parallel arrays an unnest() takes."""
    return tuple([r[n] for r in rows] for n in names)


def add(conn, cluster: str, tc: int, si: int | None = None, repeat: int = 1,
        dry_run: bool = False) -> tuple[int, int]:
    """Queue one state of a test case, or all of them, for a cluster. Returns (queued, matching):
    a state already waiting for that cluster is not queued twice, and only the states that have
    an expected result can be replayed."""
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be between 1 and {MAX_REPEAT}")
    where = "tr.test_case_id = %s AND (%s::int IS NULL OR tr.state_index = %s)"
    matching = conn.execute(f"SELECT count(*) AS n FROM test_results tr WHERE {where}",
                            (tc, si, si)).fetchone()["n"]
    if dry_run:
        return 0, matching
    queued = conn.execute(
        f"""INSERT INTO g5k_replays (cluster, test_case_id, state_index, repeat)
            SELECT %s, tr.test_case_id, tr.state_index, %s FROM test_results tr
            WHERE {where} ORDER BY tr.state_index
            ON CONFLICT DO NOTHING""",
        (cluster, repeat, tc, si, si)).rowcount
    return queued, matching


def take(conn, node, size: int):
    """The next replay batch for this node's cluster, as (batch_id, cases), or None. The batch
    holds each input `repeat` times in a row."""
    cluster = node["cluster"]
    if not cluster:
        return None
    # Back in the queue: what this node was handed and did not report (it is asking again), and
    # what was handed to a node that has stopped since.
    conn.execute(
        """UPDATE g5k_replays p SET status = 'pending', batch_id = NULL, node_id = NULL
           WHERE p.cluster = %s AND p.status = 'issued'
             AND (p.node_id = %s OR EXISTS (SELECT 1 FROM g5k_nodes n WHERE n.node_id = p.node_id
                                            AND (n.silent OR n.state <> 'running')))""",
        (cluster, node["node_id"]))
    waiting = conn.execute(
        """SELECT id, test_case_id, state_index, repeat FROM g5k_replays
           WHERE cluster = %s AND status = 'pending' ORDER BY id LIMIT %s
           FOR UPDATE SKIP LOCKED""", (cluster, size)).fetchall()
    picked, n = [], 0
    for p in waiting:
        if picked and n + p["repeat"] > size:
            break
        picked.append(p)
        n += p["repeat"]
    if not picked:
        return None
    found = {key(r): r for r in conn.execute(
        CASES_SQL, columns(picked, "test_case_id", "state_index"))}
    cases, issued, gone = [], [], []
    for p in picked:
        case = found.get(key(p))
        if case is None:  # the input left the corpus since it was queued
            gone.append(p["id"])
            continue
        issued.append(p["id"])
        cases += [case] * p["repeat"]
    conn.execute("UPDATE g5k_replays SET status = 'done', finished_at = now(), runs = '[]' "
                 "WHERE id = ANY(%s)", (gone,))
    if not cases:
        return None
    bid = conn.execute(
        "INSERT INTO g5k_batches (node_id, n_cases, replay) VALUES (%s,%s,true) RETURNING batch_id",
        (node["node_id"], len(cases))).fetchone()["batch_id"]
    conn.execute("UPDATE g5k_replays SET status = 'issued', batch_id = %s, node_id = %s "
                 "WHERE id = ANY(%s)", (bid, node["node_id"], issued))
    return bid, cases


def record(conn, batch_id: int, node_id: int, ok_ids, mismatches) -> int:
    """Store what a node observed for the inputs of a replay batch; g5k_results is not touched.
    `runs` lists the distinct observations of an input with the number of times each was made."""
    seen: dict[tuple[int, int], list[dict]] = {}

    def note(k, o):
        runs = seen.setdefault(k, [])
        for r in runs:
            if obs(r) == o:
                r["n"] += 1
                return
        runs.append({"n": 1, **dict(zip(OBS, o))})

    for tc, si in ok_ids:
        note((tc, si), ("ok", None, None))
    for m in mismatches:
        note((m.test_case_id, m.state_index), (m.status, m.got_final_state, m.got_exception_kind))
    version = conn.execute("SELECT worker_version FROM g5k_nodes WHERE node_id = %s",
                           (node_id,)).fetchone()["worker_version"]
    replays = conn.execute(
        "SELECT id, test_case_id, state_index FROM g5k_replays "
        "WHERE batch_id = %s AND status = 'issued' FOR UPDATE", (batch_id,)).fetchall()
    for p in replays:
        conn.execute(
            """UPDATE g5k_replays SET status = 'done', finished_at = now(), runs = %s,
                   worker_version = %s WHERE id = %s""",
            (Jsonb(seen.get(key(p), [])), version, p["id"]))
    return len(replays)


def tally(runs) -> str:
    return ", ".join(f"{r['status']} x{r['n']}" for r in runs) or "no result"


def propose(runs, stored) -> tuple[str | None, str]:
    """(action, note) for one input, from the replay's runs and the rows stored for it on the
    cluster. The action is None when the replay matches what is stored or its runs disagree."""
    if not runs:
        return None, "no result"
    if len(runs) > 1:
        return None, "unstable"
    got = runs[0]
    if got["status"] == "ok":
        return ("delete", "") if stored else (None, "same")
    if not stored:
        return "insert", ""
    same = all(obs(s) == obs(got) for s in stored)
    return (None, "same") if same else ("overwrite", "")


def describe(p, stored) -> tuple[str | None, list[str], dict]:
    """(class, diff_keys, changed) of a replay whose runs agree, else (None, [], {}).
    `changed` maps a key to (stored value, replayed value)."""
    if len(p["runs"]) != 1:
        return None, [], {}
    status, state, exc = obs(p["runs"][0])
    if status == "ok":
        return "ok", [], {}
    cls, keys = classify(status, p["final_state"], p["exception_kind"], state, exc,
                         p["undefined_flags"] or [], p["save_mode"])
    changed: dict = {}
    for s in stored:
        old, new = s["got_final_state"] or {}, state or {}
        for k in diff_keys(old, new):
            changed.setdefault(k, (old.get(k), new.get(k)))
        if s["got_exception_kind"] != exc:
            changed.setdefault("exception", (s["got_exception_kind"], exc))
    return cls, keys, changed


def stored_rows(conn, replays) -> dict[tuple, list[dict]]:
    """The g5k_results rows the nodes of each replay's cluster hold for its input."""
    stored: dict[tuple, list[dict]] = {}
    for r in conn.execute(STORED_SQL, columns(replays, "cluster", "test_case_id", "state_index")):
        stored.setdefault((r["cluster"], *key(r)), []).append(r)
    return stored


def report(conn, cluster: str | None = None) -> list[dict]:
    """The latest finished replay of each input that has not been applied, with the stored rows
    it is compared with and the proposed action."""
    replays = conn.execute(LATEST_SQL, (cluster, cluster)).fetchall()
    stored = stored_rows(conn, replays)
    for p in replays:
        p["stored"] = stored.get((p["cluster"], *key(p)), [])
        p["action"], p["note"] = propose(p["runs"], p["stored"])
        p["class"], p["diff_keys"], p["changed"] = describe(p, p["stored"])
    return replays


def apply(conn, cluster: str, action: str, tc: int | None = None, insn: str | None = None) -> list[dict]:
    """Carry out one kind of proposed action on a cluster, on every row its nodes hold for the
    inputs concerned. Returns the replays applied."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    todo = [p for p in report(conn, cluster)
            if p["action"] == action and (tc is None or p["test_case_id"] == tc)
            and (insn is None or insn.lower() in (p["instruction"] or "").lower())]
    rows = 0
    for p in todo:
        ids = [s["id"] for s in p["stored"]]
        status, state, exc = obs(p["runs"][0])
        state = Jsonb(state) if state is not None else None
        # g5k_class_counts only follows inserts (trigger): rows leaving a class are taken off here.
        if action != "insert":
            for st in p["stored"]:
                conn.execute("UPDATE g5k_class_counts SET n = n - 1 WHERE node_id = %s AND class = %s",
                             (st["node_id"], st["class"]))
                # An emptied class leaves the table, or /status would show it as `crash=0`.
                conn.execute("DELETE FROM g5k_class_counts WHERE node_id = %s AND class = %s AND n <= 0",
                             (st["node_id"], st["class"]))
                if action == "overwrite":
                    conn.execute(
                        """INSERT INTO g5k_class_counts (node_id, class, n) VALUES (%s, %s, 1)
                           ON CONFLICT (node_id, class) DO UPDATE SET n = g5k_class_counts.n + 1""",
                        (st["node_id"], p["class"]))
        if action == "delete":
            rows += conn.execute("DELETE FROM g5k_results WHERE id = ANY(%s)", (ids,)).rowcount
        elif action == "overwrite":
            rows += conn.execute(
                """UPDATE g5k_results SET status = %s, class = %s, diff_keys = %s,
                       got_final_state = %s, got_exception_kind = %s WHERE id = ANY(%s)""",
                (status, p["class"], p["diff_keys"], state, exc, ids)).rowcount
        else:  # the row goes to the node that ran the replay
            rows += conn.execute(
                """INSERT INTO g5k_results (node_id, batch_id, test_case_id, state_index,
                       instruction, status, class, diff_keys, got_final_state,
                       got_exception_kind, expected_final_state, expected_exception_kind)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (node_id, test_case_id, state_index) DO NOTHING""",
                (p["node_id"], p["batch_id"], p["test_case_id"], p["state_index"], p["instruction"],
                 status, p["class"], p["diff_keys"], state, exc,
                 Jsonb(p["final_state"]) if p["final_state"] is not None else None,
                 p["exception_kind"])).rowcount
    conn.execute("UPDATE g5k_replays SET status = 'applied', applied_at = now(), action = %s "
                 "WHERE id = ANY(%s)", (action, [p["id"] for p in todo]))
    if todo:
        conn.execute("INSERT INTO g5k_events (kind, detail) VALUES ('replay_applied', %s)",
                     (Jsonb({"cluster": cluster, "action": action, "inputs": len(todo), "rows": rows}),))
    return todo


def counts(conn, cluster: str | None = None) -> dict[str, dict[str, int]]:
    """Replays per cluster and state (pending, issued, done, applied)."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
            "SELECT cluster, status, count(*) AS n FROM g5k_replays "
            "WHERE %s::text IS NULL OR cluster = %s GROUP BY 1, 2 ORDER BY 1", (cluster, cluster)):
        out.setdefault(r["cluster"], {})[r["status"]] = r["n"]
    return out
