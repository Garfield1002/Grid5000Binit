"""Idempotent migration for the controller's own tables. Never touches x86db tables."""

DDL = """
CREATE TABLE IF NOT EXISTS instruction_features (
    instruction_id int  NOT NULL,
    feature        text NOT NULL,
    PRIMARY KEY (instruction_id, feature)
);

CREATE TABLE IF NOT EXISTS g5k_nodes (
    node_id          bigserial PRIMARY KEY,
    host             text UNIQUE NOT NULL,
    cluster          text,
    cpu_model        text,
    microcode        text,
    cpuid_features   text[] NOT NULL DEFAULT '{}',
    kernel_features  text[] NOT NULL DEFAULT '{}',
    features         text[] NOT NULL DEFAULT '{}',
    worker_version   text,
    save_mode        text,
    registered_at    timestamptz NOT NULL DEFAULT now(),
    last_seen        timestamptz NOT NULL DEFAULT now(),
    state            text NOT NULL DEFAULT 'running',
    silent           boolean NOT NULL DEFAULT false,
    cursor_tc        bigint NOT NULL DEFAULT 0,
    cursor_si        int NOT NULL DEFAULT -1,
    done_cases       bigint NOT NULL DEFAULT 0,
    ok_count         bigint NOT NULL DEFAULT 0,
    work_s           double precision NOT NULL DEFAULT 0,
    qemu_restarts    int NOT NULL DEFAULT 0,
    current_batch_id bigint,
    done_in_batch    int NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS g5k_batches (
    batch_id       bigserial PRIMARY KEY,
    node_id        bigint NOT NULL REFERENCES g5k_nodes(node_id),
    issued_at      timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text NOT NULL DEFAULT 'issued',
    n_cases        int NOT NULL,
    last_tc        bigint,
    last_si        int,
    ok_count       int,
    mismatch_count int,
    elapsed_s      double precision
);
CREATE INDEX IF NOT EXISTS idx_g5k_batches_node ON g5k_batches (node_id);

CREATE TABLE IF NOT EXISTS g5k_results (
    id                      bigserial PRIMARY KEY,
    node_id                 bigint NOT NULL REFERENCES g5k_nodes(node_id),
    batch_id                bigint,
    test_case_id            bigint NOT NULL,
    state_index             int NOT NULL,
    instruction             text,
    status                  text NOT NULL,
    class                   text NOT NULL,
    diff_keys               text[] NOT NULL DEFAULT '{}',
    got_final_state         jsonb,
    got_exception_kind      text,
    expected_final_state    jsonb,
    expected_exception_kind text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_id, test_case_id, state_index)
);
CREATE INDEX IF NOT EXISTS idx_g5k_results_class ON g5k_results (class);

-- Running mismatch counts per node and class, kept by the trigger below so that /status never scans
-- g5k_results (tens of GB). Rebuild with `controller backfill-counts`.
CREATE TABLE IF NOT EXISTS g5k_class_counts (
    node_id bigint NOT NULL,
    class   text   NOT NULL,
    n       bigint NOT NULL,
    PRIMARY KEY (node_id, class)
);

CREATE OR REPLACE FUNCTION g5k_count_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO g5k_class_counts (node_id, class, n) VALUES (NEW.node_id, NEW.class, 1)
    ON CONFLICT (node_id, class) DO UPDATE SET n = g5k_class_counts.n + 1;
    RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER g5k_results_count AFTER INSERT ON g5k_results
    FOR EACH ROW EXECUTE FUNCTION g5k_count_result();

-- States with a result per instruction (status not ERR*): one pass over the corpus, filled by the
-- controller on first use, so that the eligible total of any feature set is a sum over a small table.
-- Empty it (TRUNCATE) after test_cases / test_results change.
CREATE TABLE IF NOT EXISTS g5k_instruction_states (
    instruction_id bigint PRIMARY KEY,
    n              bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS g5k_events (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    kind    text NOT NULL,
    node_id bigint,
    host    text,
    detail  jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_g5k_events_ts ON g5k_events (ts DESC);

ALTER TABLE g5k_nodes ADD COLUMN IF NOT EXISTS save_mode text;
"""


def migrate(pool) -> None:
    with pool.connection() as conn:
        # Serialize concurrent migrations.
        conn.execute("SELECT pg_advisory_xact_lock(5000501)")
        conn.execute(DDL)


def backfill_class_counts(dsn: str) -> int:
    """Rebuild g5k_class_counts from g5k_results; returns the number of rows counted.

    The reset happens under a lock that waits for in-flight inserts, so every row with id <= boundary
    is committed and not yet counted, and every later row is counted by the trigger. The long scan
    then runs without blocking /results."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(5000501)")
        conn.execute(DDL)
        with conn.transaction():
            conn.execute("LOCK TABLE g5k_results IN SHARE ROW EXCLUSIVE MODE")
            conn.execute("TRUNCATE g5k_class_counts")
            boundary = conn.execute("SELECT coalesce(max(id), 0) FROM g5k_results").fetchone()[0]
        conn.execute(
            """INSERT INTO g5k_class_counts (node_id, class, n)
               SELECT node_id, class, count(*) FROM g5k_results WHERE id <= %s GROUP BY 1,2
               ON CONFLICT (node_id, class) DO UPDATE SET n = g5k_class_counts.n + EXCLUDED.n""",
            (boundary,),
        )
        return conn.execute("SELECT coalesce(sum(n), 0) FROM g5k_class_counts").fetchone()[0]
