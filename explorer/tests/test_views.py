import pytest

from explorer.data import Filter

ZEN, SKY, WES, NEW = ("AMD EPYC 7301 16-Core Processor", "Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz",
                      "Intel(R) Xeon(R) CPU           X5670  @ 2.93GHz", "Some New CPU")


@pytest.fixture()
def seeded(client):
    db = client.db
    # ADD (case 1): Zen differs in x87_dp on all 4 states (over its two hosts) and in flag on one;
    # Skylake has the same x87_dp rows; the unknown model has other values on the same states.
    for si in range(4):
        db.result(1 if si < 2 else 2, 1, si, keys=("x87_dp",) if si else ("flag", "x87_dp"))
        db.result(3, 1, si, keys=("x87_dp",) if si else ("flag", "x87_dp"))
        db.result(5, 1, si, keys=("x87_dp",) if si else ("flag", "x87_dp"),
                  got={"rax": si + 1, "flag": 2, "x87_dp": 99})
    # FIST (case 2): Skylake raises an exception on one state; Westmere is in the middle of it.
    db.result(3, 2, 2, klass="exception_mismatch", keys=(), exc="GP")
    db.result(4, 2, 0, keys=("x87_dp",))
    db.run_job()
    return client


def test_filter():
    f = Filter.parse("crash", "x87_dp,x87_ip")
    assert not f.keep("crash", [])
    assert not f.keep("defined_state", ["x87_dp"])
    assert f.keep("defined_state", ["x87_dp", "flag"])  # one key still counts
    assert f.keep("exception_mismatch", [])
    assert f.query(all=1) == "?xc=crash&xk=x87_dp,x87_ip&all=1"
    assert Filter().query() == ""


def test_matrix(seeded):
    d = seeded.get("/matrix.json").json()
    assert [m["cpu_model"] for m in d["models"]] == [ZEN, WES, SKY, NEW]  # uarch.toml order, unknown last
    assert d["models"][0]["uarch"] == "Zen" and d["models"][3]["uarch"] == "?"
    assert d["models"][0]["year"] == 2017 and d["models"][3]["year"] is None
    assert d["models"][1]["done"] is False and d["models"][1]["progress"] == pytest.approx(5 / 8)
    rows = {r["instruction"]: r for r in d["rows"]}
    assert set(rows) == {"ADD", "FIST"}  # VADDPD has no mismatch
    assert rows["ADD"]["states"] == 4
    assert rows["ADD"]["cells"][ZEN] == {"n": 4, "share": 1.0, "classes": {"defined_state": 4},
                                         "keys": {"x87_dp": 4, "flag": 1}}
    assert rows["FIST"]["cells"][SKY]["classes"] == {"exception_mismatch": 1}
    # Westmere's cursor is in FIST: its count there is partial; a finished model that matches has no cell
    assert rows["FIST"]["cells"][WES]["status"] == "running" and ZEN not in rows["FIST"]["cells"]
    assert d["inventory"]["keys"] == {"x87_dp": 13, "flag": 3}
    assert d["coverage"]["caught_up"]

    d = seeded.get("/matrix.json?xk=x87_dp&all=1").json()
    rows = {r["instruction"]: r for r in d["rows"]}
    assert len(rows) == 3
    assert rows["ADD"]["cells"][ZEN]["n"] == 1  # the row that also differs in flag stays
    assert rows["FIST"]["cells"][WES] == {"status": "running"}
    assert rows["VADDPD"]["cells"] == {WES: {"status": "unsupported"}}
    assert seeded.get("/matrix.json?xc=defined_state").json()["rows"][0]["instruction"] == "FIST"


def test_instruction(seeded):
    assert seeded.get("/i/NOPE.json").status_code == 404
    d = seeded.get("/i/FIST.json").json()
    assert [r["test_case_id"] for r in d["rows"]] == [2]
    cells = d["rows"][0]["cells"]
    assert cells[WES]["n"] == 1 and cells[WES]["status"] == "running"
    assert cells[SKY]["share"] == 0.25 and ZEN not in cells
    d = seeded.get("/i/VADDPD.json?all=1").json()
    assert d["rows"][0]["cells"] == {WES: {"status": "unsupported"}}


