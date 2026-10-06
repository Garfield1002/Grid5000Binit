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
def client():
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
    cfg = Config(dsn=DSN, token="tok", log_dir="/tmp", silent_after_s=0.0,
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
    # A failed auth leaves no trace in the database.
    assert client.get("/status.json", headers=H).json()["events"] == []


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
