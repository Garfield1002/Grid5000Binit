# x86db explorer

A read-only web frontend for the results database: which CPU models diverge from the reference on
which instructions, and how. It is a separate process from the controller, imports nothing from
it, and reads its tables through a database role that can write only to its own schema.

## Views

Every view has a `.json` twin with the same parameters. A page is a static file of
`explorer/pages/`: its script, `explorer/static/explorer.js`, fetches the twin of the URL it was
served at and fills the page in, so the server only ever sends JSON and files.

| URL | Shows |
|---|---|
| `/` (`/matrix.json`) | instructions × CPU models: mismatching states as a share of the instruction's states |
| `/i/<mnemonic>` | the instruction's test cases × CPU models |
| `/tc/<id>` | the models grouped by behaviour on one test case, and which keys differ on which state |
| `/tc/<id>/<state>` | initial state, expected final state, and what each group of models got |
| `/checks` | the consistency checks the summary job runs |

Filters are in the query string: `xc=` lists the classes switched off, `xk=` the diff keys ignored
(comma separated), `all=1` also lists the rows without mismatch. A count is left out only when
its class is off or *all* its differing keys are ignored, so `xk=x87_dp` keeps a state that
differs in `x87_dp` and `flag`.

A column is a CPU model: the rows of all the nodes that carried its run are added up.
`explorer/uarch.toml` maps the model names to a vendor, a microarchitecture and a launch year, and
sets the column order; add new models there. A model missing from it gets a column of its own
under `?`.

## Storage

`g5k_results` is far too large to aggregate per page, so a job inside the explorer process keeps
a summary in the explorer's own schema (`explorer` by default). It is derived data:
`DROP SCHEMA explorer CASCADE`, recreate the schema, and the job rebuilds it.

| Table | Content |
|---|---|
| `summary` | per `(node_id, test_case_id, class, diff_keys)`: `n` states and `sig`, the XOR of a 64-bit hash of each state's `(state_index, got_final_state, got_exception_kind)` |
| `watermark` | one row: last `g5k_results.id` folded in, last replay handled, the pause switch |
| `tc_states` | states per test case (the denominators) |
| `checks` | last result of each check |

XOR does not depend on order and composes, so two models behave the same on a test case exactly
when the XOR of `sig` over their nodes' rows is equal. That is what the behaviour groups compare.

The job, one step at a time:

1. fills `tc_states` (once);
2. folds the next `EXPLORER_CHUNK` result ids into `summary` and moves the watermark, in one
   transaction. It never goes past an id that an open transaction could still fill;
3. for each newly applied row of `g5k_replays`, rebuilds the summary of that cluster's nodes on
   that test case (`controller replay apply` deletes and overwrites results in place);
4. once caught up, runs the checks every hour: summary totals against `g5k_class_counts`, and no
   state held by two nodes of one model (XOR would cancel it).

After a busy step it sleeps so that it reads `EXPLORER_DUTY` (a quarter) of the time. The first
run is the backfill: the same job from id 0, a few hours on the production table. Pages show
"Summary covers N% of results" until it has caught up. `explorer pause`, `explorer resume` and
`explorer status` control and show it; it resumes where it stopped after a restart.

`g5k_replays`, `g5k_class_counts` and `g5k_instruction_states` come from controller branches that
may not be merged; each is used when the table exists and skipped otherwise.

## Settings

| Variable | Default | |
|---|---|---|
| `EXPLORER_DSN` | | connection URL of the `explorer` role |
| `EXPLORER_SCHEMA` | `explorer` | the explorer's own schema |
| `EXPLORER_CHUNK` | `20000` | result ids per step |
| `EXPLORER_DUTY` | `0.25` | share of the time the job may read while it has work |
| `EXPLORER_IDLE_S` | `30` | seconds between looks once caught up |
| `EXPLORER_JOB` | `1` | `0` serves pages without running the job |
| `EXPLORER_PAGE_TIMEOUT_S`, `EXPLORER_JOB_TIMEOUT_S` | `5`, `120` | statement timeouts |
| `LISTEN` | `0.0.0.0:8080` | |

## Tests

    cd explorer
    EXPLORER_TEST_DSN=postgresql://... uv run pytest

They need a scratch Postgres 14 or later and are skipped without `EXPLORER_TEST_DSN`. Each test
creates the few tables it reads in a throwaway schema, so nothing depends on the controller's
migrations.
