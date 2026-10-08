import dataclasses

import psycopg
import pytest

from explorer.jobs import connect
from explorer.schema import ensure

REPLAYS = """CREATE TABLE g5k_replays(id bigserial PRIMARY KEY, cluster text NOT NULL,
    test_case_id bigint NOT NULL, state_index int NOT NULL, status text NOT NULL DEFAULT 'pending',
    applied_at timestamptz)"""


def rebuilt(db):
    """The summary a backfill from zero would give for the rows as they are now."""
    db.conn.execute("TRUNCATE summary")
    db.conn.execute("UPDATE watermark SET result_id = 0, replay_applied_at = NULL")
    db.run_job()
    return db.summary()


def test_ensure_refuses_a_missing_schema(db):
    cfg = dataclasses.replace(db.cfg, schema="no_such_schema")
    with connect(cfg, 5) as conn, pytest.raises(RuntimeError):
        ensure(conn, "no_such_schema")


def test_tc_states(db):
    db.run_job()
    rows = db.conn.execute("SELECT test_case_id, n FROM tc_states ORDER BY 1").fetchall()
    assert [(r["test_case_id"], r["n"]) for r in rows] == [(1, 4), (2, 4), (3, 4), (4, 0)]


def test_fold_in_chunks_matches_one_pass(db):
    for si in range(4):
        db.result(1, 1, si)
        db.result(3, 1, si, keys=("flag",), klass="undef_flags_only")
    db.result(3, 2, 0, klass="exception_mismatch", keys=(), exc="UD")
    db.run_job()  # chunk=3: several steps
    s = db.summary()
    assert s[(1, 1, "defined_state", ("x87_dp",))][0] == 4
    assert s[(3, 1, "undef_flags_only", ("flag",))][0] == 4
    assert s[(3, 2, "exception_mismatch", ())][0] == 1
    w = db.conn.execute("SELECT * FROM watermark").fetchone()
    assert w["result_id"] == w["tip_id"] == 9
    db.cfg.chunk = 1000
    assert rebuilt(db) == s


def test_signature_does_not_depend_on_the_node_split(db):
    # the same four observations: all on node 3, or split over the two hosts of the Zen run
    for si in range(4):
        db.result(3, 1, si)
        db.result(1 if si < 2 else 2, 1, si)
    db.result(5, 1, 0)
    db.result(5, 1, 1, got={"rax": 9, "flag": 2, "x87_dp": 0})
    db.run_job()
    s = db.summary()
    sky = s[(3, 1, "defined_state", ("x87_dp",))]
    assert s[(1, 1, "defined_state", ("x87_dp",))][1] ^ s[(2, 1, "defined_state", ("x87_dp",))][1] == sky[1]
    assert s[(5, 1, "defined_state", ("x87_dp",))][1] != sky[1]


def test_fold_is_incremental_and_resumes(db):
    db.result(1, 1, 0)
    job = db.run_job()
    db.result(1, 1, 1)
    db.run_job(job)
    assert db.summary()[(1, 1, "defined_state", ("x87_dp",))][0] == 2
    db.result(1, 1, 2)
    db.run_job()  # a new process: starts from the stored watermark, counts nothing twice
    s = db.summary()
    assert s[(1, 1, "defined_state", ("x87_dp",))][0] == 3
    assert rebuilt(db) == s


def test_pause(db):
    db.run_job()
    db.conn.execute("UPDATE watermark SET paused = true")
    db.result(1, 1, 0)
    db.run_job()
    assert db.summary() == {}
    db.conn.execute("UPDATE watermark SET paused = false")
    db.run_job()
    assert len(db.summary()) == 1


def test_rows_of_an_open_transaction_are_waited_for(db):
    """An id below max(id) whose transaction is still open must not be skipped."""
    job = db.run_job()
    with psycopg.connect(db.cfg.dsn, **db.cfg.conn_kwargs(30)) as other:
        other.execute("INSERT INTO g5k_results (node_id, test_case_id, state_index, class) "
                      "VALUES (3, 1, 0, 'crash')")          # id 1, not committed
        db.result(1, 1, 1)                                   # id 2, committed
        db.run_job(job)
        assert db.conn.execute("SELECT result_id FROM watermark").fetchone()["result_id"] == 0
        other.commit()
    db.run_job(job)
    db.run_job(job)
    assert db.conn.execute("SELECT result_id FROM watermark").fetchone()["result_id"] == 2
    assert len(db.summary()) == 2


def test_replay_overwrite_and_delete(db):
    db.conn.execute(REPLAYS)
    ids = [db.result(1, 1, si) for si in range(3)]
    db.result(2, 1, 3)
    db.result(3, 1, 0)
    job = db.run_job()
    before = db.summary()
    # what `controller replay apply` does: delete one row, overwrite another in place
    db.conn.execute("DELETE FROM g5k_results WHERE id = %s", (ids[0],))
    db.conn.execute("""UPDATE g5k_results SET class = 'undef_flags_only', diff_keys = '{flag}',
                       got_final_state = '{"flag": 3}' WHERE id = %s""", (ids[1],))
    db.conn.execute("INSERT INTO g5k_replays (cluster, test_case_id, state_index, status, applied_at) "
                    "VALUES ('zen', 1, 0, 'applied', now()), ('zen', 1, 1, 'applied', now())")
    db.result(2, 2, 0)  # an unrelated new row, above the watermark
    db.run_job(job)
    s = db.summary()
    assert s[(1, 1, "defined_state", ("x87_dp",))][0] == 1
    assert s[(1, 1, "undef_flags_only", ("flag",))][0] == 1
    assert s[(3, 1, "defined_state", ("x87_dp",))] == before[(3, 1, "defined_state", ("x87_dp",))]
    assert s[(2, 2, "defined_state", ("x87_dp",))][0] == 1
    assert db.conn.execute("SELECT replay_applied_at FROM watermark").fetchone()["replay_applied_at"]
    db.run_job(job)  # handled once
    assert db.summary() == s
    assert rebuilt(db) == s


def test_checks(db):
    db.conn.execute("CREATE TABLE g5k_class_counts(node_id bigint, class text, n bigint, PRIMARY KEY (node_id, class))")
    db.conn.execute("INSERT INTO g5k_class_counts VALUES (2, 'defined_state', 2), (3, 'defined_state', 5)")
    db.result(1, 1, 0)
    db.result(2, 1, 0)  # the same state on a second host of the Zen run
    db.result(2, 1, 1)
    db.result(3, 1, 0)
    db.run_job()
    c = {r["name"]: r for r in db.conn.execute("SELECT * FROM checks")}
    assert not c["class_counts"]["ok"]
    assert {(d["node_id"], d["summary"], d["counts"]) for d in c["class_counts"]["detail"]["differences"]} \
        == {(1, 1, None), (3, 1, 5)}
    assert not c["node_overlap"]["ok"]
    assert c["node_overlap"]["detail"]["overlaps"][0]["states_on_two_nodes"] == 1
