# Grid5000Binit controller

FastAPI service that streams x86db test cases to Grid5000 worker nodes and records
mismatches. See `../SPEC.md` sections 2 and 3.

## Run

    cd controller
    export X86DB_DSN=postgresql://x86db:x86db@localhost:5432/x86db
    export CONTROLLER_TOKEN=secret        # required
    export LISTEN=0.0.0.0:8080 LOG_DIR=./logs   # optional (SILENT_AFTER_S=120)
    uv run controller compute-features    # once, and after test_cases change
    uv run controller                     # = `serve`; migrates g5k_* tables at startup
    uv run controller migrate             # migration only (idempotent)

The controller only reads `test_cases`, `test_results`, `instruction_undefined_flags`;
it writes `g5k_nodes`, `g5k_batches`, `g5k_results`, `g5k_events`, `instruction_features`, and two small
bookkeeping tables so that `/status` never scans the big tables:

- `g5k_class_counts`: mismatches per node and class, kept by a trigger on `g5k_results`.
  `uv run controller backfill-counts` rebuilds it (one long scan, no downtime).
- `g5k_instruction_states`: states with a result per instruction, filled on the first `/status` (one pass over
  `test_results`); eligible totals are sums over it. `TRUNCATE` it after `test_cases`/`test_results` change.

## Endpoints (`Authorization: Bearer $CONTROLLER_TOKEN`, or `?token=`, except the public status pages)

- `POST /register`, `GET /batch?node_id=&size=`, `POST /results`, `POST /heartbeat` (spec section 2).
  Re-registering the same `host` keeps its node_id and cursor (resume). A node also takes over the cursor and
  counters of the most advanced node with the same cpu_model, microcode, features and save_mode, which is
  then marked `moved`: a run follows the hardware spec across hosts.
- `GET /status` (HTML) and `GET /status.json`: public (no token), built at most once every 10 s,
  which is also how often the page reloads. `GET /` redirects to `/status`. `GET /mismatches?class=&host=&insn=&limit=&offset=` always needs the token.
- A failed authentication is answered 401 and logged (`auth_failure` in `logs/controller.jsonl`); it writes
  nothing to the database, so scanners cannot fill `g5k_events`.

Events (`g5k_events`, logs, `/status`): register, batch_start, batch_done, crash, node_silent,
node_recovered, node_done.

## Semantics

- Cases are (test_case_id, state_index) ordered; the cursor advances only when a batch's results are posted
  (a batch not reported is re-served). Cases with `status LIKE 'ERR%'` or without a `test_results` row are skipped.
- Node features = kernel_features ∩ cpuid_features (the non-empty one if only one is given). A case is served
  iff all its `instruction_features` are in that set. Without `compute-features`, nothing is filtered.
- `compute-features` unions features over all opcodes of an instruction_id; baseline (INTEL*, X64) features are
  dropped and `FPU*` maps to `X87`.
- Classification (`controller/classify.py`): `crash`; exception kinds differ -> `exception_mismatch`; only key
  `flag` differs and the XOR lies within the instruction's undefined flags -> `undef_flags_only`; else
  `defined_state`. Worker status `skipped` is stored with class `skipped`. Mismatch rows keep the full got/expected states.
- Only mismatches are stored; OK cases are counted (`ok_ids` is not persisted).

## Tests

    uv run pytest                       # unit tests; DB tests skipped
    G5K_TEST_DSN=postgresql://... uv run pytest   # also runs integration tests in a throwaway schema
