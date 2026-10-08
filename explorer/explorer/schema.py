"""The explorer's own tables. All derived data: DROP SCHEMA explorer CASCADE and a backfill rebuild
them. Names are unqualified; the connection's search_path puts the explorer's schema first.
Every connection uses dict rows."""

DDL = """
CREATE TABLE IF NOT EXISTS summary (
    node_id      bigint NOT NULL,
    test_case_id bigint NOT NULL,
    class        text   NOT NULL,
    diff_keys    text[] NOT NULL,
    n            bigint NOT NULL,  -- mismatching states
    sig          bigint NOT NULL,  -- XOR of their row hashes
    PRIMARY KEY (node_id, test_case_id, class, diff_keys)
);
CREATE INDEX IF NOT EXISTS summary_tc ON summary (test_case_id);

CREATE TABLE IF NOT EXISTS watermark (
    one               boolean PRIMARY KEY DEFAULT true CHECK (one),
    result_id         bigint NOT NULL DEFAULT 0,  -- last g5k_results.id folded into summary
    tip_id            bigint NOT NULL DEFAULT 0,  -- max(g5k_results.id) when the job last looked
    replay_applied_at timestamptz,                -- last g5k_replays.applied_at handled
    paused            boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
INSERT INTO watermark DEFAULT VALUES ON CONFLICT DO NOTHING;

-- States per test case (0 for ERR* cases, which are never run): the denominators. jsonb_array_length
-- reads the whole initial_states array, so it is computed once, by the job.
CREATE TABLE IF NOT EXISTS tc_states (
    test_case_id   bigint PRIMARY KEY,
    instruction_id int NOT NULL,
    n              int NOT NULL
);
CREATE INDEX IF NOT EXISTS tc_states_instruction ON tc_states (instruction_id);

CREATE TABLE IF NOT EXISTS checks (
    name       text PRIMARY KEY,
    ok         boolean NOT NULL,
    detail     jsonb NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now()
);
"""

# 64-bit hash of what a node observed for one state. jsonb's text form is canonical (sorted keys).
ROW_HASH = ("hashtextextended(state_index::text || '|' || coalesce(got_final_state::text, '') "
            "|| '|' || coalesce(got_exception_kind, ''), 0)")


def ensure(conn, schema: str) -> None:
    """Create the tables. Refuses to run when the explorer's schema is not first on the search
    path, since a missing schema is skipped silently and the tables would land in the data schema."""
    cur = conn.execute("SELECT current_schema() AS s").fetchone()["s"]
    if cur != schema:
        raise RuntimeError(f"schema {schema!r} does not exist or is not first on the search path "
                           f"(current schema: {cur!r}); see explorer/README.md")
    conn.execute("SELECT pg_advisory_xact_lock(5000502)")
    conn.execute(DDL)


def has_table(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,)).fetchone()["ok"]
