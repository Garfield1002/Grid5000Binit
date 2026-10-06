"""Integration tests against a scratch Postgres schema.

Set G5K_TEST_DSN to a Postgres DSN to enable; otherwise skipped. Everything is
created inside a throwaway schema that is dropped afterwards."""
import os
import uuid

import pytest

DSN = os.environ.get("G5K_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="G5K_TEST_DSN not set")

H = {"Authorization": "Bearer tok"}


@pytest.fixture()
def client(monkeypatch):
    import psycopg
    from fastapi.testclient import TestClient
    from controller.app import create_app
    from controller.config import Config

    schema = "g5ktest_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"SET search_path={schema}")
        c.execute("""
        CREATE TABLE instructions(id serial primary key, name text unique, url text);
        CREATE TABLE instruction_undefined_flags(instruction_id int, flag text, primary key(instruction_id, flag));
        CREATE TABLE test_cases(id bigserial primary key, instruction text unique, opcode text,
            instruction_id int, status text, initial_states jsonb default '[]');
        CREATE TABLE test_results(id bigserial primary key, test_case_id bigint, state_index int,
            exception_kind text, final_state jsonb, unique(test_case_id, state_index));
        INSERT INTO instructions(id,name) VALUES (1,'ADD'),(2,'VADDPD'),(3,'BAD');
        INSERT INTO instruction_undefined_flags VALUES (1,'AF');
        INSERT INTO test_cases(id,instruction,opcode,instruction_id,status,initial_states) VALUES
          (1,'add rax, rbx','4801d8',1,'OK','[{"rax":1,"flag":2},{"rax":2,"flag":2}]'),
          (2,'vaddpd','c5f158c2',2,NULL,'[{"rax":1,"flag":2}]'),
          (3,'bad','00',3,'ERR: x','[{"rax":1,"flag":2}]'),
          (4,'add rbx, rax','4801c3',1,'OK','[{"rax":1,"flag":2},{"rax":1,"flag":2}]');
        INSERT INTO test_results(test_case_id,state_index,final_state) VALUES
          (1,0,'{"rax":1,"flag":2}'),(1,1,'{"rax":2,"flag":2}'),(2,0,'{"rax":1,"flag":2}'),
          (3,0,'{"rax":1,"flag":2}'),(4,0,'{"rax":1,"flag":2}');
        INSERT INTO test_results(test_case_id,state_index,exception_kind) VALUES (4,1,'UD');
        """)
    # No status cache: the tests read /status.json right after changing things.
    monkeypatch.setattr("controller.app.STATUS_CACHE_S", 0.0)
    # silent_after_s is large so the background loop never flags a node (and adds events) mid-test;
    # test_silent_detection calls check_silent itself.
    cfg = Config(dsn=DSN, token="tok", log_dir="/tmp", silent_after_s=3600.0,
                 conn_kwargs={"options": f"-csearch_path={schema}"})
    with TestClient(create_app(cfg)) as tc:
        # features table is part of migration; fill like compute-features would
        import controller.cli as cli
        cli.compute_features(DSN, {"options": f"-csearch_path={schema}"})
        yield tc
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


def register(client, feats):
    r = client.post("/register", headers=H, json={"host": "h1", "cpuid_features": feats,
                                                   "kernel_features": feats})
    assert r.status_code == 200
    return r.json()["node_id"]


def test_auth(client):
    assert client.post("/register", json={"host": "x"}).status_code == 401
    assert client.get("/mismatches").status_code == 401
    assert client.get("/mismatches?token=tok").status_code == 200
    # The status pages are public, and a failed auth leaves no trace in the database.
    assert client.get("/status").status_code == 200
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/status"
    assert client.get("/status.json").json()["events"] == []


def test_status_cache(client, monkeypatch):
    monkeypatch.setattr("controller.app.STATUS_CACHE_S", 60.0)
    assert client.get("/status.json").json()["nodes"] == []
    client.post("/register", headers=H, json={"host": "h9"})
    assert client.get("/status.json").json()["nodes"] == []  # still the cached answer


def test_feature_filtering_and_cursor(client):
    nid = register(client, ["SSE"])
    b = client.get(f"/batch?node_id={nid}&size=10", headers=H).json()
    got = [(c["test_case_id"], c["state_index"]) for c in b["cases"]]
    assert got == [(1, 0), (1, 1), (4, 0), (4, 1)]  # no AVX case, no ERR case

    nid2 = client.post("/register", headers=H, json={"host": "h2", "cpuid_features": ["AVX"],
                                                    "kernel_features": []}).json()["node_id"]
    b2 = client.get(f"/batch?node_id={nid2}&size=2", headers=H).json()
    assert [(c["test_case_id"], c["state_index"]) for c in b2["cases"]] == [(1, 0), (1, 1)]
    r = client.post("/results", headers=H, json={"node_id": nid2, "batch_id": b2["batch_id"],
                                                  "ok_count": 2, "elapsed_s": 1})
    assert r.status_code == 200
    b3 = client.get(f"/batch?node_id={nid2}&size=10", headers=H).json()
    assert [(c["test_case_id"], c["state_index"]) for c in b3["cases"]] == [(2, 0), (4, 0), (4, 1)]


def add_cases(client, counts, missing=()):
    """Add ADD test cases with ids from 10 and the given state counts; state i starts with rax=i and
    ends with rax=100+i. `missing` lists the (test_case_id, state_index) left without a result."""
    from psycopg.types.json import Jsonb
    with client.app.state.pool.connection() as conn:
        for tc, n in enumerate(counts, start=10):
            conn.execute(
                "INSERT INTO test_cases(id,instruction,opcode,instruction_id,status,initial_states)"
                " VALUES (%s,%s,'4801d8',1,'OK',%s)",
                (tc, f"add #{tc}", Jsonb([{"rax": i, "flag": 2} for i in range(n)])))
            for i in range(n):
                if (tc, i) not in missing:
                    conn.execute(
                        "INSERT INTO test_results(test_case_id,state_index,final_state) VALUES (%s,%s,%s)",
                        (tc, i, Jsonb({"rax": 100 + i, "flag": 2})))


def drain(client, host, size):
    """Every batch served to a fresh SSE node, as lists of (test_case_id, state_index, initial rax, final rax)."""
    nid = client.post("/register", headers=H, json={"host": host, "cpuid_features": ["SSE"],
                                                    "kernel_features": ["SSE"]}).json()["node_id"]
    batches = []
    while True:
        b = client.get(f"/batch?node_id={nid}&size={size}", headers=H).json()
        if not b["cases"]:
            return batches
        batches.append([(c["test_case_id"], c["state_index"], c["initial_state"]["rax"],
                         (c["expected"]["final_state"] or {}).get("rax")) for c in b["cases"]])
        client.post("/results", headers=H, json={"node_id": nid, "batch_id": b["batch_id"],
                                                  "ok_count": len(b["cases"])})


def test_batches_cover_the_corpus_in_order(client):
    # Large and small test cases, an empty one, and a run of more test cases than one lookup round.
    counts = [7, 0, 1, 1, 1, 1, 1, 1, 12, 2]
    add_cases(client, counts)
    want = [(tc, i, i, 100 + i) for tc, n in enumerate(counts, start=10) for i in range(n)]
    for size in (1, 2, 3, 5, 8, 100):
        batches = drain(client, f"h-{size}", size)
        got = [c for b in batches for c in b if c[0] >= 10]
        # Each state once, in order, with its own initial state and expected result.
        assert got == want, size
        assert all(len(b) == size for b in batches[:-1]), size


def test_batch_skips_states_without_result(client):
    add_cases(client, [4, 3], missing={(10, 1), (11, 0), (11, 2)})
    got = [c[:2] for b in drain(client, "h1", 100) for c in b if c[0] >= 10]
    assert got == [(10, 0), (10, 2), (10, 3), (11, 1)]


def test_results_classification_and_done(client):
    nid = register(client, ["SSE"])
    b = client.get(f"/batch?node_id={nid}&size=10", headers=H).json()
    body = {"node_id": nid, "batch_id": b["batch_id"], "ok_count": 1, "elapsed_s": 2,
            "mismatches": [
                {"test_case_id": 1, "state_index": 0, "got_final_state": {"rax": 1, "flag": 18}},
                {"test_case_id": 1, "state_index": 1, "got_final_state": {"rax": 2, "flag": 3}},
                {"test_case_id": 4, "state_index": 0, "got_final_state": None, "got_exception_kind": "GP"},
                {"test_case_id": 4, "state_index": 1, "status": "crash"}]}
    r = client.post("/results", headers=H, json=body).json()
    assert r["classes"] == {"undef_flags_only": 1, "defined_state": 1,
                            "exception_mismatch": 1, "crash": 1}
    assert client.post("/results", headers=H, json=body).json()["duplicate"] is True
    assert len(client.get("/mismatches?class=crash", headers=H).json()) == 1
    assert len(client.get("/mismatches?host=h1&insn=add", headers=H).json()) == 4
    assert client.get(f"/batch?node_id={nid}", headers=H).json() == {"batch_id": None, "cases": []}
    assert client.post("/heartbeat", headers=H, json={"node_id": nid}).status_code == 200
    assert "h1" in client.get("/status", headers=H).text
    kinds = {e["kind"] for e in client.get("/status.json", headers=H).json()["events"]}
    assert {"register", "batch_start", "batch_done", "crash", "node_done"} <= kinds


def test_silent_detection(client):
    from controller.app import check_silent
    nid = register(client, [])
    assert check_silent(client.app.state.pool, 0.0) == 1
    assert client.get("/status.json", headers=H).json()["silent_nodes"] == ["h1"]


def test_save_mode(client):
    r = client.post("/register", headers=H, json={"host": "h3", "cpuid_features": ["SSE"],
                                                   "save_mode": "fxsave"})
    nid = r.json()["node_id"]
    nodes = client.get("/status.json", headers=H).json()["nodes"]
    assert [n["save_mode"] for n in nodes if n["node_id"] == nid] == ["fxsave"]
    b = client.get(f"/batch?node_id={nid}&size=10", headers=H).json()
    client.post("/results", headers=H, json={"node_id": nid, "batch_id": b["batch_id"],
                                              "ok_count": len(b["cases"]), "save_mode": "fxsave"})
    assert "fxsave" in client.get("/status", headers=H).text


def test_takeover_same_spec(client):
    def reg(host, microcode="0x1"):
        return client.post("/register", headers=H, json={
            "host": host, "cluster": "gros", "cpuid_features": ["SSE"], "microcode": microcode,
            "cpu_model": "Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz", "save_mode": "xsave"}).json()["node_id"]

    a = reg("gros-1")
    b = client.get(f"/batch?node_id={a}&size=3", headers=H).json()
    client.post("/results", headers=H, json={"node_id": a, "batch_id": b["batch_id"], "ok_count": 3})
    # Same spec on another host: continues after gros-1's last reported batch.
    c = reg("gros-2")
    b2 = client.get(f"/batch?node_id={c}&size=10", headers=H).json()
    assert [(x["test_case_id"], x["state_index"]) for x in b2["cases"]] == [(4, 1)]
    s = client.get("/status.json", headers=H).json()
    nodes = {n["host"]: n for n in s["nodes"]}
    assert nodes["gros-1"]["state"] == "moved" and nodes["gros-2"]["done_cases"] == 3
    assert s["events"][1]["detail"]["resumed_from"] == "gros-1"
    assert s["cpu_models"]["Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz"]["done_cases"] == 3
    # Another microcode is another run.
    d = reg("gros-3", "0x2")
    assert len(client.get(f"/batch?node_id={d}&size=10", headers=H).json()["cases"]) == 4


def status_node(client, nid, want_total=False):
    """The node's entry in /status.json; totals are counted in the background, so wait for them if asked."""
    import time
    for _ in range(50):
        (n,) = [n for n in client.get("/status.json", headers=H).json()["nodes"] if n["node_id"] == nid]
        if not want_total or n["total_cases"] is not None:
            return n
        time.sleep(0.1)
    raise AssertionError("total_cases never counted")


def test_class_counts(client):
    from psycopg.conninfo import make_conninfo
    from controller.schema import backfill_class_counts
    pool = client.app.state.pool
    nid = register(client, ["SSE"])
    assert status_node(client, nid)["mismatch_classes"] == {}
    b = client.get(f"/batch?node_id={nid}&size=10", headers=H).json()
    body = {"node_id": nid, "batch_id": b["batch_id"], "ok_count": 1, "elapsed_s": 2,
            "mismatches": [
                {"test_case_id": 1, "state_index": 0, "got_final_state": {"rax": 1, "flag": 18}},
                {"test_case_id": 1, "state_index": 1, "got_final_state": {"rax": 2, "flag": 3}},
                {"test_case_id": 4, "state_index": 1, "status": "crash"}]}
    want = {"undef_flags_only": 1, "defined_state": 1, "crash": 1}
    assert client.post("/results", headers=H, json=body).json()["classes"] == want
    assert status_node(client, nid)["mismatch_classes"] == want
    # A batch reported twice is counted once.
    client.post("/results", headers=H, json=body)
    assert status_node(client, nid)["mismatch_classes"] == want
    # /status reads the counts table, not g5k_results; backfill rebuilds it from the stored rows.
    with pool.connection() as conn:
        conn.execute("UPDATE g5k_class_counts SET n = 7")
    assert set(status_node(client, nid)["mismatch_classes"].values()) == {7}
    assert backfill_class_counts(make_conninfo(DSN, options=pool.kwargs["options"])) == 3
    assert status_node(client, nid)["mismatch_classes"] == want


def test_eligible_total(client):
    pool = client.app.state.pool
    sse = register(client, ["SSE"])
    avx = client.post("/register", headers=H, json={"host": "h2", "cpuid_features": ["AVX"],
                                                   "kernel_features": ["AVX"]}).json()["node_id"]
    # ADD: 2 cases of 2 states; VADDPD: 1 state, AVX only; the ERR case is never counted.
    assert status_node(client, sse, want_total=True)["total_cases"] == 4
    assert status_node(client, avx, want_total=True)["total_cases"] == 5
    with pool.connection() as conn:
        rows = conn.execute("SELECT instruction_id, n FROM g5k_instruction_states").fetchall()
    assert {r["instruction_id"]: r["n"] for r in rows} == {1: 4, 2: 1}
    # The total matches what the node is actually served.
    assert len(client.get(f"/batch?node_id={avx}&size=10", headers=H).json()["cases"]) == 5
