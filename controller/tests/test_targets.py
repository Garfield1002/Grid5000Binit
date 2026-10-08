from controller.targets import load_targets, match, norm_cpu


def test_targets_table():
    ts = load_targets()
    assert len(ts) == 85 and len({t.microarch for t in ts}) == 19
    assert sum(not t.abaca for t in ts) == 29
    assert load_targets("/nonexistent") == []


def test_match():
    ts = load_targets()
    assert norm_cpu("Intel(R) Xeon(R) CPU E5-2620 0 @ 2.00GHz") == "intel xeon e5-2620"
    assert match(ts, "Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz", "gros").cluster == "gros"
    assert match(ts, "AMD EPYC 7301 16-Core Processor", None).cluster == "chiclet"
    # v2 and v4 are different targets
    assert match(ts, "Intel(R) Xeon(R) CPU E5-2650 v2 @ 2.60GHz", None).microarch == "Ivy Bridge"
    # unknown model string: fall back to the cluster, including the "other clusters" column
    assert match(ts, "?", "yeti").cpu_model == "Intel Xeon Gold 6130"
    assert match(ts, "Intel(R) Core(TM) i7-10850H CPU @ 2.70GHz", "laptop") is None
