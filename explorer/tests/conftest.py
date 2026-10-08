"""Tests run against a scratch Postgres: set EXPLORER_TEST_DSN (they are skipped without it). Each
test gets two throwaway schemas, one standing in for public with just the tables the explorer reads,
one for the explorer's own tables. Nothing is imported from controller/."""
import json
import os
import uuid

import pytest

DSN = os.environ.get("EXPLORER_TEST_DSN")

TABLES = """
CREATE TABLE instructions(id serial PRIMARY KEY, name text UNIQUE, url text);
CREATE TABLE instruction_features(instruction_id int, feature text, PRIMARY KEY (instruction_id, feature));
CREATE TABLE test_cases(id bigserial PRIMARY KEY, instruction text UNIQUE, opcode text,
    instruction_id int, status text, initial_states jsonb DEFAULT '[]');
CREATE TABLE test_results(id bigserial PRIMARY KEY, test_case_id bigint, state_index int,
    exception_kind text, final_state jsonb, UNIQUE (test_case_id, state_index));
CREATE TABLE g5k_nodes(node_id bigserial PRIMARY KEY, host text UNIQUE NOT NULL, cluster text,
    cpu_model text, microcode text, save_mode text, features text[] NOT NULL DEFAULT '{}',
    state text NOT NULL DEFAULT 'running', cursor_tc bigint NOT NULL DEFAULT 0,
    cursor_si int NOT NULL DEFAULT -1, done_cases bigint NOT NULL DEFAULT 0);
CREATE TABLE g5k_results(id bigserial PRIMARY KEY, node_id bigint NOT NULL, batch_id bigint,
    test_case_id bigint NOT NULL, state_index int NOT NULL, instruction text,
    status text NOT NULL DEFAULT 'mismatch', class text NOT NULL, diff_keys text[] NOT NULL DEFAULT '{}',
    got_final_state jsonb, got_exception_kind text, expected_final_state jsonb,
    expected_exception_kind text, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_id, test_case_id, state_index));

INSERT INTO instructions(id, name, url) VALUES (1, 'ADD', 'https://example.org/add'), (2, 'FIST', NULL),
    (3, 'VADDPD', NULL);
INSERT INTO instruction_features VALUES (2, 'X87'), (3, 'AVX');
-- 4 states each; case 4 is never run
INSERT INTO test_cases(id, instruction, opcode, instruction_id, status, initial_states) VALUES
    (1, 'add rax, rbx', '4801d8', 1, NULL, '[{"rax":1,"flag":2},{"rax":2,"flag":2},{"rax":3,"flag":2},{"rax":4,"flag":2}]'),
    (2, 'fist dword ptr [rax]', 'db10', 2, NULL, '[{"rax":1},{"rax":2},{"rax":3},{"rax":4}]'),
    (3, 'vaddpd ymm0, ymm1, ymm2', 'c5f558c2', 3, NULL, '[{"rax":1},{"rax":2},{"rax":3},{"rax":4}]'),
    (4, 'bad', '00', 1, 'ERR: x', '[{"rax":1}]');
INSERT INTO test_results(test_case_id, state_index, final_state)
    SELECT tc, si, jsonb_build_object('rax', si + 1, 'flag', 2, 'x87_dp', 16)
    FROM generate_series(1, 3) tc, generate_series(0, 3) si;
-- Zen: one run over two hosts (moved, then done). Skylake: done. Westmere: no AVX, still running.
INSERT INTO g5k_nodes(node_id, host, cluster, cpu_model, features, state, cursor_tc, cursor_si, done_cases) VALUES
    (1, 'zen-1', 'zen', 'AMD EPYC 7301 16-Core Processor', '{X87,AVX}', 'moved', 2, 1, 6),
    (2, 'zen-2', 'zen', 'AMD EPYC 7301 16-Core Processor', '{X87,AVX}', 'done', 3, 3, 12),
    (3, 'sky-1', 'sky', 'Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz', '{X87,AVX}', 'done', 3, 3, 12),
    (4, 'wes-1', 'wes', 'Intel(R) Xeon(R) CPU           X5670  @ 2.93GHz', '{X87}', 'running', 2, 0, 5),
    (5, 'new-1', 'new', 'Some New CPU', '{X87,AVX}', 'done', 3, 3, 12);
"""


class Db:
    def __init__(self, conn, cfg):
        self.conn, self.cfg = conn, cfg

    def result(self, node, tc, si, klass="defined_state", keys=("x87_dp",), got=None, exc=None):
        """Insert one mismatch row; returns its id."""
        got = {"rax": si + 1, "flag": 2, "x87_dp": 0} if got is None and exc is None else got
        return self.conn.execute(
            """INSERT INTO g5k_results (node_id, test_case_id, state_index, class, diff_keys,
                   got_final_state, got_exception_kind, expected_final_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (node, tc, si, klass, list(keys), json.dumps(got) if got is not None else None, exc,
             json.dumps({"rax": si + 1, "flag": 2, "x87_dp": 16}))).fetchone()["id"]

    def summary(self):
        return {(r["node_id"], r["test_case_id"], r["class"], tuple(r["diff_keys"])): (r["n"], r["sig"])
                for r in self.conn.execute("SELECT * FROM summary")}

    def run_job(self, job=None, ticks=50):
        from explorer.jobs import Job
        job = job or Job(self.cfg)
        for _ in range(ticks):
            if not job.tick(self.conn):
                break
        return job


@pytest.fixture()
def db():
    if not DSN:
        pytest.skip("EXPLORER_TEST_DSN not set")
    import psycopg
    from explorer.config import Config
    from explorer.jobs import connect
    from explorer.schema import ensure

    data = "xt_" + uuid.uuid4().hex[:8]
    cfg = Config(dsn=DSN, schema=data + "_x", data_schema=data, run_job=False, chunk=3, tc_chunk=2,
                 check_every_s=0)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {data}")
        c.execute(f"CREATE SCHEMA {cfg.schema}")
        c.execute(f"SET search_path = {data}")
        c.execute(TABLES)
    with connect(cfg, 30) as conn:
        ensure(conn, cfg.schema)
        yield Db(conn, cfg)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {data} CASCADE")
        c.execute(f"DROP SCHEMA {cfg.schema} CASCADE")


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from explorer.app import create_app

    with TestClient(create_app(db.cfg)) as c:
        c.db = db
        yield c