def test_test_case_groups(seeded):
    assert seeded.get("/tc/999.json").status_code == 404
    d = seeded.get("/tc/1.json").json()
    assert d["mnemonic"] == "ADD" and d["url"] == "https://example.org/add" and d["states"] == 4
    groups = [(g["kind"], g["n"], g["models"]) for g in d["groups"]]
    # Zen (two hosts) and Skylake got the same values; the unknown model did not; Westmere is done with it
    assert groups == [("mismatch", 4, ["EPYC 7301", "Gold 6130"]), ("mismatch", 4, ["Some New CPU"]),
                      ("match", 0, ["X5670"])]
    assert [r["state_index"] for r in d["grid"]] == [0, 1, 2, 3]
    assert d["grid"][0]["groups"]["1"]["diff_keys"] == ["flag", "x87_dp"]
    assert d["next_after"] is None

    d = seeded.get("/tc/1.json?xk=x87_dp").json()
    assert [(g["kind"], g["n"]) for g in d["groups"]][:2] == [("mismatch", 1), ("mismatch", 1)]
    assert [r["state_index"] for r in d["grid"]] == [0]

    d = seeded.get("/tc/3.json").json()
    assert [(g["kind"], g["models"]) for g in d["groups"]] == [
        ("match", ["EPYC 7301", "Gold 6130", "Some New CPU"]), ("unsupported", ["X5670"])]


def test_test_case_grid_pages(seeded, monkeypatch):
    monkeypatch.setattr("explorer.data.GRID_PAGE", 3)
    d = seeded.get("/tc/1.json").json()
    assert [r["state_index"] for r in d["grid"]] == [0, 1, 2] and d["next_after"] == 2
    d = seeded.get("/tc/1.json?after=2").json()
    assert [r["state_index"] for r in d["grid"]] == [3] and d["next_after"] is None


def test_state(seeded):
    assert seeded.get("/tc/1/9.json").status_code == 404
    d = seeded.get("/tc/1/0.json").json()
    assert d["initial_state"] == {"rax": 1, "flag": 2}
    assert d["expected_final_state"] == {"rax": 1, "flag": 2, "x87_dp": 16}
    assert [(g["kind"], g["models"]) for g in d["groups"]] == [
        ("mismatch", ["EPYC 7301", "Gold 6130"]), ("mismatch", ["Some New CPU"]), ("match", ["X5670"])]
    assert d["keys"] == ["flag", "x87_dp"]
    assert set(seeded.get("/tc/1/0.json?all=1").json()["keys"]) == {"flag", "rax", "x87_dp"}
    # Westmere's cursor is at (2, 0): state 0 of case 2 is run, state 2 is not yet
    kinds = lambda si: {g["kind"]: g["models"] for g in seeded.get(f"/tc/2/{si}.json").json()["groups"]}
    assert kinds(0)["mismatch"] == ["X5670"]
    assert kinds(2) == {"mismatch": ["Gold 6130"], "match": ["EPYC 7301", "Some New CPU"],
                        "notrun": ["X5670"]}
    exc = {g["kind"]: g.get("exception_kind") for g in seeded.get("/tc/2/2.json").json()["groups"]}
    assert exc["mismatch"] == "GP"
    assert seeded.get("/tc/1/0.json").json()["groups"][1]["final_state"]["x87_dp"] == 99


def test_coverage_and_checks(client):
    client.db.result(1, 1, 0)
    client.db.conn.execute("UPDATE watermark SET tip_id = 1000000")
    cov = client.get("/matrix.json").json()["coverage"]
    assert not cov["caught_up"] and cov["percent"] == 0
    assert client.get("/checks.json").json()["checks"] == []
    assert client.get("/healthz").json() == {"ok": True}


def test_pages(client):
    """A page is a static file, the same for every URL of its view; its script fetches the data."""
    for url, view in (("/", "matrix"), ("/?xk=x87_dp", "matrix"), ("/i/ADD", "instruction"),
                      ("/tc/1", "test_case"), ("/tc/1/0", "state"), ("/checks", "checks")):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert f'<body data-view="{view}">' in r.text and "/static/explorer.js" in r.text
    assert client.get("/tc/nope").status_code == 422
    for name in ("explorer.css", "explorer.js"):
        assert client.get(f"/static/{name}").status_code == 200
