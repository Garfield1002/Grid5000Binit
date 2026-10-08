from controller.replay import propose, tally


def obs(status, state=None, exc=None, n=1):
    return {"n": n, "status": status, "got_final_state": state, "got_exception_kind": exc}


def test_propose():
    crash, ok = obs("crash"), obs("ok", n=10)
    wrong = obs("mismatch", {"rax": 1, "flag": 2})
    assert propose([ok], [crash]) == ("delete", "")
    assert propose([ok], []) == (None, "same")
    assert propose([wrong], []) == ("insert", "")
    assert propose([wrong], [dict(wrong)]) == (None, "same")
    assert propose([obs("mismatch", {"rax": 1, "flag": 3})], [wrong]) == ("overwrite", "")
    assert propose([obs("mismatch", exc="GP")], [obs("mismatch", exc="UD")]) == ("overwrite", "")
    # The stored rows of a cluster are taken together.
    assert propose([wrong], [dict(wrong), crash]) == ("overwrite", "")
    # Runs that disagree, or no run at all, propose nothing.
    assert propose([obs("ok", n=7), obs("crash", n=3)], [crash]) == (None, "unstable")
    assert propose([], [crash]) == (None, "no result")
    assert tally([obs("ok", n=7), obs("crash", n=3)]) == "ok x7, crash x3"
